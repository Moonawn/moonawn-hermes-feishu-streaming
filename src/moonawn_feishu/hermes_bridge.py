"""Explicit producer contract; does not patch/register a live Hermes adapter.

Hermes 0.22 edit(finalize=True) is a segment hint, not proof of whole-turn
completion. Native final suppression requires an exact confirmed receipt.
"""
from __future__ import annotations

import ast
from enum import Enum
from pathlib import Path
from .model import Conflict, Event, Identity


class NativeDisposition(str, Enum):
    PASS = "pass"          # This phase was never claimed by the independent core.
    HOLD = "hold"          # Owned, but not confirmed; native fallback must wait.
    SUPPRESS = "suppress"  # Exact final body was read back under the same identity.


class HermesBridge:
    def __init__(self, engine):
        self.engine = engine
        self.boundary_hints = set()

    def edit(self, identity: Identity, text: str, seq: int, *, finalize: bool = False):
        if finalize:
            self.boundary_hints.add(identity.key)
        return self.engine.accept(Event(identity, f"edit:{seq}", seq, "snapshot", text))

    def final(self, identity: Identity, text: str, seq: int):
        return self.engine.accept(Event(identity, f"final:{seq}", seq, "final", text))

    def may_suppress_native(self, identity: Identity, exact_text: str) -> bool:
        """Receipt predicate only: False MUST NOT be treated as permission to send."""
        return self.engine.confirmed(identity, exact_text)

    def native_disposition(self, identity: Identity, exact_text: str) -> NativeDisposition:
        phase = self.engine.phases.get(identity.key)
        if phase is None:
            return NativeDisposition.PASS
        if phase.terminal == "final" and exact_text != phase.body:
            raise Conflict("native final differs from frozen phase; requires explicit new phase")
        if self.engine.confirmed(identity, exact_text):
            return NativeDisposition.SUPPRESS
        return NativeDisposition.HOLD


def inspect_hermes(root: Path) -> dict:
    """AST-only source contract; no imports, credentials or gateway startup."""
    requirements = [
        ("gateway/run_busy.py", "GatewayBusySessionMixin", "_dispatch_busy_slash_command", {"event", "cmd_def", "quick_key", "source"}),
        ("gateway/run_turn_runner.py", "TurnRunner", "_wire_turn_agent_callbacks", {"agent", "turn_route", "reasoning_config", "stream_delta_cb", "interim_assistant_cb", "want_interim_messages"}),
        ("gateway/slash_commands_session.py", "GatewaySessionCommandsMixin", "_handle_sessions_command", {"event"}),
        ("gateway/slash_commands_session.py", "GatewaySessionCommandsMixin", "_handle_reset_command", {"event"}),
        ("gateway/slash_commands_session.py", "GatewaySessionCommandsMixin", "_handle_resume_command", {"event"}),
        ("gateway/slash_commands_session.py", "GatewaySessionCommandsMixin", "_resume_target_allowed", {"source", "target_id", "allow_override"}),
        ("gateway/slash_commands_session.py", "GatewaySessionCommandsMixin", "_same_origin_chat", {"current", "origin"}),
        ("plugins/platforms/feishu/adapter.py", "FeishuAdapter", "send", {"chat_id", "content", "reply_to", "metadata"}),
        ("plugins/platforms/feishu/adapter.py", "FeishuAdapter", "edit_message", {"chat_id", "message_id", "content", "finalize"}),
        ("plugins/platforms/feishu/adapter.py", "FeishuAdapter", "_handle_message_with_guards", {"event"}),
        ("plugins/platforms/feishu/adapter.py", "FeishuAdapter", "_send_uploaded_key", {"chat_id", "reply_to", "metadata", "caption", "key_msg_type", "key_payload", "media_tag"}),
        ("gateway/platforms/base.py", "BasePlatformAdapter", "handle_message", {"event"}),
        ("gateway/platforms/base.py", "BasePlatformAdapter", "_record_delivery_obligation", {"event", "session_key", "text_content", "delivery_adapter", "is_ephemeral_response"}),
        ("gateway/platforms/base.py", "BasePlatformAdapter", "_send_retry_is_final", {"result"}),
        ("gateway/stream_consumer.py", "StreamConsumer", "finish", {"final_text"}),
        ("gateway/stream_consumer.py", "StreamConsumer", "__init__", {"adapter", "chat_id", "metadata", "initial_reply_to_id", "run_still_current"}),
        ("gateway/run_turn.py", "GatewayTurnMixin", "_run_agent_inner", {"source", "session_key", "inbound_message_id", "event_message_id"}),
        ("gateway/run_turn.py", "GatewayTurnMixin", "_run_agent_mark_streamed_delivery", {"response", "turn_ctx"}),
        ("gateway/run_notifications.py", "GatewayNotificationsMixin", "_deliver_queued_first_response", {"stream_consumer", "session_key", "inbound_message_id", "deliver_media"}),
    ]
    results = []
    for path, cls, method, expected in requirements:
        target = root / path
        passed = False
        reason = 'missing_source'
        try:
            tree = ast.parse(target.read_text())
        except (OSError, SyntaxError, UnicodeError):
            tree = None
        if tree is not None:
            reason = 'missing_method_or_parameters'
            for node in ast.walk(tree):
                # Consumer class may carry a platform prefix across releases;
                # verify method + actual class name in the report, no silent skip.
                if isinstance(node, ast.ClassDef) and (node.name == cls or cls == "StreamConsumer" and node.name.endswith("StreamConsumer")):
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == method:
                            args = {a.arg for a in member.args.args + member.args.kwonlyargs + member.args.posonlyargs}
                            passed = expected <= args
                            if method == '__init__' and cls == 'StreamConsumer':
                                allowed = {'self', 'adapter', 'chat_id', 'config', 'metadata', 'on_new_message',
                                           'on_before_finalize', 'initial_reply_to_id', 'run_still_current'}
                                passed = passed and args <= allowed
        results.append({"file": path, "method": method, "passed": passed, 'reason': '' if passed else reason})
    return {"static_surface_passed": all(x["passed"] for x in results), "checks": results,
            "live_integration_verified": False,
            "remaining": ["real CardKit schema/read-back acceptance", "live approval and media passthrough"]}
