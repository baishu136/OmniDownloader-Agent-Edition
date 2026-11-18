# OmniDownloader Agent Edition

独立的 Headless 产品版本。复用 `../windows/core` 与 `../windows/engine`，面向 QQ / Telegram 机器人及 AI Agent，提供下载、设置管理、平台交付、桥接识别、下载可视化与缓存管理的完整服务闭环。

- 官方 MCP Python SDK 2.3：stdio、Streamable HTTP，23 个带严格 Schema 的工具（含 `list_bridges` 自动桥接识别）。
- 免令牌本机直连管理：用户与 Agent 均可读取/修改/重置下载参数、打包方式、存储目录、QQ 消息/文件/转发模式、下载可视化通知规则、功能开关、代理与机器人配置。
- 目录选择与网络代理诊断：提供桌面系统文件夹选择弹窗与跨环境 页内服务端目录浏览器、目录保存按键及代理 TCP/HTTP 连通性诊断测试。
- 链接前缀指令：支持 `/us`（下载合集、分集视频、全部图片）与 `/zip`（打包为 ZIP 压缩包发送）。
- 完善 Cookie 登录流程与托管同步：支持哔哩哔哩官方扫码、持久化专属托管 Chrome / Edge 档案（一次登录长期免密续签与多平台批量同步，自动多路 CDP 捕获含 `HttpOnly`）、默认浏览器打开以及 Netscape / Cookie 请求头 / 浏览器 JSON 数组导入与有效期检测；严格维持 Agent 零明文读写隔离权限。
- 下载可视化与报错直出：支持在设置页按节点勾选「收到链接时」「下载开始时」「下载完成时（正在发送）」反馈开关或全关静默，内容可选择「自定义模板」或「由 Agent 决定」，并在任意报错时直接输出脱敏后的真实报错内容。
- Telegram multipart 上传、OneBot 11 / NapCat 群聊/私聊消息、群文件与合并转发聊天记录交付，并自动识别已接入的 MCP / AstrBot / Bot 桥接。

Windows：运行根目录 `启动服务.bat` 或 `dist\OmniDownloaderAgent\OmniDownloaderAgent.exe`，打开 `http://127.0.0.1:58080/` 直接管理设置。stdio 使用同包 `OmniDownloaderAgentMCP.exe --stdio`。
