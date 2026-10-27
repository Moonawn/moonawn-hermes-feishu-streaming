from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from moonawn_feishu.engine import Engine
from moonawn_feishu.feishu_transport import FeishuTransport, HTTPResult
from moonawn_feishu.model import Conflict, Event, Identity, Phase, Route
from moonawn_feishu.render import body_text, render
from moonawn_feishu.store import Store
from moonawn_feishu.transport import RateLimited, Ref, Unavailable, UnknownOutcome


class DocumentedAPI:
    """Synthetic API-shaped fixtures; not recordings from a real Feishu account."""
    def __init__(self, route):
        self.route = route
        self.requests, self.cards, self.messages, self.uuids = [], {}, {}, {}
        self.faults = defaultdict(deque)
        self.flatten = False

    def fail(self, operation, fault):
        self.faults[operation].append(fault)

    async def request(self, method, path, body=None):
        self.requests.append((method, path, deepcopy(body)))
        operation = ("read" if method == "GET" else "entity" if path.endswith("/cards")
                     else "send" if "/im/" in path else "write")
        fault = self.faults[operation].popleft() if self.faults[operation] else None
        if fault == "rate": return HTTPResult(429, {"code": 230020}, {"retry-after": "2"})
        if fault == "permission": return HTTPResult(400, {"code": 230027})
        if fault == "moderation": return HTTPResult(400, {"code": 230028})
        if fault == "unrecognized": return HTTPResult(200, {"ok": True, "data": {}})
        if fault == "network": raise OSError("secret=DO_NOT_LEAK")
        if operation == "entity":
            cid = str(len(self.cards) + 100)
            self.cards[cid] = {"card": json.loads(body["data"]), "sequence": 0}
            data = {"card_id": cid}
        elif operation == "send":
            uid = body["uuid"]
            if uid not in self.uuids:
                mid = "om_" + str(len(self.messages) + 1)
                self.uuids[uid] = mid
                self.messages[mid] = {"message_id": mid, "chat_id": self.route.chat, "thread_id": self.route.thread,
                                      "msg_type": body["msg_type"], "body": {"content": body["content"]},
                                      "sender": {"id": self.route.bot, "id_type": "app_id"}}
            data = self.messages[self.uuids[uid]]
        elif operation == "write":
            cid = path.split("/cards/")[1].split("/")[0]
            item = self.cards[cid]
            if body["sequence"] <= item["sequence"]:
                return HTTPResult(400, {"code": 300317})
            item["sequence"] = body["sequence"]
            if path.endswith("/content"):
                next(e for e in item["card"]["body"]["elements"] if e.get("element_id") == "answer")["content"] = body["content"]
            elif path.endswith("/settings"):
                item["card"]["config"].update(json.loads(body["settings"])["config"])
            else:
                item["card"] = json.loads(body["card"]["data"])
            data = {}
        else:
            if "card_msg_content_type=user_card_content" not in path:
                raise AssertionError("exact card projection must be requested")
            mid = path.split("?", 1)[0].rsplit("/", 1)[-1]
            item = deepcopy(self.messages[mid])
            if item["msg_type"] == "interactive":
                cid = json.loads(item["body"]["content"])["data"]["card_id"]
                if not self.flatten:
                    item["body"]["content"] = json.dumps(self.cards[cid]["card"])
            data = {"items": [item]}
        if fault == "lost_ack":
            raise TimeoutError("synthetic committed response lost")
        return HTTPResult(200, {"code": 0, "data": deepcopy(data)})


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "delivery.db")
        self.route = Route("tenant", "bot", "profile", "chat", "thread", "anchor")
        self.wire = DocumentedAPI(self.route)
        self.now = [100.0]
        self.transport = FeishuTransport(self.store, self.wire, tenant="tenant", bot="bot", profile="profile",
                                         clock=lambda: self.now[0], send_interval=0)
        self.engine = Engine(self.store, self.transport, clock=lambda: self.now[0])
        self.identity = Identity(self.route, "inbound-one")
        self.engine.accept(Event(self.identity, "begin", 0, "begin"))
        self.block = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.block.start()

    async def asyncTearDown(self):
        self.block.stop()
        self.store.close()
        self.tmp.cleanup()

    def event(self, kind, text="", seq=1):
        return self.engine.accept(Event(self.identity, str(seq), seq, kind, text))

    async def sync(self):
        return await self.engine.sync(self.identity.key)

    async def test_documented_create_body_update_close_and_exact_read(self):
        self.event("snapshot", "开头")
        await self.sync()
        self.event("snapshot", "开头和后续", 2)
        await self.sync()
        self.event("final", "开头和后续\n末尾 END", 3)
        p = await self.sync()
        self.assertEqual((p.delivery, p.seal), ("confirmed", "closed"))
        self.assertEqual(len(self.wire.messages), 1)
        body_writes = [b for m, path, b in self.wire.requests if path.endswith("/elements/answer/content")]
        self.assertEqual(body_writes[0]["content"], "开头和后续")
        seqs = [b["sequence"] for _, _, b in self.wire.requests if b and "sequence" in b]
        self.assertEqual(seqs, sorted(set(seqs)))
        sends = [b for m, path, b in self.wire.requests if path.endswith("/reply")]
        self.assertTrue(sends[0]["reply_in_thread"])
        self.assertLessEqual(len(sends[0]["uuid"]), 50)

    async def test_entity_persisted_before_message_and_rate_retry(self):
        self.event("snapshot", "预览")
        self.wire.fail("send", "rate")
        await self.sync()
        self.event("final", "不同的完整终稿", 2)
        self.now[0] += 3
        await self.sync()
        p = await self.sync()
        self.assertEqual(p.delivery, "confirmed")
        self.assertEqual(len(self.wire.cards), 1)
        self.assertEqual(len(self.wire.messages), 1)
        self.assertEqual(body_text(next(iter(self.wire.cards.values()))["card"]), "不同的完整终稿")

    async def test_lost_message_ack_replays_same_uuid_after_restart(self):
        self.event("final", "终稿")
        self.wire.fail("send", "lost_ack")
        self.assertEqual((await self.sync()).delivery, "unknown")
        self.engine = Engine(self.store, self.transport, clock=lambda: self.now[0])
        self.assertEqual((await self.sync()).delivery, "confirmed")
        sends = [b for _, path, b in self.wire.requests if path.endswith("/reply")]
        self.assertEqual(sends[0], sends[1])
        self.assertEqual(len(self.wire.messages), 1)

    async def test_expired_message_uuid_does_not_blindly_retry(self):
        self.event("final", "终稿")
        self.wire.fail("send", "lost_ack")
        await self.sync()
        count = len(self.wire.requests)
        self.now[0] += 3601
        await self.sync()
        self.assertEqual(len(self.wire.requests), count)
        self.assertEqual(len(self.wire.messages), 1)

    async def test_rejected_replay_does_not_disprove_the_first_ambiguous_send(self):
        self.event("final", "终稿")
        self.wire.fail("send", "lost_ack")
        self.assertEqual((await self.sync()).delivery, "unknown")
        request = self.wire.request
        async def already_bound(method, path, body=None):
            if path.endswith("/reply"):
                return HTTPResult(400, {"code": 230099})
            return await request(method, path, body)
        self.wire.request = already_bound
        phase = await self.sync()
        self.assertEqual((phase.mode, phase.delivery), ("card", "unknown"))
        self.assertEqual(self.store.operation(self.identity.key + ":card:message")["state"], "receipt_required")
        self.assertEqual(len(self.wire.messages), 1)
        await self.sync()
        self.assertEqual(len(self.wire.messages), 1)

    async def test_entity_ack_unknown_never_guesses_card_id(self):
        self.event("final", "终稿")
        self.wire.fail("entity", "lost_ack")
        await self.sync()
        await self.sync()
        self.assertEqual(len(self.wire.cards), 1)
        self.assertEqual(len(self.wire.messages), 0)

    async def test_card_reference_is_not_full_body_evidence(self):
        self.event("final", "本地希望看到的正文")
        self.wire.flatten = True
        p = await self.sync()
        self.assertEqual(p.delivery, "unknown")
        self.assertFalse(self.engine.confirmed(self.identity, p.body))
        self.assertEqual(len(self.wire.messages), 1)

    async def _run_projection_fixture(self, *, corrupt=False, fixture_name="projection", transform=None):
        fixture = json.loads((Path(__file__).parent / f"fixtures/feishu_markdown_{fixture_name}.json").read_text())
        expected, observed = fixture["expected"], fixture["observed"]
        if corrupt:
            observed = observed.replace("stream verified", "stream corrupted")
        if transform:
            observed = transform(observed)
        self.assertNotEqual(expected, observed)
        request = self.wire.request

        async def projected(method, path, body=None):
            response = await request(method, path, body)
            if method == "GET":
                for item in response.data.get("data", {}).get("items", []):
                    if item["msg_type"] != "interactive":
                        continue
                    card = json.loads(item["body"]["content"])
                    for element in card.get("body", {}).get("elements", []):
                        if element.get("element_id") == "answer" and element.get("content") == expected:
                            element["content"] = observed
                    item["body"]["content"] = json.dumps(card)
            return response

        self.wire.request = projected
        self.event("final", expected)
        for _ in range(5):
            phase = await self.sync()
        return phase, expected, observed

    async def test_recorded_markdown_projection_keeps_code_and_table_in_one_card(self):
        phase, expected, observed = await self._run_projection_fixture()
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("card", "confirmed", "closed"))
        self.assertEqual(phase.diagnostic, "markdown_projection_verified")
        self.assertEqual(len(self.wire.messages), 1)
        self.assertTrue(self.engine.confirmed(self.identity, expected))
        self.assertFalse(self.engine.confirmed(self.identity, observed))

    async def test_changed_code_in_projection_still_uses_lossless_fallback(self):
        phase, expected, observed = await self._run_projection_fixture(corrupt=True)
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("text", "confirmed", "closed"))
        self.assertEqual(phase.update_attempts, 3)
        self.assertTrue(phase.preview_retired)
        texts = [json.loads(m["body"]["content"])["text"] for m in self.wire.messages.values() if m["msg_type"] == "text"]
        self.assertEqual(texts, [expected])
        self.assertTrue(self.engine.confirmed(self.identity, expected))
        self.assertFalse(self.engine.confirmed(self.identity, observed))
        self.assertNotIn(expected, [body_text(c["card"]) for c in self.wire.cards.values()])
        count = len(self.wire.messages)
        self.engine = Engine(self.store, self.transport, clock=lambda: self.now[0])
        for _ in range(4):
            await self.sync()
        self.assertEqual(len(self.wire.messages), count)

    async def test_real_long_heading_rule_table_and_multiline_code_stay_in_card(self):
        phase, expected, _ = await self._run_projection_fixture(fixture_name="long")
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("card", "confirmed", "closed"))
        self.assertEqual(len(self.wire.messages), 1)
        self.assertTrue(self.engine.confirmed(self.identity, expected))

    async def test_real_code_blank_lines_and_indentation_are_verified(self):
        phase, _, _ = await self._run_projection_fixture(fixture_name="blocks")
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("card", "confirmed", "closed"))

    async def test_real_answer_starting_with_nested_headings_stays_in_card(self):
        phase, expected, _ = await self._run_projection_fixture(fixture_name="headings")
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("card", "confirmed", "closed"))
        self.assertEqual(len(self.wire.messages), 1)
        self.assertTrue(self.engine.confirmed(self.identity, expected))

    async def test_real_normal_brief_block_boundaries_stay_in_one_card(self):
        phase, expected, _ = await self._run_projection_fixture(fixture_name="normal")
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("card", "confirmed", "closed"))
        self.assertEqual(len(self.wire.messages), 1)
        self.assertTrue(self.engine.confirmed(self.identity, expected))

    async def test_second_normal_task_tight_table_and_list_heading_boundaries(self):
        phase, expected, _ = await self._run_projection_fixture(fixture_name="normal_second")
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("card", "confirmed", "closed"))
        self.assertEqual(len(self.wire.messages), 1)
        self.assertTrue(self.engine.confirmed(self.identity, expected))

    async def test_normal_followup_trailing_source_list_newline_keeps_card(self):
        phase, expected, _ = await self._run_projection_fixture(fixture_name="normal_followup")
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("card", "confirmed", "closed"))
        self.assertEqual(len(self.wire.messages), 1)
        self.assertTrue(self.engine.confirmed(self.identity, expected))

    async def test_paragraph_before_table_keeps_card_and_real_tools(self):
        self.engine.phases[self.identity.key].tools = {"call-1": {"label":"terminal", "state":"done", "iteration":1}}
        phase, expected, _ = await self._run_projection_fixture(fixture_name="activity")
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("card", "confirmed", "closed"))
        self.assertEqual(len(self.wire.messages), 1)
        self.assertTrue(self.engine.confirmed(self.identity, expected))

    async def test_changed_table_value_still_falls_back_and_retains_tools(self):
        self.engine.phases[self.identity.key].tools = {"call-1": {"label":"terminal", "state":"done", "iteration":1}}
        phase, _, _ = await self._run_projection_fixture(fixture_name="activity", transform=lambda s:s.replace("3184", "9999"))
        self.assertEqual(phase.mode, "text")
        card = self.wire.cards[phase.card_id]
        self.assertIn("terminal", json.dumps(card, ensure_ascii=False))

    async def test_real_list_intro_blank_line_projection_keeps_detail_card(self):
        phase, expected, _ = await self._run_projection_fixture(fixture_name="workshop")
        self.assertEqual((phase.mode, phase.delivery, phase.seal), ("card", "confirmed", "closed"))
        self.assertEqual(len(self.wire.messages), 1)
        self.assertTrue(self.engine.confirmed(self.identity, expected))

    async def test_workshop_list_fact_damage_is_not_format_normalization(self):
        phase, _, _ = await self._run_projection_fixture(fixture_name="workshop", transform=lambda s:s.replace('13 人', '99 人'))
        self.assertEqual(phase.mode, "text")

    async def test_normal_brief_changed_fact_cannot_confirm_card(self):
        phase, _, _ = await self._run_projection_fixture(
            fixture_name="normal", transform=lambda s: s.replace("Pro+", "Free"))
        self.assertEqual(phase.mode, "text")

    async def test_normal_brief_changed_link_cannot_confirm_card(self):
        phase, _, _ = await self._run_projection_fixture(
            fixture_name="normal", transform=lambda s: s.replace("https://github.com/DIYgod/RSSHub", "https://example.com"))
        self.assertEqual(phase.mode, "text")

    async def test_code_indentation_loss_is_not_a_format_projection(self):
        phase, expected, _ = await self._run_projection_fixture(
            fixture_name="blocks", transform=lambda s: s.replace("  y = 2", "y = 2"))
        self.assertEqual(phase.mode, "text")
        self.assertTrue(self.engine.confirmed(self.identity, expected))

    async def test_heading_content_loss_is_not_a_format_projection(self):
        phase, expected, _ = await self._run_projection_fixture(
            fixture_name="long", transform=lambda s: s.replace("### 八、归途", "### 八"))
        self.assertEqual(phase.mode, "text")
        self.assertTrue(self.engine.confirmed(self.identity, expected))

    async def test_unreadable_projection_never_exhausts_into_duplicate_fallback(self):
        self.event("final", "正文尚未可见")
        self.wire.flatten = True
        for _ in range(8):
            phase = await self.sync()
        self.assertEqual((phase.mode, phase.delivery), ("card", "unknown"))
        self.assertEqual(phase.update_attempts, 0)
        self.assertEqual(len(self.wire.messages), 1)

    async def test_lost_final_update_ack_reconciles_actual_card(self):
        self.event("snapshot", "预览")
        await self.sync()
        self.event("final", "完整结论", 2)
        self.wire.fail("write", "lost_ack")
        await self.sync()
        self.assertEqual((await self.sync()).delivery, "confirmed")
        self.assertEqual(len(self.wire.messages), 1)

    async def test_permission_and_moderation_refusal_never_use_text_bypass(self):
        for fault in ["permission", "moderation"]:
            with self.subTest(fault=fault):
                self.wire.fail("send", fault)
                phase = Phase(self.identity); phase.body = "测试"
                with self.assertRaises(Unavailable):
                    await self.transport.create(self.route, render(phase), fault)
        self.assertEqual(len(self.wire.messages), 0)

    async def test_cross_profile_request_rejected_before_wire(self):
        with self.assertRaises(Conflict):
            await self.transport.send_text(replace(self.route, profile="other"), "正文", "key")
        self.assertEqual(self.wire.requests, [])

    async def test_thread_without_anchor_cannot_fall_back_to_chat_root(self):
        with self.assertRaises(Conflict):
            await self.transport.send_text(replace(self.route, anchor=""), "正文", "key")
        self.assertEqual(self.wire.requests, [])

    async def test_cross_chat_readback_cannot_confirm(self):
        mid = await self.transport.send_text(self.route, "正文", "key")
        self.wire.messages[mid]["chat_id"] = "another"
        with self.assertRaises(Conflict):
            await self.transport.read_text(self.route, mid)

    async def test_cross_sender_readback_cannot_confirm(self):
        mid = await self.transport.send_text(self.route, "正文", "key")
        self.wire.messages[mid]["sender"]["id"] = "another"
        with self.assertRaises(Conflict):
            await self.transport.read_text(self.route, mid)

    async def test_unknown_envelope_is_not_treated_as_success(self):
        self.wire.fail("send", "unrecognized")
        with self.assertRaises(UnknownOutcome):
            await self.transport.send_text(self.route, "正文", "key")

    async def test_transport_error_redacts_sdk_exception(self):
        self.wire.fail("send", "network")
        with self.assertRaises(UnknownOutcome) as error:
            await self.transport.send_text(self.route, "正文", "key")
        self.assertNotIn("DO_NOT_LEAK", str(error.exception))

    async def test_request_key_reuse_with_other_text_is_refused(self):
        await self.transport.send_text(self.route, "正文一", "key")
        with self.assertRaises(Conflict):
            await self.transport.send_text(self.route, "正文二", "key")
        self.assertEqual(len(self.wire.messages), 1)

    async def test_lossless_long_text_recovers_ack_and_keeps_all_tail(self):
        text = "长文与emoji👩‍💻\n" * 4000 + " END "
        self.event("final", text)
        self.wire.fail("send", "lost_ack")
        await self.sync()
        p = await self.sync()
        self.assertEqual(p.delivery, "confirmed")
        observed = "".join(json.loads(m["body"]["content"])["text"] for m in self.wire.messages.values())
        self.assertEqual(observed, text)

    async def test_empty_get_items_is_unknown_not_empty_answer(self):
        class Empty:
            async def request(self, *_): return HTTPResult(200, {"code": 0, "data": {"items": []}})
        self.transport.wire = Empty()
        with self.assertRaises(UnknownOutcome): await self.transport.read(self.route, Ref("c", "m"))

    async def test_malformed_read_shapes_hold_without_crashing_recovery(self):
        self.event("final", "终稿")
        await self.sync()
        for card in ({"schema": "2.0", "body": None}, {"schema": "2.0", "config": {}, "body": {"elements": [None]}},
                     {"schema": "2.0", "config": {}, "body": {"elements": [{"tag": "markdown", "element_id": "answer", "content": None}]}}):
            self.wire.cards[self.engine.phases[self.identity.key].card_id]["card"] = card
            with self.assertRaises(UnknownOutcome):
                await self.transport.read(self.route, Ref(self.engine.phases[self.identity.key].card_id, "om_1"))
