# 开发前检查与实施顺序

2026-11-17，检查当前工作区代码（包含尚未进入 main 的 Windows 核心修复）。

| 范围 | 发现与复用决定 |
| --- | --- |
| windows/core/models.py | Pydantic 格式、画质、图文、任务模型可直接复用。|
| windows/core/config.py | 全局单例，导入即读取配置、迁移 Cookie；不能与原 GUI 共用状态目录。每个工作进程注入独立 OMNI_STATE_DIR，不修改旧配置文件。|
| windows/core/task_manager.py | 已有异步队列、限流、取消事件、状态广播、失败恢复与原子持久化；直接调用 add_task / cancel_task / get_task。历史仅100条，不能充当多用户交付账本。新增 SQLite 仅记录服务请求、文件引用及交付，不复制媒体调度业务。|
| windows/engine | 平台路由、原生解析、yt-dlp、FFmpeg、图文与 ZIP、质量选择、媒体验证均复用。浏览器辅助能力不能在 headless 中假装可用。网络与进程边界在 Agent 工作进程中加强，避免影响旧发行版。|
| windows/omni_server.py | 可复用解析路由的实现思路；旧 API 含管理、任意本地文件导出、辅助浏览器和 GUI 操作，不直接将整个应用挂到远程服务。抽出薄的调用桥，不重建解析器。|
| windows/omni_app.py | pywebview 桌面入口，新包不依赖、不启动。|
| agent-harness | Click CLI 是 requests 封装，调用 /api/analyze、/api/download、/api/tasks 等；不是独立下载引擎，不能提供认证、交付状态或 MCP 协议。新增官方 MCP SDK 层调用同一 Agent 服务。|
| windows/tests | 已有解析、会话、质量、平台、失败策略和 API 回归测试。保留，另增 Agent 权限、协议、交付、路径和部署测试。|
| 构建与依赖 | 旧 build_release.py 含 pywebview、完整 web、Windows bin；新建自己的依赖锁、PyInstaller 入口和发行目录，仅打包核心、静态管理页、FFmpeg 与运行时。|
| 会话与存储 | 原 SessionStore 用 Windows DPAPI，Linux protect 会报错。仅在 Agent 工作进程注入 DPAPI/Fernet 替代实现，复用 Cookie 域过滤与临时 Cookie 文件机制；任务与凭据均存加密数据，Linux 密钥放持久化受限目录。|

实施：1 核心桥与隔离工作进程 → 2 MCP stdio/HTTP → 3 SQLite 引用/回执/清理 → 4 Telegram、NapCat 适配 → 5 四区设置页 → 6 便携包、Docker、接入示例 → 7 协议、生命周期、模拟平台、启动及回归验证。

管理页设计：Segoe UI/系统中文字体；浅底 #f5f6f8、白表面 #ffffff、正文 #17212f、次级 #526173、强调 #0969b7、深色底 #19212b。左侧四个锚点，右侧顺序设置表单，窄屏顶部换行。不使用营销首屏、媒体预览或图表。布局为 `[名称 / 状态 / 保存]`、`[四区导航 | 下载 → 权限 → 登录 → 服务]`。本地字体与资源，150ms交互反馈，尊重减少动态设置。检索所得极简风格与配色适用；营销布局和在线字体不适用于此管理工具，舍弃。
