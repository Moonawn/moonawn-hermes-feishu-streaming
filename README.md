<div align="center">

# Hermes 飞书流式交互

### 看见进展，也看清答案。

**简体中文** · [English](README.en.md)

[效果预览](#效果预览) · [快速开始](#快速开始) · [安装接入](docs/installation.md) · [使用指南](docs/session-experience.md)

[![CI](https://github.com/Moonawn/moonawn-hermes-feishu-streaming/actions/workflows/ci.yml/badge.svg)](https://github.com/Moonawn/moonawn-hermes-feishu-streaming/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

</div>

**让 Hermes 在飞书里逐步回答、展示进展，并行处理不同任务。**

正文保持完整，执行过程按需展开。模型、Token、耗时和输出均速集中在底部；需要另一段上下文时，可以切换会话，也可以另开独立话题。

**当前为 `0.3.0rc8` 预发布版，兼容目标为 Hermes 0.22.1。**

![Hermes 飞书流式交互：流式答复与独立会话](assets/cover.png)

## 从一条消息开始

| 你在飞书里做的事 | 插件呈现的体验 |
| :--- | :--- |
| 发送一个任务 | 正文逐步出现，最终答案保留在同一张卡片 |
| 想了解运行进展 | 底部查看模型、Token、耗时和均速，展开 Agentloop 查看工具过程 |
| 想接着之前的话题聊 | 用 `/sessions` 选择历史会话，恢复对应上下文 |
| 还有另一项任务 | 另开独立话题并行处理；同一话题可用 `/queue` 依次执行 |

## 效果预览

正文与底部信息分层，过程默认收起，需要时再展开。

![流式答复与底部运行信息](assets/streaming.png)

<details>
<summary><strong>展开 Agentloop，查看调用过程与用量明细</strong></summary>

![Agentloop 展开后的分层明细](assets/agentloop.png)

按轮呈现模型阶段与工具调用，保留关键异常，合并重复的成功摘要。限制历史节点数量，避免长任务展开后堆满页面。

</details>

<details>
<summary><strong>切换会话，另开话题并行处理</strong></summary>

![会话列表与两个独立话题](assets/sessions.png)

会话使用 Hermes 原生上下文与权限。执行中的会话不能从面板原地切换，另开话题可继续处理其他任务。

</details>

以上界面图来自同一渲染器的离线演示，使用合成数据；实际字体、主题与组件外观由飞书客户端决定。

## 快速开始

需要 **Python 3.11+、Unix、Hermes 0.22.1**。运行时为 Python，复用宿主的原生 Feishu SDK、认证与权限。

```sh
git clone https://github.com/Moonawn/moonawn-hermes-feishu-streaming.git
cd moonawn-hermes-feishu-streaming
python3 scripts/verify.py
python3 scripts/build_release.py
```

产物为 `dist/moonawn-feishu-0.3.0rc8.zip`，附 SHA-256 清单。上面两步验证源码并构建插件包，不会自动接入机器人。

首次使用请按 [安装与回退说明](docs/installation.md) 接入独立测试实例。每进程一个 profile，使用进程内 Python plugin；避免多个流式插件同时接管 Feishu 答复。

## 把过程留在答案旁边

底部常驻 **Agentloop · 已耗时 · 模型 · Token · 输出均速**。详情中查看 API 次数、输入与输出用量、缓存及工具状态。统计来自原生回调和已回报的 usage，不按字数推算。

预计剩余时间随成功任务逐步积累：至少有 5 个满足当前进度的可比样本才显示范围；样本不足时显示积累进度。输出均速包含思考与工具等待，不代表模型的纯解码速度。

[交互与统计口径 →](docs/session-experience.md)

<details>
<summary><strong>开发验证与本地预览</strong></summary>

165 项离线测试及 Python 3.11–3.13 CI 已通过。固定 Hermes 宿主另有插件加载、卸载和 SDK smoke 检查。

```sh
python3 scripts/verify.py
python3 scripts/build_preview.py
# 用浏览器打开 artifacts/preview.html
```

HTML 仅用于离线交互预览，Playwright 脚本用于布局检查，均不参与 Python 插件运行。可选宿主与浏览器验证步骤见 [安装说明](docs/installation.md)。

</details>

## 当前范围

已在隔离测试环境验证会话切换、双话题并行、停止隔离、显式排队、统计、附件下载及 Chrome Web 交互。移动端、长期压力、多 profile 共进程与真实审批完整链路尚未完成验收。

[验收与限制](docs/acceptance.md) · [实现架构](docs/architecture.md)

---

参考 [OpenClaw 飞书官方插件](https://github.com/larksuite/openclaw-lark) 的卡片设计，以及此前 [HLS fork](https://github.com/Moonawn/hermes-lark-streaming)（[原项目](https://github.com/Aowen-Nowor/hermes-lark-streaming)）的维护经验。[来源说明](ACKNOWLEDGEMENTS.md) · [第三方许可](docs/THIRD_PARTY_NOTICES.md)

[使用指南](docs/session-experience.md) · [问题反馈](https://github.com/Moonawn/moonawn-hermes-feishu-streaming/issues) · [MIT License](LICENSE) · [English](README.en.md)

Copyright (c) 2026 Moonawn.
