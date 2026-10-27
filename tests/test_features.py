from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, replace
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace as NS
import sys
import unittest
from unittest.mock import patch

from moonawn_feishu.controls import ControlService, install_feature_hooks
from moonawn_feishu.engine import Engine
from moonawn_feishu.fake_lark import FakeLark
from moonawn_feishu.feishu_transport import FeishuTransport, HTTPResult
from moonawn_feishu.model import Conflict, Event, Identity, Route, Phase
from moonawn_feishu.render import render, body_text, structure_fingerprint
from moonawn_feishu.plugin import Dispatch, ManagedConsumer, _dispatch
from moonawn_feishu.runtime import Runtime, Scope
from moonawn_feishu.store import Store
from moonawn_feishu.telemetry import DurationHistory, TurnTelemetry, metric_lines, safe_model_name
from moonawn_feishu.transport import Ref, UnknownOutcome
from moonawn_feishu.tool_display import native_preview, safe_detail


class ToolPreviewTests(unittest.TestCase):
    def test_preview_preserves_operators_without_creating_card_markup(self):
        from html import unescape
        from moonawn_feishu.render import literal
        from moonawn_feishu.tool_display import clean_tool_data
        command = 'python3 -c "print(37*37, [1,2], {3:4}, 1<2)"'
        cleaned = clean_tool_data({"preview": command})["preview"]
        self.assertEqual(cleaned, command)
        self.assertEqual(clean_tool_data({"preview": cleaned})["preview"], command)
        encoded = literal(cleaned)
        self.assertNotIn("*", encoded)
        self.assertNotIn("[", encoded)
        self.assertEqual(unescape(encoded), command)
        markup = '<at id=all>all</at> [label](https://example.com/private?token=secret)'
        encoded = literal(safe_detail(markup))
        self.assertNotIn("<at", encoded)
        self.assertNotIn("[label]", encoded)
        self.assertNotIn("secret", encoded)

    def test_recovery_preserves_start_order_even_when_call_ids_sort_differently(self):
        p = Phase(Identity(Route("app", "app", "profile", "chat"), "turn"))
        p.apply(Event(p.identity, "start-z", 1, "tool", "terminal", tool_id="z-first", tool_state="running"))
        p.apply(Event(p.identity, "start-a", 2, "tool", "terminal", tool_id="a-second", tool_state="running"))
        p.apply(Event(p.identity, "done-a", 3, "tool", "terminal", tool_id="a-second", tool_state="done"))
        restored = Phase.decode(p.encode())
        self.assertEqual(list(restored.tools), ["z-first", "a-second"])
        self.assertEqual(restored.tools["a-second"]["state"], "done")

    def test_dedicated_runtime_paths_do_not_escape_in_details(self):
        for root in ("/acceptance-home", "/root", "/work", "/workspace"):
            self.assertNotIn(root, safe_detail(root + "/cache/private-draft.md"))

    def test_heredoc_payload_is_not_given_to_display_or_mutated(self):
        display = ModuleType("agent.display")
        seen = []
        def prepare(name, args, **kw):
            seen.append(args["command"])
            return NS(text=args["command"])
        display.prepare_tool_preview = prepare
        args = {"command": "python3 - <<'PY'\nprint('private draft payload')\nPY"}
        with patch.dict(sys.modules, {"agent.display": display}):
            preview = native_preview("terminal", args)
        self.assertEqual(seen, ["python3 -"])
        self.assertNotIn("private draft payload", preview)
        self.assertIn("多行内容已省略", preview)
        self.assertIn("private draft payload", args["command"])

    def test_single_line_native_preview_remains_available(self):
        display = ModuleType("agent.display")
        display.prepare_tool_preview = lambda name, args, **kw: NS(text="curl https://example.com/public")
        with patch.dict(sys.modules, {"agent.display": display}):
            self.assertEqual(native_preview("terminal", {"command": "curl https://example.com/public"}), "curl example.com/…")


class UsageTests(unittest.TestCase):
    def test_reported_rate_uses_precise_turn_time_and_freezes(self):
        now = [10.0]
        agent = NS(model="deepseek-flash", provider="deepseek", session_prompt_tokens=900,
                   session_completion_tokens=80, _last_turn_usage=None)
        t = TurnTelemetry(clock=lambda: now[0])
        t.attach(agent)
        self.assertEqual(t.snapshot()["model"], "deepseek-flash")
        self.assertIsNone(t.snapshot()["output_rate"])
        agent.session_completion_tokens += 10
        agent._last_turn_usage = {"reported": True}
        self.assertIsNone(t.snapshot()["output_rate"])  # zero elapsed, never infinity
        now[0] += .5
        self.assertEqual(t.snapshot()["output_rate"], 20.0)
        now[0] += 3.5  # includes tool/thinking waits
        frozen = t.finish(success=True)
        self.assertEqual(frozen["output_rate"], 2.5)
        self.assertEqual(frozen["rate_basis"], "turn_elapsed")
        now[0] += 100
        agent.session_completion_tokens += 1000
        self.assertEqual(t.snapshot(), frozen)

    def test_reported_zero_output_is_distinct_from_missing(self):
        now = [0]
        t = TurnTelemetry(clock=lambda: now[0])
        agent = NS(model="test", _last_turn_usage=None)
        t.attach(agent)
        now[0] = 5
        self.assertIsNone(t.snapshot()["output_rate"])
        agent._last_turn_usage = {}
        value = t.finish()
        self.assertEqual(value["output_rate"], 0.0)
        self.assertIn("0.0 tok/s", "\n".join(metric_lines(value)))

    def test_model_identifier_does_not_expose_endpoint_path_or_markup(self):
        for model in ("https://host/model?key=private", "/Users/private/model", "sk-privatevalue123",
                      "repo/sk-secretvalue123", "<at id=all>", "my model\nTOKEN=secret"):
            self.assertEqual(safe_model_name(model), "已隐藏")
        self.assertEqual(safe_model_name("org/qwen3.5-32b:free"), "org/qwen3.5-32b:free")
        self.assertIsNone(safe_model_name(None))

    def test_footer_stays_separate_and_present_through_reply_states(self):
        route = Route("tenant", "bot", "profile", "chat", "thread")
        # Use the same identity contract as the core, without a live transport.
        p = Phase(Identity(route, "turn"))
        p.body = "只保留原始正文。"
        for terminal in ("", "final", "cancel", "error"):
            p.terminal = terminal
            card = render(p)
            self.assertEqual(body_text(card), p.body)
            footer = card["body"]["elements"][-1]
            self.assertEqual(footer["element_id"], "metrics")
            content = json.dumps(footer, ensure_ascii=False)
            for field in ("Agentloop", "已耗时", "模型", "Token", "输出均速", "tok/s"):
                self.assertIn(field, content)
            self.assertNotIn(p.body, content)
        before = render(p)
        p.body += "正文继续增长。"
        self.assertEqual(structure_fingerprint(before), structure_fingerprint(render(p)))

    def test_usage_is_per_run_and_missing_is_not_zero(self):
        agent = NS(session_prompt_tokens=9000, session_completion_tokens=900, session_api_calls=8,
                   _last_turn_usage={"old": True}, model="test", provider="fake")
        t = TurnTelemetry()
        t.attach(agent)
        self.assertIsNone(t.snapshot()["usage"])
        agent.session_api_calls += 1
        self.assertIsNone(t.snapshot()["usage"])
        agent.session_prompt_tokens += 200
        agent.session_completion_tokens += 30
        agent._last_turn_usage = {"input": 200}
        self.assertEqual(t.snapshot()["usage"]["input"], 200)
        self.assertEqual(t.snapshot()["usage"]["output"], 30)
        self.assertEqual(t.snapshot()["api_calls"], 1)

    def test_eta_stays_inside_disclosure_and_disappears_at_end(self):
        p = Phase(Identity(Route("t", "b", "p", "c"), "turn"))
        p.metrics = {"eta": {"state": "estimate", "low": 5, "high": 10, "samples": 8}}
        footer = render(p)["body"]["elements"][-1]
        self.assertNotIn("预计", footer["header"]["title"]["content"])
        self.assertIn("预计还需", json.dumps(footer["elements"], ensure_ascii=False))
        p.terminal = "final"
        self.assertNotIn("预计", json.dumps(render(p), ensure_ascii=False))

    def test_finish_freezes_elapsed_and_usage(self):
        now = [10]
        t = TurnTelemetry(clock=lambda: now[0])
        now[0] = 20
        value = t.finish()
        now[0] = 400
        t.step(99)
        self.assertEqual(t.snapshot(), value)
        self.assertEqual(value["elapsed"], 10)

    def test_history_cold_interval_overrun_and_namespace(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "own.db")
            h = DurationHistory(store, "actor-1")
            self.assertEqual(h.estimate("model", False, 1)["state"], "cold")
            for i, elapsed in enumerate([20, 30, 40, 50, 60]):
                h.record(str(i), "model", False, elapsed)
            h.record("0", "model", False, 900)
            eta = h.estimate("model", False, 5)
            self.assertEqual((eta['state'], eta['samples'], eta['low'], eta['high']), ('estimate', 5, 15, 55))
            self.assertEqual(h.estimate("model", False, 100)["state"], "overdue")
            self.assertEqual(h.estimate("model", True, 5)["state"], "cold")
            self.assertEqual(DurationHistory(store, "actor-2").estimate("model", False, 5)["state"], "cold")
            store.close()

    def test_waiting_not_estimated_and_loops_not_percent(self):
        t = TurnTelemetry()
        t.step(5, [])
        t.waiting = True
        self.assertEqual(t.snapshot()["eta"]["state"], "waiting")
        self.assertNotIn("%", "\n".join(metric_lines(t.snapshot())))
        t.tool("id")
        t.tool("id", done=True)
        t.tool("id", done=True)
        self.assertEqual(t.snapshot()["tools_started"], 1)
        self.assertEqual(t.snapshot()["tools_done"], 1)


@dataclass
class Source:
    chat_id: str = "chat"
    user_id: str = "owner"
    thread_id: str = "thread-one"
    chat_type: str = "group"
    message_id: str = "inbound"
    def to_dict(self): return asdict(self)
    @classmethod
    def from_dict(cls, data): return cls(**data)


class NativeOriginGuard:
    async def _resume_target_allowed(self, source, target_id, allow_override=False):
        return False

    def _same_origin_chat(self, current, origin):
        return current.chat_id == origin.chat_id and current.user_id == origin.user_id


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "own.db")
        self.source = Source()
        self.current = "session_one"
        self.visible = True
        self.events, self.notices, self.panels = [], [], []
        self.topics = 0
        async def entry(source): return NS(session_id=self.current)
        async def allowed(source, target, allow_override=False): return self.visible
        self.host = NS(_session_key_for_source=lambda s: s.chat_id + ":" + s.thread_id,
                       _normalize_source_for_session_key=lambda s: s,
                       async_session_store=NS(get_or_create_session=entry), _resume_target_allowed=allowed)
        async def dispatch(event): self.events.append(event)
        self.adapter = NS(_app_id="app", _moonawn_profile="profile", gateway_runner=self.host,
            _active_sessions={}, _session_tasks={}, _is_interactive_operator_authorized=lambda user: True,
            _handle_message_with_guards=dispatch,
            _moonawn_scope=lambda s, k, i, a: Scope(Route("app", "app", "profile", s.chat_id, s.thread_id, a), k, i))
        async def create(route, card, key):
            self.panels.append((route, card))
            return Ref("card", "panel_" + str(len(self.panels)))
        async def send(route, kind, payload, key): self.notices.append(payload["text"])
        async def topic(route, title, key):
            self.topics += 1
            return "root", "server-topic-id", "reply"
        self.service = ControlService(self.adapter, self.store, NS(create=create, _send=send, create_topic=topic), clock=lambda: 100)
        async def listing(source):
            return self.current, [{"id": "session_one", "title": "一个会话"}, {"id": "session_two", "title": "另一个会话"}]
        self.service.listing = listing
        self.adapter._moonawn_controls = self.service
        session_module = ModuleType("gateway.session")
        session_module.SessionSource = Source
        base_module = ModuleType("gateway.platforms.base")
        base_module.MessageEvent = NS
        self.modules = patch.dict("sys.modules", {"gateway.session": session_module, "gateway.platforms.base": base_module})
        self.modules.start()
        await self.service.show(self.source, "anchor")

    async def asyncTearDown(self):
        self.modules.stop()
        self.store.close()
        self.temp.cleanup()

    def click(self, label):
        card = self.panels[0][1]
        b = next(e for e in card["body"]["elements"] if e.get("text", {}).get("content", "").startswith(label))
        return NS(header=NS(event_id="click", app_id="app"), event=NS(
            action=NS(value=b["behaviors"][0]["value"]), operator=NS(open_id="owner"),
            context=NS(open_chat_id="chat", open_message_id="panel_1")))

    async def test_switch_retains_native_origin_and_one_shot(self):
        data = self.click("继续")
        await self.service.callback(data)
        await self.service.callback(data)
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.text, "/resume session_two")
        self.assertEqual(event.source.thread_id, "thread-one")
        self.assertFalse(event.internal)
        self.assertTrue(event.allow_gateway_control)
        self.assertEqual(event.reply_anchor_override, "panel_1")

    async def test_other_actor_chat_message_app_and_profile_are_refused(self):
        for target, field, wrong in [("operator", "open_id", "someone"), ("context", "open_chat_id", "other"),
                                     ("context", "open_message_id", "forwarded"), ("header", "app_id", "other")]:
            data = self.click("继续")
            setattr(data.header if target == "header" else getattr(data.event, target), field, wrong)
            await self.service.callback(data)
        self.adapter._moonawn_profile = "another-profile"
        await self.service.callback(self.click("继续"))
        self.assertEqual(self.events, [])
        self.assertEqual(self.notices, [])

    async def test_busy_switch_and_new_are_refused_without_interrupt(self):
        self.adapter._active_sessions["chat:thread-one"] = object()
        await self.service.callback(self.click("继续"))
        await self.service.callback(self.click("新建"))
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.notices), 2)

    async def test_busy_original_does_not_block_new_topic(self):
        self.adapter._active_sessions["chat:thread-one"] = object()
        await self.service.callback(self.click("另开"))
        self.assertEqual(self.topics, 1)
        self.assertEqual(self.panels[-1][0].thread, "server-topic-id")
        self.assertIn("chat:thread-one", self.adapter._active_sessions)

    async def test_stale_selection_never_switches_new_current_session(self):
        self.current = "later_session"
        await self.service.callback(self.click("继续"))
        self.assertEqual(self.events, [])

    async def test_becoming_busy_during_permission_lookup_refuses_switch(self):
        async def delayed(source, target, allow_override=False):
            self.adapter._active_sessions["chat:thread-one"] = object()
            return True
        self.host._resume_target_allowed = delayed
        await self.service.callback(self.click("继续"))
        self.assertEqual(self.events, [])

    async def test_expired_and_permission_revoked_refused(self):
        self.visible = False
        await self.service.callback(self.click("继续"))
        self.service.clock = lambda: 2000
        await self.service.callback(self.click("新建"))
        self.assertEqual(self.events, [])

    async def test_concurrent_duplicate_topic_creates_once(self):
        await asyncio.gather(*(self.service.callback(self.click("另开")) for _ in range(5)))
        self.assertEqual(self.topics, 1)

    async def test_unknown_topic_ack_is_not_replayed(self):
        async def missing(*args):
            self.topics += 1
            raise UnknownOutcome("synthetic")
        self.service.transport.create_topic = missing
        await self.service.callback(self.click("另开"))
        await self.service.callback(self.click("另开"))
        self.assertEqual(self.topics, 1)
        self.assertIn("回执", self.notices[0])

    async def test_typing_preference_scoped_to_actor_and_chat(self):
        await self.service.callback(self.click("打字机"))
        self.assertFalse(self.service.typing(self.source))
        self.assertTrue(self.service.typing(replace(self.source, user_id="other")))
        self.assertTrue(self.service.typing(replace(self.source, chat_id="other")))
        self.assertEqual(self.events, [])

    async def test_shutdown_drains_inflight_panel_and_rejects_new_clicks(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.service.transport.create
        async def pending(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        self.service.transport.create = pending
        showing = asyncio.create_task(self.service.show(self.source, "anchor"))
        await entered.wait()
        closing = asyncio.create_task(self.service.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await asyncio.gather(showing, closing)
        await self.service.callback(self.click("继续"))
        self.assertEqual(self.events, [])

    async def test_refresh_rebinds_current_without_switching(self):
        self.current = "session_two"
        await self.service.callback(self.click("刷新"))
        self.assertEqual(len(self.panels), 2)
        self.assertEqual(self.events, [])
        self.assertFalse(self.panels[0][1]["config"]["streaming_mode"])
        self.assertNotIn("session_one", str(self.panels[0][1]))

    async def test_pending_command_rechecks_native_binding(self):
        class Runner:
            def _wire_turn_agent_callbacks(self, agent, turn_route, reasoning_config, stream_delta_cb, interim_assistant_cb, want_interim_messages): pass
        service = self.service
        class Commands(NativeOriginGuard):
            async_session_store = self.host.async_session_store
            def _delivery_adapter_for(self, source): return service.adapter
            async def _handle_sessions_command(self, event): return "native sessions"
            async def _handle_reset_command(self, event): raise AssertionError("must not reset")
            async def _handle_resume_command(self, event): raise AssertionError("must not switch")
        undo = install_feature_hooks(Runner, Commands)
        try:
            await service.callback(self.click("继续"))
            self.current = "newer_session"
            response = await Commands()._handle_resume_command(self.events[0])
            self.assertIn("已变化", response)
        finally:
            undo()

    async def test_native_switch_runs_once_and_refreshes_panel(self):
        class Runner:
            def _wire_turn_agent_callbacks(self, agent, turn_route, reasoning_config, stream_delta_cb, interim_assistant_cb, want_interim_messages): pass
        service, test = self.service, self
        calls = []
        class Commands(NativeOriginGuard):
            async_session_store = test.host.async_session_store
            def _delivery_adapter_for(self, source): return service.adapter
            async def _handle_sessions_command(self, event): return "native sessions"
            async def _handle_reset_command(self, event): return "native reset"
            async def _handle_resume_command(self, event):
                calls.append(event)
                test.current = "session_two"
                return "native success"
        undo = install_feature_hooks(Runner, Commands)
        try:
            await service.callback(self.click("继续"))
            self.assertIsNone(await Commands()._handle_resume_command(self.events[0]))
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(self.panels), 2)
            await Commands()._handle_resume_command(self.events[0])
            self.assertEqual(len(calls), 1)
        finally:
            undo()

    async def test_inactive_alt_owner_requires_full_consistent_persisted_origin(self):
        class ProofSource:
            @staticmethod
            def from_dict(raw):
                return NS(**dict(raw, platform=NS(value=raw["platform"])))
        sys.modules["gateway.session"].SessionSource = ProofSource
        raw = dict(platform="feishu", chat_id="chat", thread_id="thread-one", user_id="owner", user_id_alt="union-owner")
        source = ProofSource.from_dict(raw)
        good = dict(id="history", source="feishu", session_key="chat:thread-one", user_id="owner",
                    chat_id="chat", thread_id="thread-one", origin_json=json.dumps(raw))
        active = dict(good)
        async def lookup(target): return active
        self.host._session_db = NS(get_session=lookup)
        self.host._same_origin_chat = lambda current, origin: current.chat_id == origin.chat_id
        self.assertTrue(await self.service.persisted_owner(source, "history"))
        for field, wrong in [("session_key", "another-key"), ("user_id", "someone"),
                             ("chat_id", "elsewhere"), ("thread_id", "another-topic"),
                             ("origin_json", None), ("origin_json", "{broken"),
                             ("origin_json", json.dumps(dict(raw, user_id_alt="another-union"))),
                             ("origin_json", json.dumps(dict(raw, thread_id="another-topic")))]:
            active = dict(good, **{field: wrong})
            self.assertFalse(await self.service.persisted_owner(source, "history"), field)

    async def test_busy_panel_keeps_running_task_and_disables_switches(self):
        class Runner:
            def _wire_turn_agent_callbacks(self, agent, turn_route, reasoning_config, stream_delta_cb, interim_assistant_cb, want_interim_messages): pass
        service, test = self.service, self
        calls = []
        class Commands(NativeOriginGuard):
            def _delivery_adapter_for(self, source): return service.adapter
            async def _handle_sessions_command(self, event): return "native sessions"
            async def _handle_reset_command(self, event): raise AssertionError("must not reset")
            async def _handle_resume_command(self, event): raise AssertionError("must not switch")
        class Busy:
            denied = None
            def _session_key_for_source(self, source): return test.host._session_key_for_source(source)
            def _check_slash_access(self, source, name): return self.denied
            @asynccontextmanager
            async def _async_profile_scope_for_source(self, source):
                calls.append("scope")
                yield
            async def _dispatch_busy_slash_command(self, event, cmd_def, quick_key, source): return "native busy"
        original = Busy._dispatch_busy_slash_command
        class Host(Commands, Busy): pass
        undo = install_feature_hooks(Runner, Commands, Busy)
        event = NS(source=self.source, metadata={}, message_id="busy-inbound", reply_anchor_override=None,
                   get_command=lambda: "sessions", get_command_args=lambda: "", allow_gateway_control=True)
        key = self.host._session_key_for_source(self.source)
        guard = object()
        self.adapter._active_sessions[key] = guard
        try:
            host = Host()
            self.assertIsNone(await host._dispatch_busy_slash_command(event, NS(name="sessions"), key, self.source))
            self.assertIs(self.adapter._active_sessions[key], guard)
            buttons = [e for e in self.panels[-1][1]["body"]["elements"] if e.get("tag") == "button"]
            self.assertFalse(buttons[0].get("disabled", False))  # topic
            self.assertTrue(all(b.get("disabled") for b in buttons[1:4]))  # new and both sessions
            self.assertEqual(calls, ["scope"])
            count = len(self.panels)
            host.denied = "denied"
            self.assertEqual(await host._dispatch_busy_slash_command(event, NS(name="sessions"), key, self.source), "denied")
            self.assertEqual(len(self.panels), count)
            host.denied = None
            for name, args, permitted in [("resume", "target", True), ("new", "", True),
                                           ("sessions", "--all", True), ("sessions", "", False)]:
                event.get_command = lambda: name
                event.get_command_args = lambda: args
                event.allow_gateway_control = permitted
                self.assertEqual(await host._dispatch_busy_slash_command(event, NS(name=name), key, self.source), "native busy")
            self.assertEqual(len(self.panels), count)
        finally:
            undo()
        self.assertIs(Busy._dispatch_busy_slash_command, original)

    async def test_control_retention_keeps_unresolved_delivery(self):
        self.store.save_operation("control:expired", {"expires_at": 1, "state": "executed"})
        self.store.save_operation("topic:old:root", {"state": "done", "created_at": 1, "request_hash": "old", "data": {"message_id": "known"}})
        self.store.save_operation("topic:unknown:root", {"state": "attempting", "created_at": 1, "request_hash": "unknown"})
        self.store.compact(before=50)
        self.assertIsNone(self.store.operation("control:expired"))
        self.assertEqual(self.store.operation("topic:old:root")["state"], "archived")
        self.assertEqual(self.store.operation("topic:unknown:root")["state"], "attempting")


class FeatureRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_tool_details_keep_call_identity_order_and_safe_previews(self):
        display = ModuleType("agent.display")
        display.prepare_tool_preview = lambda name, args, **kw: NS(text=args.get("query", ""))
        display._detect_tool_failure = lambda name, result: (bool(result.get("error")), result.get("error", ""))
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "own.db")
            runtime = Runtime(Engine(store, FakeLark()), interval=.001)
            scope = Scope(Route("app", "app", "profile", "chat"), "session", "inbound")
            adapter = NS(_moonawn=runtime)
            token = _dispatch.set(Dispatch(adapter, scope))
            agent = NS(model="fake", provider="fake")
            try:
                consumer = ManagedConsumer(adapter, "chat")
                consumer.observe_agent(agent)
                with patch.dict(sys.modules, {"agent.display": display}), patch("moonawn_feishu.plugin.time.monotonic") as clock:
                    clock.return_value = 10
                    agent.step_callback(2)
                    agent.tool_start_callback("first", "web_search", {"query": "公开参考资料一"})
                    clock.return_value = 12
                    agent.step_callback(3)
                    agent.tool_start_callback("second", "web_search", {"query": "公开参考资料二"})
                    clock.return_value = 15
                    agent.tool_complete_callback("second", "web_search", {}, {})
                    consumer._drain_mailbox()
                    clock.return_value = 18
                    agent.tool_complete_callback("first", "web_search", {}, {"error": "exit 2 secret=hidden-value /Users/private/file"})
                    consumer._drain_mailbox()
                phase = consumer.session.phase
                self.assertEqual(list(phase.tools), ["first", "second"])
                self.assertEqual(phase.tools["first"]["iteration"], 2)
                self.assertEqual(phase.tools["second"]["iteration"], 3)
                self.assertEqual(phase.tools["first"]["elapsed"], 8)
                self.assertEqual(phase.tools["second"]["elapsed"], 3)
                self.assertEqual(phase.tools["first"]["state"], "failed")
                self.assertEqual(phase.tools["second"]["preview"], "公开参考资料二")
                self.assertEqual(phase.tools["first"]["preview"], "公开参考资料一")
                self.assertNotIn("hidden-value", phase.encode())
                self.assertNotIn("/Users/private", phase.encode())
                card = render(Phase.decode(phase.encode()))
                self.assertEqual(card["body"]["elements"][-1]["tag"], "collapsible_panel")
                self.assertNotIn('"element_id": "activity"', json.dumps(card))
                self.assertIn("web&#95;search", json.dumps(card))
                consumer.restore_callbacks()
                await runtime.close()
            finally:
                _dispatch.reset(token)
                store.close()

    async def test_native_display_failure_does_not_break_original_callbacks(self):
        display = ModuleType("agent.display")
        def broken(*a, **kw): raise RuntimeError("optional host renderer unavailable")
        display.prepare_tool_preview = broken
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "own.db")
            runtime = Runtime(Engine(store, FakeLark()), interval=.001)
            adapter = NS(_moonawn=runtime)
            token = _dispatch.set(Dispatch(adapter, Scope(Route("a", "a", "p", "chat"), "s", "i")))
            original = lambda *a: "native-return"
            agent = NS(model="fake", provider="fake", tool_start_callback=original)
            try:
                consumer = ManagedConsumer(adapter, "chat")
                consumer.observe_agent(agent)
                with patch.dict(sys.modules, {"agent.display": display}):
                    self.assertEqual(agent.tool_start_callback("id", "lookup", {"secret": "never-copy"}), "native-return")
                consumer._drain_mailbox()
                self.assertNotIn("never-copy", consumer.session.phase.encode())
                consumer.restore_callbacks()
                self.assertIs(agent.tool_start_callback, original)
                await runtime.close()
            finally:
                _dispatch.reset(token)
                store.close()

    async def test_callbacks_preserve_native_hooks_and_correct_tool_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "own.db")
            runtime = Runtime(Engine(store, FakeLark()), interval=.001)
            scope = Scope(Route("app", "app", "profile", "chat"), "session", "inbound")
            adapter = NS(_moonawn=runtime)
            token = _dispatch.set(Dispatch(adapter, scope))
            native = []
            agent = NS(model="fake", provider="fake", step_callback=lambda *a: native.append(a) or "preserved",
                       tool_start_callback=None, tool_complete_callback=None)
            try:
                consumer = ManagedConsumer(adapter, "chat")
                original = agent.step_callback
                consumer.observe_agent(agent)
                wrapper = agent.step_callback
                consumer.observe_agent(agent)
                self.assertIs(agent.step_callback, wrapper)
                delegate_callback = lambda *a: "delegate"
                delegate = NS(model="other", step_callback=delegate_callback)
                consumer.observe_agent(delegate)
                self.assertIs(delegate.step_callback, delegate_callback)
                self.assertFalse(hasattr(delegate, "tool_start_callback"))
                self.assertEqual(agent.step_callback(2, []), "preserved")
                agent.tool_start_callback("call-1", "lookup", {"secret": "never-render"})
                agent.tool_complete_callback("call-1", "lookup", {}, "internal output")
                consumer._drain_mailbox()
                self.assertEqual(consumer.session.phase.tool_count, 1)
                self.assertNotIn("never-render", consumer.session.phase.encode())
                self.assertEqual(consumer.telemetry.snapshot()["iteration"], 2)
                consumer.restore_callbacks()
                self.assertIs(agent.step_callback, original)
                self.assertIsNone(agent.tool_start_callback)
                await runtime.close()
            finally:
                _dispatch.reset(token)
                store.close()

    async def test_two_threads_stream_independently_and_instant_skips_typewriter_api(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "own.db")
            fake = FakeLark()
            engine = Engine(store, fake)
            a = Identity(Route("app", "app", "p", "chat", "t1", "a1"), "one")
            b = Identity(Route("app", "app", "p", "chat", "t2", "a2"), "two")
            for identity in [a, b]:
                engine.accept(Event(identity, "begin", 0, "begin"))
                engine.accept(Event(identity, "start", 1, "snapshot", "开始"))
            engine.phases[a.key].typing = False
            await asyncio.gather(engine.sync(a.key), engine.sync(b.key))
            self.assertFalse(fake.cards[engine.phases[a.key].card_id]["card"]["config"]["streaming_mode"])
            for identity in [a, b]:
                engine.accept(Event(identity, "delta", 2, "snapshot", "开始处理"))
                await engine.sync(identity.key)
            self.assertEqual(fake.counts["update_body"], 1)
            engine.accept(Event(a, "final", 3, "final", "会话一"))
            engine.accept(Event(b, "final", 3, "final", "会话二"))
            await asyncio.gather(engine.sync(a.key), engine.sync(b.key))
            self.assertTrue(engine.confirmed(a, "会话一"))
            self.assertTrue(engine.confirmed(b, "会话二"))
            store.close()

    async def test_topic_ids_come_from_server_and_request_ids_deduplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "own.db")
            requests = []
            async def request(method, path, body):
                requests.append((path, body))
                return HTTPResult(200, {"code": 0, "data": {"message_id": "reply" if path.endswith("reply") else "root",
                    "chat_id": "chat", "thread_id": "actual-thread" if path.endswith("reply") else ""}})
            transport = FeishuTransport(store, NS(request=request), tenant="app", bot="app", profile="p", send_interval=0)
            route = Route("app", "app", "p", "chat", "old-thread", "old-anchor")
            self.assertEqual(await transport.create_topic(route, "新话题", "same"), ("root", "actual-thread", "reply"))
            await transport.create_topic(route, "新话题", "same")
            self.assertEqual(len(requests), 2)
            self.assertTrue(requests[1][1]["reply_in_thread"])
            self.assertIn("/messages/root/reply", requests[1][0])
            store.close()
