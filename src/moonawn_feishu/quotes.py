"""Permission-bound quoted answers, independent of another agent's memory."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable
from .model import Route


@dataclass(frozen=True)
class VisibleMessage:
    message_id: str
    chat: str
    thread: str
    sender: str
    answer: str
    available: bool = True


@dataclass(frozen=True)
class Quote:
    text: str
    source: str
    complete: bool
    readable: bool


class QuoteResolver:
    """The supplied reader MUST use only the requesting adapter's identity.

    No shared global cache: its permission scope cannot outlive a request.
    Raw CardKit/user-card schema conversion belongs to that reader and requires
    real API fixtures before a production implementation can be installed.
    """
    def __init__(self, reader: Callable[[Route, str], Awaitable[VisibleMessage | None]], limit: int = 12000):
        self.reader, self.limit = reader, limit

    async def resolve(self, requester: Route, message_id: str) -> Quote:
        try:
            message = await self.reader(requester, message_id)
        except Exception:
            message = None
        if (message is None or not message.available or message.message_id != message_id
                or message.chat != requester.chat or message.thread != requester.thread or not message.answer):
            return Quote("引用正文暂不可读，请补充需要讨论的内容。", message_id, False, False)
        if len(message.answer) > self.limit:
            return Quote(message.answer[:self.limit] + "\n[引用过长，后文已截断]", message_id, False, True)
        return Quote(message.answer, message_id, True, True)
