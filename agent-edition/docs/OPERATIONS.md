# OmniDownloader Agent Edition 操作手册

独立的 Headless 产品版本，代码位于 `agent-edition/`，默认独立数据与缓存位于工作区或便携目录的 `data/`。

## 部署与工作目录

- 默认数据与缓存目录：源码运行与工程内打包运行使用项目根目录下的 `data/`；将便携 ZIP 解压至独立文件夹运行时自动使用 EXE 同级 `data/`（保存 `settings.json`、`ledger.sqlite3`、`credentials/`、`sessions/` 与 `cache/omni-agent-managed/`），不再写入 `%LOCALAPPDATA%`。
- 永久下载与图文保存目录：可在管理页“01 下载与存储设置”点击“选择文件夹”浏览本机目录并点击“保存”，或通过 `update_settings` 设置 `download_dir` 与 `image_dir`，下载完成后自动导出成品，不受缓存自动清理影响。
- 网络代理诊断：支持 `http` / `https` / `socks5` 代理，兼容 Fake-IP / TUN；可在设置页直接点击“测试连通性”测量代理 TCP 握手及国内外媒体目标连通延迟。
- 已取消令牌授权设定：无 `auth.json`，打开 `http://127.0.0.1:58080/` 即可直接进入设置页；更改开关即自动保存，目录与代理提供显式保存与清空按钮，并支持“重置默认设置”。

## 链接前缀下载指令

在 `analyze_media`、`download_media`、`bot_client.py` 或聊天插件中输入链接时，可在链接最前方附带前缀指令（可组合使用）：
- `/us <链接>`：展开下载合集、分集视频或图文作品的全部图片。
- `/zip <链接>`：将下载的图文、视频或合集打包为 `.zip` 压缩包后导出与发送。
- `/us /zip <链接>`：同时下载全部内容并打包为单个 `.zip` 压缩包发送。

## QQ / OneBot 消息发送设置

在“03 QQ 消息与合集发送设置”或通过 `update_settings` 可配置：
- `qq_video_mode`：`file`（群/私聊文件上传）或 `message`（聊天视频消息 `send_group_msg`/`send_private_msg`）。
- `qq_image_mode`：`message`（聊天图片消息）或 `file`（群/私聊文件上传）。
- `qq_forward_collection`：`true` 时，多视频合集或多张图集自动打包为一条合并转发聊天记录（`send_group_forward_msg`/`send_private_forward_msg`）发送。
- 注意：`.zip` 压缩包始终以群/私聊文件上传方式发送。

## 下载可视化与报错直出

在“04 下载可视化与消息反馈设置”或通过 `update_settings` 可配置桥接对话与 MCP 调用中的阶段反馈策略：
- `visual_enabled`：总开关；关闭或取消勾选全部节点即可静默不返回过程状态。
- `visual_on_receive` / `visual_on_start` / `visual_on_complete`：分别控制「收到链接时」「下载开始时」「下载完成时（正在发送）」是否返回文字状态。
- `visual_mode`：`agent`（由 Agent 结合阶段和元数据自主组织反馈）或 `custom`（严格按 `visual_receive_template` / `visual_start_template` / `visual_complete_template` 渲染输出）。
- `visual_error_template`：无论过程通知是否开启，任务下载或发送报错时都会在 `error_message` 与 `visualization.message` 中直接输出完整脱敏报错原因。

## 平台 Cookie 登录流程

- **统一平台会话与重新验证（对齐 Android 版）**：界面「平台会话」卡片按当前选中平台切换「禁用 Cookie」开关与操作区；首次配置主按钮显示「登录获取会话」，已存在会话时自动切换为「重新验证登录」。重新验证、手动确认平台（如 Pixiv / 抖音 / 快手 / 小红书）或独立账号容器均启用防自动覆写策略（`require_explicit_save`），必须用户确认并显式点击右下保存按钮后才会更新本地加密凭据，关窗不会覆写原会话。
- **清除会话引导与 `manage_only` Google 账号管理**：点击「清除会话」移除本机下载凭据后，页面弹出非阻塞提醒，可点击「管理 Google 登录」在 `manage_only` 模式下打开 `https://accounts.google.com/` 手动退出网页端登录，此模式仅负责账号管理，绝不抓包或覆写下载凭据。
- **哔哩哔哩官方扫码登录**：管理页点击“B站扫码登录”生成官方二维码，面板实时显示「等待扫码 → 已扫码待确认 → 加密入库」阶段进度与剩余有效期，确认后自动加密入库并在线检测账号状态。
- **内置页内导航与托管抓取**：桌面环境启用本地持久化 Chrome / Edge 专属配置并连接环回 CDP（联合采集 `Storage.getCookies`、`Network.getAllCookies` 与 `Network.getCookies`，含 `HttpOnly`）；活动登录面板顶部提供「Google 账号页」「平台登录页」「重新加载登录页」快捷切换以及实时域名与 4 步进度展示。
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
