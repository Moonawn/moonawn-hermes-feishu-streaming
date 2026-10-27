# Hermes 飞书流式交互

基于 Python 的 Hermes 飞书流式交互插件，支持流式卡片、多会话与并行任务、Agentloop 展示及 Token／耗时统计。

**仓库：** `moonawn-hermes-feishu-streaming` · **版本：** `0.3.0rc8`（预发布）· **许可证：** [MIT](LICENSE)

面向已有 Hermes 飞书机器人的流式体验扩展。复用 Hermes 原生会话、入站权限、认证、媒体与审批能力；Python 内核负责卡片呈现、投递确认与恢复。运行时无需 Node.js，也不加载其他流式插件的引擎。

## 能力

- **流式答复**：累计正文合并刷新、客户端打字机效果、完整终稿回读与封口；停止或失败后保留已有内容。
- **会话与任务**：`/sessions` 卡片切换历史会话、新建会话、另开独立话题；复用 Hermes 并发限制与显式 `/queue`。
- **底部信息栏**：Agentloop、已耗时、模型、Token、平均输出速率；与正文分层，窄卡片可自然换行。
- **分层执行详情**：展示原生工具动作及脱敏预览，按轮折叠、合并重复成功摘要、保留较早关键异常；限制节点数量。
- **经验估时**：成功任务持续积累，至少 5 个满足当前进度的可比样本才显示范围；样本不足或差异过大时不猜倒计时。
- **投递恢复**：独立 SQLite outbox、单 writer、未知 ACK 核对、防止重复答复、长文分段保留全文。

## 开始使用

运行环境：**Python 3.11+、Unix、Hermes 0.22.1、进程内 Python plugin**。每进程一个 profile；Feishu SDK 与凭据由宿主提供。插件使用 Hermes 内部兼容接口，升级宿主后需要重新验收。

```sh
git clone https://github.com/Moonawn/moonawn-hermes-feishu-streaming.git
cd moonawn-hermes-feishu-streaming
python3 scripts/verify.py
python3 scripts/build_release.py
```

默认验证仅运行离线测试和合成性能检查，不需要机器人或凭据。构建生成 `dist/moonawn-feishu-0.3.0rc8.zip` 和 SHA-256 清单，**不会自动安装或启动网关**。安装目录结构、独立测试与回退见 [安装说明](docs/installation.md)。

```sh
# 生成可交互的本地模拟页面；先完成上面的 verify.py
python3 scripts/build_preview.py
# 使用浏览器打开 artifacts/preview.html
```

预览来自合成事件，不包含真实聊天。Playwright 布局检查为可选开发步骤，需要额外安装 Playwright / Chromium 后运行 `node scripts/verify_preview.cjs`。

## 验证范围

发布前在不含本机缓存、配置与账号数据的源码副本中验证离线测试及构建。此前隔离测试机器人已验证会话切换、双话题并行、停止隔离、显式排队、统计、话题附件下载与 Chrome Web 卡片交互。详细口径见 [验收与限制](docs/acceptance.md)。

当前为预发布版本。尚未完成移动端、长期压力、多 profile 共进程、分布式部署和真实审批完整链路验证；异常恢复的部分场景只有离线测试。公开发布不代表生产已切换。

## 参考与致谢

本项目由 Moonawn 独立维护。设计参考 [Lark 官方 OpenClaw 插件](https://github.com/larksuite/openclaw-lark) 的卡片生命周期与刷新组织方式，并吸收此前 [Moonawn/hermes-lark-streaming](https://github.com/Moonawn/hermes-lark-streaming) fork 的实测与修复经验；该 fork 的原项目为 [Aowen-Nowor/hermes-lark-streaming](https://github.com/Aowen-Nowor/hermes-lark-streaming)。

也参考了 HFC 等实现。项目不是 Lark、OpenClaw 或 Nous Research 的官方产品，也不表示获得其背书。具体参考范围、固定修订与真实代码复用说明见 [参考与来源](ACKNOWLEDGEMENTS.md)；Hermes 原生方法测试样本保留 [第三方 MIT 声明](docs/THIRD_PARTY_NOTICES.md)。

## 文档

- [使用与统计口径](docs/session-experience.md)
- [架构与恢复机制](docs/architecture.md)
- [Feishu API 契约](docs/feishu-api-contract.md)
- [安装与回退](docs/installation.md)
- [验收与限制](docs/acceptance.md)
