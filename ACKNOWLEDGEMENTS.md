# 参考与来源

本项目由 Moonawn 独立维护，感谢以下项目提供的实现参考与维护经验：

- [OpenClaw 飞书官方插件](https://github.com/larksuite/openclaw-lark)：卡片生命周期与刷新组织。
- [Moonawn/hermes-lark-streaming](https://github.com/Moonawn/hermes-lark-streaming) 及其[原项目](https://github.com/Aowen-Nowor/hermes-lark-streaming)：流式投递与恢复的维护经验。
- [HFC](https://github.com/baileyh8/hermes-feishu-streaming-card)、[openclaw-hermes-feishu-card](https://github.com/wzgrx/openclaw-hermes-feishu-card)：事件、投递与适配边界的对照研究。
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)：原生会话、权限、媒体、审批与 callback 宿主能力。

研究版本及 hash 见 [source-lock.json](docs/source-lock.json)，不代表持续同步上游。运行时不加载其他流式插件的引擎。

测试 fixture 中抽取的两个 Hermes 0.22.1 方法保留完整 [MIT 版权声明](docs/THIRD_PARTY_NOTICES.md)；它们不进入运行插件包。宿主版本依据见 [host-contract.json](docs/host-contract.json) 与 [feature-host-contract.json](docs/feature-host-contract.json)。
