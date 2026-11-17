# 验证记录

日期：2026-11-17。下列记录来自实际执行，模拟API和真实平台验收严格区分。

- Windows原核心：`python -m pytest windows/tests -q`，140通过，现有Pydantic弃用警告。
- Agent独立测试：80通过，包含官方SDK真实stdio/HTTP握手、发现、结构化结果、模拟平台、文件引用、幂等、恢复、认证、SSRF/路径边界、图文范围内逐图任务，以及真实工作进程与子进程取消。核心桥测试只模拟平台I/O，实际调用原TaskManager。
- 既有Agent CLI：29通过；测试使用其自身的模块搜索路径运行，没有修改旧CLI。
- 管理页面：使用实际Edge浏览器验证设置保存、关闭视频后拒绝工具调用、深浅色、390px无横向溢出、锁定；无页面脚本错误。
- Windows便携包：PyInstaller实际构建；独立EXE启动、管理设置保存、stdio MCP、FFmpeg发现、核心工作进程导入的冒烟检查通过。发行构建还显式检查打包Deno的可发现性。
- 本机没有Docker命令，已通过GitHub Actions实际构建和运行容器，覆盖认证、MCP、设置持久化、缓存文件持久化、重启中断恢复、模拟Telegram multipart交付和发送后自动清理。最终发行验证见[本仓库CI](https://github.com/baishu136/OmniDownloader-Agent-Edition/actions/workflows/agent-edition.yml)，本仓库使用独立的代码、版本及发行记录。
- 云端Windows与Ubuntu运行同一套Agent测试；云端Windows额外运行原核心测试并构建便携包。每次构建上传ZIP及SHA256，实际启动EXE后才生成可交付构建记录。
- 未配置已授权的QQ、Telegram或AstrBot真实账号；相关平台发送仅模拟API测试通过，不是实际平台端到端验收。
- 本机另执行了公开Bilibili链接的真实工作进程解析探测：当前网络DNS将目标映射到非公共地址，服务按设计返回SSRF_BLOCKED。因此本机网络环境下的真实媒体下载尚未验收；没有为获得成功结果而放宽内网防护。

测试入口：`python -m pytest agent-edition/tests -q`。部署测试入口：`tools/portable_smoke.py`、`tools/docker_smoke.py`。CI定义为仓库根目录`.github/workflows/agent-edition.yml`。平台鉴权、验证码、会员画质、QQ具体协议端权限及真实大文件传输仍需账号环境验收。
