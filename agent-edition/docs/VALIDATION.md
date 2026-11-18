# 验证记录

日期：2026-11-18。下列记录来自实际执行，模拟 API 和真实平台验收严格区分。

- Windows 原核心：`python -m pytest windows/tests -q`，140 通过，存在既有 Pydantic 弃用警告。
- Agent 独立测试：`python -m pytest agent-edition/tests -q`，98 通过，覆盖：
  - 独立默认工作目录解析（`./data/` 及便携目录同级 `data/`）与无 `auth.json` 验证；
  - 免令牌 HTTP 管理接口与官方 MCP SDK 真实 `stdio` / Streamable HTTP 握手及 23 项工具（含 `list_bridges`）发现；
  - 设置读取/局部更新/自动保存/重置默认设置，以及 `download_dir`、`image_dir` 永久导出、页内目录浏览/创建与原生文件夹选择降级逻辑；
  - 网络代理格式校验、不可达端口快速报错与 HTTP 连通性延迟探测；
  - 图文下载与打包压缩包分离，`/us`、`/zip` 与 `/us /zip` 链接前缀解析与多集视频/全图执行验证；
  - QQ 视频/图片消息与群文件发送切换、`.zip` 强制文件上传、合集/图集打包为合并转发聊天记录（`send_group_forward_msg`）及 Bot 连接信息探测；
  - 下载可视化规则与 AstrBot 插件联动验证：开关控制收到/开始/完成（正在发送）三阶段反馈、自定义模板防注入渲染、Agent 决定模式、`visual_enabled=false` 静默以及下载/发送失败时 `error_message` 直接输出；
  - 已接入桥接自动识别：Streamable HTTP / MCP stdio / AstrBot 心跳注册、`list_bridges` 工具与离线注销检测；
  - Cookie 多格式导入（Netscape / 请求头 / JSON 数组）、环回 WebSocket+HTTP 多路 CDP Cookie 合并与脱敏宿主域解析、本地桌面浏览器（Chrome / Edge）隔离会话防抖确认/即时手动抓取/关窗自动兜底挽救/阶段进度遥测、系统默认浏览器打开接口、B站扫码流程及严格权限隔离（Agent 仅可读取非敏感状态、禁用与移除，绝无读取明文/UI 元数据/来源字段、登录/导入或重新启用权限）；
  - 本机 `Host`/`Origin` 防护、兼容 Fake-IP / TUN（放行 `198.18.0.0/15` 与 `fc00::/18`，继续阻断本机回环/私网/云元数据与显式代理本地 DNS 误判）的精细化 SSRF 防护、受控路径边界、文件引用账本、异步取消进程树与核心 TaskManager 桥接。
- Windows 便携包：PyInstaller 实际构建（已移除 `pywebview` / `pythonnet` / `clr_loader`，保留 `websockets`、`qrcode`、`tkinter` 运行时）；`tools/portable_smoke.py` 及独立冻结进程实测通过（本机 Edge 检测、驱动器浏览、代理测试、桥接识别、零 `webview` 残留）。
- Linux/Docker 部署：由 GitHub Actions `.github/workflows/agent-edition.yml` 与 `tools/docker_smoke.py` 执行构建与验证。
- 未配置已授权的 QQ、Telegram 或 AstrBot 真实账号；相关平台发送与二维码/本地浏览器真实账号交互由使用方按实际环境验收。
