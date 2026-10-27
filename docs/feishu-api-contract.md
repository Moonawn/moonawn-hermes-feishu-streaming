# Feishu API 契约

核对日期：2026-10-23。实现使用 Hermes 原生 adapter 已配置的官方 `lark-oapi` client 与 executor；不自行查找凭据，不创建另一个认证或 WebSocket 服务。内核是 Python 标准库，运行插件仍依赖宿主 Hermes 和其 Feishu SDK。

| 操作 | 官方契约 | 本项目处理 |
| --- | --- | --- |
| [创建实体](https://open.feishu.cn/document/cardkit-v1/card/create) | POST `/cardkit/v1/cards`，`type=card_json`、序列化 `data`；无创建 UUID | 实体与发送消息分别记录；创建 ACK 未知时不重建实体 |
| [回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply) | POST `/im/v1/messages/:id/reply`；`uuid` 去重窗口一小时 | 稳定 UUID；仅在 55 分钟保守窗口内重试完全相同请求；保留 thread 与 anchor |
| [更新正文](https://open.feishu.cn/document/cardkit-v1/card-element/content) | PUT `/cardkit/v1/cards/:id/elements/:element_id/content`；content 是累计正文 | 不发送单独 token；正文元素 ID 固定为 `answer` |
| [更新整卡](https://open.feishu.cn/document/cardkit-v1/card/update) | PUT `/cardkit/v1/cards/:id`；`card`、`sequence`、`uuid` | 结构变化和最终正文使用整卡；序列号持久化递增 |
| [关闭流式](https://open.feishu.cn/document/cardkit-v1/card/settings) | PATCH `/cardkit/v1/cards/:id/settings`；settings 为 JSON 字符串 | 与正文确认分别记录；封口失败不触发第二份答复 |
| [回读消息](https://open.feishu.cn/document/server-docs/im-v1/message/get) | GET `/im/v1/messages/:id?card_msg_content_type=user_card_content` | 明确请求原始卡片 JSON，核对 message/chat/thread/app 与 `answer` 全文 |

官方[接收消息结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/message_content)说明默认卡片投影不是原始 JSON。不能把默认摘要、扁平化文本或 `card_id` 引用当成完整正文。若指定参数仍得不到可核对的 schema 2.0，保持 unknown，等待恢复或人工核对。

真实 SDK 和测试机器人已经验证卡片创建、更新、回读、封口与话题绑定。大部分 Transport 测试仍使用合成 API；`tests/fixtures/feishu_markdown_*.json` 是专用群实际回读样本，覆盖中文、代码、表格、标题和分隔线。

`user_card_content` 会规范化 Markdown。校验先尝试原文精确相等，再尝试有实测样本的一向格式投影并精确比较；只变换已观测的标题边界空行、`---` 到 `<hr>`、表格分隔符、冒号引导的列表边界空行及代码块序列化换行，不删除任意空白，不改变正文、代码缩进和表格单元内容。无法解释的变化保留完整原文后降级。

实测发现消息已经绑定卡片但 ACK 丢失时，重放可在 IM UUID 去重前返回 230099/200780。先前未知操作的明确拒绝也不能证明第一次未提交，必须进入 receipt_required；不会因此补发另一份正文。

错误处理区分明确限流、权限/目标不可用、明确拒绝和结果未知。权限、内容管控或撤回不换通道绕过。超时与 5xx 保留未知结果，不将其解释为失败后可以重新创建。

24 KB 卡片预算、250 ms 合并窗口和 55 分钟重试窗口是本项目选择。未测量实网吞吐与首字延迟，不把离线合并数据作为飞书性能承诺。
