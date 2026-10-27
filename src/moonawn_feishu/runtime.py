"""Host-independent lifecycle, bounded token mailbox and recovery service."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace
import threading
import time

from .engine import Coalescer, Engine
from .model import Conflict, Event, Identity, Route, digest


class CapacityExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Scope:
    route: Route
    session_key: str
    inbound_message_id: str

    def __post_init__(self):
        if not self.session_key or not self.inbound_message_id:
            raise ValueError("raw inbound ID and session key are required")

    @property
    def turn(self):
        return digest(self.session_key + "\0" + self.inbound_message_id)


class Mailbox:
    """Cumulative deltas are coalesced, never discarded from a bounded queue.

    Finish/control signals have reserved slots. Overflow stops previews only;
    the authoritative final still enters through the host's completion path.
    """
    def __init__(self, max_chars=500_000, max_tools=128):
        self.lock = threading.Lock()
        self.text = ""
        self.dirty = False
        self.done = False
        self.final_candidate = None
        self.overflow = False
        self.tools = OrderedDict()
        self.max_chars, self.max_tools = max_chars, max_tools
        self.deltas = self.coalesced = 0

    def delta(self, text):
        with self.lock:
            if self.done:
                return
            if text is None:
                self.text = ""
                return
            self.deltas += 1
            self.coalesced += int(self.dirty)
            if self.overflow or len(self.text) + len(text) > self.max_chars:
                self.overflow = True
                return
            self.text += text
            self.dirty = True

    def commentary(self, text):
        with self.lock:
            if not self.done:
                if len(text) <= self.max_chars:
                    self.text, self.dirty = text, True
                else:
                    self.overflow = True

    def tool(self, tool_id, label, state, data=None):
        with self.lock:
            # finish() closes text observation, not the authoritative turn.
            # Tool completion can arrive before queued final delivery drains
            # this bounded mailbox; the Phase rejects events after commit.
            previous = self.tools.get(tool_id, (None, None, {}))[2]
            self.tools[tool_id] = (label, state, {**previous, **(data or {})})
            if len(self.tools) > self.max_tools:
                self.tools.popitem(last=False)

    def finish(self, final_text=None):
        with self.lock:
            if not self.done:
                self.done, self.final_candidate = True, final_text

    def drain(self):
        with self.lock:
            snapshot = self.text if self.dirty else None
            tools = list(self.tools.items())
            self.dirty = False
            self.tools.clear()
            return snapshot, tools, self.done


class ReplySession:
    def __init__(self, runtime, identity):
        self.runtime, self.identity = runtime, identity
        self.queue = Coalescer(lambda: runtime.engine.sync(identity.key), interval=runtime.interval)

    @property
    def phase(self):
        return self.runtime.engine.phases[self.identity.key]

    def emit(self, kind, text="", **kwargs):
        seq = self.phase.seq + 1
        phase = self.runtime.engine.accept(Event(self.identity, f"{kind}:{seq}", seq, kind, text, **kwargs))
        self.queue.schedule(terminal=bool(phase.terminal))
        return phase

    async def commit(self, text, *, failed=False):
        if self.phase.terminal:
            if digest(text) != self.phase.content_hash:
                raise Conflict("frozen reply differs; an explicit continuation is required")
            await self.runtime.engine.sync(self.identity.key)
        else:
            self.emit("error" if failed else "final", text)
            await self.queue.drain()
        return self.runtime.engine.terminal_confirmed(self.identity, text)

    async def cancel(self):
        if not self.phase.terminal:
            self.emit("cancel")
            if not self.phase.body and not self.phase.create_attempted:
                self.phase.delivery = "withdrawn"
                self.phase.diagnostic = "cancelled_before_visible_content"
                self.runtime.engine.store.save(self.phase)
        await self.queue.drain()


class Runtime:
    def __init__(self, engine: Engine, *, interval=.25, max_active=256, recovery_interval=1.0):
        self.engine, self.interval, self.max_active = engine, interval, max_active
        self.recovery_interval = recovery_interval
        self.sessions = {}
        self._worker = None
        self._stop = asyncio.Event()
        self._attempts, self._next_attempt = {}, {}
        self.recovered = 0
        self._begin_lock = threading.RLock()

    def begin(self, scope: Scope, *, continuation=False):
        with self._begin_lock:
            return self._begin(scope, continuation=continuation)

    def _begin(self, scope: Scope, *, continuation=False):
        candidates = [p for p in self.engine.phases.values() if p.identity.turn == scope.turn
                      and (p.identity.route.tenant, p.identity.route.bot, p.identity.route.profile, p.identity.route.chat)
                      == (scope.route.tenant, scope.route.bot, scope.route.profile, scope.route.chat)]
        if candidates and any(p.identity.route != scope.route for p in candidates):
            raise Conflict("canonical route changed within the same inbound turn")
        if candidates:
            latest = max(candidates, key=lambda p: p.identity.phase)
            identity = latest.identity
            if continuation:
                if latest.terminal != "final":
                    raise Conflict("continuation requires a completed main reply")
                identity = replace(identity, phase=identity.phase + 1)
        else:
            if continuation:
                raise Conflict("continuation has no original turn")
            identity = Identity(scope.route, scope.turn)
        if identity.key not in self.engine.phases:
            active = sum(not p.terminal for p in self.engine.phases.values())
            if active >= self.max_active:
                raise CapacityExceeded("stream capacity reached; keep native delivery")
            self.engine.accept(Event(identity, "begin:0", 0, "begin"))
        session = self.sessions.get(identity.key)
        if session is None:
            session = self.sessions[identity.key] = ReplySession(self, identity)
        return session

    def start(self):
        if self._worker is None:
            for phase in self.engine.phases.values():
                if not phase.terminal:
                    if phase.metrics:
                        phase.metrics["eta"] = {"state": "finished"}
                    phase.apply(Event(phase.identity, "restart-stop", phase.seq + 1, "cancel"))
                    phase.diagnostic = "previous_process_interrupted"
                    if not phase.create_attempted:
                        phase.delivery = "withdrawn"
                    self.engine.store.save(phase)
            self._stop.clear()
            self._worker = asyncio.create_task(self._recover_loop())

    async def recover_once(self):
        now = self.engine.clock()
        for key, phase in list(self.engine.phases.items()):
            if (not phase.terminal or phase.archived or phase.delivery in {"attention", "withdrawn"}
                    or phase.delivery == "confirmed" and phase.seal == "closed"
                    or now < max(phase.retry_after, self._next_attempt.get(key, 0))):
                continue
            # All phases still use the same per-phase writer lock.
            try:
                await self.engine.sync(key)
            except Conflict:
                phase.delivery, phase.diagnostic = "attention", "identity_conflict"
                self.engine.store.save(phase)
            attempts = self._attempts.get(key, 0) + 1
            self._attempts[key] = attempts
            self._next_attempt[key] = now + min(30, 2 ** min(attempts, 5))
            if phase.delivery == "confirmed" and phase.seal == "closed":
                self.recovered += 1
                self._attempts.pop(key, None)
                self._next_attempt.pop(key, None)
            elif attempts >= 12 and phase.delivery != "confirmed":
                phase.delivery, phase.diagnostic = "attention", "recovery_requires_receipt"
                self.engine.store.save(phase)

    async def _recover_loop(self):
        while not self._stop.is_set():
            await self.recover_once()
            try:
                await asyncio.wait_for(self._stop.wait(), self.recovery_interval)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        self._stop.set()
        if self._worker:
            await self._worker
            self._worker = None
        for session in self.sessions.values():
            await session.queue.drain()

    def compact(self, retention_days=7):
        count = self.engine.store.compact(before=self.engine.clock() - retention_days * 86400)
        # Refresh archived content in memory, retain exact hash tombstones.
        for phase in self.engine.store.all():
            if phase.archived:
                self.engine.phases[phase.identity.key] = phase
                self.sessions.pop(phase.identity.key, None)
                self.engine._locks.pop(phase.identity.key, None)
        return count
