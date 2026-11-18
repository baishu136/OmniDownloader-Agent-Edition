# 验证记录

日期：2026-11-17。下列记录来自实际执行，模拟 API 和真实平台验收严格区分。

- Windows 原核心：`python -m pytest windows/tests -q`，140 通过，存在既有 Pydantic 弃用警告。
- Agent 独立测试：`python -m pytest agent-edition/tests -q`，88 通过，覆盖：
  - 独立默认工作目录解析（`./data/` 及便携目录同级 `data/`）与无 `auth.json` 验证；
  - 免令牌 HTTP 管理接口与官方 MCP SDK 真实 `stdio` / Streamable HTTP 握手及 22 项工具发现；
  - 设置读取/局部更新/自动保存/重置默认设置，以及 `download_dir`、`image_dir` 永久导出与缓存清理互不干扰；
  - 图文下载与打包压缩包分离，`/us`、`/zip` 与 `/us /zip` 链接前缀解析与多集视频/全图执行验证；
  - QQ 视频/图片消息与群文件发送切换、`.zip` 强制文件上传、合集/图集打包为合并转发聊天记录（`send_group_forward_msg`）及 Bot 连接信息探测；
  - Cookie 严格隔离权限：Agent 仅可读取非敏感状态、禁用与移除，绝无读取明文、填写/导入或重新启用权限；
  - 本机 `Host`/`Origin` 防护、兼容 Fake-IP / TUN（放行 `198.18.0.0/15` 与 `fc00::/18`，继续阻断本机回环/私网/云元数据与显式代理本地 DNS 误判）的精细化 SSRF 防护、受控路径边界、文件引用账本、异步取消进程树与核心 TaskManager 桥接。
- 真实浏览器 UI 验证：通过无头 Edge Playwright 实测深浅色切换真实反转与自动保存持久化、自定义下拉菜单动画打开与选择、自动保存、重置设置、Android 风格 Cookie 焦点卡片（导入/禁用开关/清除）、Bot 摘要卡片及 390px 窄屏无横向溢出与零控制台报错。
- Windows 便携包：PyInstaller 实际构建；`tools/portable_smoke.py` 实测通过。
- Linux/Docker 部署：由 GitHub Actions `.github/workflows/agent-edition.yml` 与 `tools/docker_smoke.py` 执行构建与验证。
- 未配置已授权的 QQ、Telegram 或 AstrBot 真实账号；相关平台发送仅模拟 API 测试通过，不是实际平台端到端验收。
