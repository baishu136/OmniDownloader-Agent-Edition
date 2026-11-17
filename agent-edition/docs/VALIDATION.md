# 验证记录

日期：2026-11-17。下列记录来自实际执行，模拟 API 和真实平台验收严格区分。

- Windows 原核心：`python -m pytest windows/tests -q`，140 通过，存在既有 Pydantic 弃用警告。
- Agent 独立测试：`python -m pytest agent-edition/tests -q`，82 通过，覆盖：
  - 独立默认工作目录解析（`./data/` 及便携目录同级 `data/`）与无 `auth.json` 验证；
  - 免令牌 HTTP 管理接口与官方 MCP SDK 真实 `stdio` / Streamable HTTP 握手及 21 项工具发现；
  - 用户与 Agent 直接读写服务设置、脱敏机器人配置、缓存清理与大批量请求审批；
  - Cookie 严格隔离权限：Agent 仅可读取非敏感状态、禁用与移除（且禁用后工作进程不再读取 Cookie），绝无读取明文、填写/导入或重新启用权限；
  - 本机 `Host`/`Origin` 防护、SSRF、受控路径边界、文件引用账本、异步取消进程树与核心 TaskManager 桥接。
- Windows 便携包：PyInstaller 实际构建；`tools/portable_smoke.py` 实测独立 EXE 启动、免令牌设置保存、`stdio` MCP 21 工具发现与设置/Cookie 状态调用、FFmpeg/Deno 发现及核心工作进程导入通过。
- Linux/Docker 部署：由 GitHub Actions `.github/workflows/agent-edition.yml` 与 `tools/docker_smoke.py` 执行构建与重启持久化/模拟交付验证。
- 未配置已授权的 QQ、Telegram 或 AstrBot 真实账号；相关平台发送仅模拟 API 测试通过，不是实际平台端到端验收。
