"""Feishu OpenAPI transport with a durable outbox and identity-bound SDK wire.

No credential lookup, token cache, HTTP server or automatic login lives here.
The host supplies its already configured lark-oapi client/executor.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
import json
import logging
import time
from typing import Protocol
from urllib.parse import quote
import uuid

from .model import Conflict, Route, digest
from .render import body_text
from .store import Store
from .transport import Observation, RateLimited, Ref, Rejected, Unavailable, UnknownOutcome

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HTTPResult:
    status: int
    data: dict
    headers: dict | None = None


class Wire(Protocol):
    async def request(self, method: str, path: str, body: dict | None = None) -> HTTPResult: ...


class LarkSDKWire:
    """Use the native adapter's SDK identity and managed blocking executor."""
    def __init__(self, client, run_blocking):
        self.client, self.run_blocking = client, run_blocking

    async def request(self, method, path, body=None):
        import lark_oapi as lark
        builder = (lark.BaseRequest.builder().http_method(getattr(lark.HttpMethod, method))
                   .uri(path).token_types({lark.AccessTokenType.TENANT}))
        if body is not None:
            builder = builder.body(body)
        response = await self.run_blocking(self.client.request, builder.build())
        raw = response.raw
        data = json.loads(raw.content)
        return HTTPResult(raw.status_code, data, dict(raw.headers or {}))

    async def upload_image(self, data):
        from io import BytesIO
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody
        request = CreateImageRequest.builder().request_body(
            CreateImageRequestBody.builder().image_type("message").image(BytesIO(data)).build()).build()
        response = await self.run_blocking(self.client.im.v1.image.create, request)
        if not response.success() or not response.data or not response.data.image_key:
            raise ValueError("activity image upload failed")
        return response.data.image_key


class FeishuTransport:
    """A transport belongs to exactly one application/profile namespace.

    Complete readback is mandatory. Flattened summaries and card references are
    never converted to observations of the desired local body.
    """
    def __init__(self, store: Store, wire: Wire, *, tenant: str, bot: str, profile: str,
                 clock=time.time, timeout: float = 20, send_interval: float = .25):
        if not all((tenant, bot, profile)):
            raise ValueError("explicit application/profile binding required")
        self.store, self.wire, self.binding = store, wire, (tenant, bot, profile)
        self.clock, self.timeout, self.send_interval = clock, timeout, send_interval
        self._send_lock = asyncio.Lock()
        self._next_send = 0.0

    def _route(self, route: Route):
        if (route.tenant, route.bot, route.profile) != self.binding:
            raise Conflict("transport identity mismatch")
        if route.thread and not route.anchor:
            raise Conflict("thread delivery requires an explicit reply anchor")

    async def _request(self, method, path, body=None):
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(self.wire.request(method, path, body), self.timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never include SDK exceptions: they can contain URLs, content or credentials.
            raise UnknownOutcome("transport interrupted; reconcile receipt") from None
        if not isinstance(response.data, dict) or type(response.data.get("code")) is not int:
            raise UnknownOutcome("unrecognized API envelope")
        code = response.data["code"]
        # Numeric diagnostics only: never log content, URLs, IDs or credentials.
        logger.info("Feishu %s status=%s code=%s elapsed_ms=%d", method, response.status,
                    code, int((time.monotonic() - started) * 1000))
        if response.status == 429 or code in {230020, 200810, 99991400}:
            try:
                delay = float((response.headers or {}).get("retry-after", "1"))
            except (TypeError, ValueError):
                delay = 1.0
            raise RateLimited(delay)
        if response.status >= 500 or code == 230049:
            raise UnknownOutcome("server result unresolved")
        if response.status in {401, 403, 404} or code in {
            200740, 200750, 300311, 230002, 230011, 230013, 230017, 230018,
            230019, 230022, 230027, 230028, 230035, 230038, 230050, 230110,
        }:
            raise Unavailable("message, destination or permission unavailable")
        if code != 0 or not 200 <= response.status < 300:
            raise Rejected(f"API rejected operation ({code})")
        data = response.data.get("data", {})
        if not isinstance(data, dict):
            raise UnknownOutcome("invalid response data")
        return data

    async def _operation(self, route, key, method, path, body, *, recoverable=False):
        self._route(route)
        request = {"route": asdict(route), "method": method, "path": path, "body": body}
        request_hash = digest(json.dumps(request, sort_keys=True, ensure_ascii=False))
        old = self.store.operation(key)
        if old:
            if old["state"] == "archived":
                raise UnknownOutcome("archived operation cannot be replayed")
            if old["request_hash"] != request_hash:
                raise Conflict("operation key reused with a different request")
            if old["state"] == "done":
                return old["data"]
            if old["state"] == "blocked":
                raise Unavailable("previous operation was refused")
            if old["state"] == "rejected":
                raise Rejected("previous operation was rejected")
            if old["state"] == "receipt_required":
                raise UnknownOutcome("ambiguous prior send requires its receipt")
            if old["state"] == "attempting" and (
                not recoverable or self.clock() - old["created_at"] >= 3300
            ):
                raise UnknownOutcome("receipt required; automatic replay not permitted")
        replaying_unknown = bool(old and old["state"] == "attempting")
        op = old or {"created_at": self.clock(), "request_hash": request_hash, "request": request}
        op["state"] = "attempting"
        self.store.save_operation(key, op)
        try:
            data = await self._request(method, path, body)
        except RateLimited:
            op["state"] = "retryable"
            self.store.save_operation(key, op)
            raise
        except Unavailable:
            op["state"] = "blocked"
            self.store.save_operation(key, op)
            raise
        except Rejected as exc:
            # A CardKit reply can commit before losing its ACK; replay then
            # returns 230099/200780 (already bound) before IM UUID dedup. That
            # rejection says nothing about whether the first send committed.
            op["state"] = "receipt_required" if replaying_unknown else "rejected"
            op["error"] = str(exc)  # generated numeric API code only
            self.store.save_operation(key, op)
            if replaying_unknown:
                raise UnknownOutcome("replay rejected; original receipt still required") from None
            raise
        op.update(state="done", data=data)
        if method in {"PUT", "PATCH"}:
            # Keep the hash/sequence receipt, not every historical token snapshot.
            op["request"] = {"route": request["route"], "method": method, "path": path}
        self.store.save_operation(key, op)
        return data

    @staticmethod
    def _uuid(key):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "moonawn-feishu:" + key))

    async def _send(self, route, msg_type, content, key):
        self._route(route)
        payload = {"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False), "uuid": self._uuid(key)}
        if route.anchor:
            path = f"/open-apis/im/v1/messages/{quote(route.anchor, safe='')}/reply"
            payload["reply_in_thread"] = bool(route.thread)
        else:
            path = "/open-apis/im/v1/messages?receive_id_type=chat_id"
            payload["receive_id"] = route.chat
        # Conservative application-wide new-message budget. Card updates have
        # a separate per-card coalescer; no sleep on the agent token thread.
        async with self._send_lock:
            await asyncio.sleep(max(0, self._next_send - time.monotonic()))
            self._next_send = time.monotonic() + self.send_interval
            data = await self._operation(route, key, "POST", path, payload, recoverable=True)
        if not isinstance(data.get("message_id"), str) or not data["message_id"]:
            raise UnknownOutcome("send ACK omitted message ID")
        if data.get("chat_id") and data["chat_id"] != route.chat:
            raise Conflict("send receipt belongs to another chat")
        return data["message_id"]

    async def create(self, route, card, request_key):
        # Persist entity receipt BEFORE sending it. An entity is not a message.
        old = self.store.operation(request_key + ":entity")
        if old and old["state"] == "archived":
            raise UnknownOutcome("archived entity cannot be recreated")
        payload = (old["request"]["body"] if old else
                   {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)})
        data = await self._operation(route, request_key + ":entity", "POST", "/open-apis/cardkit/v1/cards",
                                     payload)
        card_id = data.get("card_id")
        if not isinstance(card_id, str) or not card_id:
            raise UnknownOutcome("entity ACK omitted card ID")
        message_id = await self._send(route, "interactive", {"type": "card", "data": {"card_id": card_id}}, request_key + ":message")
        return Ref(card_id, message_id)

    async def create_topic(self, route, title, key):
        """Two idempotent IM sends; the server supplies the actual thread ID."""
        self._route(route)
        root_route = replace(route, thread="", anchor="")
        root = await self._send(root_route, "text", {"text": title}, key + ":root")
        reply_route = replace(root_route, anchor=root)
        body = {"msg_type": "text", "content": json.dumps({"text": "在此话题继续对话。"}, ensure_ascii=False),
                "reply_in_thread": True, "uuid": self._uuid(key + ":reply")}
        async with self._send_lock:
            await asyncio.sleep(max(0, self._next_send - time.monotonic()))
            self._next_send = time.monotonic() + self.send_interval
            data = await self._operation(reply_route, key + ":reply", "POST",
                f"/open-apis/im/v1/messages/{quote(root, safe='')}/reply", body, recoverable=True)
        if data.get("chat_id") != route.chat or not data.get("thread_id") or not data.get("message_id"):
            raise UnknownOutcome("topic receipt lacks a verified chat/thread/message binding")
        return root, data["thread_id"], data["message_id"]

    async def recover_create(self, route, request_key):
        self._route(route)
        entity = self.store.operation(request_key + ":entity")
        if not entity or entity["state"] != "done" or not entity["data"].get("card_id"):
            return None
        if entity["request"]["route"] != asdict(route):
            raise Conflict("entity identity mismatch")
        card_id = entity["data"]["card_id"]
        mid = await self._send(route, "interactive", {"type": "card", "data": {"card_id": card_id}}, request_key + ":message")
        return Ref(card_id, mid)

    async def _card_write(self, route, ref, sequence, suffix, method, payload):
        if not 0 < sequence < 2**31:
            raise ValueError("CardKit sequence outside int32 range")
        key = f"{digest(json.dumps(asdict(route), sort_keys=True))}:{ref.card_id}:{sequence}:{suffix}"
        payload = dict(payload, sequence=sequence, uuid=self._uuid(key))
        path = f"/open-apis/cardkit/v1/cards/{quote(ref.card_id, safe='')}{suffix}"
        await self._operation(route, key, method, path, payload)

    async def update(self, route, ref, card, sequence):
        await self._card_write(route, ref, sequence, "", "PUT", {"card": {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)}})

    async def update_body(self, route, ref, text, sequence):
        await self._card_write(route, ref, sequence, "/elements/answer/content", "PUT", {"content": text})

    async def close(self, route, ref, sequence):
        await self._card_write(route, ref, sequence, "/settings", "PATCH", {"settings": json.dumps({"config": {"streaming_mode": False}})})
        self.store.save_operation("sealed:" + ref.card_id, {"route": asdict(route), "message_id": ref.message_id})

    async def _message(self, route, message_id):
        self._route(route)
        # Default IM projection flattens cards. This documented query returns
        # original schema 2.0, allowing exact body verification after updates.
        data = await self._request("GET", f"/open-apis/im/v1/messages/{quote(message_id, safe='')}?card_msg_content_type=user_card_content")
        items = data.get("items", [])
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or items[0].get("message_id") != message_id:
            raise UnknownOutcome("exact message receipt missing")
        item = items[0]
        if item.get("deleted"):
            raise Unavailable("message recalled")
        if item.get("chat_id") != route.chat or (item.get("thread_id") or "") != route.thread:
            raise Conflict("message readback route mismatch")
        sender = item.get("sender") or {}
        if not isinstance(sender, dict):
            raise UnknownOutcome("invalid sender representation")
        if sender.get("id_type") != "app_id" or sender.get("id") != route.bot:
            raise Conflict("readback sender mismatch")
        try:
            body = json.loads(item["body"]["content"])
        except (KeyError, TypeError, ValueError):
            raise UnknownOutcome("message body is not readable") from None
        return item, body

    async def read(self, route, ref):
        item, card = await self._message(route, ref.message_id)
        if item.get("msg_type") != "interactive" or not isinstance(card, dict):
            raise UnknownOutcome("unexpected card representation")
        body = card.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("elements"), list) or not isinstance(card.get("config"), dict):
            raise UnknownOutcome("card body projection unavailable; exact readback required")
        elements = body["elements"]
        if any(not isinstance(e, dict) for e in elements):
            raise UnknownOutcome("invalid card elements")
        answers = [e for e in elements if e.get("element_id") == "answer" and e.get("tag") == "markdown"]
        # Empty cards are permitted only when their full schema is available.
        if card.get("schema") != "2.0" or len(answers) > 1 or any(not isinstance(e.get("content"), str) for e in answers):
            raise UnknownOutcome("card body projection unavailable; exact readback required")
        sealed = self.store.operation("sealed:" + ref.card_id)
        closed = (card.get("config", {}).get("streaming_mode") is False or
                  bool(sealed and sealed.get("route") == asdict(route) and sealed.get("message_id") == ref.message_id))
        return Observation(route, body_text(card), closed, projection="feishu_card")

    async def send_text(self, route, text, request_key):
        return await self._send(route, "text", {"text": text}, request_key)

    async def recover_text(self, route, request_key):
        self._route(route)
        op = self.store.operation(request_key)
        if not op:
            return None
        if op["state"] == "archived":
            raise UnknownOutcome("archived text cannot be replayed")
        req = op["request"]
        if req["route"] != asdict(route):
            raise Conflict("text receipt identity mismatch")
        payload = json.loads(req["body"]["content"])
        return await self._send(route, "text", payload, request_key)

    async def read_text(self, route, message_id):
        item, body = await self._message(route, message_id)
        if item.get("msg_type") != "text" or not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise UnknownOutcome("exact text projection unavailable")
        return Observation(route, body["text"], True)
