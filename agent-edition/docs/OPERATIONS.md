# OmniDownloader Agent Edition 操作手册

独立的 Headless 产品版本，代码位于 `agent-edition/`，默认独立数据与缓存位于工作区或便携目录的 `data/`。

## 部署与工作目录

- 默认数据与缓存目录：源码运行与工程内打包运行使用项目根目录下的 `data/`；将便携 ZIP 解压至独立文件夹运行时自动使用 EXE 同级 `data/`（保存 `settings.json`、`ledger.sqlite3`、`credentials/`、`sessions/` 与 `cache/omni-agent-managed/`），不再写入 `%LOCALAPPDATA%`。
- 永久下载与图文保存目录：可在管理页“01 下载与存储设置”或通过 `update_settings` 设置 `download_dir` 与 `image_dir`，下载完成后自动安全导出成品至此目录，不受临时缓存清理影响。
- 已取消令牌授权设定：无 `auth.json`，打开 `http://127.0.0.1:58080/` 即可直接进入设置页；更改任何设置会自动保存，也支持一键“重置默认设置”。

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

## MCP 工具（22 项）

- 媒体与交付：`analyze_media`、`download_media`、`get_task_status`、`list_tasks`、`cancel_task`、`download_collection`、`get_media_file`、`get_service_capabilities`、`deliver_media`、`get_delivery_status`。
- 设置与运维：`get_settings`、`update_settings`、`reset_settings`、`get_bot_configs`、`update_bot_config`、`probe_bot`、`list_approvals`、`approve_request`、`cleanup_cache`。
- Cookie 权限隔离：`list_cookie_statuses`（只读非敏感状态）、`disable_platform_cookie`（禁用）、`remove_platform_cookie`（移除）。Agent 绝无读取 Cookie 明文、导入或重新启用的权限。

Windows stdio 示例：

```json
{"mcpServers":{"omni-agent":{"command":"C:\\OmniDownloaderAgent\\OmniDownloaderAgentMCP.exe","args":["--stdio"],"env":{"OMNI_AGENT_SERVER":"http://127.0.0.1:58080"}}}}
```

HTTP 客户端示例：

```json
{"mcpServers":{"omni-agent":{"url":"http://127.0.0.1:58080/mcp"}}}
```
