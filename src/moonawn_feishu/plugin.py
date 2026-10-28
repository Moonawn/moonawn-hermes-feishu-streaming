"""Opt-in Hermes platform plugin. Imports/registration alone never send messages.

The compatibility hooks only act on this plugin's adapter/consumer instances.
Native inbound admission, media, approvals and busy-input policy remain host-owned.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import inspect
import logging
from pathlib import Path
import sys
import time

from .engine import Engine
from .feishu_transport import FeishuTransport, LarkSDKWire
from .model import Conflict, Route, digest
from .runtime import CapacityExceeded, Mailbox, Runtime, Scope
from .store import Store
from .telemetry import DurationHistory, TurnTelemetry
from .tool_display import native_preview, native_outcome, native_action, clean_tool_data

_dispatch = ContextVar("moonawn_feishu_dispatch", default=None)
logger = logging.getLogger(__name__)


@dataclass
class Dispatch:
    adapter: object
    scope: Scope
    session: object = None
    consumers: int = 0
    consumer: object = None
    native_only: bool = False
    source: object = None


def check_signature(function, required):
    params = inspect.signature(function).parameters
    missing = set(required) - set(params)
    if missing:
        raise RuntimeError("unsupported Hermes contract: " + function.__name__ + ": " + ", ".join(sorted(missing)))


def clean_final(adapter, text):
    # Same attachment extraction as the queued native delivery path. File bytes
    # and approval buttons are still delivered by the native adapter.
    from gateway.run import _strip_response_attachments_for_direct_send
    return _strip_response_attachments_for_direct_send(text, adapter)


class ManagedConsumer:
    def __init__(self, adapter, chat_id, config=None, metadata=None, on_new_message=None,
                 on_before_finalize=None, initial_reply_to_id=None, run_still_current=None):
        dispatch = _dispatch.get()
        if dispatch is None or dispatch.adapter is not adapter or dispatch.scope.route.chat != chat_id:
            raise Conflict("stream consumer has no explicit inbound scope")
        self.adapter, self.chat_id, self.cfg, self.metadata = adapter, chat_id, config, metadata
        continuation = bool(dispatch.consumers and dispatch.session and dispatch.session.phase.terminal == "final")
        self.session = adapter._moonawn.begin(dispatch.scope, continuation=continuation)
        dispatch.session = self.session
        dispatch.consumers += 1
        dispatch.consumer = self
        self.scope = dispatch.scope
        self.mailbox = Mailbox()
        self.stream_deltas_enabled = True
        self._turn_split_delivery = False
        self._run_still_current = run_still_current or (lambda: True)
        self._before = on_before_finalize
        self._new_message = on_new_message
        self._notified = False
        self._before_notified = False
        self._loop = None
        self._tool_index = 0
        route = self.scope.route
        owner = str(getattr(dispatch.source, "user_id", ""))
        namespace = "\0".join((route.tenant, route.profile, route.chat, owner))
        self.telemetry = TurnTelemetry(DurationHistory(adapter._moonawn.engine.store, namespace), self.scope.turn)
        self._metric_at = 0.0
        self._callback_restore = []
        self._tool_started = {}
        self.host_failed = False
        diagnostics = getattr(adapter, '_moonawn_diagnostics', None)
        if diagnostics:
            diagnostics.consumers += 1
        controls = getattr(adapter, "_moonawn_controls", None)
        if controls and dispatch.source:
            self.session.phase.typing = controls.typing(dispatch.source)

    def observe_agent(self, agent):
        # A turn owns exactly one native agent. Delegated/repeated wiring must
        # not insert another observer into this turn's card or restore stack.
        if self.telemetry.agent is not None:
            return
        self.telemetry.attach(agent)
        def chain(name, observer):
            original = getattr(agent, name, None)
            def callback(*args, **kwargs):
                # Observe in finally: native approvals/hooks retain their return
                # value and exceptions; telemetry never changes tool arguments.
                try:
                    return original(*args, **kwargs) if original else None
                finally:
                    observer(*args, **kwargs)
            setattr(agent, name, callback)
            self._callback_restore.append((agent, name, original, callback))
        chain("step_callback", self.telemetry.step)
        def started(call_id, tool_name, args):
            self.telemetry.tool(call_id, name=tool_name)
            with self.telemetry.lock:
                if len(self._tool_started) < 4096:
                    self._tool_started.setdefault(call_id, (time.monotonic(), self.telemetry.iteration))
                iteration = self._tool_started.get(call_id, (None, self.telemetry.iteration))[1]
            self.on_tool_event(call_id, tool_name, data={"preview": native_preview(tool_name, args),
                                                       "action": native_action(tool_name), "iteration": iteration})
        def completed(call_id, tool_name, args, result):
            self.telemetry.tool(call_id, done=True, name=tool_name)
            failed, outcome = native_outcome(tool_name, result)
            with self.telemetry.lock:
                start = self._tool_started.pop(call_id, None)
            data = {"preview": native_preview(tool_name, args), "outcome": outcome, "action": native_action(tool_name)}
            if start:
                data.update(elapsed=max(0, time.monotonic() - start[0]), iteration=start[1])
            self.on_tool_event(call_id, tool_name, finished=True, ok=not failed, data=data)
        chain("tool_start_callback", started)
        chain("tool_complete_callback", completed)

    def restore_callbacks(self):
        for agent, name, original, wrapper in self._callback_restore:
            if getattr(agent, name, None) is wrapper:
                setattr(agent, name, original)
        self._callback_restore.clear()

    def update_metrics(self, *, terminal=False, success=False):
        if self.session.phase.terminal:
            return
        phase = self.session.phase
        if not (phase.body or phase.tools or phase.create_attempted or terminal and success):
            return  # a silent run must not publish a statistics-only reply
        now = time.monotonic()
        if terminal or now >= self._metric_at:
            self._metric_at = now + 2
            value = self.telemetry.finish(success=success) if terminal else self.telemetry.snapshot()
            self.session.emit("metrics", data=value)

    @property
    def message_id(self):
        return self.session.phase.message_id or None

    @property
    def already_sent(self):
        return bool(self.message_id)

    @property
    def final_content_delivered(self):
        p = self.session.phase
        return p.terminal == "final" and p.delivery == "confirmed"

    final_response_sent = property(lambda self: self.final_content_delivered)
    accepts_tool_progress = property(lambda self: True)

    def has_delivered_text(self, text):
        return self.session.runtime.engine.confirmed(self.session.identity, clean_final(self.adapter, text))

    has_durably_delivered_text = has_delivered_text
    delivered_final_matches = has_delivered_text

    def on_delta(self, text):
        self.mailbox.delta(text)

    def on_segment_break(self):
        self.mailbox.delta(None)

    def on_commentary(self, text):
        self.mailbox.commentary(text)

    def on_tool_progress(self, line):
        # Progress strings can contain command paths/arguments; never expose
        # those to an external group. Typed tool facts may use on_tool_event.
        if self.telemetry.agent is not None:
            return  # authoritative call IDs come from native callbacks
        self._tool_index += 1
        self.mailbox.tool(f"progress:{self._tool_index}", "执行工具", "running")

    def on_tool_event(self, tool_id, label, *, finished=False, ok=True, data=None):
        if self.session.phase.terminal:
            return
        from .render import safe_tool_label
        self.mailbox.tool(str(tool_id), safe_tool_label(label), "done" if finished and ok else "failed" if finished else "running", clean_tool_data(data or {}))

    def finish(self, final_text=None):
        # This is a stream boundary. Freeze only after the host's final filters
        # and response transformations have reached adapter.send.
        self.mailbox.finish(final_text)

    def request_reopen_seed(self):
        return None

    def flush_pending_sync(self, timeout=5.0):
        if self._loop is None or not self._loop.is_running():
            return False
        try:
            if asyncio.get_running_loop() is self._loop:
                return False  # cannot block the writer's own loop
        except RuntimeError:
            pass
        future = asyncio.run_coroutine_threadsafe(self._flush(), self._loop)
        try:
            future.result(timeout=timeout)
            return True
        except Exception:
            return False

    def _drain_mailbox(self):
        text, tools, done = self.mailbox.drain()
        if text and not self.session.phase.terminal:
            if not any(marker.startswith(text.strip()) for marker in ("NO_REPLY", "[SILENT]")):
                self.session.emit("snapshot", text)
        for tool_id, (label, state, data) in tools:
            self.session.emit("tool", label, tool_id=tool_id, tool_state=state, data=data)
        return done

    async def _flush(self):
        self._drain_mailbox()
        await self.session.queue.drain()

    def close_for_approval_prompt(self, placeholder=None, reason="Approval", reopen=False):
        # Native approval/card callbacks keep their ownership; no fake buttons.
        self.mailbox.tool("approval", "等待你的确认", "running")
        self.telemetry.waiting = True
        future = concurrent.futures.Future()
        future.set_result(True)
        return future

    async def finalize_from_host(self, text, *, failed=False):
        self._drain_mailbox()
        if not self._run_still_current():
            self.update_metrics(terminal=True)
            await self._cancel_observed()
            return False
        value = clean_final(self.adapter, text)
        if not value.strip() or value.strip() in {"NO_REPLY", "[SILENT]", "(empty)"}:
            self.update_metrics(terminal=True)
            await self.session.cancel()
            return False
        if self._before is not None and not self._before_notified:
            self._before_notified = True
            try:
                result = self._before()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass  # typing cleanup is cosmetic, not a delivery obligation
        self.update_metrics(terminal=True, success=not failed)
        started = time.monotonic()
        try:
            return await self.session.commit(value, failed=failed)
        finally:
            diagnostics = getattr(self.adapter, '_moonawn_diagnostics', None)
            if diagnostics:
                diagnostics.record('final_delivery', time.monotonic() - started)

    async def _cancel_observed(self):
        started = time.monotonic()
        try:
            await self.session.cancel()
        finally:
            diagnostics = getattr(self.adapter, '_moonawn_diagnostics', None)
            if diagnostics:
                diagnostics.record('stop_observed', time.monotonic() - started)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        try:
            while True:
                if not self._run_still_current():
                    self.update_metrics(terminal=True)
                    await self._cancel_observed()
                    return
                done = self._drain_mailbox()
                self.update_metrics()
                if self.message_id and self._new_message and not self._notified:
                    self._notified = True
                    try:
                        result = self._new_message()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        pass  # cosmetic host hook, same contract as native sink
                if done:
                    # Hermes gives this observer only five seconds to exit.
                    # Final delivery awaits the independent writer later.
                    return
                await asyncio.sleep(.025)
        except asyncio.CancelledError:
            if not self.mailbox.done or not self._run_still_current():
                self.update_metrics(terminal=True)
                await self._cancel_observed()
            raise


def install_host_hooks(consumer_module, turn_class, notification_class):
    """Validate all boundaries before making the first reversible assignment."""
    original_consumer = consumer_module.GatewayStreamConsumer
    original_mark = turn_class._run_agent_mark_streamed_delivery
    original_queue = notification_class._deliver_queued_first_response
    if getattr(original_consumer, "_moonawn_router", False):
        raise RuntimeError("one Moonawn plugin owner per process; unload before reloading")
    check_signature(original_consumer.__init__, {"adapter", "chat_id", "metadata", "initial_reply_to_id", "run_still_current"})
    if set(inspect.signature(original_consumer.__init__).parameters) - set(inspect.signature(ManagedConsumer.__init__).parameters):
        raise RuntimeError("new Hermes consumer parameters require an explicit compatibility review")
    check_signature(original_mark, {"response", "turn_ctx"})
    original_inner = getattr(turn_class, "_run_agent_inner", None)
    if original_inner is None:
        raise RuntimeError("Hermes turn boundary is unavailable")
    check_signature(original_inner, {"source", "session_key", "inbound_message_id", "event_message_id"})
    check_signature(original_queue, {"response", "source", "adapter", "stream_consumer", "session_key", "inbound_message_id", "deliver_media"})
    queue_signature = inspect.signature(original_queue)
    inner_signature = inspect.signature(original_inner)

    class ConsumerRouter(original_consumer):
        _moonawn_router = True

        def __new__(cls, *args, **kwargs):
            adapter = kwargs.get("adapter", args[0] if args else None)
            dispatch = _dispatch.get()
            if getattr(adapter, "_moonawn", None) is not None and dispatch is not None and dispatch.adapter is adapter and not dispatch.native_only:
                try:
                    return ManagedConsumer(*args, **kwargs)
                except CapacityExceeded:
                    dispatch.native_only = True  # refused before claiming any delivery
            return super().__new__(cls)

    @wraps(original_inner)
    async def inner(*args, **kwargs):
        bound = inner_signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        host, source = next(iter(values.values())), values["source"]
        adapter = host._delivery_adapter_for(source)
        if getattr(adapter, "_moonawn", None) is None:
            return await original_inner(*args, **kwargs)
        outer = _dispatch.get()
        inbound, session_key = values["inbound_message_id"], values["session_key"]
        if not inbound or not session_key:
            # Proactive jobs have no inbound identity. Do not inherit the
            # surrounding conversation's delivery ownership accidentally.
            token = _dispatch.set(None)
            try:
                return await original_inner(*args, **kwargs)
            finally:
                _dispatch.reset(token)
        scope = adapter._moonawn_scope(source, session_key, inbound, values["event_message_id"])
        dispatch = outer if outer and outer.adapter is adapter and outer.scope == scope else Dispatch(adapter, scope)
        dispatch.source = source
        token = _dispatch.set(dispatch)
        try:
            return await original_inner(*args, **kwargs)
        finally:
            if dispatch.consumer:
                dispatch.consumer.restore_callbacks()
            _dispatch.reset(token)
            # Recursive queued turns return their last result to the outer
            # adapter completion send. Carry that exact result's ownership.
            if outer and outer.adapter is adapter and outer is not dispatch:
                outer.scope, outer.session = dispatch.scope, dispatch.session
                outer.consumer, outer.consumers = dispatch.consumer, dispatch.consumers
                outer.native_only = dispatch.native_only

    @wraps(original_mark)
    async def mark(self, response, turn_ctx):
        consumer = turn_ctx.stream_consumer_holder[0]
        if not isinstance(consumer, ManagedConsumer) or not isinstance(response, dict):
            return await original_mark(self, response, turn_ctx)
        # Hermes can append notices/footers after this boundary. Freeze the
        # exact final at adapter.send, after all host transformations instead.
        consumer.host_failed = bool(response.get("failed"))
        consumer.update_metrics(terminal=True, success=not consumer.host_failed)
        response.pop("already_sent", None)

    @wraps(original_queue)
    async def queued(*args, **kwargs):
        bound = queue_signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        adapter = values["adapter"]
        if getattr(adapter, "_moonawn", None) is None:
            return await original_queue(*args, **kwargs)
        inbound, session_key = values["inbound_message_id"], values["session_key"]
        if not inbound or not session_key:
            # Missing identity must not silently reuse a parent turn.
            raise Conflict("queued delivery requires its original inbound identity")
        scope = adapter._moonawn_scope(values["source"], session_key, inbound, values["event_message_id"])
        consumer = values["stream_consumer"]
        owned_consumer = isinstance(consumer, ManagedConsumer) and consumer.scope == scope
        reply = (consumer.session if owned_consumer
                 else adapter._moonawn.begin(scope))
        dispatch = Dispatch(adapter, scope, reply)
        token = _dispatch.set(dispatch)
        try:
            text = clean_final(adapter, values["response"])
            if text:
                # This first response is committed before recursive queue
                # handling reaches the normal mark/send boundary. Drain late
                # tool events and freeze its own counters before final becomes
                # immutable, otherwise the footer retains a live snapshot.
                if owned_consumer:
                    consumer._drain_mailbox()
                    consumer.update_metrics(terminal=True, success=values["deliver_media"])
                values["text_already_delivered"] = await reply.commit(text, failed=not values["deliver_media"])
            # Preserve all present/future parameters and the native media path.
            return await original_queue(*bound.args, **bound.kwargs)
        finally:
            _dispatch.reset(token)

    consumer_module.GatewayStreamConsumer = ConsumerRouter
    turn_class._run_agent_inner = inner
    turn_class._run_agent_mark_streamed_delivery = mark
    notification_class._deliver_queued_first_response = queued
    def undo():
        if consumer_module.GatewayStreamConsumer is ConsumerRouter:
            consumer_module.GatewayStreamConsumer = original_consumer
        if turn_class._run_agent_inner is inner:
            turn_class._run_agent_inner = original_inner
        if turn_class._run_agent_mark_streamed_delivery is mark:
            turn_class._run_agent_mark_streamed_delivery = original_mark
        if notification_class._deliver_queued_first_response is queued:
            notification_class._deliver_queued_first_response = original_queue
    return undo


def adapter_class(native):
    class MoonawnFeishuAdapter(native):
        _moonawn = None
        _moonawn_controls = None
        _moonawn_diagnostics = None

        async def _handle_message_with_guards(self, event):
            # The native per-chat guard can be held by a slow inline command
            # reply in another topic. /stop must reach native admission without
            # waiting for that unrelated I/O. All identity/ACL/session checks
            # still run in handle_message; no direct agent interruption here.
            if getattr(event, 'get_command', lambda: None)() != 'stop':
                return await super()._handle_message_with_guards(event)
            started = time.monotonic()
            logger.info('Streaming stage=stop_dispatch_started')
            try:
                return await self.handle_message(event)
            finally:
                diagnostics = self._moonawn_diagnostics
                elapsed = time.monotonic() - started
                if diagnostics:
                    diagnostics.record('stop_dispatch', elapsed)
                else:
                    logger.info('Streaming stage=stop_dispatch elapsed_ms=%d', elapsed * 1000)

        def _moonawn_scope(self, source, session_key, inbound_id, anchor):
            from hermes_constants import get_hermes_home
            if Path(get_hermes_home()).resolve() != self._moonawn_home:
                raise Conflict("multiplexed profiles require separate delivery runtimes")
            route = Route(self._app_id, self._app_id, self._moonawn_profile, str(source.chat_id),
                          str(getattr(source, "thread_id", None) or ""), str(anchor or ""))
            return Scope(route, str(session_key), str(inbound_id))

        async def _send_uploaded_key(self, *, chat_id, reply_to, metadata, caption,
                                     key_msg_type, key_payload, media_tag):
            # Native media delivery does not pass reply_to. Creating a file
            # with receive_id_type=thread_id is rejected by Feishu (99992402).
            # Reuse only the exact admitted turn's known reply anchor, keeping
            # native upload, file-type selection and error handling intact.
            dispatch = _dispatch.get()
            thread = str((metadata or {}).get("thread_id") or "")
            if (thread and not reply_to and not (metadata or {}).get("reply_to_message_id")
                    and dispatch and dispatch.adapter is self):
                route = dispatch.scope.route
                if route.chat == chat_id and route.thread == thread and route.anchor:
                    reply_to = route.anchor
            return await super()._send_uploaded_key(
                chat_id=chat_id, reply_to=reply_to, metadata=metadata, caption=caption,
                key_msg_type=key_msg_type, key_payload=key_payload, media_tag=media_tag)

        async def connect(self, *, is_reconnect=False):
            result = await super().connect(is_reconnect=is_reconnect)
            if result and self._moonawn is None:
                from hermes_constants import get_hermes_home
                self._moonawn_home = Path(get_hermes_home()).resolve()
                self._moonawn_profile = digest(str(self._moonawn_home))[:24]
                store = None
                try:
                    store = Store(self._moonawn_home / "moonawn-feishu" / "delivery.db")
                    transport = FeishuTransport(store, LarkSDKWire(self._client, self._run_blocking),
                                                tenant=self._app_id, bot=self._app_id, profile=self._moonawn_profile)
                    from .activity_asset import prepare_ripple
                    ripple_key = await prepare_ripple(store, transport.wire, transport.binding)
                    self._moonawn = Runtime(Engine(store, transport, ripple_key=ripple_key))
                    from .controls import ControlService
                    self._moonawn_controls = ControlService(self, store, transport)
                    self._moonawn.compact()
                    self._moonawn.start()
                    from .diagnostics import Diagnostics, runtime_facts
                    self._moonawn_diagnostics = Diagnostics(
                        self._moonawn_home / 'moonawn-feishu' / 'health.json', lambda: runtime_facts(self))
                    self._moonawn_diagnostics.start()
                except Exception:
                    self._moonawn = None
                    if store is not None:
                        store.close()
                    await super().disconnect()
                    raise
            return result

        async def disconnect(self):
            try:
                if self._moonawn is not None:
                    try:
                        if self._moonawn_controls:
                            await self._moonawn_controls.close()
                        await self._moonawn.close()
                    finally:
                        if self._moonawn_diagnostics:
                            await self._moonawn_diagnostics.close()
                            self._moonawn_diagnostics = None
                        self._moonawn.engine.store.close()
                        self._moonawn = None
                        self._moonawn_controls = None
            finally:
                await super().disconnect()

        def format_tool_event(self, event, *, mode="all", preview_max_len=40):
            dispatch = _dispatch.get()
            if dispatch and dispatch.adapter is self and dispatch.consumer:
                if dispatch.consumer.telemetry.agent is not None:
                    return None
                name = type(event).__name__
                if name in {"ToolCallChunk", "ToolCallFinished"}:
                    dispatch.consumer.on_tool_event(getattr(event, "index", 0), getattr(event, "tool_name", "执行工具"),
                                                    finished=name == "ToolCallFinished", ok=getattr(event, "ok", True))
                    return None
            return super().format_tool_event(event, mode=mode, preview_max_len=preview_max_len)

        async def _process_message_background(self, event, session_key):
            if self._moonawn is None or not getattr(event, "message_id", None):
                return await super()._process_message_background(event, session_key)
            from gateway.platforms.base import _reply_anchor_for_event
            scope = self._moonawn_scope(event.source, session_key, event.message_id, _reply_anchor_for_event(event))
            # Slash commands and their approval notices remain native UI. Only
            # model turns need the independent streaming delivery lifecycle.
            command = getattr(event, "get_command", lambda: None)()
            token = _dispatch.set(Dispatch(self, scope, source=event.source, native_only=bool(command)))
            try:
                return await super()._process_message_background(event, session_key)
            finally:
                _dispatch.reset(token)

        async def _handle_card_action_event(self, data):
            value = getattr(getattr(getattr(data, "event", None), "action", None), "value", None)
            if isinstance(value, dict) and value.get("moonawn_action"):
                if self._moonawn_controls:
                    await self._moonawn_controls.callback(data)
                return
            return await super()._handle_card_action_event(data)

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            dispatch = _dispatch.get()
            if (self._moonawn is None or dispatch is None or dispatch.adapter is not self or dispatch.native_only
                    or dispatch.scope.route.chat != chat_id or not (metadata or {}).get("notify")):
                return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
            from gateway.platforms.base import SendResult
            try:
                reply = dispatch.session or self._moonawn.begin(dispatch.scope)
            except CapacityExceeded:
                dispatch.native_only = True
                return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
            dispatch.session = reply
            try:
                consumer = dispatch.consumer
                if consumer is not None:
                    confirmed = await consumer.finalize_from_host(content, failed=consumer.host_failed)
                else:
                    confirmed = await reply.commit(content)
            except Conflict:
                return SendResult(success=False, error="Reply identity/final mismatch; explicit continuation required",
                                  raw_response={"moonawn_delivery_owner": reply.identity.key})
            return SendResult(success=confirmed, message_id=reply.phase.message_id or None,
                              error=None if confirmed else "Delivery retained by Moonawn; receipt reconciliation pending",
                              raw_response={"moonawn_delivery_owner": reply.identity.key})

        def _send_retry_is_final(self, result):
            raw = getattr(result, "raw_response", None)
            if isinstance(raw, dict) and raw.get("moonawn_delivery_owner"):
                return True  # the durable outbox owns retry and exact text fallback
            return super()._send_retry_is_final(result)

        async def _record_delivery_obligation(self, event, session_key, text_content,
                                               delivery_adapter, is_ephemeral_response):
            dispatch = _dispatch.get()
            inbound = getattr(event, "ledger_message_id", None)
            if inbound is None:
                inbound = getattr(event, "message_id", "")
            if (dispatch and dispatch.adapter is self and not dispatch.native_only
                    and delivery_adapter is self and self._moonawn is not None
                    and dispatch.scope.session_key == session_key
                    and dispatch.scope.inbound_message_id == str(inbound)
                    and dispatch.scope.route.chat == str(event.source.chat_id)):
                # Keep the native crash marker until send() persists the exact
                # final. A second native ledger would replay outside our scope
                # and duplicate a card whose ACK was merely delayed/lost.
                return None
            return await super()._record_delivery_obligation(
                event, session_key, text_content, delivery_adapter, is_ephemeral_response)

        async def edit_message(self, chat_id, message_id, content, *, finalize=False):
            if self._moonawn is not None:
                for reply in self._moonawn.sessions.values():
                    if reply.phase.message_id == message_id and reply.identity.route.chat == chat_id:
                        from gateway.platforms.base import SendResult
                        dispatch = _dispatch.get()
                        if dispatch is None or dispatch.adapter is not self or reply.identity.turn != dispatch.scope.turn:
                            return SendResult(success=False, error="Cross-turn card edit refused")
                        if reply.phase.terminal:
                            confirmed = self._moonawn.engine.terminal_confirmed(reply.identity, content)
                            return SendResult(success=confirmed, message_id=message_id,
                                              error=None if confirmed else "Frozen final cannot be replaced")
                        reply.emit("snapshot", content)
                        await reply.queue.drain()
                        return SendResult(success=True, message_id=message_id)
            return await super().edit_message(chat_id, message_id, content, finalize=finalize)
    return MoonawnFeishuAdapter


def register(ctx):
    if any("hermes_lark_streaming" in name or "hermes_feishu_streaming_card" in name for name in sys.modules):
        raise RuntimeError("another Feishu streaming plugin is loaded; only one delivery owner is supported")
    from plugins.platforms.feishu import adapter as feishu
    from gateway import stream_consumer
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run_notifications import GatewayNotificationsMixin
    check_signature(feishu.FeishuAdapter._process_message_background, {"event", "session_key"})
    check_signature(feishu.FeishuAdapter._handle_message_with_guards, {"event"})
    check_signature(feishu.FeishuAdapter.handle_message, {"event"})
    check_signature(feishu.FeishuAdapter._record_delivery_obligation,
                    {"event", "session_key", "text_content", "delivery_adapter", "is_ephemeral_response"})
    check_signature(feishu.FeishuAdapter._send_retry_is_final, {"result"})
    check_signature(feishu.FeishuAdapter._send_uploaded_key,
                    {"chat_id", "reply_to", "metadata", "caption", "key_msg_type", "key_payload", "media_tag"})
    replacement = adapter_class(feishu.FeishuAdapter)
    undo = install_host_hooks(stream_consumer, GatewayTurnMixin, GatewayNotificationsMixin)
    undo_features = None

    class Registration:
        def __getattr__(self, key):
            return getattr(ctx, key)

        def register_platform(self, **kwargs):
            kwargs["adapter_factory"] = replacement
            return ctx.register_platform(**kwargs)

    try:
        from gateway.run_turn_runner import TurnRunner
        from gateway.slash_commands_session import GatewaySessionCommandsMixin
        from gateway.run_busy import GatewayBusySessionMixin
        from .controls import install_feature_hooks
        undo_features = install_feature_hooks(TurnRunner, GatewaySessionCommandsMixin, GatewayBusySessionMixin)
        feishu.register(Registration())
        def unload():
            undo_features()
            undo()
        ctx.on_unload(unload)
    except Exception:
        if undo_features:
            undo_features()
        undo()
        raise
