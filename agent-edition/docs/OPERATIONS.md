# OmniDownloader Agent Edition 0.1.0

这是独立的 Headless 产品版本，代码和产物位于 `agent-edition/`。现有 Android、iOS、Windows GUI 入口及发行脚本均保留。本版本在独立公开仓库 `baishu136/OmniDownloader-Agent-Edition` 的 `main` 分支维护。

## 部署

### Windows 便携包

解压 `OmniDownloader-Agent-Edition-0.1.0-windows-x64.zip`，双击 `OmniDownloaderAgent.exe`。无需另装 Python、FFmpeg、yt-dlp 或 WebView2。首次打开 `http://127.0.0.1:58080/`，后续可从托盘打开页面或安全退出。

访问令牌在 `%LOCALAPPDATA%\OmniDownloaderAgent\auth.json`。在管理页选择此文件，读取 `admin` 令牌并解锁。`agent` 是独立的媒体调用令牌；不要将管理员令牌提供给 Agent。只在浏览器标签页 sessionStorage 中保留管理令牌，锁定后清除。

主 EXE 为无控制台服务。`OmniDownloaderAgentMCP.exe` 是同包中的 stdio/工作进程入口；MCP 客户端通过管道启动它，不影响普通双击启动体验。两个 EXE 共用 `_internal`，不要单独移动其中一个。

开发模式（仓库根目录，Python 3.12+）：

```powershell
uv venv .local/agent-venv
uv pip install --python .local/agent-venv/Scripts/python.exe --require-hashes -r agent-edition/requirements.lock
$env:PYTHONPATH = (Resolve-Path agent-edition).Path
.local/agent-venv/Scripts/python.exe -m omni_agent --serve
```

### Docker / Linux

在 `agent-edition` 目录执行：

```sh
docker compose up -d --build
docker compose logs
docker compose exec omni-agent cat /data/auth.json
```

最后一条命令仅由管理员本地执行，用于首次读取初始化令牌，不要把输出发送给 Agent 或贴到日志。默认只映射宿主机 `127.0.0.1:58080`。`agent-data` 保存设置、加密凭据、密钥和 SQLite；`agent-cache` 保存工作目录及下载文件。两者都必须持久化。Linux 使用 Fernet 加密，密钥 `credentials/master.key` 必须仅当前用户可读；备份时应同时备份密钥及加密凭据，丢失密钥将无法恢复登录。

可选 `.env`：

```dotenv
OMNI_AGENT_ADMIN_TOKEN=至少24字符且与Agent令牌不同的随机管理员密钥
OMNI_AGENT_TOKEN=至少24字符的独立随机Agent密钥
```

留空则首次自动生成。其他变量：`OMNI_AGENT_DATA`、`OMNI_AGENT_CACHE`（首次启动的缓存父目录）、`OMNI_AGENT_HOST`、`OMNI_AGENT_PORT`、`OMNI_AGENT_PROXY`。外部 HTTPS 反向代理需设置 `OMNI_AGENT_ALLOWED_HOSTS=media.example.com` 以及 `OMNI_AGENT_PUBLIC_ORIGIN=https://media.example.com`，显式将代理接到容器端口；保持 Bearer 认证。不要直接对公网发布未加密 HTTP。代理不自动信任 `X-Forwarded-*` 头。

独立 Linux 源码部署使用同一模块入口；需 FFmpeg，YouTube JS挑战需要 Deno。Docker 已打包这些组件。Python wheel 本身不携带共享核心，源码部署须保留 `agent-edition/` 与 `windows/` 的相邻目录结构。

## MCP

官方 MCP Python SDK 2.3.0，支持当前 SDK 协议协商。HTTP 地址为 `/mcp`，使用 Streamable HTTP，不使用旧 SSE endpoint。

Windows stdio 示例（修改解压路径即可运行）：

```json
{"mcpServers":{"omni-agent":{"command":"C:\\OmniDownloaderAgent\\OmniDownloaderAgentMCP.exe","args":["--stdio"],"env":{"OMNI_AGENT_SERVER":"http://127.0.0.1:58080"}}}}
```

同一 Windows 用户默认读取本地 `auth.json` 中的 Agent 令牌。服务尚未启动时，stdio 桥会在后台启动本地服务。使用独立聊天身份时，在环境中设置 `OMNI_AGENT_TOKEN`。远程 stdio 桥只接受 HTTPS。

HTTP 客户端示例：

```json
{"mcpServers":{"omni-agent":{"url":"http://127.0.0.1:58080/mcp","headers":{"Authorization":"Bearer 实际Agent身份令牌"}}}}
```

示例文件位于 `examples/`；管理页提供包含实际路径的配置复制功能。HTTP 配置中的令牌需由管理员自行填入，普通设置与 MCP 不会返回凭据。

| 工具 | 实际行为 |
| --- | --- |
| analyze_media | 解析作品链接，`media_type=video/images`；只返回真实元数据、格式、合集摘要，不返回直链。 |
| download_media | 异步创建任务，返回任务 ID。 |
| get_task_status | 返回所属任务状态、进度、速度、文件数量、结果引用和错误代码。 |
| list_tasks | 按当前身份隔离，可按状态筛选，最多100条。 |
| cancel_task | 取消自己的请求；最后一个引用取消时终止核心进程与所有子进程。 |
| download_collection | 1起始的 `start/count` 范围，默认仅1项，受管理员上限限制。 |
| get_media_file | 按文件 ID 返回 `omni-file://` 引用、大小、MIME及SHA256；不接受文件路径。 |
| get_service_capabilities | 返回真实运行时、功能开关、限制、候选平台和连接检测结果。 |
| deliver_media | 异步上传所有任务文件到许可目标，返回交付 ID。 |
| get_delivery_status | 返回交付状态和已确认平台回执。 |

结果为 `{"ok":true,"result":{...}}` 或 `{"ok":false,"error":{"code":"...","message":"..."}}`。参数 Schema 由 SDK 注册，未知参数会拒绝。常见代码：`INVALID_PARAMETER`、`FEATURE_DISABLED`、`LIMIT_EXCEEDED`、`CONFIRMATION_REQUIRED`、`AUTH_REQUIRED`、`SSRF_BLOCKED`、`NOT_FOUND`、`CACHE_FULL`、`FILE_TOO_LARGE`、`BOT_REJECTED`、`DELIVERY_UNCERTAIN`、`CAPABILITY_UNAVAILABLE`。同一身份传相同 `request_key` 可安全重复查询原请求；若改变参数会报 `IDEMPOTENCY_CONFLICT`。

默认模式省略 `options`。自主选择例：

```json
{"url":"实际作品链接","options":{"use_defaults":false,"download_type":"AUDIO_ONLY","audio_format":"mp3"},"request_key":"chat-message-001"}
```

支持的下载类型：`VIDEO_WITH_AUDIO`、`VIDEO_ONLY`、`AUDIO_ONLY`、`GIF`、`COVER`、`IMAGE`、`IMAGE_ZIP`。画质参数为 `height`，容器为 `container=mp4/mkv/auto`。只允许受控输出配置，不允许 Agent 指定服务器路径。核心支持保留原视频编码、封装、音频转码及 GIF；`preserve_original=false` 明确返回不支持任意视频重编码。最高画质是当前账号真实可访问源中的选择，不保证4K、会员资源或所有编码均可用。

大于管理员确认阈值的批量请求、主页请求、原图ZIP、超过2160p的请求或按文件上限估算超过1GiB的任务，返回一次性 `approval_id`。管理员在管理页批准后，Agent 使用完全相同的参数和该ID重试；ID与身份、参数、配置、凭据版本绑定，10分钟失效，不可跨用户使用。主页功能默认关闭；当前提供有界的YouTube视频列表与X首个图片分页，均受最大解析/下载数量限制。其他平台没有可用无头接口时会明确失败，不启动浏览器绕过验证。

## 管理页

四区：下载默认值与上限；功能权限与缓存；平台 Cookie 导入/检测/退出；MCP、机器人、交付、确认与清理。支持本地深浅色主题，无外部 CDN、媒体播放器或下载操作首页。

更换缓存父目录前需停止活动任务、完成交付并清空缓存。服务只创建并管理父目录下的 `omni-agent-managed`，不会递归清理用户选择的父目录。代理仅允许管理员配置 HTTP/HTTPS/SOCKS5 URL，不接受URL内凭据。

Cookie 可从 Netscape 文件或请求头导入。导入不等于登录成功；当前实际账号检测接口仅覆盖 Bilibili 和 Pixiv，其他平台显示未验证。验证码、DRM、付费权益、浏览器辅助流程不自动绕过。凭据失效由检测或实际解析返回认证错误体现。需要浏览器完成的登录在外部浏览器完成后导入 Cookie。

## Telegram / QQ

管理页“服务与交付”配置独立的机器人令牌、API地址、上传上限和目标名单。先保存再检测连接。业务下载引擎不包含聊天平台代码。

Telegram 公共 API 使用 `https://api.telegram.org`，启用本地模式才可使用自建 Bot API Server。实现真实 multipart 的 sendVideo/sendAudio/sendPhoto/sendDocument，多文件逐个发送、逐个记录回执。公共上传文档/视频/音频上限50MiB、图片10MiB；本地 API 文档等最高2000MiB，并受管理员更低的上限约束。大图片自动转为文档以保留原图。使用回执中的 message_id 和匹配 chat.id 判断成功。不是读取消息的完整 Telegram 框架。

QQ目标格式为 `group:群号` 或 `private:QQ号`。NapCat 通过 `get_version_info`、`get_status` 和目标查询检测能力，分别调用 `upload_group_file` / `upload_private_file`。这些是协议端扩展，并非所有 OneBot 11 实现都有；不支持时返回能力错误。文件接口必须返回 file_id、id 或 message_id，只有成功状态而无标识的实现会进入 UNCERTAIN 并保留文件。

NapCat 服务必须能读取文件。配置服务端共享目录（例如 `/cache`）和 NapCat 对应路径（例如 `/omni-cache`），将同一个缓存卷挂到两端；适配器只映射已记录的受控文件，不接受 Agent 提供任意路径。QQ上传上限由管理员按协议端能力配置，不能保证每个账号拥有相同限制。

完整调用闭环示例：

```powershell
$env:OMNI_AGENT_TOKEN = '此聊天的独立身份令牌'
.local/agent-venv/Scripts/python.exe agent-edition/examples/bot_client.py --adapter telegram --target 123456789 --url '实际作品链接'
.local/agent-venv/Scripts/python.exe agent-edition/examples/bot_client.py --adapter onebot --target group:123456789 --url '实际作品链接'
```

下载后客户端自动排队交付并等待回执；发送与清理由服务完成。无 Bot 令牌环境不可执行真实发送验证。运行这些例子会真实发送到已授权目标。

### AstrBot

两种方式：在 AstrBot 的 MCP 设置中加入上述 HTTP配置，系统提示要求在 READY 后调用 deliver_media；或将 `examples/astrbot_plugin` 拷贝为 AstrBot 的 `data/plugins/astrbot_plugin_omni_agent`。

轻量插件注册 `/omni 作品链接` 和 LLM工具 `omni_download_and_deliver(url, download_type)`。聊天框架提供消息接收及自然语言 Agent，插件按消息来源绑定发送目标，LLM不能选择别人的聊天。

1. 在管理页为每个聊天创建独立身份，目标必须与 Bot 允许列表匹配。
2. 从 AstrBot 会话面板复制准确的 `unified_msg_origin`；按照 `chat-identities.example.json` 保存受限权限的 `data/omni-agent-chats.json`，其中每个会话使用不同的令牌。
3. 为 AstrBot 设置 `OMNI_AGENT_SERVER` 和可选 `OMNI_AGENT_CHAT_IDENTITIES` 文件路径，重启插件。
4. 当前聊天发 `/omni 实际作品链接`，或要求 Agent 调用配套工具。插件只排队，最终平台确认和清理在服务中继续进行。

独立令牌是权限边界；共享同一令牌的调用属于同一身份。不要把通用服务令牌交给不可信用户。插件源码按 AstrBot 当前公开的 Star、command、llm_tool、unified_msg_origin 接口编写；本环境未启动 AstrBot，真实框架与账号端到端验收仍需执行。

## 文件生命周期与恢复

请求：QUEUED → DOWNLOADING → PROCESSING → READY → SENDING → SENT → CLEANED；失败为FAILED，主动取消为CANCELLED，过期为EXPIRED。交付额外使用UNCERTAIN以表示不可安全推断的上传结果。实际媒体下载任务由旧 TaskManager 执行，SQLite记录服务请求/文件引用/交付，不替代解析和下载调度。

READY 不清理。发送失败保留。只有所有文件都有平台确认回执，交付才进入SENT；只有所有任务/交付引用释放，自动清理或“清理已发送缓存”才删除文件。多用户缓存复用会创建独立引用；第一个用户发送成功不会删除另一个用户待发的文件。失败缓存超过TTL可清理，但SENDING、QUEUED交付和UNCERTAIN始终阻止删除；管理员应核对不确定交付后决定重试。

上传网络中断或进程异常退出时不会自动重传，避免重复发送。明确拒绝与限流可有限重试；手动重试会跳过已持久化回执的文件。SQLite WAL、事务、唯一键保证状态与回执幂等。重启后活动下载安全结束为失败，不假装断点恢复成功；已完成文件、引用和回执保留。单状态目录只允许一个服务进程。Windows工作进程在内核Job Object中运行，取消或服务退出时销毁子进程树；Linux使用独立进程组和容器tini。

受控文件记录包含路径、大小和SHA256。删除前再次验证受控目录、链接/联接和文件摘要；拒绝目录逃逸、链接和被替换内容。过期工作目录仅按账本 UUID删除，绝不删除下载父目录或任意用户文件。不要用另一个程序往受控目录写入文件。

## 安全边界与已知限制

- 原始链接仅接收已识别平台的HTTP(S)地址。工作进程检查每次requests请求、yt-dlp请求、DNS与实际连接地址，禁止私网、回环、云元数据和特殊地址，跟随重定向仍检查连接。FFmpeg仅允许本地文件/管道协议，禁止媒体清单驱动FFmpeg访问网络。Generic extractor仅供核心内部解析已取得的媒体清单，不开放任意外部直链入口。
- 管理员显式指定的代理是可信网络基础设施，可访问其私网代理端口。代理必须配置目的地址ACL；代理自己的DNS/转发行为不受本进程完全控制，不应连接不可信的开放代理。HTTP(S)代理不会把SSRF防线延伸到远程代理内部。
- 系统或代理DNS若把平台映射为非公共的Fake-IP地址，会触发SSRF_BLOCKED；此时需要为服务配置返回真实公网地址的DNS/网络环境，不能简单关闭SSRF检查。
- 核心下载大小参数、每250ms的单文件/缓存监视、队列/并发/超时限制共同控制资源。硬磁盘配额仍建议用于多租户部署；流式写入存在一个监视周期和缓冲块的有限超量窗口。
- 默认版没有OAuth发现，使用独立Bearer令牌。支持自定义请求头的MCP客户端可直接接入；只支持OAuth的托管客户端需要另外的认证网关。
- 没有已授权的真实聊天测试账号时，只能进行模拟平台测试，不能宣称真实QQ/Telegram发送已验收。
- 平台能力依赖账号、网络和上游接口；管理页如实标记未验证/不支持的能力。无需和不会提供绕过会员、DRM、验证码的能力。

## 验证与构建

```powershell
uv pip install --python .local/agent-venv/Scripts/python.exe pytest pytest-asyncio pyinstaller pystray aiofiles
.local/agent-venv/Scripts/python.exe -m pytest agent-edition/tests -q
.local/agent-venv/Scripts/python.exe -m pytest windows/tests -q
.local/agent-venv/Scripts/python.exe agent-edition/build_windows.py
.local/agent-venv/Scripts/python.exe agent-edition/tools/portable_smoke.py --directory agent-edition/dist/OmniDownloaderAgent
```

Linux/Docker验证由 `.github/workflows/agent-edition.yml` 执行，包括构建、实际容器启动、HTTP/MCP、设置持久化与重启恢复。Windows单独构建并实际启动独立EXE。具体本次结果见 [VALIDATION.md](VALIDATION.md)。测试中的假平台/假提取器只存在于测试文件，生产服务始终调用实际核心和平台API。

参考官方接口：[MCP SDK](https://github.com/modelcontextprotocol/python-sdk)、[Telegram文件发送](https://core.telegram.org/bots/api#sending-files)、[NapCat上传群文件](https://napcat.apifox.cn/226658753e0)、[AstrBot公开API](https://github.com/AstrBotDevs/AstrBot/tree/master/astrbot/api)。
