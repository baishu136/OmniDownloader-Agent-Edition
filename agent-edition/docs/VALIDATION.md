# 验证记录

日期：2026-11-20。下列记录来自实际执行，模拟 API 和真实平台验收严格区分。

- Windows 原核心：`python -m pytest windows/tests -q`，142 通过，存在既有 Pydantic 弃用警告（新增 TikTok 图文拦截与图片解析、快手图集与图片后缀 `photoUrl` 视频误判拦截）。
- Agent 独立测试：`python -m pytest agent-edition/tests -q`，154 通过（2026-11-20 稳定性增量后），覆盖：
  - 结构化错误分类（`stage`/`retryable`/`suggested_action`）、未知错误按消息判定可重试，以及 URL、凭据与 Windows/POSIX 绝对路径脱敏；
  - 临时性错误的有上限指数退避重试、非可重试错误不重试、重试不重复登记文件、重试次数上限与结构化失败回报；
  - 重启时 `QUEUED` 继续调度、进行中任务显式失败、`READY` 缓存缺失标记 `FILE_MISSING`；交付前预检缺失或截断文件并拒绝创建交付记录；
  - Cookie 保存后解密回读、失败回滚且保持启用状态、上一版本备份与恢复接口、重启后可读，以及 Agent 接口不暴露回滚备份字段；
  - 环境自检报告（目录可写、磁盘余量、数据库、加密存储、FFmpeg/Deno、代理、Bot、凭据可读）不含 Cookie 明文或键名；
  - 交付状态逐文件列出 `sent` 标记，部分成功时可继续发送未完成文件；
  - 清理预览与实际清理计数一致、磁盘不足在下载前以 `DISK_FULL` 阻断、配置导出/导入校验与失败回退、任务 `next_action` 提示；
  - 聊天文本多链接识别（前缀指令、去重、追踪参数清理、SSRF 与非白名单标记）以及成品实际类型/尺寸元数据；
  - 首次启动标记、管理任务列表与仅可重试可恢复错误的约束；
  - 机器人交付代理策略（公网 Bot 绑定代理、本机/私网直连）；
  - 管理页“最近任务”区域与重试接口：真实启动服务后页面与 `/static/app.js` 正常返回、`node --check` 通过，创建失败任务后 `GET /api/admin/tasks` 返回 `next_action`，`POST .../retry` 将其重新置为 `QUEUED`（未做浏览器截图，本机浏览器控制不可用）；
  - 独立默认工作目录解析（`./data/` 及便携目录同级 `data/`）与无 `auth.json` 验证；
  - 免令牌 HTTP 管理接口与官方 MCP SDK 真实 `stdio` / Streamable HTTP 握手及 23 项工具（含 `list_bridges`）发现；
  - 设置读取/局部更新/自动保存/重置默认设置，以及 `download_dir`、`image_dir` 永久导出、页内目录浏览/创建、原生文件夹选择降级逻辑，以及仅限管理端与系统托盘的「开机自启动」开关（Windows 注册表 `HKCU\...\Run` 静默 `--no-browser --tray` 启动、容器/无桌面环境诚实禁用提示、与 Agent 零改写权限隔离）；
  - 网络代理格式校验、不可达端口快速报错与 HTTP 连通性延迟探测；
  - 图文自动识别、独立格式与打包分离：默认模式下自动识别/回退图文提取，不加 `/us` 时仅下载首图（或当前单集视频），支持 `/us`（全量）、`/zip`（打包）与 `/merge`（合并转发）任意组合解析，支持 `image_format`（`original`/`jpg`/`png`/`webp`，散图与 ZIP 包均统一转换并更新 `manifest.json`，多帧 GIF 动图保留动态与字节直通），以及 `task_files.ord` 严格保序；
  - QQ 与 Telegram 合集/图集发送验证与防图片误发视频防护：QQ 视频/图片消息与群文件切换、`.zip` 强制文件上传、合并转发（`send_group_forward_msg`）及默认 30 条上限（`qq_forward_max_items`）自动分批、单次交付探针缓存、Windows 盘符 `file:///` URI 转换；Telegram 与 OneBot 结合二进制文件头魔数与扩展名（`.jfif` 等）防误将图片/动图/ZIP 按视频发送（即便上游错传 `kind='video'` 或扩展名错标 `.mp4` 也不走 `sendVideo`）、`sendMediaGroup` 相册分批、连续混排（穿插 GIF/大文件）严格保序及 `BOT_REJECTED` / `BOT_FILE_TOO_LARGE` 单发降级 `sendDocument` 兜底、WebP/AVIF/BMP/JFIF EXIF 方向校正与内存无损转 JPEG/PNG、`is_profile()` 排除 TikTok `/photo/` 与 X `/u/status/` 单作品链接，以及 `worker.py` `COVER` 封面传递与魔数防伪装守卫；
  - 下载可视化高可靠机制、事件流与直发联动验证：SQLite `visual_events` 持久化出箱、`after_seq` 游标增量消费与 `wait_seconds` 长轮询在缓存秒命中及极速下载下零丢包、Telegram `sendMessage`/`editMessageText` 原地编辑刷新与 OneBot 群文本直发（受 `allowed_targets` 白名单约束且不重复发阶段）、`server_bot`/`caller_only`/`auto` 通道切换、`AgentError` 附带 `visualization` 错误载荷、`POST /api/admin/visual/test` 预览及试发、MCP 23 工具保持不变下的 `Context.report_progress` 实时通知、AstrBot `context.send_message`/`pending_events` 与 `OmniBotClient` 事件流与报错直出；
  - 已接入桥接自动识别：Streamable HTTP / MCP stdio / AstrBot 心跳注册、`list_bridges` 工具与离线注销检测；
  - Cookie 多格式导入（Netscape / 请求头 / JSON 数组）、环回 WebSocket+HTTP 多路 CDP Cookie 合并与脱敏宿主域解析、对齐 Android 版登录体验（首次「登录获取会话」与重验证「重新验证登录」切换、重新验证及独立容器防自动覆写 `require_explicit_save`、清除会话 Google 注销提醒与 `manage_only` 只管理模式、页内 Google/平台快捷导航、Google/独立登录档案多账号隔离容器 `data/browser-profiles/<id>_e<epoch>/` 与两阶段 `epoch`/`owner_token` 所有权校验安全退出）、多平台批量工作窗/后台无头一键免密同步、浏览器首选策略（Chrome 优先于 Edge 及可选切换）、防风控纯净无调试模式（屏蔽 `AutomationControlled`、Google 流程智能省去 `--remote-debugging-port`、Windows `WM_CLOSE` 优雅刷盘关闭及同内核 `.omni-browser.json` 匹配无头读回与一键 `relaunch-clean`）、系统默认浏览器打开接口、B站扫码流程及严格权限隔离（Agent 仅可读取非敏感状态、禁用与移除，绝无读取明文/UI 元数据/来源字段/`owner_token`/浏览器偏好/独立档案/浏览器同步或重置权限）；
  - 本机 `Host`/`Origin` 防护、兼容 Fake-IP / TUN（放行 `198.18.0.0/15` 与 `fc00::/18`，继续阻断本机回环/私网/云元数据与显式代理本地 DNS 误判）的精细化 SSRF 防护、受控路径边界、文件引用账本、异步取消进程树与核心 TaskManager 桥接。
- Windows 便携包（2026-11-20 本轮）：`python build_windows.py` 实际构建成功，产物 `dist/OmniDownloader-Agent-Edition-0.2.0-windows-x64.zip`（185,648,536 字节，SHA-256 `273f51ff9b7b0856ca3b2099dead311d8d9d0d7e603e7fab2f5587a9761072f9`）；`tools/portable_smoke.py --directory dist/OmniDownloaderAgent` 通过：无令牌管理页、stdio MCP 真实握手与 23 工具发现、设置更新、FFmpeg/Deno 发现、核心 worker 导入均正常。
- Windows 便携包（历史）：PyInstaller 实际构建（已移除 `pywebview` / `pythonnet` / `clr_loader`，保留 `websockets`、`qrcode`、`tkinter` 运行时）；`tools/portable_smoke.py` 及独立冻结进程实测通过（本机 Edge 检测、驱动器浏览、代理测试、桥接识别、零 `webview` 残留）。
- Linux/Docker 部署：由 GitHub Actions `.github/workflows/agent-edition.yml` 与 `tools/docker_smoke.py` 执行构建与验证。
- 未配置已授权的 QQ、Telegram 或 AstrBot 真实账号；相关平台发送与二维码/本地浏览器真实账号交互由使用方按实际环境验收。
