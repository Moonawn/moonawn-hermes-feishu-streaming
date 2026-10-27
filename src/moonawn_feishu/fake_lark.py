"""Deterministic, network-free transport with failures before/after commit."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict, deque
from copy import deepcopy
import json

from .model import Route, digest
from .render import body_text, fingerprint
from .transport import Observation, RateLimited, Ref, Rejected, Unavailable, UnknownOutcome


class FakeLark:
    def __init__(self):
        self.cards, self.messages, self.requests = {}, {}, {}
        self.faults = defaultdict(deque)
        self.counts = Counter()
        self.history = []
        self.update_barrier = None

    def fail_next(self, operation: str, fault: str):
        self.faults[operation].append(fault)

    def _attempt(self, operation: str) -> str:
        self.counts[operation] += 1
        fault = self.faults[operation].popleft() if self.faults[operation] else ""
        if fault == "rate_limit":
            raise RateLimited(2)
        if fault == "reject":
            raise Rejected("synthetic definitive rejection")
        if fault == "offline":
            raise UnknownOutcome("synthetic network unavailable")
        if fault == "withdrawn":
            raise Unavailable("synthetic message withdrawal")
        return fault

    def _card(self, route: Route, ref: Ref) -> dict:
        c = self.cards.get(ref.card_id)
        if not c or c["route"] != route or c["message_id"] != ref.message_id:
            raise Unavailable("exact card identity unavailable")
        return c

    async def create(self, route: Route, card: dict, request_key: str) -> Ref:
        fault = self._attempt("create")
        if request_key in self.requests:
            return self.requests[request_key]
        n = digest(request_key)[:12]
        ref = Ref("fake_card_" + n, "fake_msg_" + n)
        self.cards[ref.card_id] = {"route": route, "message_id": ref.message_id,
                                   "card": deepcopy(card), "sequence": 0, "closed": False}
        self.requests[request_key] = ref
        self.history.append(("create", ref.card_id, body_text(card)))
        if fault == "lost_ack":
            raise UnknownOutcome("synthetic create committed, receipt lost")
        return ref

    async def update(self, route: Route, ref: Ref, card: dict, sequence: int):
        fault = self._attempt("update")
        if self.update_barrier:
            entered, release = self.update_barrier
            self.update_barrier = None
            entered.set()
            await release.wait()
        c = self._card(route, ref)
        if sequence <= c["sequence"]:
            raise Rejected("sequence must increase")
        c["card"], c["sequence"] = deepcopy(card), sequence
        self.history.append(("update", ref.card_id, body_text(card)))
        if fault == "lost_ack":
            raise UnknownOutcome("synthetic update committed, receipt lost")

    async def close(self, route: Route, ref: Ref, sequence: int):
        fault = self._attempt("close")
        c = self._card(route, ref)
        if sequence <= c["sequence"]:
            raise Rejected("sequence must increase")
        c["closed"], c["sequence"] = True, sequence
        self.history.append(("close", ref.card_id, ""))
        if fault == "lost_ack":
            raise UnknownOutcome("synthetic close committed, receipt lost")

    async def update_body(self, route: Route, ref: Ref, text: str, sequence: int):
        fault = self._attempt("update_body")
        if self.update_barrier:
            entered, release = self.update_barrier
            self.update_barrier = None
            entered.set()
            await release.wait()
        c = self._card(route, ref)
        if sequence <= c["sequence"]:
            raise Rejected("sequence must increase")
        element = next((e for e in c["card"]["body"]["elements"] if e.get("element_id") == "answer"), None)
        if element is None:
            raise Rejected("answer element has not been created")
        element["content"], c["sequence"] = text, sequence
        self.history.append(("update_body", ref.card_id, text))
        if fault == "lost_ack":
            raise UnknownOutcome("synthetic body update committed, receipt lost")

    async def read(self, route: Route, ref: Ref) -> Observation:
        fault = self._attempt("read")
        c = self._card(route, ref)
        returned_route = route if fault != "wrong_identity" else Route("other", "other", "other", "other")
        text = body_text(c["card"]) if fault != "wrong_body" else "mismatch"
        return Observation(returned_route, text, c["closed"], fingerprint(c["card"]))

    async def send_text(self, route: Route, text: str, request_key: str) -> str:
        fault = self._attempt("send_text")
        message_id = "fake_text_" + digest(request_key)[:12]
        self.messages.setdefault(message_id, {"route": route, "text": text})
        self.history.append(("text", message_id, text))
        if fault == "lost_ack":
            raise UnknownOutcome("synthetic text committed, receipt lost")
        return message_id

    async def read_text(self, route: Route, message_id: str) -> Observation:
        fault = self._attempt("read_text")
        m = self.messages.get(message_id)
        if not m or m["route"] != route:
            raise Unavailable("exact text identity unavailable")
        return Observation(route, m["text"] if fault != "wrong_body" else "mismatch", True)
