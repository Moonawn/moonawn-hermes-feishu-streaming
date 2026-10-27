"""Per-run observations; no guessed token counts or loop-based percentages."""
from __future__ import annotations

import math
import re
import threading
import time

from .model import digest

COUNTERS = {
    "input": "session_prompt_tokens", "output": "session_completion_tokens",
    "cache_read": "session_cache_read_tokens", "cache_write": "session_cache_write_tokens",
    "reasoning": "session_reasoning_tokens", "api_calls": "session_api_calls",
}
TIMELINE_LIMIT = 5
HISTORY_LIMIT = 120
MIN_SAMPLES = 5


def tool_family(name):
    """A small public taxonomy, never arguments, commands or task text."""
    if name in {"terminal", "execute_code", "process"}: return "compute"
    if name in {"web_search", "web_extract", "search_files", "read_file", "skill_view"}: return "read"
    if str(name).startswith("browser_"): return "browser"
    if name in {"write_file", "patch", "image_generate", "video_generate", "text_to_speech"}: return "create"
    if name == "delegate_task": return "delegate"
    return "other"


class DurationHistory:
    """Bounded, local observations, partitioned by origin/model/task shape."""
    def __init__(self, store, namespace):
        self.store, self.namespace = store, namespace

    def key(self, model, tools):
        return "duration:" + digest(self.namespace + "\0" + model + "\0" + str(bool(tools)))

    def samples(self, model, tools):
        # v1 pairs remain usable as a coarse prior, without inventing features.
        key = self.key(model, tools)
        legacy = (self.store.operation(key) or {}).get("samples", [])
        current = (self.store.operation(key.replace("duration:", "duration-v2:", 1)) or {}).get("samples", [])
        rows = {}
        for item in (legacy[-40:] + current[-HISTORY_LIMIT:]):
            row = dict(item) if isinstance(item, dict) else {"run": item[0], "elapsed": item[1]} if isinstance(item, list) and len(item) == 2 else {}
            elapsed = row.get("elapsed")
            if isinstance(row.get("run"), str) and isinstance(elapsed, (int, float)) and math.isfinite(elapsed) and elapsed >= 1:
                rows[row['run']] = row
        return list(rows.values())[-HISTORY_LIMIT:]

    def record(self, run, model, tools, elapsed, *, shape=None):
        # Keep the v1 operation untouched so rolling back the plugin does not
        # require reverting a live database or exposing v2 records to v1 code.
        key = self.key(model, tools).replace("duration:", "duration-v2:", 1)
        rows = self.samples(model, tools)
        if any(x.get("run") == run for x in rows) or not math.isfinite(elapsed) or elapsed < 1:
            return False
        row = {"run": run, "elapsed": round(elapsed, 1)}
        if shape:
            row["shape"] = dict(shape)
        self.store.save_operation(key, {"version": 2, "samples": (rows + [row])[-HISTORY_LIMIT:]})
        return True

    def estimate(self, model, tools, elapsed, *, shape=None):
        rows = self.samples(model, tools)
        # A task already at loop 8 must not use completed one-loop tasks as
        # comparables. Legacy samples have no shape (and may contain deliberate
        # acceptance-test sleeps), so retain them without warming the new ETA.
        comparable = [r for r in rows if not shape or (r.get("shape") and
                      (r["shape"].get("iteration", 0) >= shape["iteration"] and
                       r["shape"].get("tools", 0) >= shape["tools"]))]
        similar = [r for r in comparable if shape and r.get("shape", {}).get("family") == shape["family"]]
        basis = "similar" if len(similar) >= MIN_SAMPLES else "general"
        selected = similar if basis == "similar" else comparable
        samples = sorted(r["elapsed"] for r in selected)
        facts = {"samples": len(samples), "total_samples": len(rows), "minimum": MIN_SAMPLES, "basis": basis}
        if len(samples) < MIN_SAMPLES:
            return {"state": "cold", **facts}
        low, high = samples[int((len(samples) - 1) * .2)], samples[math.ceil((len(samples) - 1) * .8)]
        if high > max(1, low) * 4:
            return {"state": "variable", **facts}
        if elapsed >= high:
            return {"state": "overdue", **facts}
        return {"state": "estimate", "low": max(0, math.ceil(low - elapsed)),
                "high": math.ceil(high - elapsed), **facts}


class TurnTelemetry:
    def __init__(self, history=None, run="", clock=time.monotonic):
        self.clock, self.history, self.run = clock, history, run
        self.started, self.ended = clock(), None
        self.lock = threading.RLock()
        self.agent = None
        self.baseline = {}
        self.latest_usage = None
        self.observed_usage = False
        self.iteration = 0
        self.tool_ids = set()
        self.completed = set()
        self._had_wait = False
        self.waiting = False
        self.families = set()
        self.model = "unknown"
        self.display_model = None
        self._frozen = None
        self.steps = []
        self._tool_round = {}

    def attach(self, agent):
        with self.lock:
            if self.agent is not None:
                return
            self.agent = agent
            self.model = str(getattr(agent, "provider", "")) + "/" + str(getattr(agent, "model", "unknown"))
            self.display_model = safe_model_name(getattr(agent, "model", None))
            self.baseline = {k: int(getattr(agent, name, 0) or 0) for k, name in COUNTERS.items()}
            self.latest_usage = getattr(agent, "_last_turn_usage", None)

    def step(self, iteration, previous_tools=None):
        with self.lock:
            iteration = int(iteration)
            if self._frozen is not None or iteration <= self.iteration:
                return
            now = self.clock()
            if self.steps:
                self._end_step(self.steps[-1], now)
            self.iteration = iteration
            self.steps.append({"iteration": iteration, "start": now, "model_end": None,
                               "end": None, "tools_started": 0, "tools_done": 0})
            self.steps = self.steps[-TIMELINE_LIMIT:]
            self.waiting = False

    @staticmethod
    def _end_step(step, now):
        step["end"] = now
        if step["model_end"] is None:
            step["model_end"] = now

    @property
    def waiting(self):
        return self._waiting

    @waiting.setter
    def waiting(self, value):
        self._waiting = bool(value)
        self._had_wait |= bool(value)

    def shape(self):
        return {"iteration": self.iteration, "tools": len(self.tool_ids),
                "family": next(iter(self.families)) if len(self.families) == 1 else "mixed" if self.families else "none"}

    def tool(self, call_id, *, done=False, name=None):
        with self.lock:
            if self._frozen is not None:
                return
            if name:
                self.families.add(tool_family(name))
            call_id = str(call_id)
            if call_id not in self.tool_ids and len(self.tool_ids) < 4096:
                self.tool_ids.add(call_id)
                # A completion without a start cannot reveal model timing.
                if not done and self.steps:
                    step = self.steps[-1]
                    self._tool_round[call_id] = step["iteration"]
                    step["tools_started"] += 1
                    if step["model_end"] is None:
                        step["model_end"] = self.clock()
            if done and call_id in self.tool_ids and call_id not in self.completed:
                self.completed.add(call_id)
                iteration = self._tool_round.pop(call_id, None)
                step = next((s for s in self.steps if s["iteration"] == iteration), None)
                if step:
                    step["tools_done"] += 1
            self.waiting = False

    def snapshot(self):
        with self.lock:
            if self._frozen is not None:
                return dict(self._frozen)
            elapsed = max(0, (self.ended if self.ended is not None else self.clock()) - self.started)
            counts = {k: max(0, int(getattr(self.agent, name, 0) or 0) - self.baseline.get(k, 0))
                      for k, name in COUNTERS.items()} if self.agent is not None else {}
            # A provider may omit usage. Cumulative counter deltas are reported
            # quantities, never an estimate of tokens still being generated.
            marker = getattr(self.agent, "_last_turn_usage", None)
            self.observed_usage |= bool(isinstance(marker, dict) and marker is not self.latest_usage)
            usage = ({k: counts[k] for k in COUNTERS if k != "api_calls"} if self.observed_usage else None)
            eta = {"state": "waiting"} if self.waiting else {"state": "cold", "samples": 0}
            if self.history and not self.waiting:
                eta = self.history.estimate(self.model, self.tool_ids, elapsed, shape=self.shape())
            # This is an end-to-end average of REPORTED output, including
            # thinking/tool waits; it is not the provider's decoding speed.
            rate = round(usage["output"] / elapsed, 1) if usage is not None and elapsed > 0 else None
            now = self.ended if self.ended is not None else self.clock()
            timeline = [{"iteration": s["iteration"],
                         "model_elapsed": round(max(0, (s["model_end"] if s["model_end"] is not None else now) - s["start"]), 1),
                         "model_active": s["model_end"] is None,
                         "tools_started": s["tools_started"], "tools_done": s["tools_done"],
                         "ended": s["end"] is not None} for s in self.steps]
            return {"elapsed": int(elapsed), "iteration": self.iteration,
                    "model": self.display_model, "output_rate": rate, "rate_basis": "turn_elapsed",
                    "api_calls": counts.get("api_calls", 0), "usage": usage,
                    "tools_started": len(self.tool_ids), "tools_done": len(self.completed),
                    "waiting": self.waiting, "eta": eta, "timeline": timeline}

    def finish(self, *, success=False):
        with self.lock:
            if self._frozen is not None:
                return dict(self._frozen)
            self.ended = self.clock()
            if self.steps:
                self._end_step(self.steps[-1], self.ended)
            self._frozen = self.snapshot()
            self._frozen["eta"] = {"state": "finished"}
            if self.history and self.agent is not None:
                recorded = False
                if success and not self._had_wait:
                    recorded = self.history.record(self.run, self.model, self.tool_ids,
                                                   self.ended - self.started, shape=self.shape())
                self._frozen["eta"].update(samples=sum(bool(r.get("shape")) for r in self.history.samples(self.model, self.tool_ids)),
                                           recorded=recorded, excluded_wait=self._had_wait)
            return dict(self._frozen)


def duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}分{seconds % 60:02d}秒" if seconds >= 60 else f"{seconds}秒"


def safe_model_name(value):
    """Only a public model identifier, never an endpoint, path or credentials."""
    if value is None or str(value) in {"", "unknown"}:
        return None
    value = str(value)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}", value)
            or "://" in value or value.count("/") > 1
            or re.search(r"(?i)(?:^|/)sk[-_]", value)):
        return "已隐藏"
    return value


def metric_lines(metrics):
    metrics = metrics or {}
    finished = metrics.get("eta", {}).get("state") == "finished"
    missing = "未回报" if finished else "等待回报"
    model = safe_model_name(metrics.get("model")) or f"模型{missing}"
    usage = metrics.get("usage")
    elapsed = duration(metrics["elapsed"]) if "elapsed" in metrics else "—"
    rate = metrics.get("output_rate") if usage is not None and metrics.get("rate_basis") == "turn_elapsed" else None
    speed = f"{rate:,.1f}" if rate is not None else "—"
    total = f"{usage['input'] + usage['output']:,}" if usage is not None else missing
    loop = str(metrics['iteration']) if metrics.get("iteration") else "待启动"
    return [f"已耗时 {elapsed} · {model} · Token {total} · 输出均速 {speed} tok/s"]


def metric_details(metrics):
    """Secondary observations stay bounded and separate from the status line."""
    if not metrics:
        return []
    loop = str(metrics['iteration']) if metrics.get("iteration") else "待启动"
    lines = [f"Agent loop {loop} · API {metrics.get('api_calls', 0)} · 工具 {metrics.get('tools_done', 0)}/{metrics.get('tools_started', 0)}"]
    usage = metrics.get("usage")
    if usage is not None:
        lines.append(f"Token 输入 {usage['input']:,} / 输出 {usage['output']:,}")
        fields = [("缓存读取", "cache_read"), ("缓存写入", "cache_write"), ("推理", "reasoning")]
        parts = [f"{label} {usage[key]:,}" for label, key in fields if usage.get(key)]
        if parts:
            lines.append("其中 " + " · ".join(parts) + "（不重复相加）")
    else:
        lines.append("Token 尚未回报；不根据字数估算")
    eta = metrics.get("eta", {})
    state = eta.get("state")
    if state == "estimate":
        basis = "相近任务" if eta.get("basis") == "similar" else "同类总体参考"
        lines.append(f"预计还需 {duration(eta['low'])}–{duration(eta['high'])} · {basis} {eta['samples']} 次，可能变化")
    elif state == "overdue":
        lines.append(f"耗时已超出参考区间 · 仍在执行 · 已积累 {eta.get('total_samples', eta.get('samples', 0))} 次")
    elif state == "variable":
        lines.append(f"预计剩余暂不展示 · {eta['samples']} 次参考耗时差异较大，继续积累")
    elif state == "waiting":
        lines.append("等待确认 · 暂停估时")
    elif state != "finished":
        lines.append(f"预计剩余 · 积累中 {eta.get('samples', 0)}/{eta.get('minimum', MIN_SAMPLES)} 次可比任务 · 暂不估算")
    elif "samples" in eta:
        lines.append(f"耗时参考已积累 {eta['samples']} 次" + (" · 本次已记入" if eta.get("recorded") else " · 本次等待确认，未计入" if eta.get("excluded_wait") else ""))
    lines.append("输出均速包含思考和工具等待时间")
    return lines


def remaining_time_detail(metrics):
    """Keep the requested ETA inside the disclosure, away from its one-line title."""
    if not metrics or metrics.get("eta", {}).get("state") == "finished":
        return ""
    return next((line for line in metric_details(metrics)
                 if line.startswith(("预计", "耗时已超出", "等待确认"))), "")
