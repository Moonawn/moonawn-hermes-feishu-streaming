"""Explicit transport outcomes. No HTTP client, credentials or network side effects."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from .model import Route


class UnknownOutcome(Exception):
    """Request may have committed; never blindly resend via another channel."""


class Rejected(Exception):
    """Transport guarantees this operation did not commit."""


class Unavailable(Exception):
    """Deleted/recalled message or revoked permission: do not resurrect."""


class RateLimited(Rejected):
    def __init__(self, retry_after: float):
        self.retry_after = max(0.25, min(retry_after, 30))


@dataclass(frozen=True)
class Ref:
    card_id: str
    message_id: str


@dataclass(frozen=True)
class Observation:
    route: Route
    body: str
    closed: bool
    render_hash: str = ""
    projection: str = ""


class Transport(Protocol):
    async def create(self, route: Route, card: dict, request_key: str) -> Ref: ...
    async def update(self, route: Route, ref: Ref, card: dict, sequence: int) -> None: ...
    async def update_body(self, route: Route, ref: Ref, text: str, sequence: int) -> None: ...
    async def close(self, route: Route, ref: Ref, sequence: int) -> None: ...
    async def read(self, route: Route, ref: Ref) -> Observation: ...
    async def send_text(self, route: Route, text: str, request_key: str) -> str: ...
    async def read_text(self, route: Route, message_id: str) -> Observation: ...
