"""Build a self-contained preview from real, entirely offline engine replays."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from moonawn_feishu.engine import Engine
from moonawn_feishu.fake_lark import FakeLark
from moonawn_feishu.model import Event, Identity, Route
from moonawn_feishu.store import Store
from moonawn_feishu.controls import panel_card
from moonawn_feishu.telemetry import TurnTelemetry, DurationHistory


async def scenario(name, title, subtitle):
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / "preview.db")
        fake = FakeLark()
        engine = Engine(store, fake)
        identity = Identity(Route("demo", "demo", "demo", "demo"), name)
        frames = []
        seq = 0

        async def emit(kind, text="", caption="", **kwargs):
            nonlocal seq
            engine.accept(Event(identity, f"{name}:{identity.phase}:{seq}", seq, kind, text, **kwargs))
            seq += 1
            await engine.sync(identity.key)
            capture(caption)

        def capture(caption):
            phases = sorted(engine.phases.values(), key=lambda p: p.identity.phase)
            frames.append({
                "caption": caption,
                # Show what the fake server actually committed, not desired state.
                "cards": [deepcopy(c["card"]) for c in fake.cards.values()],
                "texts": [m["text"] for m in fake.messages.values()],
                "phases": [{"phase": p.identity.phase, "delivery": p.delivery,
                            "seal": p.seal, "terminal": p.terminal, "diagnostic": p.diagnostic}
                           for p in phases],
                "calls": dict(fake.counts),
            })

        await emit("begin", caption="消息已接收，先给出安静、明确的状态。")
        if name == "reading":
            await emit("snapshot", "可以先建立一个小而稳定的版本。", "正文出现后成为视觉中心。")
            await emit("snapshot", "可以先建立一个小而稳定的版本。\n\n**先保证完整，再优化速度。**", "累计正文增量呈现，不重复发送整条消息。")
            await emit("final", "可以先建立一个小而稳定的版本。\n\n**先保证完整，再优化速度。**\n\n- 主回复生成完毕后保持不变。\n- 新的补充单独呈现，方便回看。\n- 工具过程默认收起，需要时再展开。\n\n这样既能及时看到进展，也能留下清晰的最终答案。",
                       "最终正文已回读一致，卡片完成封口。")
        elif name == "tools":
            await emit("tool", "阅读参考文档", "过程提示收进可展开的区域。", tool_id="read", tool_state="running")
            await emit("snapshot", "我正在整理不同方案的共同点。", "正文与执行过程分层。")
            await emit("tool", "阅读参考文档", "完成状态替换进行状态，不堆叠同一操作。", tool_id="read", tool_state="done")
            await emit("tool", "对照接口边界", "第二项操作仍位于同一个折叠区域。", tool_id="compare", tool_state="done")
            await emit("final", "**先把回答交付做稳定。**\n\n保留原有消息入口，将正文、执行过程和最终确认分别处理。流式更新只提交最新的可见状态，结束时核对完整正文。",
                       "主回复已冻结，后续内容不会覆盖它。")
            identity = replace(identity, phase=1)
            seq = 0
            await emit("begin", caption="显式开启一个补充阶段。")
            await emit("final", "补充一个细节：若发送结果暂时未知，应先核对原消息，再决定下一步。这样可以避免重复答复。",
                       "补充独立成卡；上面的主回复保留完整。")
        elif name == "uncertain":
            await emit("snapshot", "先保留当前已经生成的内容。", "已有一张卡片，接下来模拟终稿更新回执丢失。")
            fake.fail_next("update", "lost_ack")
            await emit("final", "最终内容已经生成。\n\n**即使更新回执丢失，也先核对原消息。**\n\n本次模拟中，服务器已写入终稿，只是确认响应没有返回。",
                       "服务器已写入，但客户端尚未确认；不另发备用文本。")
            await engine.sync(identity.key)
            capture("原卡回读与终稿一致，确认后封口。整个过程仍只有一份答复。")
        elif name == "stopped":
            await emit("snapshot", "目前已整理出两点：\n\n- 先保留完整正文。\n- 将执行过程放在次要位置。", "模拟用户在过程中停止。")
            await emit("cancel", caption="停止后保留已生成的内容，明确标记未继续执行。")
            await emit("snapshot", "迟到内容，不应出现", "迟到的回调被忽略，原内容保持不变。")
        elif name == "document":
            await emit("snapshot", "下面用一个小例子说明结构。", "复杂内容也沿用同一阅读样式。")
            fence = chr(96) * 3
            body = ("**把内容层与投递层分开。**\n\n"
                    "| 内容 | 呈现方式 |\n| --- | --- |\n| 正文 | 默认展开 |\n| 执行过程 | 默认折叠 |\n| 补充 | 独立阶段 |\n\n"
                    + fence + 'python\nreply.final("完整答案")\n' + fence
                    + "\n\n中文、空格、表格与代码都保留原文。较宽的代码只在代码区域内横向滚动。")
            await emit("final", body, "Markdown 原文完整保留，页面不横向溢出。")
        elif name == "failure":
            await emit("tool", "检查资料访问权限", "过程中的失败保留明确状态。", tool_id="read", tool_state="failed")
            await emit("error", "本次没有完成资料整理。\n\n目前无法读取需要的文档。你可以补充可访问的资料，再继续处理。\n\n已经完成的工作会保留，未完成的部分不会显示为成功。", "失败只呈现一次，不再重复追加相同兜底消息。")
            capture("相同错误回调不会产生第二份答复。")
        elif name == "long":
            await emit("snapshot", "正在整理较长的答复。", "较长正文先呈现简短进展。")
            paragraph = "阅读时保留段落之间的关系，让每一个结论都有完整的说明。正文超过卡片预算时，按顺序分段保留全部内容。中文、换行、标点与末尾都保持完整，不用截断换取表面上的成功。"
            body = "\n\n".join(f"{n + 1}. {paragraph}" for n in range(120)) + "\n\n全文结束 · END"
            await emit("final", body, "旧预览说明转为分段呈现；下方保留完整正文与最终尾标。")
        elif name == "queued":
            await emit("final", "**任务 A 已完成。**\n\n这份答复保留任务 A 的完整结果。", "第一份任务先完成。")
            identity = replace(identity, turn="queued-second-task")
            seq = 0
            await emit("begin", caption="第二份独立入站使用自己的投递身份。")
            await emit("final", "**任务 B 已完成。**\n\n排队任务独立答复，保留自己的结果和原请求归属。", "两份任务各有一份完整答复，互不覆盖。")
        elif name == "telemetry":
            now = [0.0]
            history = DurationHistory(store, "demo")
            for i, seconds in enumerate([40, 55, 65, 80, 95]):
                history.record(f"demo-{i}", "demo/deepseek-flash", True, seconds,
                               shape={"iteration": 2, "tools": 1, "family": "none"})
            agent = SimpleNamespace(provider="demo", model="deepseek-flash", session_prompt_tokens=10000,
                                    session_completion_tokens=1000, session_api_calls=10, _last_turn_usage=None)
            meter = TurnTelemetry(history, "demo-run", clock=lambda: now[0])
            meter.attach(agent)
            meter.step(1)
            await emit("snapshot", "我正在核对资料中的关键结论。", "正文渐进出现；此时提供方尚未回报 token。")
            now[0] = 8
            await emit("metrics", data=meter.snapshot(), caption="token 不从字数猜测；Agent loop 与 API 返回次数分开。")
            agent.session_prompt_tokens += 3260
            agent.session_completion_tokens += 420
            agent.session_api_calls += 1
            agent._last_turn_usage = {"reported": True}
            meter.step(2)
            meter.tool("read")
            now[0] = 21
            await emit("tool", "核对参考资料", tool_id="read", tool_state="running", data={"iteration": 2, "preview": "比较资料中的关键结论"})
            await emit("metrics", data=meter.snapshot(), caption="模拟历史样本给出剩余区间；不把轮次上限当作完成度。")
            await emit("snapshot", "我正在核对资料中的关键结论。\n\n已经找到一致的部分，接下来整理分歧与建议。", "打字机只负责呈现新增长的正文。")
            now[0] = 36
            meter.tool("read", done=True)
            await emit("tool", "核对参考资料", tool_id="read", tool_state="done", data={"iteration": 2, "elapsed": 28, "outcome": "已完成"})
            agent.session_prompt_tokens += 4400
            agent.session_completion_tokens += 680
            agent.session_api_calls += 1
            await emit("metrics", data=meter.finish(success=True), caption="本次运行统计冻结，历史会话累计不混入。")
            await emit("final", "**资料已经整理完成。**\n\n主要结论、依据与分歧已分别列出。可以继续在当前会话追问，也可以打开会话面板，另开一个独立话题。", "最终内容一次补齐，终稿不排队等待打字动画。")
        store.close()
        return {"id": name, "title": title, "subtitle": subtitle, "frames": frames}


async def main():
    cases = [
        ("reading", "日常答复", "让正文成为中心"),
        ("tools", "工具与补充", "过程收起，主回复保留"),
        ("uncertain", "回执丢失", "先核对，避免双发"),
        ("stopped", "主动停止", "保留已经生成的内容"),
        ("document", "表格与代码", "保持原文与阅读节奏"),
        ("failure", "执行未完成", "失败清楚呈现一次"),
        ("long", "长文分段", "完整保留直到末尾"),
        ("queued", "独立任务", "各自答复，互不覆盖"),
        ("telemetry", "运行统计", "底部常驻 · 模型、用量与速率"),
    ]
    scenarios = [await scenario(*case) for case in cases]
    rows = [{"id": "demo-one", "title": "资料整理"}, {"id": "demo-two", "title": "方案讨论"}]
    demo_frames = []
    for mode in range(5):
        items = rows + ([{"id": "demo-new", "title": "新会话"}] if mode == 2 else [])
        actions = {k: "demo:" + k for k in ("topic", "new", "refresh", "typing")}
        actions.update({f"switch:{i}": f"demo:switch:{i}" for i in range(len(items))})
        current = "demo-two" if mode == 1 else "demo-new" if mode == 2 else "demo-one"
        card = panel_card(items, current, actions, busy=mode == 3, typing=mode != 4,
                          notice="离线交互示例 · 点击按钮体验会话切换与独立话题。")
        cards, phases = [card], []
        if mode == 3:
            from moonawn_feishu.render import render
            from moonawn_feishu.model import Phase
            for n, text in enumerate(["资料整理正在继续，原有上下文保持独立。", "新的话题正在准备另一项任务，两个话题可以同时运行。"]):
                identity = Identity(Route("demo", "demo", "demo", "demo", f"topic-{n}", f"anchor-{n}"), f"run-{n}")
                p = Phase(identity, body=text, status="working")
                cards.append(render(p))
        demo_frames.append({"caption": ["点击继续可切换历史会话。", "当前会话已切换为方案讨论（模拟）。", "已新建一个空会话（模拟）。", "两个话题各自执行，运行中的会话禁止原地切换。", "打字机已关闭，下次回答立即呈现新文本（模拟）。"][mode],
                            "cards": cards, "texts": [], "phases": phases, "calls": {}})
    scenarios.append({"id": "sessions", "title": "会话工作台", "subtitle": "切换历史 · 独立话题并行", "frames": demo_frames})
    verification = json.loads((ROOT / "artifacts/verification.json").read_text())
    data = {"scenarios": scenarios, "tests": verification["tests_run"],
            "benchmark": verification["benchmark"]}
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    (ROOT / "artifacts/preview-data.json").write_text(encoded + "\n")
    template = (ROOT / "preview/template.html").read_text()
    (ROOT / "artifacts/preview.html").write_text(
        template.replace("__PREVIEW_DATA__", encoded.replace("<", "\\u003c")).replace("__RIPPLE_DATA__", "data:image/gif;base64," + __import__("base64").b64encode(__import__("moonawn_feishu.activity_asset",fromlist=["ripple_bytes"]).ripple_bytes()).decode()))
    print(f"Built {len(scenarios)} scenarios / {sum(len(s['frames']) for s in scenarios)} frames")


if __name__ == "__main__":
    asyncio.run(main())
