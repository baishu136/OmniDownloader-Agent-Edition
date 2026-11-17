# OmniDownloader Agent Edition

独立的 Headless 产品版本。复用 `../windows/core` 与 `../windows/engine`，不启动或替换 Android、iOS、Windows GUI。面向 QQ / Telegram 机器人及 AI Agent，提供下载、设置管理、平台交付与缓存管理的完整服务闭环。

- 官方 MCP Python SDK 2.3：stdio、Streamable HTTP，21 个带严格 Schema 的工具。
- 免令牌本机直连管理：用户与 Agent 均可读取/修改下载参数、功能开关、缓存/代理、审批请求与机器人配置。
- Cookie 权限隔离：平台 Cookie 只能由用户在本地管理页导入与启用；Agent 仅可查看非敏感状态、禁用（`disable_platform_cookie`）与移除（`remove_platform_cookie`），不可读取或写入 Cookie。
- Telegram multipart 上传、OneBot 11 / NapCat 群聊与私聊文件接口。
- SQLite 文件引用与回执账本，成功清理、失败保留、重启安全结束及不确定发送人工核对。
- 本地深浅色设置页、加密 Cookie 存储（Windows DPAPI / Linux Fernet），无完整下载 GUI 或外部 CDN。
- 独立本地工作目录 `data/`；Windows 便携包内置 Python/FFmpeg/yt-dlp/Deno；Docker 支持非 root 和只读根文件系统。

Windows：运行根目录 `启动服务.bat` 或 `dist\OmniDownloaderAgent\OmniDownloaderAgent.exe`，打开 `http://127.0.0.1:58080/` 直接管理设置。stdio 使用同包 `OmniDownloaderAgentMCP.exe --stdio`。

Docker（当前目录）：

```sh
docker compose up -d --build
```

默认只映射宿主机本地端口。设置、凭据、状态与下载缓存保存在两个持久化卷中。

```text
omni_agent/
  api.py / mcp_server.py    免令牌管理 API、本机安全边界与 21 个 MCP 工具
  service.py / runner.py   设置管理、Cookie 权限隔离、异步任务与核心进程桥
  worker.py                调用既有 TaskManager 与平台引擎
  store.py                 文件引用、交付回执、恢复与清理
  credentials.py           DPAPI/Fernet 与既有 Cookie 存储桥
  delivery.py              Telegram / OneBot 独立适配器
  security.py              URL、网络与受控目录检查
  static/                  免登录四区管理页
examples/                  MCP 配置、完整调用客户端、AstrBot 插件
tests/                     协议、设置管理、Cookie 隔离、生命周期测试
tools/                     独立 EXE 与 Docker 冒烟检查
```
