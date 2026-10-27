"""Readable CardKit 2.0: stable body, quiet status and collapsed activity."""
from __future__ import annotations

import json
from html import escape
from copy import deepcopy
import re
from .model import Phase, digest
from .telemetry import TIMELINE_LIMIT, metric_lines, metric_details, remaining_time_detail

CARD_BYTES = 24_000  # Conservative project budget, not an asserted Feishu API limit.
ACTIVITY_VISIBLE_TOOLS = 12


class CardBudgetExceeded(ValueError):
    pass


def safe_tool_label(value: str) -> str:
    """Tool chrome is public UI, never a raw command/argument/result log."""
    value = re.sub(r"(?i)(?:bearer\s+\S+|sk[-_][A-Za-z0-9_-]{8,}|(?:api[_-]?key|token|secret)\s*[:=]\s*\S+)", "[已隐藏]", str(value))
    value = re.sub(r"(?:file://)?/(?:Users|home|opt|tmp|var|mnt|etc)/[^\s，。；]*", "[内部路径]", value)
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"[`*\[\]{}<>\\]", "", value)
    return " ".join(value.split())[:80] or "执行工具"


def summary_text(value: str) -> str:
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"[`#*_]", "", value)
    return " ".join(value.split())[:80]


def literal(value):
    text = escape(str(value))
    for char in "_*`[]\\":
        text = text.replace(char, f"&#{ord(char)};")
    return text


def md(content):
    return {"tag": "markdown", "content": content, "text_size": "notation"}


def disclosure(title, elements, *, element_id):
    return {"tag": "collapsible_panel", "element_id": element_id, "expanded": False,
            "header": {"title": md(title),
                       "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "color": "grey", "size": "16px 16px"},
                       "icon_position": "right", "icon_expanded_angle": -180,
                       "padding": "4px 0px 4px 0px"},
            "padding": "0px", "vertical_spacing": "4px", "elements": elements}


def tool_rows(tools, terminal):
    """Collapse only identical successful facts, never failures or active work."""
    groups = []
    for tool in tools:
        key = tuple(tool.get(k) for k in ("label", "action", "preview", "outcome"))
        previous = next((g for g in groups if g[0] == key and g[1][0]["state"] == "done"), None)
        if tool["state"] == "done" and previous:
            previous[1].append(tool)
        else:
            groups.append((key, [tool]))
    rows = []
    for _, calls in groups:
        tool = calls[0]
        state = tool["state"]
        label = literal(safe_tool_label(tool["label"]))
        status = {"done": "✓ 完成", "failed": "调用未完成", "running": "状态未回传" if terminal else "执行中"}[state]
        elapsed = sum(t.get("elapsed", 0) for t in calls)
        timing = (f" · {'累计 ' if len(calls) > 1 else ''}{elapsed:.1f}s" if any("elapsed" in t for t in calls) else "")
        count = f" ×{len(calls)}" if len(calls) > 1 else ""
        action = literal(tool.get("action", ""))
        preview = literal(tool.get("preview", ""))
        outcome = literal(tool.get("outcome", ""))
        lead = f"**{label}{count}** · {status}{timing}"
        if state == "failed":
            lead = f"<font color='red'>{lead}</font>"
        # The native action and preview form one compact detail line; failure
        # suffix stays explicit instead of being hidden by parameter text.
        detail = " · ".join(x for x in (action, preview) if x)
        if outcome:
            lead += " · " + outcome
        rows.append(md(lead + (f"\n<font color='grey'>{detail}</font>" if detail else "")))
    return rows


def activity_rows(phase, metrics):
    """Only five recent native rounds and twelve retained tool details enter JSON.

    Collapsing alone is not a size limit: old rounds never enter the client DOM.
    Model intervals are callback boundaries, not private reasoning transcripts.
    """
    groups = {}
    for tool in list(phase.tools.values())[-ACTIVITY_VISIBLE_TOOLS:]:
        groups.setdefault(tool.get("iteration", 0), []).append(tool)
    observations = {s["iteration"]: s for s in metrics.get("timeline", [])[-TIMELINE_LIMIT:]}
    current = metrics.get("iteration", 0)
    iterations = sorted(set(groups) | set(observations) | ({current} if current else set()))[-TIMELINE_LIMIT:]
    rows, shown = [], 0
    # Older important calls stay visible without reintroducing all old rounds.
    earlier = [t for t in phase.tools.values() if t.get("iteration", 0) not in iterations]
    notable = [t for t in earlier if t["state"] != "done" or t.get("elapsed", 0) >= 10][:3]
    if notable:
        rows.append(disclosure("**较早关键调用** · 异常／未回传／耗时较长", tool_rows(notable, phase.terminal), element_id="loop_important"))
        shown += len(notable)
    failed = sum(t["state"] == "failed" for t in phase.tools.values())
    active = sum(t["state"] == "running" for t in phase.tools.values())
    if failed:
        rows.append(md(f"<font color='red'>留存明细中有 {failed} 项调用未完成</font> · 具体原因见对应轮次"))
    elif active and not phase.terminal:
        rows.append(md(f"正在执行 {active} 项工具调用"))
    remaining = remaining_time_detail(metrics)
    if remaining:
        rows.append(md(remaining))
    for iteration in iterations:
        tools = groups.get(iteration, [])
        step = observations.get(iteration)
        number = f"{iteration:02d}" if iteration else "—"
        model = f"思考／生成 {step['model_elapsed']:.1f}s" if step else "模型阶段未记录"
        total = max(len(tools), step.get("tools_started", 0) if step else 0)
        details = tool_rows(tools, phase.terminal)
        shown += len(tools)
        if total == 1 and len(tools) == 1:
            tool = tools[0]
            state = {"done": "✓", "failed": "!", "running": "未回传" if phase.terminal else "执行中"}[tool["state"]]
            end = literal(safe_tool_label(tool["label"])) + " " + state
            if "elapsed" in tool:
                end += f" {tool['elapsed']:.1f}s"
        elif total:
            done = step.get("tools_done", 0) if step else sum(t["state"] != "running" for t in tools)
            failed = sum(t["state"] == "failed" for t in tools)
            end = f"工具 {done}/{total}" + (f" · {failed} 项未完成" if failed else "")
        else:
            end = ({"final": "答复完成", "cancel": "已停止", "error": "执行未完成"}.get(phase.terminal)
                   if iteration == current else None) or ("等待选择" if phase.status == "waiting" else "生成中" if step and step.get("model_active") else "本轮结束" if step and step.get("ended") else "阶段待回报")
        title = f"<font color='grey'>{number}</font>  {model}  <font color='grey'>→</font>  {end}"
        if total > len(tools):
            details.append(md(f"本轮 {total} 次调用，仅保留 {len(tools)} 条关键或近期摘要。"))
        # One compact line per round. Secondary details are independently closed.
        rows.append(disclosure(title, details, element_id=f"loop_{iteration}") if details else md(title))
    count = ("至少 " if phase.tool_count_capped else "") + str(phase.tool_count)
    if (current > len(iterations) or phase.tool_count > shown or phase.tool_count_capped):
        rows.append(md(f"共 {current or '—'} 轮 · {count} 次调用 · 展示最近 {len(iterations)} 轮与 {shown} 条留存摘要"))
    if not rows:
        rows = [md("本轮未调用工具。" if phase.terminal else "等待原生阶段事件。")]
    facts = metric_details(metrics)
    if facts:
        # Avoid repeating the loop header/ETA; token subitems are useful on
        # demand but do not compete with execution progress.
        facts = [line for line in facts[1:] if not line.startswith(("预计", "耗时已超出", "等待确认"))]
        facts.insert(0, f"API 已返回 {metrics.get('api_calls', 0)} 次 · 工具已回传 {metrics.get('tools_done', 0)}/{metrics.get('tools_started', 0)}")
        rows.append(disclosure("用量与耗时依据", [md("\n".join(literal(x) for x in facts))], element_id="loop_usage"))
    return rows


def render(phase: Phase, *, ripple_key: str = "") -> dict:
    labels = {"thinking": "正在思考", "working": "正在整理", "waiting": "等待你的选择"}
    status = {"final": "答复完成", "cancel": "已停止", "error": "本次执行未完成"}.get(
        phase.terminal, labels[phase.status])
    if phase.identity.phase:
        status = f"补充答复 {phase.identity.phase} · {status}"
    if not phase.terminal and phase.status != "waiting" and any(t["state"] == "running" for t in phase.tools.values()):
        status = "工具执行中"
    ornament = "<font color='blue'>✦</font>  **HERMES**  <font color='grey'>/  " + status + "</font>"
    elements = [{"tag": "markdown", "element_id": "status", "content": ornament, "text_size": "notation"}]
    if phase.body:
        elements.append({"tag": "markdown", "element_id": "answer", "content": phase.body, "text_size": "normal_v2"})
    if phase.terminal in {"cancel", "error"}:
        elements.append({"tag": "markdown", "element_id": "notice", "text_size": "notation",
                         "content": "已有内容已保留。" if phase.body else "本次未生成完整答复。"})
    metrics = dict(phase.metrics or {})
    if phase.terminal:
        metrics["eta"] = {**metrics.get("eta", {}), "state": "finished"}
    # The summary IS the disclosure header: one footer line, no second panel.
    line = metric_lines(metrics)[0]
    content = escape(line).replace("_", "&#95;")
    parts = content.split(" · ")
    parts[1] = f"**{parts[1]}**"
    parts[2] = re.sub(r"^Token ([\d,]+)", r"Token **\1**", parts[2])
    loop = str(metrics["iteration"]) if metrics.get("iteration") else "待启动"
    title = "<font color='grey'>**Agentloop " + loop + "** · " + " · ".join(parts) + "</font>"
    rows = activity_rows(phase, metrics)
    elements.append({"tag": "hr", "element_id": "metrics_rule"})
    elements.append(disclosure(title, rows, element_id="metrics"))
    active = not phase.terminal and phase.seal != "closed" and phase.status != "waiting"
    title_node = elements[-1]["header"]["title"]
    if ripple_key and active and phase.typing:
        title_node["icon"] = {"tag": "custom_icon", "img_key": ripple_key}
    else:
        # Static terminal/waiting/reduced-motion fallback; no frame timer or API writes.
        title_node["content"] = ("✓ " if phase.terminal == "final" else "• ") + title_node["content"]
    doc = {"schema": "2.0", "config": {"update_multi": True, "streaming_mode": phase.seal != "closed" and phase.typing,
                                         "summary": {"content": summary_text(phase.body) or status},
                                         "streaming_config": {"print_frequency_ms": {"default": 50},
                                                              "print_step": {"default": 2}, "print_strategy": "fast"}},
           "body": {"direction": "vertical", "padding": "16px 16px 12px 16px", "vertical_spacing": "12px", "elements": elements}}
    if len(json.dumps(doc, ensure_ascii=False).encode()) > CARD_BYTES:
        raise CardBudgetExceeded("full content exceeds project card budget")
    return doc


def body_text(card: dict) -> str:
    return next((e["content"] for e in card.get("body", {}).get("elements", []) if e.get("element_id") == "answer"), "")


def feishu_markdown_projection(text: str) -> str:
    """One-way, narrowly observed user_card_content formatting conversion.

    Real Feishu readback joins a fenced block to the preceding paragraph,
    doubles code-body newlines, projects heading/rule boundaries, expands
    table separators and removes the blank line after a table. This does not
    strip arbitrary whitespace or change code, cell contents or alignment.
    Unsupported conversions still require the lossless fallback.
    """
    def outside(part):
        lines = part.splitlines(keepends=True)
        heading = lambda line: bool(re.match(r"^#{1,6} \S", line))
        table = lambda line: line.strip().startswith("|") and line.strip().endswith("|")
        list_item = lambda line: bool(re.match(r"^(?:[-+*]|\d+[.)]) \S", line))
        rules = {i for i, line in enumerate(lines) if line.rstrip("\n") == "---"
                 and (i == 0 or lines[i - 1] == "\n")}
        original = list(lines)
        for i, line in enumerate(original):
            if i in rules:
                lines[i] = "<hr>" + ("\n" if line.endswith("\n") else "")
            elif line == "\n" and 0 < i < len(lines) - 1:
                prev, after = original[i - 1], original[i + 1]
                # CardKit preserves boundaries between block constructs, but
                # joins headings and list endings to ordinary paragraphs.
                if ((i + 1 in rules and not list_item(prev)) or
                    (heading(after) and not heading(prev) and not table(prev) and not list_item(prev) and i - 1 not in rules) or
                    (heading(prev) and not heading(after) and not table(after)) or
                    (table(after) and prev.strip() and not heading(prev) and not list_item(prev)
                     and not table(prev) and i - 1 not in rules) or
                    (i - 1 in rules and not after.startswith(("|", "#"))) or
                    (list_item(after) and prev.rstrip().endswith(("：", ":"))
                     and not heading(prev) and not table(prev) and not list_item(prev)) or
                    (list_item(prev) and not list_item(after) and i + 1 not in rules
                     and not heading(after) and not table(after))):
                    lines[i] = ""
        for i, line in enumerate(lines):
            if i == 0 or not lines[i - 1].strip().startswith("|"):
                continue
            stripped = line.strip()
            if not (stripped.startswith("|") and stripped.endswith("|")):
                continue
            cells = [c.strip() for c in stripped[1:-1].split("|")]
            if not cells or not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                continue
            cells = [(":" if c.startswith(":") else "") + "--------" + (":" if c.endswith(":") else "") for c in cells]
            lines[i] = "| " + " | ".join(cells) + " |" + ("\n" if line.endswith("\n") else "")
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("|") and lines[j].strip().endswith("|"):
                j += 1
            if (j > i + 1 and j + 1 < len(lines) and lines[j] == "\n"
                    and lines[j + 1].strip() and not heading(lines[j + 1])):
                lines[j] = ""
        # A heading immediately followed by a table gains a blank line in
        # user_card_content, even when the source omitted that boundary.
        result = "".join(line + ("\n" if heading(line) and i + 1 < len(lines) and table(lines[i + 1]) else "")
                         for i, line in enumerate(lines))
        # A list ending the answer is serialized with its closing newline.
        if lines and list_item(lines[-1]) and not result.endswith("\n"):
            result += "\n"
        return result

    result, end = [], 0
    pattern = re.compile(r"^```[A-Za-z0-9_+.-]*\n[\s\S]*?^```(?=\n|$)", re.MULTILINE)
    for match in pattern.finditer(text):
        prefix = outside(text[end:match.start()])
        if prefix.endswith("\n\n"):
            tail = prefix.rstrip("\n")
            prefix = tail + ("\n" if tail.split("\n")[-1].startswith("|") else "")
        block = match.group()
        opening, code = block[:-3].split("\n", 1)
        result.extend((prefix, opening + "\n" + code.replace("\n", "\n\n") + "```"))
        end = match.end()
    result.append(outside(text[end:]))
    projected = "".join(result)
    # A heading at the start of the answer gets one leading line in the
    # actual CardKit projection; interior headings use the boundaries above.
    return ("\n" if re.match(r"^#{1,6} \S", text) else "") + projected


def fingerprint(card: dict) -> str:
    # Streaming closure is an independent transport state, not a body change.
    normalized = dict(card)
    normalized["config"] = {k: v for k, v in card.get("config", {}).items() if k != "streaming_mode"}
    return digest(json.dumps(normalized, ensure_ascii=False, sort_keys=True))


def structure_fingerprint(card: dict) -> str:
    """Ignore changing answer text and preview summary, retain every other field."""
    normalized = deepcopy(card)
    normalized.get("config", {}).pop("summary", None)
    for element in normalized.get("body", {}).get("elements", []):
        if element.get("element_id") == "answer":
            element["content"] = ""
    return fingerprint(normalized)


def chunks(text: str, size: int = 3000) -> list[str]:
    """Lossless Unicode slices, including whitespace; no Markdown rewriting."""
    return [text[i:i + size] for i in range(0, len(text), size)]
