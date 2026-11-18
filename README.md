# OmniDownloader Agent Edition

面向 AI Agent、QQ / Telegram 机器人的独立 Headless 媒体解析、下载与交付服务。

本仓库独立维护代码、版本、CI 和发行包。媒体引擎复用自 [OmniDownloader](https://github.com/baishu136/OmniDownloader)，来源与许可见 [UPSTREAM.md](UPSTREAM.md) 和 [LICENSE](LICENSE)。

- 官方 MCP Python SDK：stdio 与 Streamable HTTP，23 个媒体、设置管理、自动识别桥接、运维与 Cookie 权限控制工具。
- 免令牌本机直连与文件夹/代理工具：打开管理页直接进入设置工作区；提供原生文件夹选择窗口（无头环境降级为页内服务端浏览）、显式保存按键、重置默认设置及网络代理连通性测试。
- 下载可视化与报错直出：支持在设置页按节点开启/关闭「收到链接时」「下载开始时」「下载完成时（正在发送）」反馈或完全静默，内容支持自定义模板或交由 Agent 决定，并在任意失败时直接输出完整脱敏报错原因。
- 自动识别已接入桥接：自动识别 Streamable HTTP (`/mcp`)、MCP stdio (`--stdio`)、AstrBot 插件及本地 Bot 连接状态。
- 图文与打包分离 + 下载指令前缀：支持 `/us`（下载合集、分集视频、全部图片）与 `/zip`（打包成 ZIP 压缩包发送），支持独立设置媒体下载保存目录、图文保存目录与服务缓存目录。
- QQ / OneBot 11 / NapCat 消息发送设置：支持配置视频/图片以聊天消息或群/私聊文件发送，支持将合集/图集打包为合并转发聊天记录（`send_*_forward_msg`），并展示实时 Bot 连接与账号详情。
- 完善 Cookie 登录流程与权限隔离：支持哔哩哔哩官方扫码登录、拉起本机 Chrome / Edge 独立临时窗口自动读取、系统默认浏览器打开及 Netscape / 请求头 / JSON 数组导入；Agent 无法读取 Cookie 明文或填写/登录/启用，仅可查看非敏感状态、禁用或移除。

## Windows 本地独立部署与运行

- 工作根目录：`F:\AGENT\Gemini\OmniDownloader Agent Edition`
- 数据与缓存目录：`data/`（设置保存于 `data/settings.json`，无 `auth.json`）
- 打包可执行入口：`agent-edition/dist/OmniDownloaderAgent/OmniDownloaderAgent.exe`（或运行根目录 `启动服务.bat`）
- 首次启动会自动打开 `http://127.0.0.1:58080/`，直接进入设置页。
- MCP stdio 入口：`agent-edition/dist/OmniDownloaderAgent/OmniDownloaderAgentMCP.exe --stdio`

## 开发与测试

```powershell
.\.venv\Scripts\pytest.exe agent-edition\tests -q
.\.venv\Scripts\python.exe agent-edition\build_windows.py
.\.venv\Scripts\python.exe agent-edition\tools\portable_smoke.py --directory agent-edition\dist\OmniDownloaderAgent
```
