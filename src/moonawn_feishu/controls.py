"""Native session controls. Cards carry opaque, expiring, owner-bound actions."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import wraps
import secrets
import json
import re
import time

from .model import Conflict, Route, digest
from .render import safe_tool_label
from .transport import Rejected, Unavailable, UnknownOutcome, RateLimited


def button(label, nonce, *, disabled=False, primary=False, disabled_tip="当前会话执行中，请等待结束或另开话题"):
    return {"tag": "button", "text": {"tag": "plain_text", "content": label[:100]},
            "type": "primary" if primary else "default", "size": "medium", "width": "fill",
            "disabled": disabled, "disabled_tips": {"tag": "plain_text", "content": disabled_tip},
            "behaviors": [{"type": "callback", "value": {"moonawn_action": nonce}}]}


def panel_card(rows, current, actions, *, busy=False, typing=True, notice=""):
    """Pure schema 2.0 renderer, shared by the offline preview and live panel."""
    elements = [{"tag": "markdown", "content": "**会话工作台**", "text_size": "normal_v2"},
                {"tag": "markdown", "text_size": "notation", "content": notice or (
                    "当前会话正在执行。可另开话题并行处理。" if busy else "选择历史会话继续，也可以开始新的话题。")},
                button("另开独立话题", actions["topic"], primary=True),
                button("新建当前会话", actions["new"], disabled=busy)]
    for index, row in enumerate(rows):
        selected = str(row.get("id")) == current
        title = safe_tool_label(row.get("title") or f"未命名会话 {index + 1}")
        elements.append(button(("当前 · " if selected else "继续 · ") + title,
                               actions[f"switch:{index}"], disabled=busy or selected,
                               disabled_tip="已经处于此会话" if selected else "当前会话执行中，请等待结束或另开话题"))
    elements.extend([
        button("刷新会话列表", actions["refresh"]),
        button("打字机：开 · 点击关闭" if typing else "打字机：关 · 点击开启", actions["typing"]),
        {"tag": "markdown", "text_size": "notation",
         "content": "切换只影响本话题后续消息。新话题保持独立上下文；群聊中仍需按机器人规则 @。\n仅发起人可操作 · 按钮 15 分钟有效 · 动效设置从下次回答生效"}])
    return {"schema": "2.0", "config": {"update_multi": True, "streaming_mode": False,
            "enable_forward": False, "summary": {"content": "Hermes 会话工作台"}},
            "body": {"padding": "16px 16px 16px 16px", "vertical_spacing": "8px", "elements": elements}}


class ControlService:
    def __init__(self, adapter, store, transport, *, clock=time.time):
        self.adapter, self.store, self.transport, self.clock = adapter, store, transport, clock
        self.lock = asyncio.Lock()
        self.closed = False
        self._showing = set()

    async def close(self):
        self.closed = True
        async with self.lock:
            pass
        if self._showing:
            await asyncio.gather(*list(self._showing))

    @property
    def host(self):
        return getattr(self.adapter, "gateway_runner", None) or getattr(getattr(self.adapter, "_message_handler", None), "__self__", None)

    def pref_key(self, source):
        return "typing:" + digest("\0".join((self.adapter._moonawn_profile, str(source.chat_id), str(source.user_id))))

    def typing(self, source):
        return (self.store.operation(self.pref_key(source)) or {}).get("enabled", True)

    async def normalized(self, source):
        return await asyncio.to_thread(self.host._normalize_source_for_session_key, source)

    def busy(self, source):
        key = self.host._session_key_for_source(source)
        active = getattr(self.adapter, "_active_sessions", {})
        tasks = getattr(self.adapter, "_session_tasks", {})
        return key in active and tasks.get(key) is not asyncio.current_task()

    def route(self, source, anchor):
        key = self.host._session_key_for_source(source)
        return self.adapter._moonawn_scope(source, key, anchor, anchor).route

    async def listing(self, source):
        if not getattr(self.host, "_session_db", None):
            raise Unavailable("native session history unavailable")
        from hermes_cli.session_listing import query_session_listing
        current = await self.host.async_session_store.get_or_create_session(source)
        rows = await asyncio.to_thread(
            query_session_listing, getattr(self.host._session_db, "_db", self.host._session_db),
            source=source.platform.value, session_key=self.host._session_key_for_source(source),
            current_session_id=current.session_id, include_current_session=True,
            include_all_sources=False, include_unnamed=True, search_query=None, limit=10, exclude_sources=["tool"])
        rows = [r for r in rows if await self.host._resume_row_visible(source, r, allow_all=False)]
        return str(current.session_id), rows[:10]

    async def persisted_owner(self, source, target):
        """Prove an inactive Feishu origin from its full native persisted row.

        Hermes 0.22.1's legacy user_id-only fallback rejects every inactive
        alt-keyed Feishu user, even when origin_json and session_key exist.
        Missing, legacy or inconsistent proof must still fail closed.
        """
        from gateway.session import SessionSource
        try:
            if getattr(source.platform, "value", None) != "feishu":
                return False
            row = await self.host._session_db.get_session(target) or {}
            raw = json.loads(row.get("origin_json") or "null")
            if not isinstance(raw, dict):
                return False
            origin = SessionSource.from_dict(raw)
            key = self.host._session_key_for_source(source)
            return bool(row.get("id") == target and row.get("source") == "feishu"
                and row.get("session_key") == key == self.host._session_key_for_source(origin)
                and row.get("user_id") == source.user_id == origin.user_id
                and source.user_id and source.user_id_alt and source.user_id_alt == origin.user_id_alt
                and row.get("chat_id") == source.chat_id == origin.chat_id
                and (row.get("thread_id") or "") == (source.thread_id or "") == (origin.thread_id or "")
                and self.host._same_origin_chat(source, origin))
        except (AttributeError, TypeError, ValueError, KeyError):
            return False

    async def show(self, source, anchor, *, notice=""):
        if self.closed:
            raise Unavailable("controls are shutting down")
        done = asyncio.get_running_loop().create_future()
        self._showing.add(done)
        try:
            return await self._show(source, anchor, notice=notice)
        finally:
            done.set_result(None)
            self._showing.discard(done)

    async def _show(self, source, anchor, *, notice=""):
        source = await self.normalized(source)
        if not source.user_id or not anchor:
            raise Conflict("session panel requires explicit actor and anchor")
        current, rows = await self.listing(source)
        panel = secrets.token_urlsafe(18)
        state = {"source": source.to_dict(), "expected": current, "anchor": anchor,
                 "profile": self.adapter._moonawn_profile, "message_id": "", "expires_at": self.clock() + 900}
        records = {"panel:" + panel: state}
        actions = {}
        values = [(name, "") for name in ("new", "topic", "refresh", "typing")]
        values += [(f"switch:{i}", str(row["id"])) for i, row in enumerate(rows)]
        for name, target in values:
            nonce = secrets.token_urlsafe(24)
            actions[name] = nonce
            records["control:" + nonce] = {"panel": panel, "action": name.split(":")[0],
                "target": target, "state": "ready", "expires_at": state["expires_at"]}
        self.store.save_operations(records)
        card = panel_card(rows, current, actions, busy=self.busy(source), typing=self.typing(source), notice=notice)
        ref = await self.transport.create(self.route(source, anchor), card, "panel-send:" + panel)
        state["message_id"] = ref.message_id
        self.store.save_operation("panel:" + panel, state)
        return ref

    def validate(self, data):
        event, header = getattr(data, "event", None), getattr(data, "header", None)
        value = getattr(getattr(event, "action", None), "value", {})
        nonce = value.get("moonawn_action") if isinstance(value, dict) else None
        if not isinstance(nonce, str) or len(nonce) > 100:
            raise Conflict("invalid control")
        op = self.store.operation("control:" + nonce)
        panel = self.store.operation("panel:" + op["panel"]) if op else None
        context, operator = getattr(event, "context", None), getattr(event, "operator", None)
        if (not panel or panel["profile"] != self.adapter._moonawn_profile
                or not getattr(header, "event_id", None) or getattr(header, "app_id", None) != self.adapter._app_id
                or not panel["message_id"] or getattr(context, "open_message_id", None) != panel["message_id"]
                or getattr(context, "open_chat_id", None) != panel["source"]["chat_id"]
                or getattr(operator, "open_id", None) != panel["source"]["user_id"]
                or not self.adapter._is_interactive_operator_authorized(operator.open_id)):
            raise Conflict("control identity mismatch")
        return nonce, op, panel

    async def notice(self, panel, text, key):
        from gateway.session import SessionSource
        source = SessionSource.from_dict(panel["source"])
        await self.transport._send(self.route(source, panel["message_id"]), "text", {"text": text}, "control-notice:" + key)

    async def callback(self, data):
        # The native SDK handler already ACKs promptly and schedules this coroutine.
        async with self.lock:
            if self.closed:
                return
            try:
                nonce, op, panel = self.validate(data)
            except Conflict:
                return  # never reflect somebody else's private panel to a caller
            if panel["expires_at"] <= self.clock():
                await self.notice(panel, "会话面板已过期，请发送 /sessions 重新打开。", nonce + ":expired")
                return
            if op["state"] != "ready":
                await self.notice(panel, "该操作已提交，请刷新会话列表查看当前状态。", nonce + ":duplicate")
                return
            op["state"] = "dispatching"
            self.store.save_operation("control:" + nonce, op)
            from gateway.session import SessionSource
            from gateway.platforms.base import MessageEvent
            source = await self.normalized(SessionSource.from_dict(panel["source"]))
            try:
                if op["action"] in {"new", "switch"}:
                    await self.guard(source, panel, op)
                    command = "/new" if op["action"] == "new" else "/resume " + op["target"]
                    event = MessageEvent(text=command, source=source, user_id=source.user_id,
                        message_id="moonawn:" + str(data.header.event_id), reply_anchor_override=panel["message_id"],
                        metadata={"moonawn_control": nonce}, internal=False, allow_gateway_control=True)
                    # Native admission/authorization and the command's own ownership
                    # checks remain authoritative. A second guard runs at execution.
                    await self.adapter._handle_message_with_guards(event)
                elif op["action"] == "topic":
                    root, thread, anchor = await self.transport.create_topic(self.route(source, panel["message_id"]), "独立会话", "topic:" + nonce)
                    target = replace(source, thread_id=thread, message_id=anchor)
                    await self.show(target, anchor, notice="独立话题已建立。在此话题继续发消息，原会话仍可执行。")
                elif op["action"] == "typing":
                    self.store.save_operation(self.pref_key(source), {"enabled": not self.typing(source)})
                    await self.show(source, panel["message_id"], notice="打字机设置已保存，从下次回答生效。")
                elif op["action"] == "refresh":
                    await self.show(source, panel["message_id"])
                else:
                    raise Conflict("unsupported control")
                latest = self.store.operation("control:" + nonce)
                op = latest if latest and latest["state"] in {"executing", "executed"} else dict(op, state="submitted")
            except Conflict:
                op["state"] = "refused"
                await self.notice(panel, "当前会话已变化或仍在执行。请刷新列表；需要并行时选择另开独立话题。", nonce)
            except (Unavailable, Rejected):
                op["state"] = "refused"
                await self.notice(panel, "当前群聊、权限或接口不支持此操作。原会话保持原状，请使用 /sessions 查看。", nonce)
            except (UnknownOutcome, RateLimited):
                op["state"] = "unknown"
                await self.notice(panel, "操作回执暂未确认，请先检查话题和会话列表，避免重复创建。", nonce)
            finally:
                self.store.save_operation("control:" + nonce, op)

    async def guard(self, source, panel, op):
        if panel["expires_at"] <= self.clock() or self.busy(source):
            raise Conflict("expired or busy")
        entry = await self.host.async_session_store.get_or_create_session(source)
        if str(entry.session_id) != panel["expected"] or self.busy(source):
            raise Conflict("stale session binding")
        if op["action"] == "switch" and not await self.host._resume_target_allowed(source, op["target"], allow_override=False):
            raise Conflict("session is not visible to this origin")
        if self.busy(source):
            raise Conflict("session became busy during permission lookup")
        if op["action"] == "switch" and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", op["target"]):
            raise Conflict("invalid native session identifier")


def install_feature_hooks(turn_runner, session_class, busy_class=None):
    from .plugin import _dispatch, check_signature
    wire = turn_runner._wire_turn_agent_callbacks
    originals = {name: getattr(session_class, name) for name in (
        "_handle_sessions_command", "_handle_reset_command", "_handle_resume_command")}
    original_allowed = session_class._resume_target_allowed
    check_signature(original_allowed, {"source", "target_id", "allow_override"})
    check_signature(session_class._same_origin_chat, {"current", "origin"})
    check_signature(wire, {"agent", "turn_route", "reasoning_config", "stream_delta_cb", "interim_assistant_cb", "want_interim_messages"})
    for original in originals.values():
        check_signature(original, {"event"})
    original_busy = busy_class._dispatch_busy_slash_command if busy_class else None
    if original_busy:
        check_signature(original_busy, {"event", "cmd_def", "quick_key", "source"})

    async def busy_panel(self, event, cmd_def, quick_key, source):
        adapter = self._delivery_adapter_for(source)
        controls = getattr(adapter, "_moonawn_controls", None)
        if (controls and cmd_def.name == "sessions" and event.get_command() == "sessions"
                and not event.get_command_args().strip() and event.allow_gateway_control
                and quick_key == self._session_key_for_source(source)):
            # Admission and slash ACL already ran upstream. Recheck the ACL at
            # this scoped read-only entry; never dispatch /new or /resume here.
            denied = self._check_slash_access(source, "sessions")
            if denied is not None:
                return denied
            async with self._async_profile_scope_for_source(source):
                return await self._handle_sessions_command(event)
        return await original_busy(self, event, cmd_def, quick_key, source)

    @wraps(wire)
    def observed(self, agent, *args, **kwargs):
        result = wire(self, agent, *args, **kwargs)
        dispatch = _dispatch.get()
        native_context = getattr(self, "_ctx", None)
        if (dispatch and dispatch.consumer and not dispatch.native_only
                and getattr(native_context, "session_key", None) == dispatch.scope.session_key):
            dispatch.consumer.observe_agent(agent)
        return result

    def wrap_command(name, original):
        @wraps(original)
        async def command(self, event):
            adapter = self._delivery_adapter_for(event.source)
            controls = getattr(adapter, "_moonawn_controls", None)
            if not controls:
                return await original(self, event)
            nonce = (event.metadata or {}).get("moonawn_control")
            if nonce:
                op = controls.store.operation("control:" + nonce)
                panel = controls.store.operation("panel:" + op["panel"]) if op else None
                expected_action = "new" if name == "_handle_reset_command" else "switch"
                if (not panel or op["state"] not in {"dispatching", "submitted"} or op["action"] != expected_action
                        or panel["profile"] != adapter._moonawn_profile
                        or event.source.to_dict() != panel["source"]):
                    return "会话操作已失效，请重新打开 /sessions。"
                # A delayed background command must not switch a newer session.
                try:
                    await controls.guard(event.source, panel, op)
                except Conflict:
                    return "当前会话正在执行或已变化，请刷新列表或另开话题。"
                op["state"] = "executing"
                controls.store.save_operation("control:" + nonce, op)
                result = await original(self, event)
                op["state"] = "executed"
                controls.store.save_operation("control:" + nonce, op)
                current = await self.async_session_store.get_or_create_session(event.source)
                changed = (str(current.session_id) != panel["expected"] if op["action"] == "new"
                           else str(current.session_id) == op["target"])
                if not changed:
                    return "会话未切换，请刷新列表后重试。"
                try:
                    await controls.show(event.source, event.reply_anchor_override or event.message_id,
                                        notice="已新建会话。" if op["action"] == "new" else "已切换会话，后续消息会沿用该上下文。")
                    return None
                except (Conflict, Unavailable, Rejected, UnknownOutcome, RateLimited):
                    return "会话已切换，面板暂未刷新。可发送 /sessions 重新打开。"
            if name == "_handle_sessions_command" and not event.get_command_args().strip():
                try:
                    await controls.show(event.source, event.reply_anchor_override or event.message_id)
                    return None
                except (Conflict, Unavailable, Rejected):
                    return await original(self, event)
                except (UnknownOutcome, RateLimited):
                    return "会话面板回执暂未确认，可使用 /resume 查看原生文字列表。"
            return await original(self, event)
        return command

    wrappers = {name: wrap_command(name, fn) for name, fn in originals.items()}
    @wraps(original_allowed)
    async def allowed(self, source, target_id, allow_override=False):
        if await original_allowed(self, source, target_id, allow_override=allow_override):
            return True
        adapter = self._delivery_adapter_for(source)
        controls = getattr(adapter, "_moonawn_controls", None)
        return await controls.persisted_owner(source, target_id) if controls else False

    turn_runner._wire_turn_agent_callbacks = observed
    session_class._resume_target_allowed = allowed
    if busy_class:
        busy_class._dispatch_busy_slash_command = wraps(original_busy)(busy_panel)
    for name, fn in wrappers.items():
        setattr(session_class, name, fn)
    def undo():
        if turn_runner._wire_turn_agent_callbacks is observed:
            turn_runner._wire_turn_agent_callbacks = wire
        if session_class._resume_target_allowed is allowed:
            session_class._resume_target_allowed = original_allowed
        if busy_class and busy_class._dispatch_busy_slash_command is busy_panel:
            busy_class._dispatch_busy_slash_command = original_busy
        for name, fn in wrappers.items():
            if getattr(session_class, name) is fn:
                setattr(session_class, name, originals[name])
    return undo
