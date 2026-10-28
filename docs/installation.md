# 安装、验证与回退

`0.3.0rc9` 在 QA12 基础上增加停止入站隔离、运行诊断和 Agentloop 精修。它不是生产部署完成声明。

## 支持边界

- Python 3.11+、Unix、Hermes 0.22.1、进程内 Python plugin、每进程一个 profile。
- 原生 Feishu adapter 与 `lark-oapi` SDK 由 Hermes 提供。固定宿主验收使用 SDK 1.7.5。
- 不支持多个进程共享投递库、plugin host 子进程隔离或多个 profile 共进程。
- 不能同时由 HLS、HFC 或其他 Feishu 流式插件接管答复。Hermes 内部接口升级后必须重测。

## 下载与安装前诊断

从 [v0.3.0rc9 预发布页](https://github.com/Moonawn/moonawn-hermes-feishu-streaming/releases/tag/v0.3.0rc9) 下载 ZIP、`SHA256SUMS` 和 `manifest.json`。将 ZIP 与校验文件放在同一目录，macOS 使用 `shasum -a 256 -c SHA256SUMS`，Linux 使用 `sha256sum -c SHA256SUMS`。

解压后，使用 **Hermes 所用的 Python 解释器**，在插件目录执行（替换示例路径）：

```sh
python3 scripts/doctor.py --hermes-root /path/to/hermes-agent --hermes-home /path/to/test-home
```

首次安装前 `installed_plugin` 提示未安装是预期结果。诊断只解析版本、宿主源码和所选 home 的插件目录，不导入宿主，不读取凭据，不发送消息，也不更改配置。`--source-only` 可只做源码检查，不能据此确认 SDK 可用。

启动测试网关并完成一条流式任务后，再运行同一命令并加 `--require-running`。它核对最近 20 秒的运行快照、实际注册 factory、加载版本和是否实际创建过流式 consumer。快照过期不会判作成功；`WARN` 项仍需核对，诊断通过不代表消息投递或审批全链路已验收。

## 构建与离线检查

```sh
python3 scripts/verify.py
python3 scripts/build_release.py
```

产物在 `dist/`，包含版本化 ZIP 与 `manifest.json`。ZIP 内是 `moonawn-feishu/`，其下包含 `plugin.yaml`、`__init__.py`、`moonawn_feishu/` 和文档/许可证。无需将 `src/` 加入运行环境的 PYTHONPATH；`pip install` 仅安装 Python 源码包，不等于注册 Hermes 平台插件。

构建读取项目版本，拒绝覆盖内容不同的同名产物，不会操作任何网关。测试与构建无需真实凭据或聊天。

## 在独立测试实例接入

1. 准备已有原生 Feishu 能力的独立 Hermes 0.22.1 测试实例，使用专用机器人与测试会话。
2. 等待目标实例在途任务结束，再暂停该实例；保留原插件版本及启用方式。
3. 解压 ZIP，将 `moonawn-feishu/` 放入**所选 Hermes home** 的 `plugins/` 目录，按宿主的用户插件启用流程启用它，并避免其他流式插件覆盖同一平台。这里的路径由测试实例决定，不应默认指向日常机器人。
4. 启动该测试实例，检查 Feishu 平台实际使用 `MoonawnFeishuAdapter`，且宿主 streaming 已启用。只看到“插件加载成功”不足以证明工厂没有被后加载的插件覆盖。
5. 在专用聊天中依次验收正文、工具与统计、`/sessions` 切换、双话题、停止隔离、显式 `/queue`、附件与审批。

原生 allowlist、权限与凭据由宿主管理；本项目不提供凭据、自动迁移配置或默认生产切换脚本。缺失必要方法/签名时拒绝加载；卸载会恢复兼容 hooks。

## 状态与回退

运行时额外写入 `moonawn-feishu/health.json`：仅版本、布尔状态、计数、最近 128 次操作耗时和事件循环最大延迟，不包含正文、用户或聊天标识。快照每 5 秒更新，关闭时标记停止；它用于诊断，不是恢复依据。

独立投递库位于所选 Hermes home 的 `moonawn-feishu/delivery.db`，权限为 0600，使用单进程锁。需要在线备份时调用 `Store.backup()`（SQLite backup API）；不要普通复制活动数据库。

回退前等待在途任务结束，暂停指定测试实例，卸载本插件并恢复之前插件及启用方式，再启动该实例。保留投递库供未确认 ACK 核对；不要删除状态后重放未知结果任务。经验估时的新旧记录使用独立键，旧数据不会因升级被抹除。

## 可选宿主和浏览器检查

- `python3 scripts/verify_container.py`：只使用本机已存在的固定 Hermes 镜像，禁网络、只读挂载源码，不加载账号配置或启动网关。
- `python3 scripts/verify_host.py --release-dir dist`：固定镜像中的真实 plugin manager/SDK smoke。需要在 `.reference-cache/wheels/` 预先准备 `lark-oapi==1.7.5` 与 `requests-toolbelt==1.0.0` wheel；缺失时不代表宿主验收通过。
- `python3 scripts/verify.py --with-reference-cache`：额外检查固定研究源码与宿主源码 hash。普通克隆默认不含这些缓存；不把“未运行”写成“通过”。`fetch_references.py` 只恢复 source-lock 中的历史研究文件，不自动补齐 0.22.1 宿主快照。
- `python3 scripts/build_preview.py` 后用浏览器打开 `artifacts/preview.html`；可选 `node scripts/verify_preview.cjs` 需要 Playwright / Chromium。
