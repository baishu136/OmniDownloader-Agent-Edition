# OmniDownloader Agent Edition 操作手册

独立的 Headless 产品版本，代码位于 `agent-edition/`，默认独立数据与缓存位于工作区或便携目录的 `data/`。

## 部署与工作目录

- 默认数据与缓存目录：源码运行与工程内打包运行使用项目根目录下的 `data/`；将便携 ZIP 解压至独立文件夹运行时自动使用 EXE 同级 `data/`（保存 `settings.json`、`ledger.sqlite3`、`credentials/`、`sessions/` 与 `cache/omni-agent-managed/`），不再写入 `%LOCALAPPDATA%`。
- 图文独立格式设置（`image_format`）：在“01 下载与存储设置”可独立配置「默认图片格式」（`original` 保留原图格式默认 / `jpg` / `png` / `webp`，亦可由 Agent/MCP 在 `use_defaults=false` 时单次覆盖）；非 `original` 模式下在 Worker 层统一转换散图及 ZIP 包内图片（同步更新 `manifest.json`，遇多帧 GIF 等动画自动保留动态不拆帧），本地 `image_dir` 导出与 QQ / Telegram 发送天然一致。
- 永久下载与图文保存目录：可在管理页“01 下载与存储设置”点击“选择文件夹”浏览本机目录并点击“保存”，或通过 `update_settings` 设置 `download_dir` 与 `image_dir`，下载完成后自动导出成品，不受缓存自动清理影响。
- 网络代理诊断：支持 `http` / `https` / `socks5` 代理，兼容 Fake-IP / TUN；可在设置页直接点击“测试连通性”测量代理 TCP 握手及国内外媒体目标连通延迟。
- 已取消令牌授权设定：无 `auth.json`，打开 `http://127.0.0.1:58080/` 即可直接进入设置页；更改开关即自动保存，目录与代理提供显式保存与清空按钮，并支持“重置默认设置”。

## 链接前缀下载指令

在 `analyze_media`、`download_media`、`bot_client.py` 或聊天插件中输入链接时，可在链接最前方附带前缀指令（可任意组合使用）：
- `/us <链接>`：展开下载合集、分集视频或图文作品的全部图片（**不加 `/us` 时图文作品默认仅下载首图，多集视频默认下载当前单视频**；支持自动识别小红书、抖音、快手、Pixiv、B站动态、X、TikTok 等图文作品）。
- `/zip <链接>`：将下载的图文、视频或合集打包为 `.zip` 压缩包后导出与发送（不加 `/us` 时仅打包首图/当前单集）。
- `/merge <链接>`：强制将该次下载结果以合并转发聊天记录（QQ / OneBot `send_*_forward_msg`）或媒体组相册（Telegram `sendMediaGroup`）方式发出。
- `/us /merge <链接>` 或 `/us /zip <链接>`：同时下载全部内容并以合并转发或单个 `.zip` 压缩包发送。

## QQ / OneBot 与 Telegram 合集/图集发送设置

在“03 QQ 消息与合集发送设置”或通过 `update_settings` 可配置：
- `qq_video_mode`：`file`（群/私聊文件上传）或 `message`（聊天视频消息 `send_group_msg`/`send_private_msg`）。
- `qq_image_mode`：`message`（聊天图片消息）或 `file`（群/私聊文件上传）。
- `qq_forward_collection`：`true` 时，多视频合集或多张图集严格按原始顺序打包为合并转发聊天记录（`send_group_forward_msg`/`send_private_forward_msg`）发送；也可在单次请求中通过 `/merge` 前缀指令临时启用。
- `qq_forward_max_items`：合并转发单批最多条数（默认 **30** 条，范围 2–100）；超出时自动按批次拆分并标注批次标题（如 `第 1/2 批`），防止单包过大超时或被协议端拒发。
- Telegram 媒体组与格式兼容：多图/合集按连续区间每批最多 10 个聚合为 `sendMediaGroup` 相册发送并严格保留混排顺序（若被拒或转换后超出照片上限，则平滑降级逐条并回退 `sendDocument`）；`.webp` / `.avif` / `.bmp` / `.jfif` 原图在内存中自动校正 EXIF 方向并转换为兼容 JPEG/PNG 发出（不改动受控缓存原文件），`.gif` 自动使用 `sendAnimation` 发送；服务端与适配器结合二进制文件头魔数强制校验真实媒体类型，即使上游错传 `kind='video'` 也绝不把图片当视频发送。
- 注意：`.zip` 压缩包始终以群/私聊文件或文档方式上传发送。

## 下载可视化与报错直出

在“04 下载可视化与消息反馈设置”或通过 `update_settings` 可配置防消息忽略的状态直发通道、事件流通知策略与报错直出模板：
- `visual_enabled`：总开关；关闭或取消勾选全部节点即可静默不返回过程状态。
- `visual_on_receive` / `visual_on_start` / `visual_on_complete`：分别控制「收到链接时」「下载开始时」「下载完成时（正在发送）」是否返回文字状态；服务端采用 SQLite 持久化里程碑事件表 `visual_events`，配合 `after_seq` 游标与 `wait_seconds` 长轮询，即使缓存秒命中或极速下载也不会丢失或重复阶段状态。
- `visual_notify_channel` / `visual_notify_target`：支持 `auto`（智能协同：传入 `notify_adapter`/`notify_target` 或已配置 `visual_notify_target` 时服务端直发，否则走事件流与 MCP 进度通知）、`server_bot`（服务端通过已在 `allowed_targets` 白名单内的 QQ/Telegram 机器人即时推送文本进度，不等待 LLM 回合汇总）与 `caller_only`（仅通过 `pending_events` 与 MCP `notifications/progress` 返回）；可开启 `visual_reuse_message`（Telegram 下优先原地 `editMessageText` 刷新同任务首条消息）。
- `visual_mode`：`agent`（由 Agent 结合阶段和元数据自主组织反馈）或 `custom`（严格按 `visual_receive_template` / `visual_start_template` / `visual_complete_template` 渲染输出）。
- `visual_error_template`：无论过程通知是否开启，任务下载或发送报错时都会在 `error_message`、`AgentError` 的 `visualization` 载荷与直发通道中直接输出完整脱敏报错原因；管理页提供“预览并测试状态通知”（`POST /api/admin/visual/test`）一键预览四阶段文案并在已配目标时真实验证发送。

## 平台 Cookie 登录流程

- **统一平台会话与重新验证（对齐 Android 版）**：界面「平台会话」卡片按当前选中平台切换「禁用 Cookie」开关与操作区；首次配置主按钮显示「登录获取会话」，已存在会话时自动切换为「重新验证登录」。重新验证、手动确认平台（如 Pixiv / 抖音 / 快手 / 小红书）或独立账号容器均启用防自动覆写策略（`require_explicit_save`），必须用户确认并显式点击右下保存按钮后才会更新本地加密凭据，关窗不会覆写原会话。
- **清除会话引导与 `manage_only` Google 账号管理**：点击「清除会话」移除本机下载凭据后，页面弹出非阻塞提醒，可点击「管理 Google 登录」在 `manage_only` 模式下打开 `https://accounts.google.com/` 手动退出网页端登录，此模式仅负责账号管理，绝不抓包或覆写下载凭据。
- **哔哩哔哩官方扫码登录**：管理页点击“B站扫码登录”生成官方二维码，面板实时显示「等待扫码 → 已扫码待确认 → 加密入库」阶段进度与剩余有效期，确认后自动加密入库并在线检测账号状态。
- **内置页内导航与防 Google 风控纯净登录模式（解决 Edge / Chrome 无法登录 Google 问题）**：浏览器默认优先选用 Google Chrome（其次 Microsoft Edge / Brave），支持在页面直接切换本机已安装的浏览器并选择「智能模式 / 始终纯净无调试 / 始终实时监听」；启动参数默认启用 `--disable-blink-features=AutomationControlled` 与 `--disable-background-mode` 屏蔽 `navigator.webdriver` 与后台锁，且在涉及 `accounts.google.com`、YouTube、Google 管理或独立账号容器时智能进入**纯净无调试模式**（不传入 `--remote-debugging-port`），用户可像日常浏览器一样无障碍登录 Google，关窗或点击确认按钮后自动通过 Windows `WM_CLOSE` 优雅刷盘退出并由同内核无头进程无损读出全量 Cookie（含 `HttpOnly` 与 App-Bound 加密会话）。实时 CDP 监听模式下活动面板顶部仍提供「Google 账号页」「平台登录页」「重新加载登录页」及一键「纯净模式重开」。
- **Google / 独立登录档案（多账号隔离容器）**：支持新建与激活多个命名独立档案，每个档案使用独立浏览器隔离目录 `data/browser-profiles/<id>_e<epoch>/` 互不串号；支持单容器打开登录、后台无头免密同步、全选/退出当前/退出所选（通过两阶段递增 `epoch` 与 `owner_token` 所有权校验仅删除仍归属于该档案的凭据，绝不误删手工覆写的新会话），同时保留底部默认共享档案的「一键免密同步」「管理 Google 登录」与「清理共享档案」。
- **默认浏览器打开与多格式导入**：任何环境均可点击「默认浏览器打开」跳转官方登录页，或上传/粘贴 Netscape `cookies.txt`、HTTP `Cookie:` 请求头及浏览器扩展导出的 JSON 数组。
- **严格权限隔离**：仅用户可在管理页登录、管理多账号容器、导入或重新启用 Cookie；Agent 仅拥有 `list_cookie_statuses`（只读非敏感状态，不含 `ui`/`owner_token`/独立档案目录）、`disable_platform_cookie`（禁用）与 `remove_platform_cookie`（移除）权限。

## 自动识别已接入桥接与 MCP 工具（23 项）

服务端自动追踪经由 `/mcp`（Streamable HTTP）、`--stdio` 桥、AstrBot 插件及 Bot CLI 接入的客户端来源、调用次数与活跃状态，并在界面“06 服务状态、接入桥接与 Bot 连接”及 `list_bridges` 工具中实时展示。

- 媒体与交付：`analyze_media`、`download_media`、`get_task_status`、`list_tasks`、`cancel_task`、`download_collection`、`get_media_file`、`get_service_capabilities`、`deliver_media`、`get_delivery_status`。
- 设置、桥接与运维：`get_settings`、`update_settings`、`reset_settings`、`list_bridges`、`get_bot_configs`、`update_bot_config`、`probe_bot`、`list_approvals`、`approve_request`、`cleanup_cache`。
- Cookie 权限隔离：`list_cookie_statuses`、`disable_platform_cookie`、`remove_platform_cookie`。

Windows stdio 示例：

```json
{"mcpServers":{"omni-agent":{"command":"C:\\OmniDownloaderAgent\\OmniDownloaderAgentMCP.exe","args":["--stdio"],"env":{"OMNI_AGENT_SERVER":"http://127.0.0.1:58080"}}}}
```

HTTP 客户端示例：

```json
{"mcpServers":{"omni-agent":{"url":"http://127.0.0.1:58080/mcp"}}}
```
