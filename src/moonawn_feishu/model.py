"""Typed reply phases: identity and explicit events, never text-length heuristics."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, field
import hashlib
import json
import time
from .tool_display import clean_tool_data


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Conflict(ValueError):
    pass


@dataclass(frozen=True)
class Route:
    tenant: str
    bot: str
    profile: str
    chat: str
    thread: str = ""
    anchor: str = ""

    def __post_init__(self):
        if not all((self.tenant, self.bot, self.profile, self.chat)):
            raise ValueError("tenant, bot, profile and chat are required")


@dataclass(frozen=True)
class Identity:
    route: Route
    turn: str
    phase: int = 0

    def __post_init__(self):
        if not self.turn or self.phase < 0:
            raise ValueError("explicit turn and nonnegative phase required")

    @property
    def key(self) -> str:
        return digest(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False))


@dataclass(frozen=True)
class Event:
    identity: Identity
    event_id: str
    seq: int
    kind: str
    text: str = ""
    tool_id: str = ""
    tool_state: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class Phase:
    identity: Identity
    seq: int = -1
    body: str = ""
    status: str = "thinking"
    terminal: str = ""
    tools: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    typing: bool = True
    tool_count: int = 0
    tool_seen: dict = field(default_factory=dict)
    tool_count_capped: bool = False
    content_hash: str = ""
    card_id: str = ""
    message_id: str = ""
    sequence: int = 0
    create_attempted: bool = False
    delivery: str = "pending"
    seal: str = "open"
    mode: str = "card"
    last_render_hash: str = ""
    last_structure_hash: str = ""
    update_attempts: int = 0
    retry_after: float = 0
    parts: list = field(default_factory=list)
    diagnostic: str = ""
    updated_at: float = field(default_factory=time.time)
    archived: bool = False
    preview_retired: bool = False
    # Only bounded ephemeral replay protection; final state is durably frozen.
    seen: OrderedDict = field(default_factory=OrderedDict, repr=False)

    def apply(self, event: Event) -> bool:
        if event.identity != self.identity:
            raise Conflict("event belongs to another reply phase")
        if not event.event_id or event.seq < 0 or len(event.text.encode()) > 2_000_000:
            raise ValueError("invalid event envelope")
        fingerprint = digest(json.dumps(asdict(event), sort_keys=True, ensure_ascii=False))
        if event.event_id in self.seen:
            if self.seen[event.event_id] != fingerprint:
                raise Conflict("event id reused with another payload")
            return False
        if self.terminal:
            if event.kind == "final" and self.terminal == "final" and digest(event.text) != self.content_hash:
                raise Conflict("frozen final cannot be replaced; start a new phase")
            return False
        if event.seq <= self.seq:
            return False
        if event.kind not in {"begin", "snapshot", "status", "tool", "metrics", "final", "cancel", "error"}:
            raise ValueError("unsupported event kind")
        if event.kind == "status" and event.text not in {"thinking", "working", "waiting"}:
            raise ValueError("unsupported status")
        if event.kind == "tool" and (not event.tool_id or event.tool_state not in {"running", "done", "failed"}):
            raise ValueError("invalid tool event")
        if event.kind == "final" and not event.text.strip():
            raise ValueError("empty final requires explicit cancel or error")
        self.seq = event.seq
        self.updated_at = time.time()
        self.seen[event.event_id] = fingerprint
        if len(self.seen) > 512:
            self.seen.popitem(last=False)
        if event.kind == "snapshot":
            self.body = event.text
            self.status = "working"
        elif event.kind == "status":
            self.status = event.text
        elif event.kind == "metrics":
            self.metrics = event.data
        elif event.kind == "tool":
            tool_key = digest(event.tool_id)
            if tool_key not in self.tool_seen:
                if len(self.tool_seen) < 4096:
                    self.tool_seen[tool_key] = True
                    self.tool_count += 1
                else:
                    self.tool_count_capped = True
            previous = self.tools.get(event.tool_id, {})
            self.tools[event.tool_id] = {**previous, "label": event.text[:160], "state": event.tool_state,
                                         **clean_tool_data(event.data)}
            # Display history is bounded; final body is never cropped.
            if len(self.tools) > 12:
                keys = list(self.tools)
                # Reserve three older exceptions/unfinished/slow calls, then
                # fill with newest calls. Routine successes cannot erase the
                # only explanation of an earlier problem in a long task.
                priority = lambda k: (3 if self.tools[k]["state"] == "failed" else
                                      2 if self.tools[k]["state"] == "running" else
                                      1 if self.tools[k].get("elapsed", 0) >= 10 else 0)
                older = [k for k in keys[:-9] if priority(k)]
                important = sorted(older, key=lambda k: (priority(k), keys.index(k)), reverse=True)[:3]
                keep = set(important + keys[-(12 - len(important)):])
                self.tools = {k: v for k, v in self.tools.items() if k in keep}
        elif event.kind in {"final", "cancel", "error"}:
            self.terminal = event.kind
            if event.kind == "final" or event.kind == "error" and event.text:
                self.body = event.text
            self.content_hash = digest(self.body)
        return True

    def encode(self) -> str:
        obj = asdict(self)
        obj.pop("seen")
        # Tool insertion order is the execution start order. Sorting nested
        # call IDs here silently reordered the activity timeline on restore.
        return json.dumps(obj, ensure_ascii=False)

    @classmethod
    def decode(cls, value: str) -> Phase:
        obj = json.loads(value)
        raw = obj.pop("identity")
        return cls(identity=Identity(Route(**raw.pop("route")), **raw), **obj)
