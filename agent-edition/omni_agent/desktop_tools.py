"""本机目录选择器、服务端目录浏览与网络代理连通性诊断工具。"""
import asyncio
import json
import os
from pathlib import Path
import socket
import string
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import requests

from .config import validate_proxy_url
from .errors import AgentError, safe_text
from .security import validate_url


def _base_command() -> list[str]:
    if getattr(sys, 'frozen', False):
        return [str(Path(sys.executable).with_name('OmniDownloaderAgent.exe'))]
    return [sys.executable, '-m', 'omni_agent']


def run_pick_dir_cli(initial_dir: str, title: str, out_file: str) -> None:
    """在独立桌面子进程中运行系统文件夹选择框并将结果写入临时 JSON。"""
    payload = {'supported': True, 'cancelled': True, 'path': ''}
    try:
        if os.name == 'nt':
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        root.update()
        initial = initial_dir if initial_dir and Path(initial_dir).is_dir() else None
        selected = filedialog.askdirectory(initialdir=initial, title=title or '选择文件夹', parent=root)
        root.destroy()
        if selected:
            payload = {'supported': True, 'cancelled': False, 'path': str(Path(selected))}
    except Exception as exc:
        payload = {'supported': False, 'reason': 'HEADLESS', 'error': safe_text(exc)}
    Path(out_file).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')


async def pick_directory(initial_dir: str = '', title: str = '选择文件夹') -> dict:
    """唤起本机系统文件夹选择对话框（无图形环境时返回 supported=False 以便降级为页内浏览）。"""
    if os.name != 'nt' and not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY') and sys.platform != 'darwin':
        return {'supported': False, 'reason': 'HEADLESS', 'path': ''}
    with tempfile.TemporaryDirectory(prefix='omni-pick-') as tmp:
        out_path = Path(tmp) / 'pick.json'
        cmd = _base_command() + [
            '--pick-dir',
            '--initial', initial_dir or '',
            '--title', title or '选择文件夹',
            '--out', str(out_path),
        ]
        kwargs = {'creationflags': 0x08000000} if os.name == 'nt' else {}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            **kwargs,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {'supported': True, 'cancelled': True, 'path': ''}
        if out_path.is_file():
            try:
                return json.loads(out_path.read_text('utf-8'))
            except (OSError, ValueError):
                pass
        return {'supported': False, 'reason': 'HEADLESS', 'path': ''}


def _windows_drives() -> list[dict]:
    import ctypes
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    drives = []
    for idx, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << idx):
            root = f'{letter}:\\'
            if os.path.exists(root):
                drives.append({'name': root, 'path': root})
    return drives


def list_directories(path: str = '') -> dict:
    """列出服务端可见目录，供无原生弹窗或跨环境时在网页内选择目标文件夹。"""
    raw = str(path or '').strip()
    if not raw:
        if os.name == 'nt':
            entries = _windows_drives()
            return {'current': '', 'parent': '', 'entries': entries, 'can_create': False}
        raw = '/'
    try:
        target = Path(raw).expanduser().resolve()
    except OSError:
        raise AgentError('INVALID_PARAMETER', '目录路径格式无效') from None
    if not target.exists() or not target.is_dir():
        raise AgentError('NOT_FOUND', '目录不存在或无法访问')
    entries = []
    try:
        children = sorted(
            (p for p in target.iterdir() if p.is_dir() and not p.name.startswith('.')),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        raise AgentError('PATH_DENIED', '无权读取该目录内容') from None
    for child in children[:300]:
        entries.append({'name': child.name, 'path': str(child)})
    is_root = target == Path(target.anchor)
    if os.name == 'nt' and is_root:
        parent = ''
    elif is_root:
        parent = ''
    else:
        parent = str(target.parent)
    return {
        'current': str(target),
        'parent': parent,
        'entries': entries,
        'can_create': True,
    }


def create_directory(parent: str, name: str) -> dict:
    """在服务端目录浏览器当前路径下创建新子目录。"""
    folder = str(name or '').strip()
    if not folder or folder in ('.', '..') or any(sep in folder for sep in ('/', '\\', '\x00', '\r', '\n')):
        raise AgentError('INVALID_PARAMETER', '文件夹名称无效')
    base = Path(str(parent or '')).resolve()
    if not str(parent or '').strip() or not base.is_dir():
        raise AgentError('INVALID_PARAMETER', '父目录不存在')
    target = (base / folder).resolve()
    if not target.is_relative_to(base):
        raise AgentError('PATH_DENIED', '非法目录路径')
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AgentError('INVALID_PARAMETER', f'创建文件夹失败：{safe_text(exc)}') from None
    return {'path': str(target)}


def _probe_proxy_sync(proxy: str, target: str = '') -> dict:
    try:
        proxy = validate_proxy_url(proxy.strip())
    except ValueError as exc:
        raise AgentError('INVALID_PARAMETER', str(exc)) from None

    tcp_ok = True
    tcp_ms = None
    if proxy:
        parsed = urlsplit(proxy)
        started = time.perf_counter()
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=4):
                tcp_ms = round((time.perf_counter() - started) * 1000)
        except OSError as exc:
            return {
                'ok': False,
                'proxy': proxy,
                'mode': 'proxy',
                'tcp_ok': False,
                'tcp_ms': round((time.perf_counter() - started) * 1000),
                'targets': [],
                'message': f'无法连接代理 {parsed.hostname}:{parsed.port}（{safe_text(exc)}）',
            }

    if target.strip():
        clean_target = target.strip()
        validate_url(clean_target, initial=True, using_proxy=bool(proxy))
        probes = [('自定义目标', clean_target)]
    else:
        probes = [
            ('哔哩哔哩', 'https://api.bilibili.com/x/web-interface/nav'),
            ('YouTube', 'https://www.youtube.com/generate_204'),
            ('Telegram API', 'https://api.telegram.org'),
        ]

    session = requests.Session()
    session.trust_env = False
    proxies = {'http': proxy, 'https': proxy} if proxy else {}
    results = []
    try:
        for label, url in probes:
            started = time.perf_counter()
            try:
                response = session.get(
                    url,
                    proxies=proxies,
                    timeout=(5, 8),
                    allow_redirects=False,
                    headers={'User-Agent': 'OmniDownloader-Probe/1.0'},
                )
                latency_ms = round((time.perf_counter() - started) * 1000)
                ok = 200 <= response.status_code < 400
                results.append({
                    'name': label,
                    'url': url,
                    'ok': ok,
                    'status_code': response.status_code,
                    'latency_ms': latency_ms,
                    'error': '' if ok else f'HTTP {response.status_code}',
                })
            except requests.RequestException as exc:
                latency_ms = round((time.perf_counter() - started) * 1000)
                results.append({
                    'name': label,
                    'url': url,
                    'ok': False,
                    'status_code': None,
                    'latency_ms': latency_ms,
                    'error': safe_text(exc),
                })
    finally:
        session.close()

    any_ok = any(item['ok'] for item in results)
    all_ok = all(item['ok'] for item in results)
    prefix = f'代理 {proxy} 握手 {tcp_ms} ms' if proxy else '直连模式（未配置代理）'
    detail = '；'.join(
        f"{item['name']} {'通过(' + str(item['status_code']) + ' / ' + str(item['latency_ms']) + 'ms)' if item['ok'] else '失败(' + item['error'] + ')'}"
        for item in results
    )
    return {
        'ok': any_ok,
        'all_ok': all_ok,
        'proxy': proxy,
        'mode': 'proxy' if proxy else 'direct',
        'tcp_ok': tcp_ok,
        'tcp_ms': tcp_ms,
        'targets': results,
        'message': f'{prefix}：{detail}',
    }


async def probe_proxy_connectivity(proxy: str = '', target: str = '') -> dict:
    """异步执行代理 TCP 握手与目标站点 HTTP 连通性探测。"""
    return await asyncio.to_thread(_probe_proxy_sync, proxy, target)
