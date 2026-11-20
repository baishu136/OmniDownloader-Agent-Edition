# OmniDownloader Agent Edition

面向 AI Agent、QQ / Telegram 机器人的独立 Headless 媒体解析、下载与交付服务。

本仓库独立维护代码、版本、CI 和发行包。媒体引擎复用自 [OmniDownloader](https://github.com/baishu136/OmniDownloader)，来源与许可见 [UPSTREAM.md](UPSTREAM.md) 和 [LICENSE](LICENSE)。

- 官方 MCP Python SDK：stdio 与 Streamable HTTP，23 个媒体、设置管理、自动识别桥接、运维与 Cookie 权限控制工具。
- 免令牌本机直连与文件夹/代理工具：取消 `auth.json` 与令牌锁屏，打开管理页直接进入设置工作区；提供原生文件夹选择弹窗（无头环境降级为页内服务端浏览）、显式保存按键、重置默认设置及网络代理连通性测试（兼容 Fake-IP / TUN）。
- 下载可视化高可靠反馈与报错直出：基于持久化事件流 `visual_events` 与 `after_seq`/`wait_seconds` 长轮询防止极速下载或缓存命中丢失状态，支持按节点开关或全关静默、自定义模板/Agent 决定模式、`server_bot` 白名单 QQ/Telegram 机器人即时文本直发（含 Telegram 同任务原地编辑刷新）与 MCP 进度通知、页内预览及试发测试，以及任意报错直接输出完整脱敏报错原因。
- 稳定性与可恢复性：临时性下载错误按 `task_retries` / `retry_base_seconds` 有上限退避重试；重启时排队任务继续、进行中任务显式失败、缓存缺失的 `READY` 任务标记 `FILE_MISSING`；交付前校验文件存在与大小并逐文件回报发送状态；错误统一带 `stage` / `retryable` / `suggested_action`；`GET /api/admin/diagnostics` 提供不含凭据的环境自检；Cookie 保存后解密回读，失败不覆盖旧凭据并支持 `POST /api/admin/sessions/{platform}/restore` 回滚。
- 自动识别已接入桥接：自动追踪与扫描 Streamable HTTP (`/mcp`)、MCP stdio (`--stdio`)、AstrBot 插件及本地 Bot 连接状态。
- 图文独立格式、智能识别与指令前缀：默认模式自动识别图文作品，支持 `/us`（下载合集、分集视频、全部图片；不加 `/us` 时图文仅下载首图）、`/zip`（打包成 ZIP 压缩包发送）与 `/merge`（以合并转发或媒体组相册发送）；支持单独设置图片输出格式 `image_format`（`original` / `jpg` / `png` / `webp`，遇动图自动保留动态）及独立设置媒体/图文/缓存目录。
- QQ / OneBot 11 / NapCat 与 Telegram 合集发送：严格保持多图/合集原始顺序，支持配置 QQ 视频/图片以聊天消息或群/私聊文件发送、将合集/图集打包为合并转发聊天记录（`send_*_forward_msg`，默认单批 30 条上限 `qq_forward_max_items` 自动分批），以及 Telegram `sendMediaGroup` 相册聚合、WebP/AVIF 内存转换与 GIF `sendAnimation`。
- 完善 Cookie 登录流程与多账号隔离容器：对齐 Android 版「登录获取会话 / 重新验证登录」与防自动覆盖策略，支持 Chrome 优先与本机 Edge/Chrome/Brave 选择及**纯净无调试模式**（Google 登录流程自动屏蔽调试特征与 `--remote-debugging-port`，彻底解决 Edge / Chrome 无法登录 Google 问题）、清除后 `manage_only` Google 账号管理、页内快捷跳转与一键纯净重开、多账号独立浏览器容器隔离与所有权安全退出、哔哩哔哩官方扫码、持久档案免密同步与多格式导入；Agent 无法读取 Cookie 明文、来源、浏览器偏好或独立档案，仅可查看非敏感状态、禁用或移除。
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

## ☕ 赞助与支持

如果您觉得本项目对您有所帮助，欢迎赞助支持作者！您的支持是持续维护与迭代的最大动力。

| 微信支付 (WeChat Pay) | 支付宝 (Alipay) |
| :---: | :---: |
| <img src="docs/images/sponsor_wechat.jpg" width="240" alt="微信支付" /> | <img src="docs/images/sponsor_alipay.jpg" width="240" alt="支付宝" /> |
