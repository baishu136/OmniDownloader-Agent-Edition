"""本机开机自启动管理（Windows 注册表 Run 与受限的 Linux 桌面自启动）。"""
import os
from pathlib import Path
import subprocess
import sys

from .config import default_data_dir
from .errors import AgentError, safe_text

RUN_KEY_PATH = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE_NAME = 'OmniDownloaderAgent'
LINUX_DESKTOP_NAME = 'omni-downloader-agent.desktop'


def _is_windows() -> bool:
    return os.name == 'nt'


def _is_container() -> bool:
    return Path('/.dockerenv').exists() or bool(os.environ.get('container'))


def is_autostart_supported() -> tuple[bool, str]:
    """判断当前运行环境是否支持桌面级开机自启动。"""
    if _is_windows():
        return True, ''
    if sys.platform.startswith('linux'):
        if _is_container():
            return False, 'UNSUPPORTED_CONTAINER'
        if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            return False, 'HEADLESS'
        return True, ''
    return False, 'UNSUPPORTED_PLATFORM'


def _normalize_data_dir(path: str | Path | None) -> Path | None:
    if not path:
        return None
    return Path(path).expanduser().resolve()


def _resolve_gui_python() -> str:
    """Windows 源码运行时优先选用 pythonw.exe，避免开机后残留控制台黑框。"""
    exe = Path(sys.executable)
    if _is_windows():
        if getattr(sys, 'frozen', False):
            gui_exe = exe.with_name('OmniDownloaderAgent.exe')
            if gui_exe.is_file():
                return str(gui_exe)
            return str(exe)
        pythonw = exe.with_name('pythonw.exe')
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


def build_autostart_command(
    *,
    data_dir: str | Path | None = None,
    host: str = '127.0.0.1',
    port: int = 58080,
) -> tuple[str, list[str]]:
    """构造开机自启动命令行（静默后台托盘启动，不自动弹出浏览器窗口）。"""
    executable = _resolve_gui_python()
    service_args = ['--serve', '--no-browser']
    if _is_windows():
        service_args.append('--tray')
    clean_host = str(host or '127.0.0.1').strip() or '127.0.0.1'
    if clean_host != '127.0.0.1':
        service_args.extend(['--host', clean_host])
    clean_port = int(port or 58080)
    if clean_port != 58080:
        service_args.extend(['--port', str(clean_port)])

    resolved_data = _normalize_data_dir(data_dir)
    if resolved_data is not None and resolved_data != default_data_dir().resolve():
        service_args.extend(['--data', str(resolved_data)])

    if getattr(sys, 'frozen', False):
        argv = [executable, *service_args]
    else:
        pkg_root = str(Path(__file__).resolve().parents[1])
        runner_code = (
            'import sys; '
            f'sys.path.insert(0, {pkg_root!r}); '
            'sys.argv[0] = "omni_agent"; '
            'from omni_agent.__main__ import main; '
            'main()'
        )
        argv = [executable, '-c', runner_code, *service_args]

    if _is_windows():
        return subprocess.list2cmdline(argv), argv
    import shlex
    return shlex.join(argv), argv


def _linux_desktop_path() -> Path:
    xdg_home = Path(os.environ.get('XDG_CONFIG_HOME') or (Path.home() / '.config'))
    return xdg_home / 'autostart' / LINUX_DESKTOP_NAME


def _read_windows_run_value() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return str(value) if value is not None else None
    except FileNotFoundError:
        return None


def _write_windows_run_value(command: str | None) -> None:
    import winreg
    if command is None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            return
        return
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)


def get_autostart_status(
    *,
    data_dir: str | Path | None = None,
    host: str = '127.0.0.1',
    port: int = 58080,
) -> dict:
    """读取本机系统当前真实的开机自启动状态（不含敏感凭据）。"""
    supported, reason = is_autostart_supported()
    if not supported:
        hint = (
            '当前处于容器或无桌面环境，建议通过 Docker 重启策略或系统守护进程管理自启动'
            if reason in ('UNSUPPORTED_CONTAINER', 'HEADLESS')
            else '当前操作系统暂不支持桌面开机自启动配置'
        )
        return {
            'supported': False,
            'enabled': False,
            'stale': False,
            'reason': reason,
            'mode': 'unsupported',
            'message': hint,
        }

    expected_cmd, _ = build_autostart_command(data_dir=data_dir, host=host, port=port)
    if _is_windows():
        try:
            current = _read_windows_run_value()
        except OSError as exc:
            return {
                'supported': True,
                'enabled': False,
                'stale': False,
                'reason': 'READ_ERROR',
                'mode': 'registry',
                'message': f'读取 Windows 启动项失败：{safe_text(exc)}',
            }
        enabled = bool(current and current.strip())
        stale = bool(enabled and current.strip() != expected_cmd.strip())
        if not enabled:
            msg = '未开启；启用后将随 Windows 登录后台静默启动服务并驻留系统托盘'
        elif stale:
            msg = '已开启（检测到大版本号或目录变更，建议关闭后重开以更新启动路径）'
        else:
            msg = '已开启；Windows 登录后将在系统托盘静默运行（不自动弹出浏览器）'
        return {
            'supported': True,
            'enabled': enabled,
            'stale': stale,
            'reason': '',
            'mode': 'registry',
            'message': msg,
        }

    desktop_file = _linux_desktop_path()
    enabled = desktop_file.is_file()
    stale = False
    if enabled:
        try:
            content = desktop_file.read_text('utf-8')
            stale = f'Exec={expected_cmd}' not in content
        except OSError:
            stale = True
    msg = (
        '已开启；桌面会话登录后将自动在后台启动服务'
        if enabled and not stale
        else ('已开启（启动项路径与当前实例不一致，可重新开启同步）' if enabled else '未开启；启用后将写入用户桌面自启动项')
    )
    return {
        'supported': True,
        'enabled': enabled,
        'stale': stale,
        'reason': '',
        'mode': 'xdg_autostart',
        'message': msg,
    }


def set_autostart(
    enabled: bool,
    *,
    data_dir: str | Path | None = None,
    host: str = '127.0.0.1',
    port: int = 58080,
) -> dict:
    """启用或取消本机开机自启动，并返回同步后的真实系统状态。"""
    supported, reason = is_autostart_supported()
    if not supported:
        if enabled:
            raise AgentError('CAPABILITY_UNAVAILABLE', '当前运行环境不支持桌面开机自启动，请使用容器 restart 策略或系统服务管理')
        return get_autostart_status(data_dir=data_dir, host=host, port=port)

    command, _ = build_autostart_command(data_dir=data_dir, host=host, port=port)
    if _is_windows():
        try:
            _write_windows_run_value(command if enabled else None)
        except OSError as exc:
            raise AgentError('SYSTEM_DENIED', f'写入 Windows 开机启动项失败：{safe_text(exc)}') from None
    else:
        desktop_file = _linux_desktop_path()
        try:
            if enabled:
                desktop_file.parent.mkdir(parents=True, exist_ok=True)
                desktop_entry = '\n'.join([
                    '[Desktop Entry]',
                    'Type=Application',
                    'Version=1.0',
                    'Name=OmniDownloader Agent Edition',
                    'Comment=Headless media download & bot delivery service',
                    f'Exec={command}',
                    'Terminal=false',
                    'X-GNOME-Autostart-enabled=true',
                    '',
                ])
                desktop_file.write_text(desktop_entry, encoding='utf-8')
            else:
                desktop_file.unlink(missing_ok=True)
        except OSError as exc:
            raise AgentError('SYSTEM_DENIED', f'更新桌面自启动项失败：{safe_text(exc)}') from None

    return get_autostart_status(data_dir=data_dir, host=host, port=port)
