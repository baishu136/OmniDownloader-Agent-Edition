# OmniDownloader Agent Edition

独立的 Headless 产品版本。复用 `../windows/core` 与 `../windows/engine`，面向 QQ / Telegram 机器人及 AI Agent，提供下载、设置管理、平台交付、桥接识别、下载可视化与缓存管理的完整服务闭环。

- 官方 MCP Python SDK 2.3：stdio、Streamable HTTP，23 个带严格 Schema 的工具（含 `list_bridges` 自动桥接识别）。
- 免令牌本机直连管理：用户与 Agent 均可读取/修改/重置下载参数、打包方式、存储目录、QQ 消息/文件/转发模式、下载可视化通知规则、功能开关、代理与机器人配置；管理页与 Windows 系统托盘菜单均支持本机**开机自启动**开关（仅管理端可控，Agent 不可改写宿主机启动项）。
- 目录选择与网络代理诊断：提供桌面系统文件夹选择弹窗与跨环境 页内服务端目录浏览器、目录保存按键及代理 TCP/HTTP 连通性诊断测试。
- 链接前缀指令与图文独立格式设置：支持 `/us`（下载合集、分集视频、全部图片；不加 `/us` 时图文仅下载首图）、`/zip`（打包为 ZIP 压缩包发送）与 `/merge`（强制合并转发/媒体组发送），默认模式自动识别图文作品；支持独立配置 `image_format`（`original` / `jpg` / `png` / `webp`，自动保留多帧 GIF 动画，本地保存与机器人交付统一生效）并保持 `task_files.ord` 严格原序。
- 完善 Cookie 登录流程与多账号隔离容器：对齐 Android 版「登录获取会话 / 重新验证登录」及防误覆写策略（`require_explicit_save`），优先 Google Chrome 并支持页面选择本机 Edge/Chrome/Brave，内置**防风控纯净无调试模式**（Google 登录流程自动关闭调试端口、彻底解决 Edge / Chrome 无法登录 Google 问题），提供清除后 Google 账号注销提示与 `manage_only` 管理页、页内快捷导航、Google / 独立登录档案（多账号隔离与 `owner_token` 安全退出）、哔哩哔哩官方扫码、持久档案免密同步与默认浏览器导入；严格维持 Agent 零明文及零浏览器偏好/独立档案读写隔离。
- 下载可视化高可靠反馈与报错直出：采用 SQLite `visual_events` 事件流与 `after_seq`/`wait_seconds` 长轮询防极速下载/缓存命中丢阶段，支持 `auto` / `server_bot`（QQ/Telegram 机器人白名单即时文本直发及 Telegram 原地编辑刷新）/ `caller_only` 通道、MCP `Context.report_progress` 进度推送、管理页预览与测试通知，以及任意报错直接输出脱敏报错内容。
- Telegram 相册/动图/WebP/AVIF 兼容与 OneBot 11 / NapCat 群聊/私聊消息、群文件及合并转发（默认单批 30 条上限自动分批 `qq_forward_max_items`）交付，并自动识别已接入的 MCP / AstrBot / Bot 桥接。

Windows：运行根目录 `启动服务.bat` 或 `dist\OmniDownloaderAgent\OmniDownloaderAgent.exe`，打开 `http://127.0.0.1:58080/` 直接管理设置。stdio 使用同包 `OmniDownloaderAgentMCP.exe --stdio`。
