# OmniDownloader Agent Edition

面向 AI Agent、QQ / Telegram 机器人的独立 Headless 媒体解析、下载与交付服务。

本仓库独立维护代码、版本、CI 和发行包。媒体引擎复用自 [OmniDownloader](https://github.com/baishu136/OmniDownloader)，来源与许可见 [UPSTREAM.md](UPSTREAM.md) 和 [LICENSE](LICENSE)。

- 官方 MCP Python SDK：stdio 与 Streamable HTTP，21 个媒体、设置管理、运维与 Cookie 权限控制工具。
- 免令牌本机直连：取消 `auth.json` 与令牌授权锁屏；用户和 Agent 可直接管理下载参数、功能开关、缓存/代理、机器人配置与大批量审批（保留本机 Host/Origin、SSRF 与受控目录防护）。
- Cookie 严格权限隔离：仅用户可在管理页导入、检测与启用平台 Cookie；Agent 无法读取 Cookie 明文或填写/登录/启用，仅可查看非敏感状态、禁用（`disable_platform_cookie`）或移除（`remove_platform_cookie`）。
- 独立本地工作目录：源码运行默认使用工程根目录下的 `./data/`，独立便携包使用工作区根目录或 EXE 同级 `./data/`，不再写入 `%LOCALAPPDATA%`。
- Telegram / OneBot 11 / NapCat 文件交付，平台确认回执后自动清理，共享缓存引用保护。
- SQLite 任务与交付账本，加密平台凭据，重启恢复，幂等回执与受控目录删除。

[下载发行包](https://github.com/baishu136/OmniDownloader-Agent-Edition/releases) · [完整部署与接入](agent-edition/docs/OPERATIONS.md) · [验证记录](agent-edition/docs/VALIDATION.md) · [模块结构](agent-edition/README.md)

## Windows

下载 Agent Edition ZIP，解压到独立目录后双击 `OmniDownloaderAgent.exe`。首次自动打开 `http://127.0.0.1:58080/` 直接进入设置页，免令牌即可配置下载参数、功能权限、平台 Cookie 与机器人交付。

工作与数据目录默认保存于所在独立目录的 `data/`（设置位于 `data/settings.json`），无需另装 Python、FFmpeg、yt-dlp 或浏览器运行时。stdio 客户端使用同包 `OmniDownloaderAgentMCP.exe --stdio`。

## Docker

```sh
git clone https://github.com/baishu136/OmniDownloader-Agent-Edition.git
cd OmniDownloader-Agent-Edition/agent-edition
docker compose up -d --build
```

默认只映射宿主机本地端口 `127.0.0.1:58080`。配置、凭据、缓存与任务状态使用持久化卷。如需外部接入，请配置 `OMNI_AGENT_ALLOWED_HOSTS`、`OMNI_AGENT_PUBLIC_ORIGIN` 与独立 HTTPS 反向代理访问控制。

## MCP 与聊天机器人

- **媒体与交付工具（10 项）**：`analyze_media`、`download_media`、`get_task_status`、`list_tasks`、`cancel_task`、`download_collection`、`get_media_file`、`get_service_capabilities`、`deliver_media`、`get_delivery_status`。
- **设置、运维与 Cookie 控制工具（11 项）**：`get_settings`、`update_settings`、`get_bot_configs`、`update_bot_config`、`probe_bot`、`list_approvals`、`approve_request`、`cleanup_cache`、`list_cookie_statuses`、`disable_platform_cookie`、`remove_platform_cookie`。

管理页分为下载设置、功能权限、平台登录、服务与交付四区。QQ / Telegram 连接、MCP 配置与 AstrBot 示例见 [操作手册](agent-edition/docs/OPERATIONS.md)。
