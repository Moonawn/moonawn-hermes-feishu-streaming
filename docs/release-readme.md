# Hermes 飞书流式交互 · 0.3.0rc8

基于 Python 的 Hermes 飞书流式交互插件，支持流式卡片、多会话与并行任务、Agentloop 展示及 Token／耗时统计。

源码与问题反馈：https://github.com/Moonawn/moonawn-hermes-feishu-streaming

本包包含 `plugin.yaml`、相对导入入口、`moonawn_feishu/` 模块和公开文档。插件 ID 为 `moonawn-feishu`。运行时需要 Hermes 0.22.1 原生 Feishu 依赖；Python 3.11+、Unix、单 profile / 单进程 / 进程内 plugin。

这是预发布候选。先阅读 [安装与回退](installation.md)、[验收与限制](acceptance.md)、[使用与统计](session-experience.md)。不要与另一个 Feishu 流式插件同时接管答复。包不含凭据、配置、状态库、真实验收日志或第三方运行时副本。

本项目采用 [MIT](../LICENSE)。设计参考与此前 fork 关系见 [ACKNOWLEDGEMENTS](../ACKNOWLEDGEMENTS.md)，第三方来源见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。
