# Hermes 飞书流式交互 · 0.3.0rc9

这次预发布改善停止入口、安装可检查性与 Agentloop 明细。兼容目标为 **Hermes 0.22.1、Python 3.11+、Unix、每进程一个 profile**；先接入独立测试实例。

- `/stop` 不再等待同群慢命令释放 chat lock，仍由 Hermes 原生权限和会话逻辑处理。
- 新增只读 `doctor`：检查宿主版本、19 项静态契约、SDK、已安装插件和近期实际 factory／consumer 状态。
- 新增有界运行诊断，区分命令处理、取消收尾、终稿提交耗时与事件循环调度延迟。
- Agentloop 去掉重复完成状态，区分极短耗时与未记录耗时，明确标记失败调用；继续限制最近轮次与工具明细数量。
- 提供源码插件 ZIP、`SHA256SUMS` 和逐文件 `manifest.json`，附安装及回退说明。

验证包括 174 项离线测试、192 帧布局检查、固定真实 Hermes 宿主 smoke，以及专用飞书环境的双话题停止隔离和三项投递恢复。故障使用受控本地注入；不代表服务端限流、全网中断或整个网关崩溃均已验收。

**仍有边界：**测试宿主出现较大调度延迟，尚不承诺稳定的秒级停止；移动端、长期压力、完整审批链路和多 profile 共进程仍未验收。详情见 [验收与限制](acceptance.md)。

下载后先核对 `SHA256SUMS`，再按 [安装与回退](installation.md) 操作。ZIP 只含插件、公开文档及图片，不附带凭据、配置或运行数据库。

[源码与问题反馈](https://github.com/Moonawn/moonawn-hermes-feishu-streaming) · [使用指南](session-experience.md) · [MIT](../LICENSE)
