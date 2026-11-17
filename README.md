# OmniDownloader Agent Edition

面向 AI Agent、QQ / Telegram 机器人的独立 Headless 媒体解析、下载与交付服务。

本仓库独立维护代码、版本、CI 和发行包。媒体引擎复用自 [OmniDownloader](https://github.com/baishu136/OmniDownloader)，来源与许可见 [UPSTREAM.md](UPSTREAM.md) 和 [LICENSE](LICENSE)。

- 官方 MCP Python SDK：stdio 与 Streamable HTTP，10个媒体、任务和交付工具。
- 管理员限制优先：默认下载参数、功能权限、体积/画质/批量限制和高消耗审批。
- Telegram / OneBot 11 / NapCat 文件交付，平台确认回执后自动清理，共享缓存引用保护。
- SQLite任务与交付账本，加密平台凭据，重启恢复，幂等回执与受控目录删除。
- 本地深浅色管理页，Windows便携包，非root Docker及持久化挂载。

[下载发行包](https://github.com/baishu136/OmniDownloader-Agent-Edition/releases) · [完整部署与接入](agent-edition/docs/OPERATIONS.md) · [验证记录](agent-edition/docs/VALIDATION.md) · [模块结构](agent-edition/README.md)

## Windows

下载 Agent Edition ZIP，解压后双击 `OmniDownloaderAgent.exe`。首次自动打开 `http://127.0.0.1:58080/`。管理令牌位于 `%LOCALAPPDATA%\OmniDownloaderAgent\auth.json`，可在管理页选择文件读取。

不需要另装 Python、FFmpeg、yt-dlp 或浏览器运行时。stdio客户端使用同包 `OmniDownloaderAgentMCP.exe --stdio`。

## Docker

```sh
git clone https://github.com/baishu136/OmniDownloader-Agent-Edition.git
cd OmniDownloader-Agent-Edition/agent-edition
docker compose up -d --build
```

默认只映射宿主机本地端口。配置、密钥、凭据、缓存与任务状态使用持久化卷。远程接入需配置允许的Host、HTTPS代理和独立Agent令牌。

## MCP 与聊天机器人

工具包括 analyze_media、download_media、get_task_status、list_tasks、cancel_task、download_collection、get_media_file、get_service_capabilities、deliver_media、get_delivery_status。

管理页分为下载设置、功能权限、平台登录、服务与交付四区。QQ / Telegram 连接、每个聊天独立身份、MCP配置与AstrBot示例见 [操作手册](agent-edition/docs/OPERATIONS.md)。
