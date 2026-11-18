# OmniDownloader Agent Edition

面向 AI Agent、QQ / Telegram 机器人的独立 Headless 媒体解析、下载与交付服务。

本仓库独立维护代码、版本、CI 和发行包。媒体引擎复用自 [OmniDownloader](https://github.com/baishu136/OmniDownloader)，来源与许可见 [UPSTREAM.md](UPSTREAM.md) 和 [LICENSE](LICENSE)。

- 官方 MCP Python SDK：stdio 与 Streamable HTTP，23 个媒体、设置管理、自动识别桥接、运维与 Cookie 权限控制工具。
- 免令牌本机直连与文件夹/代理工具：取消 `auth.json` 与令牌锁屏，打开管理页直接进入设置工作区；提供原生文件夹选择弹窗（无头环境降级为页内服务端浏览）、显式保存按键、重置默认设置及网络代理连通性测试（兼容 Fake-IP / TUN）。
- 下载可视化与报错直出：支持在设置页按节点选择返回「收到链接时」「下载开始时」「下载完成时（正在发送）」或全关静默，内容支持自定义模板或交由 Agent 决定，并在任意失败时直接输出完整脱敏报错原因。
- 自动识别已接入桥接：自动追踪与扫描 Streamable HTTP (`/mcp`)、MCP stdio (`--stdio`)、AstrBot 插件及本地 Bot 连接状态。
- 图文与打包分离 + 下载指令前缀：支持 `/us`（下载合集、分集视频、全部图片）与 `/zip`（打包成 ZIP 压缩包发送），支持独立设置媒体下载保存目录、图文保存目录与服务缓存目录。
- QQ / OneBot 11 / NapCat 消息发送设置：支持配置视频/图片以聊天消息或群/私聊文件发送，支持将合集/图集打包为合并转发聊天记录（`send_*_forward_msg`），并展示实时 Bot 连接与账号详情。
- 完善 Cookie 登录流程与多账号隔离容器：对齐 Android 版「登录获取会话 / 重新验证登录」与防自动覆盖策略、清除后 `manage_only` Google 账号管理、页内快捷跳转导航、多账号独立浏览器容器隔离与所有权安全退出、哔哩哔哩官方扫码、持久托管 Chrome / Edge 免密同步与 Netscape / 请求头 / JSON 数组导入；Agent 无法读取 Cookie 明文、来源或独立档案，仅可查看非敏感状态、禁用或移除。
- 独立本地工作目录：源码运行默认使用工程根目录下的 `./data/`，独立便携包使用工作区或 EXE 同级 `./data/`，不再写入 `%LOCALAPPDATA%`。

[下载发行包](https://github.com/baishu136/OmniDownloader-Agent-Edition/releases) · [完整部署与接入](agent-edition/docs/OPERATIONS.md) · [验证记录](agent-edition/docs/VALIDATION.md) · [模块结构](agent-edition/README.md)

## Windows

下载 Agent Edition ZIP，解压到独立目录后双击 `OmniDownloaderAgent.exe`。首次自动打开 `http://127.0.0.1:58080/` 直接进入设置页，免令牌即可配置下载、存储目录、QQ 发送方式、下载可视化规则、平台 Cookie 与机器人交付。

工作与数据目录默认保存于所在独立目录的 `data/`（设置位于 `data/settings.json`），无需另装 Python、FFmpeg、yt-dlp 或额外浏览插件。stdio 客户端使用同包 `OmniDownloaderAgentMCP.exe --stdio`。

## Docker

```sh
git clone https://github.com/baishu136/OmniDownloader-Agent-Edition.git
cd OmniDownloader-Agent-Edition/agent-edition
docker compose up -d --build
```

默认只映射宿主机本地端口 `127.0.0.1:58080`。配置、凭据、缓存与任务状态使用持久化卷。

## MCP 与聊天机器人

- **媒体与交付工具（10 项）**：`analyze_media`、`download_media`、`get_task_status`、`list_tasks`、`cancel_task`、`download_collection`、`get_media_file`、`get_service_capabilities`、`deliver_media`、`get_delivery_status`。
- **设置、桥接、运维与 Cookie 控制工具（13 项）**：`get_settings`、`update_settings`、`reset_settings`、`list_bridges`、`get_bot_configs`、`update_bot_config`、`probe_bot`、`list_approvals`、`approve_request`、`cleanup_cache`、`list_cookie_statuses`、`disable_platform_cookie`、`remove_platform_cookie`。

QQ / Telegram 连接、链接前缀指令、下载可视化、MCP 配置与 AstrBot 示例见 [操作手册](agent-edition/docs/OPERATIONS.md)。
