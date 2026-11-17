# OmniDownloader Agent Edition

独立的 Headless 产品版本。复用 `../windows/core` 与 `../windows/engine`，面向 QQ / Telegram 机器人及 AI Agent，提供下载、设置管理、平台交付与缓存管理的完整服务闭环。

- 官方 MCP Python SDK 2.3：stdio、Streamable HTTP，22 个带严格 Schema 的工具。
- 免令牌本机直连管理：用户与 Agent 均可读取/修改/重置下载参数、打包方式、存储目录、QQ 消息/文件/转发模式、功能开关、代理与机器人配置。
- 链接前缀指令：支持 `/us`（下载合集、分集视频、全部图片）与 `/zip`（打包为 ZIP 压缩包发送）。
- Cookie 权限隔离：参照加密会话卡片设计；仅用户可导入与启用，Agent 仅可查看非敏感状态、禁用（`disable_platform_cookie`）与移除（`remove_platform_cookie`）。
- Telegram multipart 上传、OneBot 11 / NapCat 群聊/私聊消息、群文件与合并转发聊天记录交付，并实时显示 Bot 连接详情。
- SQLite 文件引用与回执账本，成功清理、失败保留、重启安全结束，支持独立的永久下载保存目录与图文保存目录。

Windows：运行根目录 `启动服务.bat` 或 `dist\OmniDownloaderAgent\OmniDownloaderAgent.exe`，打开 `http://127.0.0.1:58080/` 直接管理设置。stdio 使用同包 `OmniDownloaderAgentMCP.exe --stdio`。
