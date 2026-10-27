from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from moonawn_feishu.engine import Coalescer, Engine
from moonawn_feishu.fake_lark import FakeLark
from moonawn_feishu.hermes_bridge import HermesBridge, NativeDisposition, inspect_hermes
from moonawn_feishu.model import Conflict, Event, Identity, Phase, Route
from moonawn_feishu.render import CardBudgetExceeded, body_text, chunks, render
from moonawn_feishu.store import Store
from moonawn_feishu.transport import Ref
from moonawn_feishu.quotes import QuoteResolver, VisibleMessage


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.store = Store(self.path)
        self.fake = FakeLark()
        self.engine = Engine(self.store, self.fake)
        self.identity = Identity(Route("test_tenant", "test_bot", "test_profile", "test_chat", "test_thread", "test_anchor"), "turn1")
        self.no_network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden in offline tests"))
        self.no_network.start()
        self.emit("begin", seq=0)

    async def asyncTearDown(self):
        self.no_network.stop()
        self.store.close()
        self.tmp.cleanup()

    def emit(self, kind, text="", *, seq=1, identity=None, **kw):
        i = identity or self.identity
        return self.engine.accept(Event(i, f"{kind}:{seq}", seq, kind, text, **kw))

    async def sync(self, identity=None):
        return await self.engine.sync((identity or self.identity).key)

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.engine = Engine(self.store, self.fake)

    async def test_final_exact_and_single_card(self):
        self.emit("snapshot", "前缀")
        await self.sync()
        self.emit("final", "前缀\n完整末尾 END", seq=2)
        p = await self.sync()
        self.assertTrue(self.engine.confirmed(self.identity, "前缀\n完整末尾 END"))
        self.assertEqual(p.seal, "closed")
        self.assertEqual(self.fake.counts["create"], 1)
        self.assertFalse(self.engine.confirmed(self.identity, "不同正文"))

    async def test_repeated_final_and_restart_do_not_republish(self):
        self.emit("final", "终稿")
        await self.sync()
        counts = dict(self.fake.counts)
        self.restart()
        self.emit("final", "终稿", seq=3)
        for _ in range(3): await self.sync()
        for op in ["create", "update", "close", "send_text"]:
            self.assertEqual(self.fake.counts[op], counts.get(op, 0))

    async def test_style_rejection_does_not_duplicate_verified_body(self):
        self.emit("snapshot", "相同正文")
        await self.sync()
        self.emit("final", "相同正文", seq=2)
        self.fake.fail_next("update", "reject")
        p = await self.sync()
        self.assertEqual(p.delivery, "confirmed")
        self.assertEqual(self.fake.counts["send_text"], 0)

    async def test_style_retry_budget_never_revokes_confirmed_body(self):
        self.emit("snapshot", "相同正文")
        await self.sync()
        self.emit("final", "相同正文", seq=2)
        for _ in range(5):
            self.fake.fail_next("update", "reject")
            p = await self.sync()
        self.assertEqual(p.delivery, "confirmed")
        self.assertTrue(self.engine.confirmed(self.identity, "相同正文"))
        self.assertEqual(self.fake.counts["send_text"], 0)
        self.assertEqual(self.fake.counts["update"], 3)

    async def test_native_delivery_gate_holds_unknown_receipt(self):
        bridge = HermesBridge(self.engine)
        self.emit("final", "终稿")
        self.fake.fail_next("create", "lost_ack")
        await self.sync()
        self.assertEqual(bridge.native_disposition(self.identity, "终稿"), NativeDisposition.HOLD)
        other = replace(self.identity, turn="unclaimed")
        self.assertEqual(bridge.native_disposition(other, "其他"), NativeDisposition.PASS)
        with self.assertRaises(Conflict):
            bridge.native_disposition(self.identity, "不匹配的终稿")
        ref = next(iter(self.fake.requests.values()))
        await self.engine.adopt_card_receipt(self.identity.key, ref)
        self.assertEqual(bridge.native_disposition(self.identity, "终稿"), NativeDisposition.SUPPRESS)

    async def test_quote_cross_bot_only_uses_requester_identity(self):
        seen = []
        async def reader(requester, mid):
            seen.append(requester)
            return VisibleMessage(mid, requester.chat, requester.thread, "other_bot", "被引用的正文")
        q = await QuoteResolver(reader).resolve(self.identity.route, "fake_quoted")
        self.assertTrue(q.readable)
        self.assertEqual(q.text, "被引用的正文")
        self.assertEqual(seen, [self.identity.route])

    async def test_unreadable_or_wrong_scope_quote_does_not_borrow_history(self):
        for msg in [None, VisibleMessage("x", "another_chat", "", "b", "私有正文"), VisibleMessage("x", self.identity.route.chat, self.identity.route.thread, "b", "撤回", False)]:
            async def reader(_r, _m): return msg
            q = await QuoteResolver(reader).resolve(self.identity.route, "x")
            self.assertFalse(q.readable)
            self.assertNotIn("私有正文", q.text)

    async def test_quote_truncation_is_explicit(self):
        async def reader(r, mid): return VisibleMessage(mid, r.chat, r.thread, "b", "甲乙丙丁")
        q = await QuoteResolver(reader, limit=2).resolve(self.identity.route, "x")
        self.assertFalse(q.complete)
        self.assertIn("已截断", q.text)

    async def test_frozen_main_survives_three_continuations(self):
        for n in range(4):
            identity = replace(self.identity, phase=n)
            if n:
                self.emit("begin", seq=0, identity=identity)
            self.emit("final", f"阶段{n}完整正文", seq=1, identity=identity)
            await self.sync(identity)
        self.assertEqual(len(self.fake.cards), 4)
        self.assertEqual([body_text(c["card"]) for c in self.fake.cards.values()], [f"阶段{n}完整正文" for n in range(4)])

    async def test_continuation_waits_for_previous_receipt(self):
        self.emit("final", "主回复")
        self.fake.fail_next("create", "lost_ack")
        p = await self.sync()
        next_id = replace(self.identity, phase=1)
        self.emit("begin", seq=0, identity=next_id)
        self.emit("final", "补充", identity=next_id)
        await self.sync(next_id)
        self.assertEqual(len(self.fake.cards), 1)
        ref = next(iter(self.fake.requests.values()))
        await self.engine.adopt_card_receipt(p.identity.key, ref)
        await self.sync(next_id)
        self.assertEqual(len(self.fake.cards), 2)

    async def test_cannot_start_continuation_before_main_frozen(self):
        with self.assertRaises(Conflict):
            self.emit("begin", seq=0, identity=replace(self.identity, phase=1))

    async def test_lost_update_ack_reconciles_without_fallback(self):
        self.emit("snapshot", "早期")
        await self.sync()
        self.emit("final", "最终")
        # use an increasing sequence for the final
        self.emit("final", "最终", seq=2)
        self.fake.fail_next("update", "lost_ack")
        await self.sync()
        self.restart()
        p = await self.sync()
        self.assertEqual(p.delivery, "confirmed")
        self.assertEqual(self.fake.counts["send_text"], 0)
        self.assertEqual(self.fake.counts["create"], 1)

    async def test_transient_stale_readback_recovers_without_text_fallback(self):
        self.emit("snapshot", "尚未完成")
        await self.sync()
        self.emit("final", "完整终稿", seq=2)
        self.fake.fail_next("read", "wrong_body")
        self.fake.fail_next("read", "wrong_body")
        self.assertEqual((await self.sync()).delivery, "unknown")
        phase = await self.sync()
        self.assertEqual((phase.delivery, phase.seal), ("confirmed", "closed"))
        self.assertEqual(self.fake.counts["send_text"], 0)

    async def test_close_unknown_keeps_confirmed_body(self):
        self.emit("final", "一份终稿")
        self.fake.fail_next("close", "lost_ack")
        p = await self.sync()
        self.assertEqual(p.delivery, "confirmed")
        self.assertEqual(p.seal, "unknown")
        await self.sync()
        self.assertEqual(p.seal, "closed")
        self.assertEqual(self.fake.counts["send_text"], 0)

    async def test_create_unknown_does_not_send_second_message_after_restart(self):
        self.emit("final", "保留正文")
        self.fake.fail_next("create", "lost_ack")
        await self.sync()
        self.restart()
        p = await self.sync()
        self.assertEqual(p.delivery, "unknown")
        self.assertEqual(len(self.fake.cards), 1)
        self.assertEqual(self.fake.counts["create"], 1)

    async def test_definitive_rejection_uses_one_lossless_fallback(self):
        text = "中文 👩‍💻\n" * 1500 + " END "
        self.emit("final", text)
        self.fake.fail_next("create", "reject")
        p = await self.sync()
        self.assertEqual(p.delivery, "confirmed")
        self.assertEqual("".join(x["text"] for x in self.fake.messages.values()), text)
        calls = self.fake.counts["send_text"]
        self.restart()
        await self.sync()
        self.assertEqual(self.fake.counts["send_text"], calls)

    async def test_text_read_failure_does_not_resend_acked_part(self):
        self.emit("final", "完整文本")
        self.fake.fail_next("create", "reject")
        self.fake.fail_next("read_text", "offline")
        await self.sync()
        self.restart()
        p = await self.sync()
        self.assertEqual(p.delivery, "confirmed")
        self.assertEqual(self.fake.counts["send_text"], 1)

    async def test_text_ack_loss_requires_receipt_no_blind_retry(self):
        self.emit("final", "完整文本")
        self.fake.fail_next("create", "reject")
        self.fake.fail_next("send_text", "lost_ack")
        await self.sync()
        self.restart()
        p = await self.sync()
        self.assertEqual(p.delivery, "unknown")
        self.assertEqual(self.fake.counts["send_text"], 1)
        self.assertEqual(p.body, "完整文本")

    async def test_oversize_final_not_truncated(self):
        text = "长正文\n" * 9000 + "末尾校验"
        self.emit("final", text)
        p = await self.sync()
        self.assertEqual(p.mode, "text")
        self.assertEqual("".join(x["text"] for x in self.fake.messages.values()), text)
        self.assertEqual(self.fake.counts["create"], 0)

    async def test_same_anchor_new_turn_same_answer_is_not_deduplicated(self):
        for turn in ["turn1", "turn2"]:
            i = replace(self.identity, turn=turn)
            if turn == "turn2": self.emit("begin", seq=0, identity=i)
            self.emit("final", "相同答案", identity=i)
            await self.sync(i)
        self.assertEqual(len(self.fake.cards), 2)

    async def test_scope_changes_produce_independent_ownership(self):
        routes = [self.identity.route, replace(self.identity.route, bot="second"), replace(self.identity.route, profile="second"), replace(self.identity.route, thread="second")]
        for n, r in enumerate(routes):
            i = replace(self.identity, route=r)
            if n: self.emit("begin", seq=0, identity=i)
            self.emit("final", "同文", identity=i)
            await self.sync(i)
        self.assertEqual(len(self.fake.cards), 4)

    async def test_wrong_identity_receipt_never_confirms(self):
        self.emit("final", "私有正文")
        self.fake.fail_next("read", "wrong_identity")
        with self.assertRaises(Conflict): await self.sync()
        self.assertFalse(self.engine.confirmed(self.identity, "私有正文"))
        self.assertEqual(self.fake.counts["send_text"], 0)

    async def test_withdrawn_card_not_resurrected(self):
        self.emit("snapshot", "预览")
        await self.sync()
        self.emit("final", "终稿", seq=2)
        self.fake.fail_next("read", "withdrawn")
        p = await self.sync()
        await self.sync()
        self.assertEqual(p.delivery, "withdrawn")
        self.assertEqual(len(self.fake.cards), 1)
        self.assertEqual(self.fake.counts["send_text"], 0)

    async def test_final_never_replaced_by_late_callback(self):
        self.emit("final", "正确终稿")
        self.emit("snapshot", "迟到子任务", seq=3)
        with self.assertRaises(Conflict): self.emit("final", "第二个不同终稿", seq=4)
        self.assertEqual(self.engine.phases[self.identity.key].body, "正确终稿")

    async def test_cancel_preserves_preview_and_discards_late_output(self):
        self.emit("snapshot", "部分内容")
        self.emit("cancel", seq=2)
        self.emit("snapshot", "迟到内容", seq=3)
        p = await self.sync()
        self.assertEqual(p.body, "部分内容")
        self.assertEqual(p.terminal, "cancel")
        self.assertFalse(self.engine.confirmed(self.identity, "部分内容"))

    async def test_monotonic_sequences_ignore_out_of_order_updates(self):
        self.emit("snapshot", "新版", seq=5)
        self.emit("snapshot", "旧版", seq=2)
        self.assertEqual(self.engine.phases[self.identity.key].body, "新版")

    async def test_duplicate_event_id_with_different_content_is_rejected(self):
        self.emit("snapshot", "甲")
        with self.assertRaises(Conflict): self.emit("snapshot", "乙")

    async def test_same_body_final_still_updates_visible_status(self):
        self.emit("snapshot", "正文")
        await self.sync()
        self.emit("final", "正文", seq=2)
        await self.sync()
        card = next(iter(self.fake.cards.values()))["card"]
        self.assertIn("答复完成", card["body"]["elements"][0]["content"])

    async def test_unchanged_snapshot_causes_no_extra_write(self):
        self.emit("snapshot", "正文")
        await self.sync()
        for seq in range(2, 12):
            self.emit("snapshot", "正文", seq=seq)
            await self.sync()
        self.assertEqual(self.fake.counts["update"], 0)

    async def test_body_only_streaming_does_not_replace_whole_card(self):
        self.emit("snapshot", "初始正文")
        await self.sync()
        self.emit("snapshot", "初始正文加上后续", seq=2)
        await self.sync()
        self.assertEqual(self.fake.counts["update_body"], 1)
        self.assertEqual(self.fake.counts["update"], 0)
        self.emit("tool", "读取文档", seq=3, tool_id="read", tool_state="done")
        await self.sync()
        self.assertEqual(self.fake.counts["update"], 1)
        self.emit("final", "初始正文加上后续", seq=4)
        await self.sync()
        self.assertEqual(self.fake.counts["update"], 2)
        self.assertEqual(self.fake.counts["send_text"], 0)
        self.assertTrue(self.engine.confirmed(self.identity, "初始正文加上后续"))

    async def test_rate_limit_respects_retry_window(self):
        now = [100.0]
        self.engine.clock = lambda: now[0]
        self.emit("final", "正文")
        self.fake.fail_next("create", "rate_limit")
        await self.sync()
        await self.sync()
        self.assertEqual(self.fake.counts["create"], 1)
        now[0] += 3
        p = await self.sync()
        self.assertEqual(p.delivery, "confirmed")

    async def test_one_owner_for_database(self):
        with self.assertRaises(RuntimeError): Store(self.path)

    async def test_refuses_unrelated_database(self):
        p = Path(self.tmp.name) / "unrelated.db"
        with closing(sqlite3.connect(p)) as db:
            with db: db.execute("CREATE TABLE user_data (value TEXT)")
        with self.assertRaises(ValueError): Store(p)
        with closing(sqlite3.connect(p)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("user_data",)])

    async def test_refuses_foreign_database_even_with_matching_table_name(self):
        p = Path(self.tmp.name) / "foreign.db"
        with closing(sqlite3.connect(p)) as db:
            with db: db.execute("CREATE TABLE phases (key TEXT, state TEXT)")
        with self.assertRaises(ValueError): Store(p)

    async def test_hermes_finalize_flag_is_not_turn_completion(self):
        bridge = HermesBridge(self.engine)
        p = bridge.edit(self.identity, "工具前说明", 1, finalize=True)
        self.assertEqual(p.terminal, "")
        await self.sync()
        self.assertFalse(bridge.may_suppress_native(self.identity, "工具前说明"))
        bridge.final(self.identity, "正式答复", 2)
        await self.sync()
        self.assertTrue(bridge.may_suppress_native(self.identity, "正式答复"))

    async def test_terminal_wakes_debounce_and_preserves_inflight_order(self):
        self.emit("snapshot", "第一段")
        await self.sync()
        self.emit("snapshot", "第二段", seq=2)
        entered, release = asyncio.Event(), asyncio.Event()
        self.fake.update_barrier = (entered, release)
        queue = Coalescer(lambda: self.sync(), interval=0.001)
        queue.schedule()
        await asyncio.wait_for(entered.wait(), timeout=1)
        self.emit("final", "权威最终内容", seq=3)
        queue.schedule(terminal=True)
        release.set()
        await queue.drain()
        writes = [x[2] for x in self.fake.history if x[0] in {"create", "update"}]
        self.assertEqual(writes[-1], "权威最终内容")
        self.assertTrue(self.engine.confirmed(self.identity, "权威最终内容"))

    async def test_burst_coalesces_to_one_latest_write(self):
        queue = Coalescer(lambda: self.sync(), interval=0.25)
        for seq in range(1, 1001):
            self.emit("snapshot", "累积正文" + str(seq), seq=seq)
            queue.schedule()
        self.emit("final", "完整最终答案", seq=1001)
        queue.schedule(terminal=True)
        await asyncio.wait_for(queue.drain(), timeout=1)
        self.assertEqual(queue.writes, 1)
        self.assertEqual(queue.coalesced, 1000)


class RenderingTests(unittest.TestCase):
    def phase(self):
        return Phase(Identity(Route("t", "b", "p", "c"), "turn"))

    def test_code_table_chinese_and_whitespace_are_preserved(self):
        p = self.phase()
        p.body = "# 标题\n\n| 项目 | 数值 |\n| --- | --- |\n| 甲 | 1 |\n\n```python\nprint('ok')\n```\n "
        self.assertEqual(body_text(render(p)), p.body)
        self.assertEqual("".join(chunks(p.body, 7)), p.body)

    def test_tool_panel_bounded_without_cropping_answer(self):
        p = self.phase()
        for n in range(100): p.apply(Event(p.identity, str(n), n, "tool", f"工具 {n}", str(n), "done"))
        self.assertEqual(len(p.tools), 12)
        self.assertEqual(p.tool_count, 100)
        self.assertLess(len(json.dumps(render(p)).encode()), 24000)
        panel = next(e for e in render(p)["body"]["elements"] if e.get("element_id") == "metrics")
        shown = [e["content"] for g in panel["elements"] if g["tag"] == "collapsible_panel"
                 for e in g["elements"] if "✓" in e["content"]]
        self.assertEqual(len(shown), 12)
        self.assertIn("工具 99", shown[-1])

    def test_activity_shows_retained_calls_in_start_order(self):
        p = self.phase()
        for n in range(10):
            state = "running" if n == 0 else "failed" if n == 1 else "done"
            p.apply(Event(p.identity, str(n), n, "tool", f"工具 {n}", str(n), state))
        panel = next(e for e in render(p)["body"]["elements"] if e.get("element_id") == "metrics")
        text = json.dumps(panel["elements"], ensure_ascii=False)
        for n in (0, 1, 9):
            self.assertIn(f"工具 {n}", text)
        self.assertIn("工具 8", text)
        self.assertFalse(panel["expanded"])
        self.assertEqual(len(p.tools), 10)

    def test_repeated_evicted_tool_does_not_inflate_count(self):
        p = self.phase()
        for n in range(20): p.apply(Event(p.identity, str(n), n, "tool", f"工具 {n}", str(n), "running"))
        p = Phase.decode(p.encode())
        p.apply(Event(p.identity, "complete-old-tool", 21, "tool", "工具 0", "0", "done"))
        self.assertEqual(p.tool_count, 20)
        self.assertEqual(list(p.tools)[-1], "0")

    def test_tool_count_limit_is_explicit_and_bounded(self):
        p = self.phase()
        p.tool_seen = {str(n): True for n in range(4096)}
        p.tool_count = 4096
        p.apply(Event(p.identity, "extra", 1, "tool", "额外工具", "extra", "done"))
        self.assertEqual(len(p.tool_seen), 4096)
        self.assertTrue(p.tool_count_capped)
        panel = next(e for e in render(p)["body"]["elements"] if e.get("element_id") == "metrics")
        self.assertIn("至少 4096", json.dumps(panel, ensure_ascii=False))

    def test_missing_hermes_source_fails_does_not_skip(self):
        report = inspect_hermes(Path("/nonexistent/offline-fixture"))
        self.assertFalse(report["static_surface_passed"])
        self.assertFalse(report["live_integration_verified"])
