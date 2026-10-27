# 参考、来源与致谢

本项目由 Moonawn 独立维护，采用 Python 实现 Hermes 的飞书流式交互。以下区分架构参考、既有 fork 的经验来源和实际随仓库分发的第三方测试代码。项目不属于 Lark、OpenClaw 或 Nous Research 的官方产品，不表示这些项目对本实现作出背书。

## 架构与经验来源

| 项目 | 本项目参考范围 | 固定研究版本 |
| --- | --- | --- |
| [larksuite/openclaw-lark](https://github.com/larksuite/openclaw-lark) | Lark 官方 OpenClaw 插件：卡片生命周期、累计刷新、正文与投递分层、不可用状态处理；未将其 TypeScript 引擎作为运行依赖 | `dde0be3680d6fd5443cab426c8f4b3216266346a` |
| [Moonawn/hermes-lark-streaming](https://github.com/Moonawn/hermes-lark-streaming) | 维护和验收旧 fork 时积累的终稿投递、补充、队列、统计与恢复问题经验；新仓库独立维护 | 本地对照修订 `cd009b06a493488db63fa0047edf25d29ee1ad31` |
| [Aowen-Nowor/hermes-lark-streaming](https://github.com/Aowen-Nowor/hermes-lark-streaming) | 上述 fork 的原项目；保留原作者与社区贡献者的来源归属 | 与上述 fork 来源关系对应 |
| [baileyh8/hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card) | 生命周期、刷新、事件协议、投递策略的对照研究 | `e03c57369503b3364313bf005d7f0e5087650557` |
| [wzgrx/openclaw-hermes-feishu-card](https://github.com/wzgrx/openclaw-hermes-feishu-card) | Python 适配层与卡片引擎边界的对照研究 | `88f4089bd1e450b8c50a23b3535fac3256707bd8` |

2026-10-15 的逐文件研究清单与 hash 保留在 [source-lock.json](docs/source-lock.json)。这是历史研究版本，不代表持续同步到上游最新源码。研究缓存不随源码或发布包分发；运行时代码不导入上述流式插件。

以上参考项目的版权归各自作者。OpenClaw 参考版本采用 [MIT](https://github.com/larksuite/openclaw-lark/blob/dde0be3680d6fd5443cab426c8f4b3216266346a/LICENSE)，Copyright (c) 2026 Lark Technologies Pte. Ltd.；旧 fork 原项目采用 [MIT](https://github.com/Aowen-Nowor/hermes-lark-streaming/blob/github_sync/LICENSE)，Copyright (c) 2026 Aowen_Nowor / Hermes Lark Streaming Contributors。HFC 的 [MIT 许可](https://github.com/baileyh8/hermes-feishu-streaming-card/blob/e03c57369503b3364313bf005d7f0e5087650557/LICENSE)与其他来源的许可由其原仓库维护。

## 实际复用的宿主与测试代码

[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 是运行宿主。插件复用其原生 Feishu adapter、会话、权限、媒体、审批与 callback 接口。当前兼容目标为 0.22.1；[host-contract.json](docs/host-contract.json) 和 [feature-host-contract.json](docs/feature-host-contract.json) 记录验收源码 hash。

`tests/fixtures/hermes-contract.json` 包含从 Hermes 0.22.1 源码抽取的 `_deliver_queued_first_response` 与 `_send_with_retry` 方法，供隔离测试执行真实宿主分支；其余条目为方法签名快照。测试样本属于真实第三方代码复用，保留完整的 Nous Research MIT 声明，见 [THIRD_PARTY_NOTICES](docs/THIRD_PARTY_NOTICES.md)。测试样本不进入运行插件 ZIP。

工具名称、动作和预览调用宿主原生显示函数；插件进一步限制长度、脱敏并组织折叠布局。运行时 SDK 由宿主提供，仓库不打包 SDK 副本。

## 后续贡献

新增复制、移植或改编代码时，在对应文件和第三方声明中记录原仓库、修订、范围及许可证；保留原版权，不以架构致谢代替实际代码归属。相似度扫描只用于检查，不能作为“百分之百原创”的证明。
