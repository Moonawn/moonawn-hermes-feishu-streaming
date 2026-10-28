# 会话与运行体验 · 0.3.0rc9

`0.3.0rc9` 以隔离 QA12 验收为基线，补充停止路径、诊断及明细排版优化。已验证范围与未完成项见 [验收与限制](acceptance.md)。

## 使用方式

发送 `/sessions` 打开会话工作台。默认列出本来源可见的最近 10 个 Hermes 会话，包括未命名会话；选择“继续”切换上下文。“新建当前会话”建立新会话。“另开独立话题”创建飞书话题，可以在原任务继续执行时处理另一项任务。群聊中的 @ 规则仍按原生入站权限执行。

运行中无参数 `/sessions` 仍可打开面板，先经过原生 slash ACL，再进入原生 profile scope。带参数 `/sessions` 与其他忙时命令保留宿主行为。执行中的原会话禁止从面板原地新建或切换。Hermes 的原生 `/new` 会中断当前运行，面板在入口与命令实际执行前检查两次，避免这个副作用。手动输入原生命令仍保留 Hermes 原有语义。

飞书原生话题标识参与 Hermes session key；并行话题各有上下文、Mailbox、投递身份和运行统计。面板只列出当前来源的历史，不提供跨群、跨话题搬移上下文。新话题从空上下文开始，未实现对已有会话的 clone/branch。原生 Feishu `/topic` 并不提供此能力，不能把 Telegram 的命令直接移植过来。

既有隔离验收使用 2 个并行会话的宿主上限，实测第三项在容量满时明确拒绝。需要同一话题依次执行时可使用原生显式 `/queue`；各项拥有独立答复和最终统计。普通消息自动 FIFO/redirect 沿用当前宿主行为。

原生带参数 `/sessions`、`/resume` 保留。面板不是新的会话数据库：查询复用 `query_session_listing`，筛选复用 `_resume_row_visible`，切换再次调用 `_resume_target_allowed`，最终进入原生消息与命令入口。插件自己的 SQLite 只保存投递、动作回执、动效偏好与耗时样本。Hermes 0.22.1 对非当前飞书历史会话的原生 owner 检查缺少 alt ID 支持；本插件仅在原生检查不通过且持久化 session key、完整 origin、用户双标识、chat/thread 全部一致时补充确认，缺失来源或身份不一致仍拒绝。新建继续遵循原生命令审批，实测使用单次确认。

## 卡片运行信息

卡片在卡片底部保留一条可展开的信息栏，与 `answer` 正文元素分离；三点呼吸/静态标记、Agentloop 轮次、已耗时、模型、Token、输出均速集中显示。点击整行展开最近 5 轮的阶段摘要；每轮工具详情独立折叠，总共最多 12 条工具摘要，最多 3 条名额保留较早异常/未完成/慢调用，其余旧历史不进入客户端节点树。同轮相同成功摘要合并次数，失败与执行中不合并。API 次数、Token 子项和耗时积累数量在“用量与耗时依据”中按需查看。未回报时保留字段占位，完成/停止/失败后保留最后统计。这里的固定指卡片内位置，不是飞书窗口的悬浮吸底。顶部用小型蓝色符号、HERMES 与状态组成轻量标识行。超出卡片预算后的纯文本降级沿用原有完整内容保底，无法承诺每段文本也带卡片信息区。

| 信息 | 数据来源和边界 |
| --- | --- |
| 模型 | attach agent 时的模型标识；屏蔽 URL、内部路径、凭据格式和富文本注入，不显示 provider endpoint |
| 输入 / 输出 token | attach agent 时记录 session counter 基线，展示本轮增量；输入包含缓存，输出包含提供方计入其中的推理 token |
| Token 合计 | 本轮已回报输入 + 输出；不重复加入缓存或推理子项 |
| 平均输出速率 | 已回报输出 Token / 本轮精确 monotonic 耗时，单位 tok/s；包含思考及工具等待，不代表模型解码速率。无 usage 或耗时为零时不计算；完成后冻结 |
| 缓存 / 推理 | 子项单独标注“其中”，不再加进输入输出总和 |
| 尚未回报 | 提供方未给出 usage 时显示等待回报，不根据字符数猜测；有些调用缺少 usage 时只显示已回报部分 |
| 已用时长 | 运行观察器的 monotonic 时钟；覆盖本轮消费和 Agent 执行，结束冻结；不含入站排队等待 |
| Agent loop | 原生 `step_callback` 迭代序号；不是工具次数，也不是完成百分比 |
| 模型阶段 | 原生 step 到首个 tool start，或下一 step / finish 的观察时长；包含思考、生成和请求等待，不等于纯推理耗时。不采集推理正文；缺少阶段事件的历史记录明确显示未记录 |
| API 已返回次数 | 本轮 `session_api_calls` 增量，未返回的请求和最终失败尝试不算已返回 |
| 工具过程 | 原生 start/complete callback 的 call ID；保留已有 hooks、审批与返回值，显示脱敏后的原生参数预览、轮次、状态与完成耗时，不输出原始 args/result；失败复用 Hermes 分类与摘要 |
| 预计剩余 | 同应用/profile、聊天、用户、provider/model 和有无工具的成功耗时；排除已不足以覆盖当前轮数/工具数的记录，优先相同工具类别，至少 5 个可比样本、最多保留 120 个；约 20%–80% 区间减去已用时长。样本不足显示积累进度，跨度过大暂停估时 |

估时是粗略经验范围，不是统计置信区间，也不是预测承诺。没有任务计划时，不使用 max_iterations 预算当作完成度；样本不足显示“积累中”，超出历史上界显示继续执行，等待确认时暂停估时。失败/取消以及经历人工等待的任务不写入成功历史。旧版缺少任务特征的记录保留，运行中的新版估时不拿它们补齐样本门槛。新口径采用独立记录格式，重启继续积累，旧版可直接回退。Agent 子任务用量是否并入由 Hermes/提供方决定；没有独立、可归属的子任务使用量时不承诺完整汇总。

统计最多每 2 秒刷新一次，复用同一 writer。为保留 NO_REPLY 静默语义，不为了统计栏单独发布空白卡片。正文增量默认走 CardKit 元素更新，50 ms / 2 字步长交给飞书客户端；不为每个字发送一次网络请求。面板可关闭打字机，关闭后正文通过整卡更新立即呈现。偏好绑定用户和聊天，从下次回答生效。终稿直接完整替换，不等待动画队列。

## 交互与并发防护

按钮只携带随机 opaque nonce。同一面板及其所有按钮记录在一次 SQLite 事务中发布，避免逐按钮磁盘同步。服务端记录 profile、原始 SessionSource、请求人、chat、实际卡片 message ID、期望的当前 session、动作和 15 分钟期限。回调校验 SDK 事件的 app/operator/chat/message 与本地记录，原生用户 allowlist 再校验一次。转发卡、其他用户、旧 profile、过期与重复点击不获得操作权限。新建/切换在真正执行时再次核对当前会话，防止排队中的旧按钮改动新任务。

原生同步 SDK handler 立即 ACK 并调度异步处理，原生审批和更新提示按钮继续走原处理器。断开连接先停止接受新交互并等待在途面板操作，再关闭自己的 Store。操作面板为非流式卡片，避免在正文流式卡片上点击导致正文更新被飞书交互窗口暂时阻塞。

独立话题由两次带固定 UUID 的 IM 调用建立：发送根消息，再 `reply_in_thread=true` 回复该根消息。仅接受响应中的真实 chat/thread/message 绑定；没有 thread_id 不虚构成功。每步有独立 outbox。超时或未知 ACK 不自动重做新建；用户看到未确认提示。若群不支持话题或缺少权限，可能只留下已成功发送的根消息，原会话不改变。保留它供核对，不静默删除。

## 维护与验证边界

体验层兼容点包括 `TurnRunner._wire_turn_agent_callbacks`、sessions/reset/resume handler 和严格历史 owner 校验。加载检查签名，注册异常回滚，卸载恢复；callback 包装只作用于本插件 consumer，要求相同 session key 且每轮只绑定一个 Agent。话题媒体继续走原生上传与类型选择，仅在当前 chat/thread 归属完全匹配且缺少显式回复目标时补齐 anchor。当前固定 Hermes 0.22.1 的 21 个相关源码 hash 在 `feature-host-contract.json`，连同投递、归属、入站和媒体验证形成 19 个静态方法检查。真实镜像 smoke 执行原生 callback wiring、媒体锚点、session key 与停止入口。

离线预览的运行统计来自合成 agent 和本实现的 telemetry；会话按钮演示使用同一个 schema 2.0 renderer 与本地演示状态。它不是飞书客户端，也不会调用真实会话或网络接口。

本轮专用群已经验证 CardKit 回读、真实按钮、命令 admission 和双话题并发。不同单聊/群类型、手机客户端、长期负载下首字/刷新/封口时延仍不在这次结果的覆盖范围。离线预览不能替代客户端证据。

## 官方依据

- [CardKit 流式更新文本](https://open.feishu.cn/document/cardkit-v1/card-element/content)：累计文本、前缀匹配与打字效果、交互期间错误码。
- [JSON 2.0 按钮](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/interactive-components/button)：直接置于 elements、behaviors callback 与 disabled。
- [卡片回传交互](https://open.feishu.cn/document/feishu-cards/card-callback-communication)：3 秒 ACK、operator 和消息上下文。
- [回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply)：reply_in_thread、返回 thread_id 与不支持话题的错误。
