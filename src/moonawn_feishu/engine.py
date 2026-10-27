"""One delivery owner per reply phase; ACK uncertainty is a durable state."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

from .model import Conflict, Event, Identity, Phase, Route, digest
from .render import CardBudgetExceeded, chunks, fingerprint, render, structure_fingerprint, feishu_markdown_projection
from .store import Store
from .transport import Observation, RateLimited, Ref, Rejected, Transport, Unavailable, UnknownOutcome


class Engine:
    def __init__(self, store: Store, transport: Transport, *, clock=time.time, ripple_key=""):
        self.store, self.transport, self.clock = store, transport, clock
        self.phases = {p.identity.key: p for p in store.all()}
        self._locks = {}
        self.ripple_key = ripple_key

    def accept(self, event: Event) -> Phase:
        key = event.identity.key
        phase = self.phases.get(key)
        if phase is None:
            if event.kind != "begin":
                raise Conflict("begin with explicit identity before emitting content")
            if event.identity.phase:
                previous = self.phases.get(replace(event.identity, phase=event.identity.phase - 1).key)
                if previous is None or previous.terminal != "final":
                    raise Conflict("continuation requires a frozen previous final")
            phase = Phase(event.identity)
            self.phases[key] = phase
        if phase.apply(event) and (phase.terminal or event.kind == "begin"):
            self.store.save(phase)
        return phase

    def confirmed(self, identity: Identity, exact_text: str) -> bool:
        p = self.phases.get(identity.key)
        return bool(p and p.delivery == "confirmed" and p.terminal == "final" and p.content_hash == digest(exact_text))

    def terminal_confirmed(self, identity: Identity, exact_text: str) -> bool:
        p = self.phases.get(identity.key)
        return bool(p and p.delivery == "confirmed" and p.terminal and p.content_hash == digest(exact_text))

    def _verify(self, p: Phase, observation: Observation, text: str) -> bool:
        if observation.route != p.identity.route:
            p.delivery = "attention"
            p.diagnostic = "readback_identity_mismatch"
            self.store.save(p)
            raise Conflict("read-back belongs to another route")
        if observation.body == text:
            return True
        if observation.projection == "feishu_card" and observation.body == feishu_markdown_projection(text):
            p.diagnostic = "markdown_projection_verified"
            return True
        return False

    async def sync(self, key: str) -> Phase:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            p = self.phases[key]
            if p.archived or p.delivery in {"withdrawn", "attention"} or self.clock() < p.retry_after:
                return p
            if p.identity.phase:
                prev = self.phases[replace(p.identity, phase=p.identity.phase - 1).key]
                if prev.delivery != "confirmed":
                    p.diagnostic = "waiting_for_previous_delivery"
                    return p
            try:
                await self._sync(p)
            except RateLimited as exc:
                p.retry_after = self.clock() + exc.retry_after
                p.diagnostic = "rate_limited"
            except Unavailable:
                p.delivery = "withdrawn"
                p.diagnostic = "message_unavailable_no_replacement"
            except UnknownOutcome:
                if p.delivery != "confirmed":
                    p.delivery = "unknown"
                p.diagnostic = "transport_result_unknown"
            except Rejected:
                p.delivery, p.diagnostic = "attention", "operation_rejected"
            except asyncio.CancelledError:
                p.diagnostic = "interrupted_reconcile_before_retry"
                self.store.save(p)
                raise
            finally:
                self.store.save(p)
            return p

    async def _sync(self, p: Phase):
        if p.mode == "text":
            if p.terminal:
                await self._text(p)
            return
        route = p.identity.route
        ref = Ref(p.card_id, p.message_id)
        # Reconcile a known card BEFORE considering retry/fallback. Content
        # verification and streaming closure are deliberately independent.
        if p.terminal and p.card_id:
            observed = await self.transport.read(route, ref)
            if self._verify(p, observed, p.body):
                p.delivery = "confirmed"
                if observed.closed:
                    p.seal = "closed"
                try:
                    expected_card = render(p, ripple_key=self.ripple_key)
                    expected_hash = fingerprint(expected_card)
                except CardBudgetExceeded:
                    await self._close(p)
                    return
                if observed.render_hash == expected_hash or p.last_render_hash == expected_hash:
                    await self._close(p)
                    return
            else:
                # An ACK or a cached render hash is not proof of the observed
                # body; a known stale card must receive the exact final again.
                p.last_render_hash = ""
        try:
            card = render(p, ripple_key=self.ripple_key)
        except CardBudgetExceeded:
            if not p.terminal:
                p.diagnostic = "large_preview_deferred_until_final"
                return
            # No pending ambiguous write may be bypassed. A known-card read
            # above already proved the complete final is absent.
            if p.create_attempted and not p.card_id:
                p.delivery = "unknown"
                p.diagnostic = "creation_receipt_required"
                return
            p.mode = "text"
            self.store.save(p)
            await self._text(p)
            return
        render_hash = fingerprint(card)
        structure_hash = structure_fingerprint(card)
        text_snapshot = p.body
        if not p.card_id:
            if p.create_attempted:
                recover = getattr(self.transport, "recover_create", None)
                if recover:
                    recovered = await recover(route, f"{p.identity.key}:card")
                    if recovered:
                        p.card_id, p.message_id = recovered.card_id, recovered.message_id
                        self.store.save(p)
                        return await self._sync(p)
                p.delivery = "unknown"
                p.diagnostic = "creation_receipt_required"
                return
            p.create_attempted = True
            self.store.save(p)  # write-ahead before any external operation
            try:
                ref = await self.transport.create(route, card, f"{p.identity.key}:card")
            except RateLimited:
                p.create_attempted = False  # definitive non-commit
                raise
            except Rejected:
                p.mode = "text"
                self.store.save(p)
                if p.terminal:
                    await self._text(p)
                return
            p.card_id, p.message_id = ref.card_id, ref.message_id
            p.last_render_hash = render_hash
            p.last_structure_hash = structure_hash
            self.store.save(p)
        elif render_hash != p.last_render_hash:
            if p.terminal and p.update_attempts >= 3:
                if p.delivery == "confirmed":
                    p.diagnostic = "body_confirmed_final_style_budget_exhausted"
                    await self._close(p)
                else:
                    # A complete, identity-checked read above still differs
                    # after bounded final writes. Feishu can rewrite Markdown
                    # whitespace in user_card_content (notably fences/tables).
                    # Do not weaken exactness or leave a spinner indefinitely:
                    # retire this preview, then verify a single lossless text
                    # delivery through the existing durable fallback outbox.
                    p.mode = "text"
                    p.diagnostic = "final_projection_mismatch_text_fallback"
                    self.store.save(p)
                    await self._text(p)
                return
            p.sequence += 1
            if p.terminal:
                p.update_attempts += 1
            self.store.save(p)
            try:
                if (not p.terminal and p.typing and p.last_structure_hash == structure_hash
                        and hasattr(self.transport, "update_body")):
                    await self.transport.update_body(route, ref, text_snapshot, p.sequence)
                else:
                    await self.transport.update(route, ref, card, p.sequence)
            except RateLimited:
                if p.terminal:
                    p.update_attempts -= 1
                raise
            except Rejected:
                # Explicit rejection after a mismatching read-back permits a
                # single durable fallback owner. No generic exception fallback.
                if p.delivery == "confirmed":
                    p.diagnostic = "body_confirmed_final_style_pending"
                    await self._close(p)
                elif p.terminal:
                    p.mode = "text"
                    self.store.save(p)
                    await self._text(p)
                return
            p.last_render_hash = render_hash
            p.last_structure_hash = structure_hash
        if p.terminal:
            observation = await self.transport.read(route, ref)
            if self._verify(p, observation, p.body):
                p.delivery = "confirmed"
                self.store.save(p)
                await self._close(p)
            else:
                p.delivery, p.diagnostic = "unknown", "final_readback_mismatch"
        elif text_snapshot != p.body:
            # Events arriving during I/O remain in the current Phase; next
            # coalesced flush reads them instead of restoring the old snapshot.
            p.diagnostic = "newer_snapshot_waiting"

    async def _close(self, p: Phase):
        if p.seal == "closed" or not p.card_id:
            return
        p.sequence += 1
        p.seal = "unknown"
        self.store.save(p)
        try:
            await self.transport.close(p.identity.route, Ref(p.card_id, p.message_id), p.sequence)
            p.seal = "closed"
        except (UnknownOutcome, Rejected):
            # Closure trouble must not revoke already verified final content
            # or trigger a second copy of that content.
            p.diagnostic = "body_confirmed_close_pending"

    async def _text(self, p: Phase):
        # Retire a partial preview before the complete lossless text is sent.
        # Never leave an earlier card falsely claiming to be the full answer.
        if p.card_id and not p.preview_retired:
            preview = replace(p, body="完整答复已改为下方文本呈现。", terminal="final")
            p.sequence += 1
            self.store.save(p)
            await self.transport.update(p.identity.route, Ref(p.card_id, p.message_id), render(preview, ripple_key=self.ripple_key), p.sequence)
            p.preview_retired = True
            self.store.save(p)
        if not p.parts:
            p.parts = [{"text": text, "attempted": False, "message_id": "", "verified": False}
                       for text in chunks(p.body)]
            self.store.save(p)
        if not p.parts:
            p.delivery = "attention"
            p.diagnostic = "no_final_text"
            return
        for i, part in enumerate(p.parts):
            if part["verified"]:
                continue
            if not part["message_id"]:
                if part["attempted"]:
                    recover = getattr(self.transport, "recover_text", None)
                    if recover:
                        part["message_id"] = await recover(p.identity.route, f"{p.identity.key}:text:{i}:{p.content_hash}") or ""
                    if part["message_id"]:
                        self.store.save(p)
                    else:
                        p.delivery, p.diagnostic = "unknown", "text_receipt_required"
                        return
                if not part["message_id"]:
                    part["attempted"] = True
                    self.store.save(p)
                    try:
                        part["message_id"] = await self.transport.send_text(
                            p.identity.route, part["text"], f"{p.identity.key}:text:{i}:{p.content_hash}")
                    except RateLimited:
                        part["attempted"] = False
                        raise
                    except Rejected:
                        p.delivery, p.diagnostic = "attention", "text_rejected"
                        return
                    self.store.save(p)
            observation = await self.transport.read_text(p.identity.route, part["message_id"])
            if not self._verify(p, observation, part["text"]):
                p.delivery, p.diagnostic = "attention", "text_readback_mismatch"
                return
            part["verified"] = True
            self.store.save(p)
        p.delivery = "confirmed"
        await self._close(p)
        if not p.card_id:
            p.seal = "closed"

    async def adopt_card_receipt(self, key: str, ref: Ref):
        """Explicit reconciliation, never guessed from another chat's cache."""
        p = self.phases[key]
        observation = await self.transport.read(p.identity.route, ref)
        if not self._verify(p, observation, p.body):
            raise Conflict("candidate receipt does not match exact current content")
        p.card_id, p.message_id = ref.card_id, ref.message_id
        p.delivery = "confirmed" if p.terminal else "pending"
        self.store.save(p)


class Coalescer:
    """One in-flight write + one latest state, terminal wakes the throttle."""
    def __init__(self, write, interval: float = 0.25):
        self.write, self.interval = write, interval
        self.task = None
        self.pending = False
        self.terminal = False
        self.closed = False
        self.wake = asyncio.Event()
        self.scheduled = self.writes = self.coalesced = 0

    def schedule(self, *, terminal: bool = False):
        if self.closed:
            return
        self.scheduled += 1
        if self.pending or (self.task and not self.task.done()):
            self.coalesced += 1
        self.pending = True
        self.terminal |= terminal
        if terminal:
            self.wake.set()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while self.pending:
            if not self.terminal:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
            self.pending = False
            self.wake.clear()
            await self.write()
            self.writes += 1
            if self.terminal and not self.pending:
                self.closed = True

    async def drain(self):
        if self.task:
            # A host cleanup timeout must not cancel an in-flight SDK write.
            # Its ACK belongs to this durable writer, not to the waiting task.
            await asyncio.shield(self.task)
