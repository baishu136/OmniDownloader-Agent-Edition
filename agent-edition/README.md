# OmniDownloader Agent Edition

独立的 Headless 产品版本。复用 `../windows/core` 与 `../windows/engine`，不启动或替换 Android、iOS、Windows GUI。面向 QQ / Telegram 机器人及 AI Agent，提供下载、平台交付与缓存管理的完整服务闭环。

- 官方 MCP Python SDK 2.3：stdio、Streamable HTTP，10个带 Schema 的工具。
- 管理员权限、默认参数、画质/体积/数量/并发限制、一次性批量确认。
- Telegram multipart 上传、OneBot 11 / NapCat 群聊与私聊文件接口。
- SQLite 文件引用与回执账本，成功清理、失败保留、重启安全结束及不确定发送人工核对。
- 本地深浅色设置页、加密 Cookie 导入、独立聊天身份，无完整下载 GUI 或外部 CDN。
- Windows 便携包内置 Python/FFmpeg/yt-dlp/Deno；Docker 支持非 root 和只读根文件系统。

Windows：解压后双击 `OmniDownloaderAgent.exe`，打开 `http://127.0.0.1:58080/`，从 `%LOCALAPPDATA%\OmniDownloaderAgent\auth.json` 导入管理员令牌。stdio 使用同包 `OmniDownloaderAgentMCP.exe --stdio`。

Docker（当前目录）：

```sh
docker compose up -d --build
```

默认只映射本机端口。设置、凭据、状态与下载缓存保存在两个持久化卷中。

```text
omni_agent/
  api.py / mcp_server.py    管理 API、认证边界与 MCP 工具
  service.py / runner.py   请求策略、异步后台任务、核心进程桥
  worker.py                调用既有 TaskManager 与平台引擎
  store.py                 文件引用、交付回执、恢复与清理
  credentials.py           DPAPI/Fernet 与既有 Cookie 存储桥
  delivery.py              Telegram / OneBot 独立适配器
  security.py              URL、网络与受控目录检查
  static/                  无框架的四区设置页
examples/                  MCP 配置、完整调用客户端、AstrBot 插件
tests/                     协议、权限、核心桥、模拟平台、生命周期测试
tools/                     独立 EXE 与实际 Docker 冒烟检查
```

开发、部署、工具参数、QQ共享目录和AstrBot接入见 [操作手册](docs/OPERATIONS.md)。开发前检查见 [核心复用检查](docs/CORE_REVIEW.md)，本次执行结果见 [验证记录](docs/VALIDATION.md)。

候选平台不等于所有媒体均可下载。验证码、会员权限和DRM不会绕过；当前未使用真实QQ、Telegram或AstrBot账号进行发送验收。OneBot协议端不返回可核对标识时保留文件，绝不假装已发送。
