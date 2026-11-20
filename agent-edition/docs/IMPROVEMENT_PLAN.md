# 基础能力优化与稳定性重构：审查结论与实施计划

日期：2026-11-20。本文记录对当前代码的实际审查结论、改造边界与逐项计划。状态按「源码确认」「测试复现」「尚未验证」区分，避免把计划写成已完成。

## 1. 现状审查（基于源码与测试）

### 1.1 已具备且可复用的能力

- 独立的 Headless 服务、23 个 MCP 工具、stdio / Streamable HTTP 双传输，管理页与下载引擎解耦。
- 下载核心运行在一次性子进程（`runner.py` + `worker.py`），具备超时、文件上限、缓存上限与进程树终止。
- SQLite 账本（`store.py`）持久化任务、文件、交付、回执、审批与可视化事件；事务使用 `BEGIN IMMEDIATE`，文件登记包含大小与 SHA-256 校验。
- 交付侧已按 `receipts` 跳过已确认文件，合并转发 / Telegram 媒体组有分批与降级；发送结果不确定时拒绝自动重发（`RECONCILIATION_REQUIRED`）。
- Cookie 由 `Vault`（Windows DPAPI / Linux Fernet）加密，核心 `SessionStore` 按平台隔离，支持多账号容器与 `owner_token` 所有权校验。
- SSRF 防护兼容 Fake-IP / TUN，受控目录 `controlled()` 阻止路径穿越与链接；日志与错误文本已脱敏 URL 和凭据。
- 可视化事件流、`after_seq` 长轮询、服务端直发与 MCP 进度通知已存在。

### 1.2 源码确认的缺陷（本轮已修复）

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 1 | 错误只有 `code`/`message`，没有阶段、可重试性与建议动作 | Agent 无法判断是否重试，用户看到的原因不可操作 | `errors.py` 新增 `classify_error`，`AgentError` 与任务/交付返回 `stage`、`retryable`、`suggested_action` |
| 2 | 下载失败没有自动重试 | 一次网络抖动即失败 | `Service._call_with_retry`：仅重试可重试错误，有上限指数退避 + 抖动，遵守取消/停机 |
| 3 | `store.recover()` 把 `QUEUED` 也标为失败 | 重启后已提交但未开始的任务被静默丢弃 | 保留 `QUEUED` 交给调度器；仅进行中任务显式失败 |
| 4 | `READY` 任务的缓存文件消失后仍显示可交付 | 交付时才失败，状态误导 | 启动时核对文件存在与大小，标记 `FILE_MISSING` |
| 5 | 交付预检只看账本状态，不核对磁盘 | 缺失/截断文件在发送阶段才报错 | `create_delivery` 在创建交付记录前校验文件 |
| 6 | Cookie 导入先覆盖后校验，无回滚 | 一次失败捕获会毁掉仍有效的登录 | 先写入并解密回读，失败恢复上一版本与启用状态；保留上一版本并可回滚 |
| 7 | `safe_text` 不脱敏本机绝对路径 | 服务端直发报错可能泄露操作员目录 | Windows / UNC / POSIX 路径统一替换 `[PATH]` |
| 8 | 本地补丁直接丢弃 `error` 阶段直发 | 上游测试失败，用户收不到失败原因 | 改为脱敏后正常直发，删除该补丁 |

### 1.3 测试复现

- 改造前基线：`pytest agent-edition/tests -q` → **111 通过 / 1 失败**。失败项为 `test_visual_events_outbox_server_bot_notify_and_mcp_progress`，原因是本地「跳过 error 直发」补丁与上游断言冲突。
- 改造后：**154 通过 / 0 失败**（新增 `test_errors.py` 8 项、`test_reliability.py` 17 项、`test_credentials.py` 7 项、`test_links.py` 5 项、`test_outputs.py` 2 项、`test_delivery_proxy.py` 4 项；原失败项恢复通过）。

### 1.4 部分支持或尚未实现（未在本轮伪装为完成）

- **暂停 / 继续**：核心 `TaskManager` 不提供断点续传，当前只有取消与重试；状态机没有 `PAUSED`。不能把「终止后重下」冒充无损续传。
- **在线校验**：仅 B 站与 Pixiv 有无头校验接口，其余平台只能标记「已保存但未验证」，这是诚实状态而非缺陷。
- **自动重试只覆盖下载阶段**；交付阶段仍需人工核对 `UNCERTAIN`，避免重复发送。
- **环境自检 / 一键诊断报告**：`capabilities` 已报告 FFmpeg/Deno，但没有目录可写、磁盘空间、数据库、代理、Bot、凭据读取的统一检查接口。
- **首次启动向导、任务管理页、配置备份迁移、机器人配额、分阶段代理策略**：尚未实现。
- **Windows / Linux / Docker 构建与真实 Bot 验收**：本轮未重新执行；测试均为本地模拟，不代表真实平台验收。

## 2. 实施计划（按 Sprint 拆分）

### Sprint 01：稳定性基础（进行中）

| 工作项 | 目标 | 涉及文件 | 依赖 | 测试 | 状态 |
|---|---|---|---|---|---|
| S1-1 结构化错误 | 统一错误码、阶段、可重试性、建议动作 | `errors.py`、`runner.py`、`store.py` | 无 | `test_errors.py` | ✅ 完成 |
| S1-2 失败自动重试 | 有上限退避、抖动、取消优先、不重复登记 | `service.py`、`config.py` | S1-1 | `test_reliability.py` | ✅ 完成 |
| S1-3 重启恢复 | `QUEUED` 继续、进行中失败、缓存缺失核对 | `store.py` | S1-1 | `test_reliability.py` | ✅ 完成 |
| S1-4 交付预检 | 发送前核对文件存在与大小 | `store.py` | 无 | `test_reliability.py` | ✅ 完成 |
| S1-5 Cookie 提交/回读/回滚 | 失败不覆盖、上一版本可恢复、重启可读 | `service.py`、`api.py` | 无 | `test_credentials.py` | ✅ 完成（核心） |
| S1-6 失败原因可直发且不泄露路径 | 脱敏后输出完整原因 | `errors.py`、`service.py` | S1-1 | 既有可视化测试 | ✅ 完成 |
| S1-7 环境自检 | 目录/磁盘/数据库/代理/Bot/凭据检查与脱敏报告 | `service.py`、`api.py`、管理页 | S1-1 | `test_reliability.py` | ✅ 完成（API，管理页入口待接） |
| S1-8 交付部分恢复明细 | 逐文件发送状态与安全续发接口 | `store.py`、`service.py` | S1-4 | `test_reliability.py` | ✅ 完成（续发沿用既有 receipts 机制） |
| S1-9 Cookie 候选/验证增强 | 可校验平台失败时保留有效版本并提示 | S1-5 基础上 | S1-5 | `test_credentials.py` | ✅ 完成 |

### Sprint 02：下载体验（进行中）

- 智能链接解析：整段聊天文案提取、多链接、重复识别已完成服务端工具（`extract_chat_links` + `POST /api/admin/links/extract`，含指令、去重、追踪参数与 SSRF/白名单标记）；短链跳转仍由解析流程执行 SSRF 校验；聊天机器人端的自动拆分仍待接入。
- 画质/格式：只在可用格式内选择、降级需显式允许已由既有策略实现；本轮新增成品实际 `outputs` 元数据（类型、扩展名、字节、宽高、是否有音轨），不返回路径。
- 文件与缓存：原子写入、同名冲突、SHA-256 已完成；磁盘空间预检与清理预览本轮完成（`DISK_FULL`、`GET /api/admin/cleanup/preview`）。
- 进度反馈：阶段、速度、最终交付状态已有；已下载字节/总字节依赖底层核心进度，尚未实现。
- 代理诊断：已有代理格式校验、连通性测试与环境自检；本轮补齐机器人交付代理策略（公网 Bot 走代理、本机/私网直连）。解析/下载分阶段使用情况与失败归因尚未实现。

### Sprint 03：管理与接入（未开始）

- 首次启动向导：后端 `GET/POST /api/admin/onboarding` 已完成（已有配置不算新用户、可跳过）；管理页向导界面待接入。
- 本地任务管理页：后端 `GET /api/admin/tasks`、`POST /api/admin/tasks/{id}/retry` 已完成（仅可重试可恢复错误）；管理页界面待接入。
- 配置备份迁移：已完成 `GET /api/admin/config/export`、`POST /api/admin/config/import`（校验 + `settings.json.bak` 备份；凭据仍只在 Vault）。
- MCP 工具结果结构化：任务结果新增 `stage`/`retryable`/`suggested_action`/`next_action`；工具数量与参数保持 23 项不变。调用次数收敛待做。
- 机器人配额：已完成按 `owner` 的 `max_active_per_owner`；群组/文件大小/每日配额与限流待做。

### Sprint 04：质量验收（未开始）

- 长时间运行内存、句柄、SQLite 锁竞争与大文件流式处理。
- 安全回归（SSRF、重定向、路径穿越、符号链接、ZIP 内容、越权）。
- Windows 便携包、Docker、MCP 真实握手回归。
- 文档与发布前检查。

## 3. 兼容性与风险

- 错误载荷新增字段是**追加式**，旧客户端读取 `code`/`message` 不受影响；管理接口新增 `restore` 路由与设置项 `task_retries` / `retry_base_seconds`，旧 `settings.json` 自动补默认值。
- `Settings` 仍为 `extra="forbid"`：旧版本收到新字段会报错，这是既有的严格校验行为，升级时需同版本配套。
- 重试会重新调用下载核心；重试前会删除本次尝试未登记的临时产物，账本中的文件不受影响。真实网络下的重试行为尚未做端到端验收。
- 重启后 `QUEUED` 会自动继续执行，这是行为变化；如需「只恢复不执行」，应在后续提供配置开关。

## 4. 验收命令

```sh
python -m pytest agent-edition/tests -q
```

预期：154 通过。Windows 便携包已实际构建并完成 stdio MCP 冒烟；Docker 构建因本机未安装 Docker 未执行。
