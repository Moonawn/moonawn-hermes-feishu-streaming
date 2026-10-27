"""Hermes display previews, kept separate from raw tool arguments/results."""
from __future__ import annotations

import math
import re
from urllib.parse import urlsplit


def safe_detail(value, limit=240):
    text = str(value or "")
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text, force=True)
    except Exception:
        pass
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = re.sub(r"(?i)(?:bearer\s+\S+|sk[-_][A-Za-z0-9_-]{8,}|(?:api[_-]?key|token|secret|password|authorization)\s*[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+))", "[已隐藏]", text)
    def url(match):
        try:
            parsed = urlsplit(match.group())
            return (parsed.hostname or "链接") + ("/…" if parsed.path or parsed.query else "")
        except ValueError:
            return "[链接已隐藏]"
    text = re.sub(r"https?://[^\s<>]+", url, text)
    text = re.sub(r"(?:file://)?(?:/(?:Users|home|opt|tmp|var|mnt|etc|workspace|work|private|root|acceptance-home)/|[A-Za-z]:[\\/]|~/)[^\s，。；]*", "[内部路径]", text)
    text = re.sub(r"\b(?:ou_|oc_|om_|cli_)[A-Za-z0-9]+\b", "[内部标识]", text)
    # Preserve operators and punctuation from the native preview. Markdown
    # escaping belongs to render.literal; deleting '*' silently changes 37*37
    # into 3737 and repeated sanitization used to damage redaction markers too.
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def native_preview(tool_name, args):
    # These fields can be source code, stored memory or typed credentials.
    # Native names/status remain visible without copying their payloads.
    if tool_name in {"memory", "session_search", "session_recall", "execute_code", "browser_exec", "browser_type"}:
        return ""
    try:
        from agent.display import prepare_tool_preview
        display_args = args
        omitted = False
        if tool_name == "terminal" and isinstance(args, dict):
            command = str(args.get("command", ""))
            if "\n" in command or "<<" in command:
                # Native shell summarization may flatten heredoc source or
                # draft text into its preview. Only pass its invocation line
                # to the display helper; execution still receives all args.
                first = command.lstrip().splitlines()[0] if command.strip() else ""
                display_args = dict(args, command=first.split("<<", 1)[0].rstrip())
                omitted = True
        preview = prepare_tool_preview(tool_name, display_args, fallback="", max_len=0)
        return safe_detail(preview.text, 180) + (" · 多行内容已省略" if omitted else "")
    except Exception:
        # Older hosts may lack the display helper; never substitute raw JSON.
        return ""


def native_outcome(tool_name, result):
    try:
        from agent.display import _detect_tool_failure
        failed, suffix = _detect_tool_failure(tool_name, result)
        return bool(failed), safe_detail(suffix, 120)
    except Exception:
        failed = isinstance(result, dict) and bool(result.get("error") or result.get("is_error") or result.get("success") is False)
        return failed, ""


def native_action(tool_name):
    """Use Hermes' public action label; never infer purpose from private text."""
    try:
        from agent.display import get_tool_verb
        return safe_detail(get_tool_verb(tool_name), 72)
    except Exception:
        return ""


def clean_tool_data(data):
    clean = {}
    for key, limit in (("preview", 240), ("outcome", 120), ("action", 72)):
        value = safe_detail(data.get(key), limit)
        if value:
            clean[key] = value
    for key in ("elapsed", "iteration"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            clean[key] = round(value, 1) if key == "elapsed" else int(value)
    return clean
