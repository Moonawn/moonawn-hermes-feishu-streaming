from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from moonawn_feishu.engine import Engine
from moonawn_feishu.fake_lark import FakeLark
from moonawn_feishu.model import Conflict, Route
from moonawn_feishu.render import body_text, render, safe_tool_label
from moonawn_feishu.runtime import CapacityExceeded, Mailbox, Runtime, Scope
from moonawn_feishu.store import Store


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.fake = FakeLark()
        self.engine = Engine(self.store, self.fake)
        self.runtime = Runtime(self.engine, interval=.001)
        self.scope = Scope(Route("t", "b", "p", "chat", "thread", "canonical-anchor"), "session", "raw-inbound")
        self.block = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.block.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.block.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_raw_inbound_distinguishes_same_topic_same_final(self):
        a = self.runtime.begin(self.scope)
        await a.commit("同样正文")
        b = self.runtime.begin(replace(self.scope, inbound_message_id="next-inbound"))
        await b.commit("同样正文")
        self.assertEqual(len(self.fake.cards), 2)

    async def test_changed_anchor_for_same_inbound_is_detected(self):
        self.runtime.begin(self.scope)
        with self.assertRaises(Conflict):
            self.runtime.begin(replace(self.scope, route=replace(self.scope.route, anchor="wrong-anchor")))

    async def test_explicit_continuation_never_replaces_main(self):
        main = self.runtime.begin(self.scope)
        await main.commit("完整主稿")
        followup = self.runtime.begin(self.scope, continuation=True)
        await followup.commit("完整补稿")
        self.assertEqual([body_text(c["card"]) for c in self.fake.cards.values()], ["完整主稿", "完整补稿"])
        with self.assertRaises(Conflict): await main.commit("短补稿")

    async def test_failure_final_delivered_once_on_repeated_host_fallback(self):
        reply = self.runtime.begin(self.scope)
        self.assertTrue(await reply.commit("本次执行未完成。", failed=True))
        self.assertTrue(await reply.commit("本次执行未完成。"))
        self.assertEqual(len(self.fake.cards), 1)
        self.assertEqual(reply.phase.terminal, "error")

    async def test_mailbox_delta_burst_is_lossless_and_terminal_reserved(self):
        box = Mailbox(max_chars=100000)
        await asyncio.to_thread(lambda: [box.delta("甲") for _ in range(20000)])
        box.finish("权威最终正文")
        box.delta("迟到")
        text, tools, done = box.drain()
        self.assertEqual(text, "甲" * 20000)
        self.assertTrue(done)
        self.assertEqual(box.final_candidate, "权威最终正文")

    async def test_preview_capacity_does_not_drop_authoritative_final(self):
        box = Mailbox(max_chars=3)
        box.delta("前缀")
        box.delta("超过容量的内容")
        box.finish("完整终稿")
        self.assertTrue(box.overflow)
        self.assertEqual(box.final_candidate, "完整终稿")

    async def test_construction_on_hermes_worker_thread_keeps_sqlite_safe(self):
        reply = await asyncio.to_thread(self.runtime.begin, self.scope)
        self.assertTrue(await reply.commit("跨线程后交付"))

    async def test_capacity_rejects_new_turn_explicitly(self):
        self.runtime.max_active = 1
        self.runtime.begin(self.scope)
        with self.assertRaises(CapacityExceeded):
            self.runtime.begin(replace(self.scope, inbound_message_id="second"))
        self.assertEqual(len(self.fake.cards), 0)

    async def test_background_recovery_retries_rate_limit_without_new_event(self):
        now = [10.0]
        self.engine.clock = lambda: now[0]
        reply = self.runtime.begin(self.scope)
        self.fake.fail_next("create", "rate_limit")
        self.assertFalse(await reply.commit("完整终稿"))
        now[0] += 3
        await self.runtime.recover_once()
        self.assertTrue(self.engine.confirmed(reply.identity, "完整终稿"))

    async def test_compaction_keeps_dedup_and_unresolved_body(self):
        reply = self.runtime.begin(self.scope)
        await reply.commit("旧正文")
        reply.phase.updated_at = 1
        self.store.save(reply.phase)
        unresolved = self.runtime.begin(replace(self.scope, inbound_message_id="pending"))
        self.fake.fail_next("create", "lost_ack")
        await unresolved.commit("必须保留的未确认正文")
        unresolved.phase.updated_at = 1
        self.store.save(unresolved.phase)
        self.assertEqual(self.runtime.compact(retention_days=0), 1)
        self.assertTrue(self.engine.confirmed(reply.identity, "旧正文"))
        self.assertEqual(unresolved.phase.body, "必须保留的未确认正文")
        again = self.runtime.begin(self.scope)
        await again.commit("旧正文")
        self.assertEqual(len(self.fake.cards), 2)

    async def test_backup_uses_independent_snapshot_with_correct_receipts(self):
        reply = self.runtime.begin(self.scope)
        await reply.commit("终稿")
        dest = Path(self.tmp.name) / "snapshot.db"
        self.store.backup(dest)
        other = Store(dest)
        try: self.assertEqual(other.all()[0].content_hash, reply.phase.content_hash)
        finally: other.close()

    async def test_tool_chrome_hides_internal_paths_and_secrets(self):
        label = safe_tool_label("读取 /Users/private/key.txt token=abc123 <at id=all>所有人</at>")
        self.assertNotIn("/Users", label)
        self.assertNotIn("abc123", label)
        self.assertNotIn("<at", label)

    async def test_rendering_preserves_body_with_variable_code_fences(self):
        reply = self.runtime.begin(self.scope)
        body = "中文\n\n````md\n```python\nprint(1)\n```\n````\n\n|A|B|\n|-|-|\n|长文|1|\n"
        await reply.commit(body)
        self.assertEqual(body_text(render(reply.phase)), body)

    async def test_cancel_followed_by_late_output_preserves_cancelled_card(self):
        reply = self.runtime.begin(self.scope)
        reply.emit("snapshot", "已生成部分")
        await reply.cancel()
        reply.emit("snapshot", "迟到正文")
        self.assertEqual(reply.phase.body, "已生成部分")
        self.assertEqual(reply.phase.terminal, "cancel")

    async def test_restart_seals_partial_without_claiming_completed_final(self):
        reply = self.runtime.begin(self.scope)
        reply.emit("snapshot", "重启前部分内容")
        await reply.queue.drain()
        self.runtime.start()
        await self.runtime.recover_once()
        self.assertEqual(reply.phase.terminal, "cancel")
        self.assertEqual(reply.phase.seal, "closed")
        self.assertFalse(self.engine.confirmed(reply.identity, "重启前部分内容"))
