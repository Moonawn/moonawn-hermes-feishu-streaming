# README 图片

- `cover.png`：通过内置 `image_gen` 生成的概念封面，延续 Moonawn 仓库的暖色材质与大字排版；不是产品界面截图。
- `streaming.png`、`agentloop.png`、`sessions.png`：使用项目离线预览的合成事件，经 Playwright 截取渲染区域；不含真实账号或聊天。
- `feishu-web.png`、`feishu-web-demo.gif`：2026-10-28 在 Chrome 飞书 Web 中操作 `0.3.0rc9` 专用测试机器人。任务使用虚构活动数据，实际运行模型及两次工具调用；保留原始数字与文字。图片只裁取卡片正文和明细区域，排除账号、群名、引用栏及侧栏。GIF 将收起、轮次、单次调用三张截图对齐后轮播；不是连续录屏或速度基准。截图保留实际指针显示。

<details>
<summary>封面生成提示词</summary>

```text
Use case: ads-marketing
Asset type: wide GitHub README cover, landscape 2:1.
Create an elegant editorial cover for the independent Python project "Hermes 飞书流式交互". Match the established Moonawn repository cover aesthetic: warm ivory paper surface, soft peach daylight and delicate out-of-focus botanical shadows, very large confident black sans-serif typography on the left, tactile small sculptural objects on the right, lots of clear space. Premium restrained photographic 3D art direction, readable even at 900px wide.
On the left, exact text in this hierarchy:
"Hermes"
"Feishu"
"飞书流式交互"
"看见进展，也看清答案。"
Hermes and Feishu are the two bold headline lines, Chinese title smaller, final Chinese tagline muted. No additional text.
On the right: a sculptural composition of one tall frosted mint message-card tile with a few engraved horizontal lines and a tiny bottom row of three indicator dots, and two smaller warm-white paper message cards running along two separate thin brushed-metal tracks. Small brass connectors. This is a conceptual illustration of streamed answers, a compact activity footer and parallel conversations, NOT a screenshot or a literal UI mockup. Show coherence, precise spacing, soft shadows, subtle grain, understated material realism. Composition follows a wide repository cover with text occupying roughly the left half, objects on the right half.
Avoid gradients in lettering, neon, purple, logos of third parties, badges, watermarks, extra paragraphs, code, literal app screenshots, terminal windows, blue tech wallpaper. No claims about speed or official status.
```

</details>
