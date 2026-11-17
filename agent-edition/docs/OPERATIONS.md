# OmniDownloader Agent Edition

独立的 Headless 产品版本，代码位于 `agent-edition/`，默认独立数据与缓存位于工作区或便携目录的 `data/`。

## 部署与工作目录

### Windows 本地独立部署

- 默认数据与缓存目录：源码运行与工程内打包运行使用项目根目录下的 `data/`；将便携 ZIP 解压至独立文件夹运行时自动使用 EXE 同级 `data/`（保存 `settings.json`、`ledger.sqlite3`、`credentials/`、`sessions/` 与 `cache/omni-agent-managed/`），不再写入 `%LOCALAPPDATA%`。
- 已取消令牌授权设定：无 `auth.json`，打开 `http://127.0.0.1:58080/` 即可直接进入四区设置页；本机服务通过 Host 白名单与 Origin 同源防护限制外部跨站访问。
- 启动方式：双击 `OmniDownloaderAgent.exe`。首次启动自动打开管理页，后续可通过系统托盘菜单打开页面或安全退出。

开发与源码模式：

```powershell
.\.venv\Scripts\python.exe -m pytest agent-edition\tests -q
$env:PYTHONPATH = (Resolve-Path agent-edition).Path
.\.venv\Scripts\python.exe -m omni_agent --serve
```

### Docker / Linux

在 `agent-edition` 目录执行：

```sh
docker compose up -d --build
```

默认只映射宿主机 `127.0.0.1:58080`。`agent-data` 保存设置、加密凭据、密钥和 SQLite；`agent-cache` 保存工作目录及下载文件。如需外部 HTTPS 反向代理，请配置 `OMNI_AGENT_ALLOWED_HOSTS` 与 `OMNI_AGENT_PUBLIC_ORIGIN` 并在反代层做好访问控制。

## MCP 工具

支持官方 MCP Python SDK 2.3.0，提供 `stdio` 与 Streamable HTTP（`/mcp`），均免令牌直连。

Windows stdio 示例：

```json
{"mcpServers":{"omni-agent":{"command":"C:\\OmniDownloaderAgent\\OmniDownloaderAgentMCP.exe","args":["--stdio"],"env":{"OMNI_AGENT_SERVER":"http://127.0.0.1:58080"}}}}
```

HTTP 客户端示例：

```json
{"mcpServers":{"omni-agent":{"url":"http://127.0.0.1:58080/mcp"}}}
```

### 工具清单（21 项）

| 工具 | 实际行为 |
| --- | --- |
| `analyze_media` | 解析作品链接，`media_type=video/images`；返回真实元数据、格式、合集摘要，不返回直链。 |
| `download_media` | 异步创建下载任务，返回任务 ID。 |
| `get_task_status` | 返回任务状态、进度、速度、文件数量、结果引用和错误代码。 |
| `list_tasks` | 列出任务，可按状态筛选，最多 100 条。 |
| `cancel_task` | 取消活动任务并终止关联核心子进程。 |
| `download_collection` | 下载合集 1 起始范围（`start`/`count`）。 |
| `get_media_file` | 按文件 ID 返回 `omni-file://` 引用、大小、MIME 与 SHA256。 |
| `get_service_capabilities` | 返回运行时组件、支持平台、功能开关、限制与权限说明。 |
| `deliver_media` | 异步上传任务文件至已配置的 Telegram 或 QQ 目标。 |
| `get_delivery_status` | 查询交付状态与平台确认回执。 |
| `get_settings` | 读取全部下载默认值、画质/大小/并发上限、代理、缓存目录与主题设置。 |
| `update_settings` | 更新一项或多项服务设置（部分更新或全量更新）。 |
| `get_bot_configs` | 读取 Telegram 与 OneBot 机器人配置（不含明文 Token，仅返回 `has_token`）。 |
| `update_bot_config` | 修改 Telegram 或 OneBot 机器人配置。 |
| `probe_bot` | 检测机器人适配器连接状态。 |
| `list_approvals` | 列出待确认的大批量/主页操作请求。 |
| `approve_request` | 批准指定的 `approval_id`。 |
| `cleanup_cache` | 清理已交付或过期的受控缓存文件。 |
| `list_cookie_statuses` | 仅读取各平台 Cookie 的非敏感状态（平台名、是否导入、是否启用、校验状态）。 |
| `disable_platform_cookie` | 禁用指定平台或 `"all"` 全部平台的 Cookie（保留文件以便用户日后手动启用）。 |
| `remove_platform_cookie` | 彻底移除指定平台或 `"all"` 全部平台的 Cookie 凭据。 |

## Cookie 权限隔离规则

- **用户权限（Web 管理页）**：查看平台状态、粘贴/导入 Cookie 文件、在线检测有效性（支持 B站与 Pixiv）、启用/禁用切换、移除。
- **Agent 权限（MCP / `/api/tools/*`）**：仅可查看非敏感元状态（`list_cookie_statuses`）、禁用（`disable_platform_cookie`）或移除（`remove_platform_cookie`）。**Agent 绝无读取 Cookie 明文、填写/导入、登录或重新启用 Cookie 的权限。**
