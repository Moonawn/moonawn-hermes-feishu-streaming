from __future__ import annotations

import asyncio
import ast
from contextlib import suppress
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import socket
import sys
import tempfile
import textwrap
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from moonawn_feishu.engine import Engine
from moonawn_feishu.fake_lark import FakeLark
from moonawn_feishu.model import Conflict, Route
from moonawn_feishu.plugin import Dispatch, ManagedConsumer, _dispatch, adapter_class, install_host_hooks
from moonawn_feishu.runtime import Runtime, Scope
from moonawn_feishu.store import Store


@dataclass
class Result:
    success: bool
    message_id: str | None = None
    error: str | None = None
    raw_response: object = None


class NativeAdapter:
    async def _send_uploaded_key(self, **kwargs):
        self.native_calls.append(kwargs)
        return Result(True, "native_file")

    def _send_retry_is_final(self, result):
        return False

    async def _record_delivery_obligation(self, event, session_key, text_content,
                                          delivery_adapter, is_ephemeral_response):
        return "native-obligation"

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.native_calls.append((chat_id, content, reply_to, metadata))
        return Result(True, "native_message")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.native_calls.append((chat_id, message_id, content, finalize))
        return Result(True, message_id)

    def format_tool_event(self, event, *, mode="all", preview_max_len=40):
        return "native tool"


class NativeConsumer:
    def __init__(self, adapter, chat_id, config=None, metadata=None, on_new_message=None,
                 on_before_finalize=None, initial_reply_to_id=None, run_still_current=None):
        self.adapter, self.chat_id = adapter, chat_id


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "delivery.db")
        self.fake = FakeLark()
        self.runtime = Runtime(Engine(self.store, self.fake), interval=.001)
        self.adapter = adapter_class(NativeAdapter)()
        self.adapter._moonawn = self.runtime
        self.adapter.native_calls = []
        self.route = Route("t", "b", "p", "chat", "thread", "topic-root")
        self.scope = Scope(self.route, "session", "raw-inbound")
        self.adapter._moonawn_scope = lambda source, session, inbound, anchor: Scope(
            Route("t", "b", "p", source.chat_id, source.thread_id, anchor or ""), session, inbound)
        self.dispatch = Dispatch(self.adapter, self.scope)
        self.token = _dispatch.set(self.dispatch)
        run_module = ModuleType("gateway.run")
        run_module._strip_response_attachments_for_direct_send = lambda text, adapter: text
        base = ModuleType("gateway.platforms.base")
        base.SendResult = Result
        egress = ModuleType("gateway.relay.egress")
        egress.declined_send = lambda result: False
        self.modules = patch.dict(sys.modules, {"gateway": ModuleType("gateway"), "gateway.run": run_module,
                                                "gateway.platforms.base": base, "gateway.relay.egress": egress})
        self.modules.start()
        self.block = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.block.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.block.stop()
        self.modules.stop()
        _dispatch.reset(self.token)
        self.store.close()
        self.tmp.cleanup()

    def hooks(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/hermes-contract.json").read_text())
        source = fixture["contracts"]["gateway/run_notifications.py:_deliver_queued_first_response"]["source"]
        namespace = {"logger": logging.getLogger("offline.contract"), "MessageEvent": SimpleNamespace}
        exec(compile("from __future__ import annotations\n" + textwrap.dedent(source), "<actual Hermes queued delivery>", "exec"), namespace)
        self.actual_queue_source = source
        class Notifications:
            _deliver_queued_first_response = namespace["_deliver_queued_first_response"]

            async def _send_queued_final_text(host, adapter, source, text, metadata, anchor, session, inbound):
                host.delivery_calls.append((anchor, session, inbound, metadata))
                return await adapter.send(source.chat_id, text, reply_to=anchor, metadata=dict(metadata or {}, notify=True))

            async def _deliver_media_from_response(host, response, event, adapter, thread_metadata=None):
                host.media_calls.append((response, event, thread_metadata))
        class Turns:
            async def _run_agent_inner(host, source, session_key=None, inbound_message_id=None, event_message_id=None):
                consumer = ManagedConsumer(self.adapter, source.chat_id)
                consumer.on_delta(inbound_message_id)
                consumer.finish(inbound_message_id)
                await consumer.run()
                await consumer.finalize_from_host(inbound_message_id)
                return consumer

            def _delivery_adapter_for(host, source):
                return self.adapter

            async def _run_agent_mark_streamed_delivery(host, response, turn_ctx):
                host.native_mark_calls = getattr(host, "native_mark_calls", 0) + 1
        consumer_module = SimpleNamespace(GatewayStreamConsumer=NativeConsumer)
        install_host_hooks(consumer_module, Turns, Notifications)
        host = Notifications()
        host.delivery_calls, host.media_calls = [], []
        return consumer_module, Turns(), host

    async def consumer(self):
        # Hermes constructs the sink in its worker thread; no runtime DB imports
        # or producer callbacks may assume that constructor runs on the main loop.
        return await asyncio.to_thread(ManagedConsumer, self.adapter, "chat")

    async def test_host_completion_uses_transformed_final_not_preview(self):
        module, turns, _ = self.hooks()
        consumer = await self.consumer()
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("旧预览")
        consumer.finish("流式结束时的正文")
        await task
        self.assertFalse(consumer.final_content_delivered)
        response = {"final_response": "过滤和追加后的完整终稿", "response_transformed": True}
        await turns._run_agent_mark_streamed_delivery(response, SimpleNamespace(stream_consumer_holder=[consumer]))
        self.assertNotIn("already_sent", response)
        final = response["final_response"] + "\n宿主在 mark 后追加的页脚"
        result = await self.adapter.send("chat", final, metadata={"notify": True})
        self.assertTrue(result.success)
        self.assertEqual(consumer.session.phase.body, final)

    async def test_observer_finish_and_cancelled_wait_do_not_cancel_slow_ack(self):
        consumer = await self.consumer()
        entered, release = asyncio.Event(), asyncio.Event()
        create = self.fake.create
        async def slow_create(*args):
            entered.set()
            await release.wait()
            return await create(*args)
        self.fake.create = slow_create
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("预览")
        await entered.wait()
        waiter = asyncio.create_task(consumer.session.queue.drain())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(consumer.session.queue.task.done())
        consumer.finish("完整终稿")
        await asyncio.wait_for(task, .2)
        self.assertFalse(consumer.session.phase.terminal)
        release.set()
        self.assertTrue(await consumer.finalize_from_host("完整终稿"))
        self.assertEqual((consumer.session.phase.terminal, consumer.session.phase.seal), ("final", "closed"))
        self.assertEqual(len(self.fake.cards), 1)

    async def test_owned_unknown_uses_actual_host_retry_gate_and_only_one_ledger(self):
        # Execute the pinned native retry method, including its real branching.
        fixture = json.loads((Path(__file__).parent / "fixtures/hermes-contract.json").read_text())
        source = fixture["contracts"]["gateway/platforms/base.py:_send_with_retry"]["method_source"]
        tree = ast.parse(source)
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_with_retry")
        method.decorator_list = []
        namespace = {}
        exec(compile("from __future__ import annotations\n" + ast.unparse(method), "<actual Hermes send retry>", "exec"), namespace)
        consumer = await self.consumer()
        self.fake.fail_next("create", "lost_ack")
        result = await namespace["_send_with_retry"](self.adapter, "chat", "完整终稿", metadata={"notify": True})
        self.assertFalse(result.success)
        self.assertTrue(self.adapter._send_retry_is_final(result))
        self.assertEqual(len(self.fake.cards), 1)
        self.assertEqual(self.adapter.native_calls, [])
        event = SimpleNamespace(message_id="raw-inbound", source=SimpleNamespace(chat_id="chat"))
        owned = await self.adapter._record_delivery_obligation(event, "session", "完整终稿", self.adapter, False)
        unrelated = await self.adapter._record_delivery_obligation(event, "another-session", "正文", self.adapter, False)
        self.assertIsNone(owned)
        self.assertEqual(unrelated, "native-obligation")
        persisted = next(p for p in self.store.all() if p.identity.key == consumer.session.identity.key)
        self.assertEqual(persisted.body, "完整终稿")

    async def test_unknown_receipt_holds_native_fallback_without_success_flag(self):
        _, turns, _ = self.hooks()
        consumer = await self.consumer()
        self.fake.fail_next("create", "lost_ack")
        response = {"final_response": "完整终稿"}
        await turns._run_agent_mark_streamed_delivery(response, SimpleNamespace(stream_consumer_holder=[consumer]))
        self.assertNotIn("already_sent", response)
        result = await self.adapter.send("chat", "完整终稿", metadata={"notify": True})
        self.assertFalse(result.success)
        self.assertEqual(len(self.fake.cards), 1)
        self.assertEqual(self.adapter.native_calls, [])

    async def test_failed_turn_card_and_normal_error_fallback_do_not_duplicate(self):
        _, turns, _ = self.hooks()
        consumer = await self.consumer()
        response = {"final_response": "任务未完成，已有内容已保留。", "failed": True}
        await turns._run_agent_mark_streamed_delivery(response, SimpleNamespace(stream_consumer_holder=[consumer]))
        result = await self.adapter.send("chat", response["final_response"], metadata={"notify": True})
        self.assertTrue(result.success)
        self.assertNotIn("already_sent", response)
        self.assertEqual(len(self.fake.cards), 1)
        self.assertEqual(self.adapter.native_calls, [])

    async def test_failed_turn_notice_added_after_mark_is_in_the_single_error_card(self):
        _, turns, _ = self.hooks()
        consumer = await self.consumer()
        consumer.on_delta("开始处理")
        consumer.finish()
        await consumer.run()
        response = {"final_response": "模型调用失败。", "failed": True}
        await turns._run_agent_mark_streamed_delivery(response, SimpleNamespace(stream_consumer_holder=[consumer]))
        self.assertFalse(consumer.session.phase.terminal)
        complete = response["final_response"] + "\n\n本轮未完成，请重试。"
        first = await self.adapter.send("chat", complete, metadata={"notify": True})
        replay = await self.adapter.send("chat", complete, metadata={"notify": True})
        self.assertTrue(first.success and replay.success)
        self.assertEqual(consumer.session.phase.body, complete)
        self.assertEqual(consumer.session.phase.terminal, "error")
        self.assertEqual(len(self.fake.cards), 1)
        self.assertEqual(self.adapter.native_calls, [])

    async def test_actual_queued_method_preserves_identity_and_delivers_media_once(self):
        _, _, host = self.hooks()
        source = SimpleNamespace(chat_id="chat", thread_id="thread")
        result = await host._deliver_queued_first_response(
            response="队列独立终稿", source=source, adapter=self.adapter, metadata={"thread_id": "thread"},
            event_message_id="topic-root", text_already_delivered=False, deliver_media=True,
            stream_consumer=None, session_key="queued-session", inbound_message_id="queued-raw-inbound")
        self.assertTrue(result)
        self.assertEqual(len(host.media_calls), 1)
        identity = next(iter(self.runtime.engine.phases.values())).identity
        self.assertEqual(identity.turn, Scope(self.route, "queued-session", "queued-raw-inbound").turn)
        self.assertEqual(identity.route.anchor, "topic-root")
        self.assertEqual(self.dispatch.scope.inbound_message_id, "raw-inbound")

    async def test_actual_queued_unknown_keeps_raw_identity_and_skips_media(self):
        _, _, host = self.hooks()
        self.fake.fail_next("create", "lost_ack")
        metadata = {"thread_id": "thread", "extra": "preserved"}
        result = await host._deliver_queued_first_response(
            "队列完整正文", SimpleNamespace(chat_id="chat", thread_id="thread"), self.adapter,
            metadata, "topic-root", False, True, None, "queued-session", "queued-inbound")
        self.assertFalse(result)
        self.assertEqual(host.delivery_calls, [("topic-root", "queued-session", "queued-inbound", metadata)])
        self.assertEqual(host.media_calls, [])
        self.assertEqual(len(self.fake.cards), 1)

    async def test_queued_first_turn_drains_callbacks_and_freezes_final_usage(self):
        _, _, host = self.hooks()
        consumer = await self.consumer()
        agent = SimpleNamespace(model="fake", session_prompt_tokens=100,
                                session_completion_tokens=10, session_api_calls=1)
        consumer.observe_agent(agent)
        agent.step_callback(1)
        agent.tool_start_callback("queued-call", "terminal", {})
        consumer.on_delta("开始")
        consumer.finish()
        await consumer.run()
        # The observer task has exited, but tool completion and final provider
        # usage arrive before the host commits this first response and recurses.
        agent.tool_complete_callback("queued-call", "terminal", {}, {})
        agent.step_callback(2)
        agent.session_prompt_tokens += 200
        agent.session_completion_tokens += 30
        agent.session_api_calls += 2
        agent._last_turn_usage = {"reported": True}
        try:
            self.assertTrue(await host._deliver_queued_first_response(
                "第一项完成", SimpleNamespace(chat_id="chat", thread_id="thread"), self.adapter,
                event_message_id="topic-root", stream_consumer=consumer,
                session_key="session", inbound_message_id="raw-inbound"))
            phase = consumer.session.phase
            self.assertEqual(phase.tools["queued-call"]["state"], "done")
            self.assertEqual(phase.metrics["eta"]["state"], "finished")
            self.assertEqual(phase.metrics["iteration"], 2)
            self.assertEqual(phase.metrics["api_calls"], 2)
            self.assertEqual(phase.metrics["usage"]["output"], 30)
            self.assertEqual(phase.metrics["tools_done"], 1)
            frozen = phase.encode()
            agent.tool_start_callback("too-late", "terminal", {})
            agent.step_callback(99)
            consumer._drain_mailbox()
            self.assertEqual(phase.encode(), frozen)
        finally:
            consumer.restore_callbacks()

    async def test_actual_failed_queued_turn_suppresses_attachments(self):
        _, _, host = self.hooks()
        result = await host._deliver_queued_first_response(
            "本次执行失败", SimpleNamespace(chat_id="chat", thread_id="thread"), self.adapter,
            event_message_id="topic-root", deliver_media=False, session_key="s", inbound_message_id="i")
        self.assertTrue(result)
        self.assertEqual(host.media_calls, [])

    async def test_missing_queued_inbound_never_inherits_parent_scope(self):
        _, _, host = self.hooks()
        with self.assertRaises(Conflict):
            await host._deliver_queued_first_response("正文", SimpleNamespace(chat_id="chat"), self.adapter)
        self.assertEqual(self.fake.cards, {})

    async def test_inline_status_and_non_final_messages_use_native_adapter(self):
        result = await self.adapter.send("chat", "系统提示", reply_to="root", metadata={"thread_id": "thread"})
        self.assertTrue(result.success)
        self.assertEqual(len(self.adapter.native_calls), 1)
        self.assertEqual(self.fake.cards, {})

    async def test_native_media_reuses_only_the_matching_turn_anchor(self):
        payload = dict(chat_id="chat", reply_to=None, metadata={"thread_id": "thread"},
                       caption=None, key_msg_type="file", key_payload={"file_key": "uploaded"},
                       media_tag={"tag": "media", "file_key": "uploaded"})
        await self.adapter._send_uploaded_key(**payload)
        self.assertEqual(self.adapter.native_calls[-1]["reply_to"], "topic-root")
        self.assertEqual(payload["metadata"], {"thread_id": "thread"})
        for changes in ({"chat_id": "another-chat"}, {"metadata": {"thread_id": "other-thread"}},
                        {"reply_to": "explicit"}, {"metadata": {"thread_id": "thread", "reply_to_message_id": "provided"}}):
            changed = dict(payload, **changes)
            await self.adapter._send_uploaded_key(**changed)
            self.assertEqual(self.adapter.native_calls[-1]["reply_to"], changed["reply_to"])

    async def test_other_platform_consumer_and_mark_are_unchanged(self):
        module, turns, _ = self.hooks()
        native = module.GatewayStreamConsumer(adapter=object(), chat_id="other")
        self.assertIsInstance(native, NativeConsumer)
        response = {"final_response": "native"}
        await turns._run_agent_mark_streamed_delivery(response, SimpleNamespace(stream_consumer_holder=[native]))
        self.assertEqual(turns.native_mark_calls, 1)

    async def test_constructor_contract_drift_fails_before_install(self):
        class Old:
            def __init__(self, adapter, chat_id): pass
        class Turns:
            async def _run_agent_mark_streamed_delivery(self, response, turn_ctx): pass
        class Notifications:
            async def _deliver_queued_first_response(self, response, source, adapter): pass
        module = SimpleNamespace(GatewayStreamConsumer=Old)
        with self.assertRaises(RuntimeError): install_host_hooks(module, Turns, Notifications)
        self.assertIs(module.GatewayStreamConsumer, Old)

    async def test_finalize_edit_is_preview_hint_not_authoritative_final(self):
        consumer = await self.consumer()
        consumer.session.emit("snapshot", "工具前说明")
        await consumer.session.queue.drain()
        result = await self.adapter.edit_message("chat", consumer.message_id, "工具后的说明", finalize=True)
        self.assertTrue(result.success)
        self.assertFalse(consumer.session.phase.terminal)

    async def test_explicit_second_consumer_gets_separate_continuation(self):
        first = await self.consumer()
        await first.finalize_from_host("完整主稿")
        second = await self.consumer()
        await second.finalize_from_host("完整补稿")
        self.assertEqual(second.session.identity.phase, 1)
        self.assertEqual(first.session.phase.body, "完整主稿")
        self.assertEqual(len(self.fake.cards), 2)

    async def test_stop_discards_late_authoritative_final(self):
        live = [True]
        consumer = ManagedConsumer(self.adapter, "chat", run_still_current=lambda: live[0])
        consumer.session.emit("snapshot", "部分正文")
        live[0] = False
        self.assertFalse(await consumer.finalize_from_host("迟到完整答案"))
        self.assertEqual(consumer.session.phase.body, "部分正文")
        self.assertEqual(consumer.session.phase.terminal, "cancel")

    async def test_silence_marker_is_not_published_as_answer(self):
        consumer = await self.consumer()
        consumer.on_delta("NO_")
        consumer.on_delta("REPLY")
        consumer.finish("NO_REPLY")
        await consumer.run()
        self.assertFalse(await consumer.finalize_from_host("NO_REPLY"))
        self.assertEqual(self.fake.cards, {})

    async def test_recursive_turn_uses_own_inbound_before_streaming(self):
        _, turns, _ = self.hooks()
        source = SimpleNamespace(chat_id="chat", thread_id="thread")
        first = await turns._run_agent_inner(source, "session", "raw-inbound", "topic-root")
        second = await turns._run_agent_inner(source, "session", "queued-inbound", "topic-root")
        self.assertNotEqual(first.session.identity.turn, second.session.identity.turn)
        self.assertEqual(second.session.identity.phase, 0)
        result = await self.adapter.send("chat", "queued-inbound", metadata={"notify": True})
        self.assertTrue(result.success)
        self.assertEqual(len(self.fake.cards), 2)

    async def test_worker_flush_includes_unconsumed_deltas(self):
        consumer = await self.consumer()
        consumer._loop = asyncio.get_running_loop()
        consumer.on_delta("需要在审批前显示的内容")
        self.assertTrue(await asyncio.to_thread(consumer.flush_pending_sync))
        self.assertEqual(consumer.session.phase.body, "需要在审批前显示的内容")
        self.assertTrue(consumer.message_id)

    async def test_cosmetic_callback_failure_does_not_break_delivery(self):
        consumer = ManagedConsumer(self.adapter, "chat", on_new_message=lambda: 1 / 0)
        consumer.on_delta("正文")
        await consumer._flush()
        consumer.finish("正文")
        await consumer.run()
        self.assertTrue(await consumer.finalize_from_host("正文"))
