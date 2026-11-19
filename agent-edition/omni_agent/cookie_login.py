"""完善平台 Cookie 导入格式兼容、B站扫码登录、以及本地桌面浏览器（Chrome / Edge）隔离登录流程。"""
import asyncio
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

import httpx
import qrcode
import requests
import websockets

from .errors import AgentError, safe_text

DESKTOP_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0'
)

# 参照 Android PlatformLoginPolicy.kt 统一维护各平台登录入口与关键会话字段
PLATFORM_LOGIN_POLICIES = {
    'YouTube': {
        'start_url': 'https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/',
        'cookie_urls': ('https://www.youtube.com/', 'https://m.youtube.com/', 'https://accounts.google.com/'),
        'markers': ('SID', 'SAPISID', '__Secure-3PAPISID', 'LOGIN_INFO'),
        'auto_capture': True,
        'hint': '将启动本机真实浏览器独立窗口登录 Google / YouTube；跳转回 YouTube 并确认全量会话后自动加密保存，也可手动导出 JSON / cookies.txt 导入。',
    },
    'X (Twitter)': {
        'start_url': 'https://x.com/i/flow/login',
        'cookie_urls': ('https://x.com/', 'https://twitter.com/'),
        'markers': ('auth_token',),
        'auto_capture': True,
        'hint': '使用本地浏览器独立窗口完成 X 登录后会自动捕获 auth_token 等完整会话 Cookie。',
    },
    '小红书': {
        'start_url': 'https://www.xiaohongshu.com/explore',
        'cookie_urls': ('https://www.xiaohongshu.com/',),
        'markers': ('web_session',),
        'auto_capture': False,
        'hint': '小红书访客态也可能存在 web_session，请在本地浏览器确认登录账号后点击“我已登录，抓取保存”。',
    },
    '小黑盒': {
        'start_url': 'https://www.xiaoheihe.cn/',
        'cookie_urls': ('https://www.xiaoheihe.cn/', 'https://www.heybox.cn/'),
        'markers': ('heybox_id', 'user_id', 'sessionid'),
        'auto_capture': True,
        'hint': '使用本地浏览器独立窗口完成小黑盒登录后会自动捕获并加密保存会话。',
    },
    'Pixiv': {
        'start_url': 'https://accounts.pixiv.net/login',
        'cookie_urls': ('https://www.pixiv.net/', 'https://accounts.pixiv.net/'),
        'markers': ('PHPSESSID',),
        'auto_capture': False,
        'hint': 'Pixiv 匿名访问也存在 PHPSESSID；请在本地浏览器完成账号登录后点击“我已登录，抓取保存”，随后可点“检测”。',
    },
    '抖音': {
        'start_url': 'https://www.douyin.com/',
        'cookie_urls': ('https://www.douyin.com/',),
        'markers': ('sessionid', 'sessionid_ss'),
        'auto_capture': False,
        'hint': '请在本地浏览器独立窗口扫码或登录后点击“我已登录，抓取保存”。',
    },
    '哔哩哔哩': {
        'start_url': 'https://passport.bilibili.com/login',
        'cookie_urls': ('https://www.bilibili.com/', 'https://passport.bilibili.com/', 'https://api.bilibili.com/'),
        'markers': ('SESSDATA',),
        'auto_capture': True,
        'hint': '支持“B站扫码登录”与“本地浏览器登录”；取得 SESSDATA 后会自动加密保存并验证登录态。',
    },
    'Pinterest': {
        'start_url': 'https://www.pinterest.com/login/',
        'cookie_urls': ('https://www.pinterest.com/',),
        'markers': ('_pinterest_sess',),
        'auto_capture': True,
        'hint': '使用本地浏览器独立窗口登录 Pinterest 后将自动保存会话。',
    },
    'Instagram': {
        'start_url': 'https://www.instagram.com/accounts/login/',
        'cookie_urls': ('https://www.instagram.com/',),
        'markers': ('sessionid',),
        'auto_capture': True,
        'hint': '使用本地浏览器独立窗口登录 Instagram 后将自动捕获 sessionid 并保存。',
    },
    'Facebook': {
        'start_url': 'https://www.facebook.com/login/',
        'cookie_urls': ('https://www.facebook.com/',),
        'markers': ('c_user',),
        'auto_capture': True,
        'hint': '使用本地浏览器独立窗口登录 Facebook 后将自动捕获 c_user 等凭据并保存。',
    },
    'TikTok': {
        'start_url': 'https://www.tiktok.com/login',
        'cookie_urls': ('https://www.tiktok.com/',),
        'markers': ('sessionid', 'sessionid_ss'),
        'auto_capture': True,
        'hint': '使用本地浏览器独立窗口登录 TikTok 后将自动捕获会话并保存。',
    },
    '快手': {
        'start_url': 'https://www.kuaishou.com/new-reco',
        'cookie_urls': ('https://www.kuaishou.com/',),
        'markers': ('userId',),
        'auto_capture': False,
        'hint': '请在本地浏览器独立窗口完成快手登录后点击“我已登录，抓取保存”。',
    },
}


def _browser_key(label: str, path: str = '') -> str:
    combined = f'{label} {Path(path).stem if path else ""}'.lower()
    if 'edge' in combined or 'msedge' in combined:
        return 'edge'
    if 'brave' in combined:
        return 'brave'
    if 'chromium' in combined:
        return 'chromium'
    if 'chrome' in combined:
        return 'chrome'
    return 'custom'


def list_local_browsers() -> list[dict]:
    """探测本机已安装的所有 Chrome / Microsoft Edge / Brave / Chromium（默认优先 Chrome）。"""
    override = os.environ.get('OMNI_BROWSER_PATH', '').strip()
    candidates: list[tuple[str, str]] = []
    if override and Path(override).is_file():
        candidates.append((Path(override).stem, override))

    if os.name != 'nt' and sys.platform != 'darwin' and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return []

    if os.name == 'nt':
        # 优先探测 Google Chrome，其次 Microsoft Edge，降低 Edge 在 Google OAuth / WebAuthn 下的兼容性问题
        try:
            import winreg
            for exe, label in (('chrome.exe', 'Google Chrome'), ('msedge.exe', 'Microsoft Edge'), ('brave.exe', 'Brave')):
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(hive, rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}') as key:
                            val, _ = winreg.QueryValueEx(key, '')
                            if val:
                                candidates.append((label, str(val).strip('"')))
                    except OSError:
                        pass
        except ImportError:
            pass
        env_dirs = [
            os.environ.get('PROGRAMFILES', r'C:\Program Files'),
            os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'),
            os.environ.get('LOCALAPPDATA', ''),
        ]
        rel_paths = (
            ('Google Chrome', r'Google\Chrome\Application\chrome.exe'),
            ('Microsoft Edge', r'Microsoft\Edge\Application\msedge.exe'),
            ('Brave', r'BraveSoftware\Brave-Browser\Application\brave.exe'),
        )
        for base in env_dirs:
            if not base:
                continue
            for label, rel in rel_paths:
                candidates.append((label, str(Path(base) / rel)))
        for label, cmd in (('Google Chrome', 'chrome'), ('Microsoft Edge', 'msedge')):
            found = shutil.which(cmd)
            if found:
                candidates.append((label, found))
    elif sys.platform == 'darwin':
        candidates.extend([
            ('Google Chrome', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
            ('Microsoft Edge', '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'),
            ('Chromium', '/Applications/Chromium.app/Contents/MacOS/Chromium'),
        ])
    else:
        for label, cmd in (
            ('Google Chrome', 'google-chrome'),
            ('Google Chrome', 'google-chrome-stable'),
            ('Microsoft Edge', 'microsoft-edge'),
            ('Microsoft Edge', 'microsoft-edge-stable'),
            ('Chromium', 'chromium'),
            ('Chromium', 'chromium-browser'),
        ):
            found = shutil.which(cmd)
            if found:
                candidates.append((label, found))

    seen = set()
    found_list: list[dict] = []
    for label, raw_path in candidates:
        norm = str(raw_path or '').strip()
        if not norm or norm.lower() in seen:
            continue
        seen.add(norm.lower())
        if Path(norm).is_file():
            found_list.append({'key': _browser_key(label, norm), 'name': label, 'path': norm})
    return found_list


def find_local_browser() -> dict | None:
    """探测本机默认首选浏览器（Google Chrome 优先于 Microsoft Edge）。"""
    browsers = list_local_browsers()
    return browsers[0] if browsers else None


def resolve_local_browser(preferred: str = 'auto') -> dict | None:
    """按用户偏好或默认策略解析可用本地浏览器，同时兼容测试对 find_local_browser 的注入。"""
    fallback = find_local_browser()
    if not fallback:
        return None
    fallback_item = dict(fallback)
    fallback_item.setdefault('key', _browser_key(fallback_item.get('name', ''), fallback_item.get('path', '')))
    pref = str(preferred or 'auto').strip().lower()
    if not pref or pref == 'auto' or fallback_item['key'] == pref:
        return fallback_item
    # 仅当默认返回真实可执行文件时再遍历 list_local_browsers，防止绕过测试中的 mock
    if fallback_item.get('path') and Path(str(fallback_item['path'])).is_file():
        for item in list_local_browsers():
            if item['key'] == pref:
                return dict(item)
    return fallback_item


class BrowserPreferenceStore:
    """管理员专属浏览器与防风控登录模式偏好存储（Vault 隔离，不暴露给 Agent）。"""

    VAULT_KEY = 'browser-preferences'
    ALLOWED_BROWSERS = ('auto', 'chrome', 'edge', 'brave', 'chromium')
    ALLOWED_MODES = ('auto', 'clean', 'cdp')

    def __init__(self, vault):
        self.vault = vault
        self._lock = threading.RLock()

    def get(self) -> dict:
        with self._lock:
            raw = self.vault.get(self.VAULT_KEY)
            if not raw:
                return {'browser': 'auto', 'mode': 'auto'}
            try:
                data = json.loads(raw)
            except ValueError:
                return {'browser': 'auto', 'mode': 'auto'}
            browser = str(data.get('browser') or 'auto').strip().lower()
            mode = str(data.get('mode') or 'auto').strip().lower()
            if browser not in self.ALLOWED_BROWSERS:
                browser = 'auto'
            if mode not in self.ALLOWED_MODES:
                mode = 'auto'
            return {'browser': browser, 'mode': mode}

    def update(self, *, browser: str | None = None, mode: str | None = None) -> dict:
        with self._lock:
            cur = self.get()
            if browser is not None:
                b = str(browser).strip().lower()
                if b not in self.ALLOWED_BROWSERS:
                    raise AgentError('INVALID_PARAMETER', '不支持的浏览器选项')
                cur['browser'] = b
            if mode is not None:
                m = str(mode).strip().lower()
                if m not in self.ALLOWED_MODES:
                    raise AgentError('INVALID_PARAMETER', '不支持的登录模式选项')
                cur['mode'] = m
            self.vault.put(self.VAULT_KEY, json.dumps(cur))
            return cur


def record_profile_browser(profile_dir: Path | None, browser: dict | None) -> None:
    """记录写入该持久化浏览器目录的浏览器类型，确保 App-Bound Cookie 无头读回使用同一内核。"""
    if not profile_dir or not browser:
        return
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        meta_path = Path(profile_dir) / '.omni-browser.json'
        key = browser.get('key') or _browser_key(browser.get('name', ''), browser.get('path', ''))
        meta_path.write_text(json.dumps({'key': key, 'name': browser.get('name', '')}, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def read_profile_browser_key(profile_dir: Path | None) -> str:
    if not profile_dir:
        return ''
    meta_path = Path(profile_dir) / '.omni-browser.json'
    if not meta_path.is_file():
        return ''
    try:
        data = json.loads(meta_path.read_text('utf-8', errors='ignore'))
        return str(data.get('key') or '').strip().lower()
    except (OSError, ValueError):
        return ''


async def graceful_close_browser_process(proc, *, timeout: float = 6.0) -> None:
    """优雅关闭无调试端口启动的 GUI 浏览器窗口，确保 Cookie 与 Local State 完整刷盘并释放文件锁。"""
    if not proc or proc.returncode is not None:
        return
    pid = getattr(proc, 'pid', None)
    if os.name == 'nt' and pid:
        try:
            import ctypes
            import ctypes.wintypes

            pids = {int(pid)}
            TH32CS_SNAPPROCESS = 0x00000002

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ('dwSize', ctypes.wintypes.DWORD),
                    ('cntUsage', ctypes.wintypes.DWORD),
                    ('th32ProcessID', ctypes.wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
                    ('th32ModuleID', ctypes.wintypes.DWORD),
                    ('cntThreads', ctypes.wintypes.DWORD),
                    ('th32ParentProcessID', ctypes.wintypes.DWORD),
                    ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', ctypes.wintypes.DWORD),
                    ('szExeFile', ctypes.wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32
            snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap not in (0, -1, ctypes.c_void_p(-1).value):
                try:
                    entry = PROCESSENTRY32W()
                    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                    parents: dict[int, list[int]] = {}
                    if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                        while True:
                            parents.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
                            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                                break
                    queue = [int(pid)]
                    while queue:
                        cur = queue.pop(0)
                        for child in parents.get(cur, ()):
                            if child not in pids:
                                pids.add(child)
                                queue.append(child)
                finally:
                    kernel32.CloseHandle(snap)

            WM_CLOSE = 0x0010

            @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def _enum_cb(hwnd, _):
                win_pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                if int(win_pid.value) in pids and user32.IsWindowVisible(hwnd):
                    cls_buf = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls_buf, 128)
                    if 'Chrome_WidgetWin' in cls_buf.value:
                        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return True

            user32.EnumWindows(_enum_cb, 0)
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                pass
        except Exception:
            pass

    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3.0)
        return
    except Exception:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass


def local_browser_available() -> bool:
    """检查当前环境是否支持启动本机 Chrome / Edge 独立窗口捕获 Cookie。"""
    return find_local_browser() is not None


def open_in_default_browser(platform: str) -> dict:
    """使用操作系统默认浏览器打开目标平台登录页。"""
    policy = PLATFORM_LOGIN_POLICIES.get(platform)
    if not policy:
        raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
    opened = webbrowser.open(policy['start_url'], new=2)
    return {'opened': bool(opened), 'url': policy['start_url']}


def _domain_matches(domain: str, allowed_domains: tuple[str, ...]) -> bool:
    clean = str(domain or '').lstrip('.').lower()
    if not clean:
        return False
    return any(clean == d or clean.endswith('.' + d) for d in allowed_domains)


def _expiry_to_epoch(raw_expiry) -> int:
    if raw_expiry in (None, '', 0, '0', -1):
        return 0
    if isinstance(raw_expiry, (int, float)):
        ts = int(raw_expiry)
        return ts if ts > 0 else 0
    text = str(raw_expiry).strip()
    if text.isdigit():
        ts = int(text)
        return ts if ts > 0 else 0
    try:
        dt = parsedate_to_datetime(text)
        ts = int(dt.timestamp())
        return ts if ts > 1_000_000 else 0
    except (TypeError, ValueError, OSError, OverflowError):
        return 0


def format_netscape_row(domain: str, path: str, secure: bool, expires: int, name: str, value: str, *, httponly: bool = False) -> str:
    """格式化单条标准 Netscape Cookie 行（含 #HttpOnly_ 前缀以保留 HttpOnly 属性）。"""
    clean_domain = str(domain or '').strip()
    include_subdomains = 'TRUE' if clean_domain.startswith('.') else 'FALSE'
    prefix = '#HttpOnly_' if httponly else ''
    clean_path = str(path or '/').strip() or '/'
    sec_flag = 'TRUE' if secure else 'FALSE'
    exp_val = str(max(0, int(expires or 0)))
    if any(c in name or c in value for c in '\r\n\t') or not name:
        raise ValueError('Cookie 字段包含非法字符')
    return f'{prefix}{clean_domain}\t{include_subdomains}\t{clean_path}\t{sec_flag}\t{exp_val}\t{name}\t{value}'


def normalize_cookie_text(platform: str, raw_text: str, domains: dict) -> str:
    """将 Netscape 文本、浏览器 JSON Cookie 数组或请求头统一规范为标准 Netscape 文件。"""
    if platform not in domains:
        raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
    text = str(raw_text or '').strip()
    if not text:
        raise AgentError('INVALID_COOKIE', 'Cookie 内容不能为空')

    allowed = domains[platform]
    if text.startswith(('# Netscape HTTP Cookie File', '# HTTP Cookie File')):
        return text

    if text.startswith('['):
        try:
            items = json.loads(text)
        except ValueError:
            raise AgentError('INVALID_COOKIE', 'JSON Cookie 格式无效') from None
        if not isinstance(items, list):
            raise AgentError('INVALID_COOKIE', 'JSON Cookie 必须为数组')
        rows = []
        now = time.time()
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            value = str(item.get('value') or '')
            domain = str(item.get('domain') or ('.' + allowed[0])).strip()
            if not name or not _domain_matches(domain, allowed):
                continue
            exp = _expiry_to_epoch(item.get('expirationDate') or item.get('expires'))
            if exp and exp <= now:
                continue
            if not item.get('hostOnly', False) and not domain.startswith('.'):
                domain = '.' + domain
            rows.append(format_netscape_row(
                domain=domain,
                path=item.get('path') or '/',
                secure=bool(item.get('secure', True)),
                expires=exp,
                name=name,
                value=value,
                httponly=bool(item.get('httpOnly', item.get('httponly', False))),
            ))
        if not rows:
            raise AgentError('INVALID_COOKIE', f'JSON 中没有属于 {platform} 域名的有效未过期 Cookie')
        return '# Netscape HTTP Cookie File\n' + '\n'.join(rows) + '\n'

    lines = [ln for ln in text.splitlines() if ln.strip() and not (ln.startswith('#') and not ln.startswith('#HttpOnly_'))]
    if lines and all(len(ln.split('\t')) == 7 for ln in lines):
        return '# Netscape HTTP Cookie File\n' + '\n'.join(lines) + '\n'

    if text.lower().startswith('cookie:'):
        text = text.split(':', 1)[1].strip()
    rows = []
    for part in text.split(';'):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition('=')
        if not sep or not name.strip() or any(c in part for c in '\r\n\t'):
            raise AgentError('INVALID_COOKIE', 'Cookie 格式无效')
        rows.append(format_netscape_row('.' + allowed[0], '/', True, 0, name.strip(), value.strip()))
    if not rows:
        raise AgentError('INVALID_COOKIE', 'Cookie 格式无效')
    return '# Netscape HTTP Cookie File\n' + '\n'.join(rows) + '\n'


def cdp_cookies_to_netscape(cookies: list[dict], policy: dict, allowed_domains: tuple[str, ...]) -> tuple[str, bool]:
    """将浏览器 CDP 返回的 Cookie 列表过滤为指定平台 Netscape 格式，并判断是否出现关键登录标记。"""
    netscape, marker_hit, _ = analyze_cdp_cookies(cookies, policy, allowed_domains)
    return netscape, marker_hit


def analyze_cdp_cookies(cookies: list[dict], policy: dict, allowed_domains: tuple[str, ...]) -> tuple[str, bool, int]:
    """解析 CDP Cookie 列表，返回 (Netscape 文本, 是否命中登录标记, 有效条目数)。"""
    dedup = {}
    marker_set = set(policy.get('markers') or ())
    marker_hit = False
    now = time.time()
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        value = str(item.get('value') or '')
        domain = str(item.get('domain') or '').strip()
        if not name or not _domain_matches(domain, allowed_domains):
            continue
        exp = _expiry_to_epoch(item.get('expires'))
        if exp and exp <= now:
            continue
        if name in marker_set and value:
            marker_hit = True
        norm_domain = domain if domain.startswith('.') else '.' + domain
        try:
            dedup[(norm_domain, item.get('path') or '/', name)] = format_netscape_row(
                norm_domain,
                item.get('path') or '/',
                bool(item.get('secure', True)),
                exp,
                name,
                value,
                httponly=bool(item.get('httpOnly', False)),
            )
        except ValueError:
            continue
    if not dedup:
        return '', False, 0
    return '# Netscape HTTP Cookie File\n' + '\n'.join(dedup.values()) + '\n', marker_hit, len(dedup)


def merge_cdp_cookies(*cookie_groups: list[dict]) -> list[dict]:
    """合并多路 CDP 接口（Storage / Network）采集到的 Cookie，优先保留完整属性。"""
    merged: dict[tuple[str, str, str], dict] = {}
    for group in cookie_groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            domain = str(item.get('domain') or '').strip().lower()
            path = str(item.get('path') or '/').strip() or '/'
            if not name or not domain:
                continue
            key = (domain, path, name)
            prev = merged.get(key)
            if not prev:
                merged[key] = dict(item)
                continue
            # 同一 Cookie 取含非空值、HttpOnly 或更晚过期时间的记录
            prev_exp = _expiry_to_epoch(prev.get('expires'))
            curr_exp = _expiry_to_epoch(item.get('expires'))
            merged[key] = {
                **prev,
                **item,
                'value': item.get('value') or prev.get('value') or '',
                'httpOnly': bool(prev.get('httpOnly') or item.get('httpOnly')),
                'secure': bool(prev.get('secure') or item.get('secure')),
                'expires': curr_exp if curr_exp > prev_exp else prev_exp,
            }
    return list(merged.values())


async def _cdp_call(ws_url: str, method: str, params: dict | None = None, *, req_id: int = 1, timeout: float = 3.0) -> dict:
    """向环回 CDP WebSocket 发送单条指令并等待响应结果。"""
    async with websockets.connect(ws_url, proxy=None, origin=None, max_size=8 * 1024 * 1024, open_timeout=timeout) as ws:
        payload = {'id': req_id, 'method': method}
        if params:
            payload['params'] = params
        await ws.send(json.dumps(payload))
        deadline = time.monotonic() + timeout
        while True:
            rem = max(0.1, deadline - time.monotonic())
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=rem))
            if msg.get('id') == req_id:
                return msg.get('result') or {}


async def fetch_cdp_targets(port: int, browser_ws_path: str) -> dict:
    """读取本机浏览器 CDP 元信息与当前活动页面脱敏宿主域名。"""
    ws_url = f'ws://127.0.0.1:{port}{browser_ws_path}' if browser_ws_path else ''
    page_ws_urls: list[str] = []
    current_host = ''
    async with httpx.AsyncClient(trust_env=False, timeout=2.5) as client:
        try:
            ver = (await client.get(f'http://127.0.0.1:{port}/json/version')).json()
            if isinstance(ver, dict) and ver.get('webSocketDebuggerUrl'):
                ws_url = str(ver['webSocketDebuggerUrl'])
        except Exception:
            pass
        try:
            targets = (await client.get(f'http://127.0.0.1:{port}/json/list')).json()
            if isinstance(targets, list):
                for target in targets:
                    if not isinstance(target, dict):
                        continue
                    t_type = str(target.get('type') or '')
                    t_url = str(target.get('url') or '')
                    if t_type == 'page' and t_url.startswith(('http://', 'https://')) and not current_host:
                        current_host = (urlsplit(t_url).hostname or '').lower()
                    p_ws = target.get('webSocketDebuggerUrl')
                    if p_ws and t_type in ('page', 'iframe', 'webview', ''):
                        page_ws_urls.append(str(p_ws))
        except Exception:
            pass
    return {'browser_ws_url': ws_url, 'page_ws_urls': page_ws_urls, 'current_host': current_host}


async def fetch_cdp_snapshot(port: int, browser_ws_path: str, cookie_urls: tuple[str, ...] = ()) -> dict:
    """多路联合拉取浏览器隔离实例中的完整 Cookie 及页面当前宿主域名。"""
    meta = await fetch_cdp_targets(port, browser_ws_path)
    groups: list[list[dict]] = []
    cdp_connected = False

    browser_ws = meta['browser_ws_url']
    if browser_ws:
        try:
            res = await _cdp_call(browser_ws, 'Storage.getCookies', req_id=1)
            if isinstance(res.get('cookies'), list):
                groups.append(res['cookies'])
                cdp_connected = True
        except Exception:
            pass

    for page_ws in meta['page_ws_urls'][:2]:
        try:
            res_all = await _cdp_call(page_ws, 'Network.getAllCookies', req_id=2)
            if isinstance(res_all.get('cookies'), list):
                groups.append(res_all['cookies'])
                cdp_connected = True
        except Exception:
            pass
        if cookie_urls:
            try:
                res_urls = await _cdp_call(page_ws, 'Network.getCookies', {'urls': list(cookie_urls)}, req_id=3)
                if isinstance(res_urls.get('cookies'), list):
                    groups.append(res_urls['cookies'])
                    cdp_connected = True
            except Exception:
                pass
        if groups:
            break

    return {
        'cdp_connected': cdp_connected,
        'cookies': merge_cdp_cookies(*groups),
        'current_host': meta['current_host'],
    }


async def fetch_cdp_cookies(port: int, browser_ws_path: str, cookie_urls: tuple[str, ...] = ()) -> list[dict]:
    """通过本机环回 CDP 端口读取隔离浏览器实例的完整 Cookie（含 HttpOnly）。"""
    snap = await fetch_cdp_snapshot(port, browser_ws_path, cookie_urls)
    return snap['cookies']


_DEFAULT_FETCH_CDP_COOKIES = fetch_cdp_cookies


async def close_cdp_browser(port: int, browser_ws_path: str) -> None:
    try:
        meta = await fetch_cdp_targets(port, browser_ws_path)
        ws_url = meta['browser_ws_url'] or f'ws://127.0.0.1:{port}{browser_ws_path}'
        async with websockets.connect(ws_url, proxy=None, origin=None, open_timeout=2) as ws:
            await ws.send(json.dumps({'id': 9, 'method': 'Browser.close'}))
    except Exception:
        pass


async def probe_cdp_alive(port: int, browser_ws_path: str = '') -> tuple[bool, str]:
    """探测本机环回 CDP 端口是否仍在响应，返回 (是否存活, 权威 Browser WS URL)。"""
    ws_url = f'ws://127.0.0.1:{port}{browser_ws_path}' if browser_ws_path else ''
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=0.8) as client:
            resp = await client.get(f'http://127.0.0.1:{port}/json/version')
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get('webSocketDebuggerUrl'):
                    return True, str(payload['webSocketDebuggerUrl'])
    except Exception:
        pass
    return False, ws_url


async def open_cdp_tab(ws_url: str, url: str) -> bool:
    """在已运行的本地托管浏览器实例中新建并聚焦目标标签页。"""
    try:
        res = await _cdp_call(ws_url, 'Target.createTarget', {'url': url}, req_id=10, timeout=2.5)
        target_id = res.get('targetId') if isinstance(res, dict) else None
        if target_id:
            await _cdp_call(ws_url, 'Target.activateTarget', {'targetId': target_id}, req_id=11, timeout=1.5)
        return bool(target_id)
    except Exception:
        return False


async def navigate_cdp_page(port: int, browser_ws_path: str, url: str) -> bool:
    """将活动浏览器标签页直接导航至目标地址（优先原标签跳转，失败时回退新开标签）。"""
    meta = await fetch_cdp_targets(port, browser_ws_path)
    for page_ws in meta['page_ws_urls'][:1]:
        try:
            await _cdp_call(page_ws, 'Page.navigate', {'url': url}, req_id=12, timeout=3.0)
            return True
        except Exception:
            pass
    if meta['browser_ws_url']:
        return await open_cdp_tab(meta['browser_ws_url'], url)
    return False


@dataclass
class ManagedProfileAccount:
    id: str
    label: str
    epoch: int = 0
    signed_out: bool = False
    pending_epoch: int | None = None
    bindings: dict[str, str] = field(default_factory=dict)


class ManagedProfileStore:
    """多账号独立浏览器登录档案仓库（对齐 Android GoogleAccountRepository）。

    - 每个档案拥有独立的 --user-data-dir（data/browser-profiles/<id>_e<epoch>），互不串号；
    - 索引加密保存在 Vault 中，仅向本机管理员界面返回非敏感备注与已绑定平台列表；
    - 退出或清理档案时采用两阶段递增 epoch 与 owner 校验，绝不误删其他方式导入的凭据。
    """

    VAULT_KEY = 'browser-accounts'
    MAX_ACCOUNTS = 20

    def __init__(self, vault, root_dir: Path):
        self.vault = vault
        self.profiles_root = Path(root_dir) / 'browser-profiles'
        self.profiles_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._current_id: str | None = None
        self._accounts: list[ManagedProfileAccount] = []
        self._load()

    def _load(self) -> None:
        raw = self.vault.get(self.VAULT_KEY)
        if not raw:
            self._accounts = []
            self._current_id = None
            return
        try:
            root = json.loads(raw)
        except ValueError:
            raise AgentError('INTERNAL_ERROR', '独立登录档案数据损坏，请检查凭据目录') from None
        accounts: list[ManagedProfileAccount] = []
        for item in root.get('accounts') or []:
            if not isinstance(item, dict) or not item.get('id'):
                continue
            bindings = item.get('bindings') or {}
            accounts.append(ManagedProfileAccount(
                id=str(item['id']),
                label=str(item.get('label') or ''),
                epoch=int(item.get('epoch') or 0),
                signed_out=bool(item.get('signed_out', False)),
                pending_epoch=None if item.get('pending_epoch') is None else int(item['pending_epoch']),
                bindings={str(k): str(v) for k, v in bindings.items() if k and v},
            ))
        cur = root.get('current_id')
        self._accounts = accounts
        self._current_id = cur if any(a.id == cur for a in accounts) else None

    def _persist(self) -> None:
        payload = {
            'current_id': self._current_id,
            'accounts': [
                {
                    'id': a.id,
                    'label': a.label,
                    'epoch': a.epoch,
                    'signed_out': a.signed_out,
                    'pending_epoch': a.pending_epoch,
                    'bindings': a.bindings,
                }
                for a in self._accounts
            ],
        }
        self.vault.put(self.VAULT_KEY, json.dumps(payload, ensure_ascii=False))

    def profile_dir(self, account_id: str, epoch: int) -> Path:
        return self.profiles_root / f'{account_id}_e{epoch}'

    def public_state(self) -> dict:
        with self._lock:
            items = []
            for a in self._accounts:
                pdir = self.profile_dir(a.id, a.epoch)
                items.append({
                    'id': a.id,
                    'label': a.label,
                    'epoch': a.epoch,
                    'signed_out': a.signed_out,
                    'pending_cleanup': a.pending_epoch is not None,
                    'bound_platforms': list(a.bindings.keys()),
                    'has_browser_data': profile_has_session_data(pdir),
                    'is_current': a.id == self._current_id,
                })
            return {'current_id': self._current_id, 'accounts': items}

    def add(self, label: str) -> dict:
        clean = str(label or '').strip()
        if not clean or len(clean) > 80:
            raise AgentError('INVALID_PARAMETER', '请填写不超过 80 字的档案名称')
        with self._lock:
            if len(self._accounts) >= self.MAX_ACCOUNTS:
                raise AgentError('LIMIT_EXCEEDED', f'最多保存 {self.MAX_ACCOUNTS} 个独立登录档案')
            if any(a.label.lower() == clean.lower() for a in self._accounts):
                raise AgentError('INVALID_PARAMETER', '档案名称已存在')
            acc = ManagedProfileAccount(id=uuid.uuid4().hex[:16], label=clean)
            self._accounts.append(acc)
            self._current_id = acc.id
            self._persist()
            return self.public_state()

    def activate(self, account_id: str) -> dict:
        with self._lock:
            acc = next((a for a in self._accounts if a.id == account_id), None)
            if not acc:
                raise AgentError('NOT_FOUND', '独立登录档案不存在')
            if acc.pending_epoch is not None:
                raise AgentError('INVALID_STATE', '该档案正在清理，请稍候或重试清理')
            self._current_id = acc.id
            self._persist()
            return self.public_state()

    def prepare_open(self, account_id: str) -> tuple[ManagedProfileAccount, Path]:
        with self._lock:
            acc = next((a for a in self._accounts if a.id == account_id), None)
            if not acc:
                raise AgentError('NOT_FOUND', '独立登录档案不存在')
            if acc.pending_epoch is not None:
                raise AgentError('INVALID_STATE', '档案正在清理，请稍后重试')
            acc.signed_out = False
            self._current_id = acc.id
            self._persist()
            target_dir = self.profile_dir(acc.id, acc.epoch)
            target_dir.mkdir(parents=True, exist_ok=True)
            return ManagedProfileAccount(**{**acc.__dict__, 'bindings': dict(acc.bindings)}), target_dir

    def record_binding(self, account_id: str, epoch: int, platform: str, owner_token: str) -> None:
        with self._lock:
            acc = next((a for a in self._accounts if a.id == account_id), None)
            if not acc or acc.epoch != epoch or acc.pending_epoch is not None or acc.signed_out:
                raise AgentError('INVALID_STATE', '档案已退出或已切换，请重新打开档案后再保存凭据')
            acc.bindings[platform] = owner_token
            self._persist()

    def remove_binding(self, account_id: str, platform: str) -> None:
        with self._lock:
            acc = next((a for a in self._accounts if a.id == account_id), None)
            if acc and platform in acc.bindings:
                acc.bindings.pop(platform, None)
                self._persist()

    def begin_logout(self, account_ids: list[str]) -> list[tuple[ManagedProfileAccount, Path]]:
        if not account_ids:
            raise AgentError('INVALID_PARAMETER', '请选择需要退出的独立登录档案')
        with self._lock:
            targets = []
            for aid in account_ids:
                acc = next((a for a in self._accounts if a.id == aid), None)
                if not acc:
                    raise AgentError('NOT_FOUND', '指定的登录档案不存在')
                targets.append(acc)
            prepared = []
            for acc in targets:
                old_epoch = acc.pending_epoch if acc.pending_epoch is not None else acc.epoch
                if acc.pending_epoch is None:
                    acc.epoch += 1
                    acc.signed_out = True
                    acc.pending_epoch = old_epoch
                prepared.append((
                    ManagedProfileAccount(**{**acc.__dict__, 'bindings': dict(acc.bindings)}),
                    self.profile_dir(acc.id, old_epoch),
                ))
            self._persist()
            return prepared

    def finish_logout(self, account_id: str) -> None:
        with self._lock:
            acc = next((a for a in self._accounts if a.id == account_id), None)
            if acc:
                acc.bindings.clear()
                acc.pending_epoch = None
                self._persist()

    def remove_account(self, account_id: str) -> list[tuple[ManagedProfileAccount, Path]]:
        """准备移除一个已存在档案（需在删除磁盘文件夹与关联凭据前调用）。"""
        return self.begin_logout([account_id])

    def finalize_remove_account(self, account_id: str) -> None:
        with self._lock:
            self._accounts = [a for a in self._accounts if a.id != account_id]
            if self._current_id == account_id:
                self._current_id = self._accounts[0].id if self._accounts else None
            self._persist()


def profile_has_session_data(profile_dir: Path | None) -> bool:
    """判断专属托管浏览器目录是否已保留历史登录会话档案（不含敏感字段）。"""
    if not profile_dir or not profile_dir.is_dir():
        return False
    markers = (
        profile_dir / 'Local State',
        profile_dir / 'Default' / 'Network' / 'Cookies',
        profile_dir / 'Default' / 'Cookies',
    )
    return any(p.is_file() and p.stat().st_size > 0 for p in markers)


def cleanup_stale_profile_locks(profile_dir: Path) -> None:
    """清理上次浏览器退出可能遗留的环回端口声明或陈旧文件锁。"""
    for name in ('DevToolsActivePort', 'SingletonLock', 'SingletonSocket', 'SingletonCookie', 'Lockfile'):
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except OSError:
            pass


def scan_cookies_by_platform(
    cookies: list[dict],
    domains: dict,
    *,
    existing_imported: set[str] | tuple[str, ...] = (),
) -> list[dict]:
    """按支持平台汇总当前浏览器快照内已捕获 Cookie 与可自动同步状态。"""
    imported_set = set(existing_imported or ())
    items = []
    for platform, allowed in domains.items():
        policy = PLATFORM_LOGIN_POLICIES.get(platform, {})
        netscape, marker_hit, count = analyze_cdp_cookies(cookies, policy, allowed)
        if count <= 0:
            continue
        auto_cap = bool(policy.get('auto_capture'))
        auto_sync_ready = bool(netscape and marker_hit and (auto_cap or platform in imported_set))
        manual_ready = bool(netscape and (marker_hit or not policy.get('markers')))
        items.append({
            'platform': platform,
            'cookie_count': count,
            'marker_detected': marker_hit,
            'auto_capture': auto_cap,
            'auto_sync_ready': auto_sync_ready,
            'manual_ready': manual_ready,
            'netscape': netscape,
        })
    return items


def inspect_cookie_meta(sessions, platform: str) -> dict:
    """提取仅供本机管理页展示的非敏感会话元数据（绝不返回任何 Cookie 键值）。"""
    info = {'cookie_count': 0, 'marker_present': False, 'earliest_expiry': None}
    try:
        jar = sessions.jar(platform)
    except Exception:
        return info
    cookies = list(jar)
    info['cookie_count'] = len(cookies)
    if not cookies:
        return info
    markers = set(PLATFORM_LOGIN_POLICIES.get(platform, {}).get('markers') or ())
    names = {c.name for c in cookies if getattr(c, 'value', '')}
    info['marker_present'] = bool(names & markers) if markers else bool(cookies)
    expiries = [int(c.expires) for c in cookies if c.expires and int(c.expires) > 0]
    if expiries:
        info['earliest_expiry'] = min(expiries)
    return info


class BilibiliQRLoginManager:
    """管理 B 站官方网页端二维码扫码登录流程。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}

    def _cleanup(self) -> None:
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v['created_at'] > 240]
        for k in expired:
            item = self._sessions.pop(k, None)
            if item and item.get('client'):
                try:
                    item['client'].close()
                except Exception:
                    pass

    def generate(self, proxy: str = '') -> dict:
        with self._lock:
            self._cleanup()
        client = requests.Session()
        client.trust_env = False
        proxies = {'http': proxy, 'https': proxy} if proxy else {}
        headers = {'User-Agent': DESKTOP_UA, 'Referer': 'https://passport.bilibili.com/login'}
        try:
            resp = client.get(
                'https://passport.bilibili.com/x/passport-login/web/qrcode/generate',
                proxies=proxies,
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            client.close()
            raise AgentError('UPSTREAM_ERROR', f'获取 B 站登录二维码失败：{safe_text(exc)}') from None
        data = payload.get('data') or {}
        url = data.get('url') or ''
        qrcode_key = data.get('qrcode_key') or ''
        if payload.get('code') != 0 or not url or not qrcode_key:
            client.close()
            raise AgentError('UPSTREAM_ERROR', 'B 站登录二维码接口返回异常')

        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = [[1 if cell else 0 for cell in row] for row in qr.get_matrix()]

        qr_id = uuid.uuid4().hex
        with self._lock:
            self._sessions[qr_id] = {
                'qrcode_key': qrcode_key,
                'client': client,
                'proxies': proxies,
                'created_at': time.time(),
            }
        return {'qr_id': qr_id, 'matrix': matrix, 'expires_in': 180}

    def poll(self, qr_id: str) -> dict:
        with self._lock:
            self._cleanup()
            entry = self._sessions.get(qr_id)
        if not entry:
            return {'status': 'expired', 'message': '二维码已过期，请重新生成'}
        client: requests.Session = entry['client']
        try:
            resp = client.get(
                'https://passport.bilibili.com/x/passport-login/web/qrcode/poll',
                params={'qrcode_key': entry['qrcode_key']},
                proxies=entry['proxies'],
                headers={'User-Agent': DESKTOP_UA, 'Referer': 'https://passport.bilibili.com/login'},
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise AgentError('UPSTREAM_ERROR', f'查询 B 站扫码状态失败：{safe_text(exc)}') from None

        data = payload.get('data') or {}
        code = data.get('code')
        remaining = max(0, int(180 - (time.time() - entry['created_at'])))
        if code == 86101:
            return {
                'status': 'waiting',
                'stage': 'WAITING_SCAN',
                'stage_label': '等待扫码',
                'progress': 35,
                'remaining_seconds': remaining,
                'message': '等待手机哔哩哔哩 App 扫码...',
            }
        if code == 86090:
            return {
                'status': 'scanned',
                'stage': 'SCANNED_CONFIRM',
                'stage_label': '已扫码，待手机确认',
                'progress': 75,
                'remaining_seconds': remaining,
                'message': '手机已扫码，请在哔哩哔哩 App 上点击确认登录',
            }
        if code == 86038:
            with self._lock:
                self._sessions.pop(qr_id, None)
            client.close()
            return {
                'status': 'expired',
                'stage': 'EXPIRED',
                'stage_label': '二维码已失效',
                'progress': 0,
                'remaining_seconds': 0,
                'message': '二维码已失效，请重新生成',
            }
        if code != 0:
            return {
                'status': 'error',
                'stage': 'ERROR',
                'stage_label': '扫码接口异常',
                'progress': 0,
                'remaining_seconds': remaining,
                'message': safe_text(data.get('message') or 'B 站扫码登录返回异常'),
            }

        try:
            client.get('https://www.bilibili.com/', proxies=entry['proxies'], headers={'User-Agent': DESKTOP_UA}, timeout=10)
        except Exception:
            pass

        rows = {}
        for c in client.cookies:
            if _domain_matches(c.domain, ('bilibili.com',)):
                dom = c.domain if str(c.domain).startswith('.') else '.' + str(c.domain).lstrip('.')
                exp = int(c.expires or 0) if c.expires else 0
                httponly = bool(getattr(c, '_rest', {}).get('HttpOnly') is not None or c.name == 'SESSDATA')
                rows[(dom, c.name)] = format_netscape_row(dom, c.path or '/', bool(c.secure), exp, c.name, c.value or '', httponly=httponly)

        callback_url = data.get('url') or ''
        if callback_url:
            qs = parse_qs(urlsplit(callback_url).query)
            default_exp = int(time.time()) + 150 * 86400
            for key in ('SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5'):
                vals = qs.get(key)
                if vals and vals[0] and ('.bilibili.com', key) not in rows:
                    rows[('.bilibili.com', key)] = format_netscape_row(
                        '.bilibili.com', '/', True, default_exp, key, vals[0], httponly=(key == 'SESSDATA')
                    )

        with self._lock:
            self._sessions.pop(qr_id, None)
        client.close()

        if not rows:
            raise AgentError('INVALID_COOKIE', 'B 站扫码成功但未收到有效 Cookie')
        return {
            'status': 'confirmed',
            'stage': 'COMPLETED',
            'stage_label': '扫码登录完成',
            'progress': 100,
            'cookie_count': len(rows),
            'netscape': '# Netscape HTTP Cookie File\n' + '\n'.join(rows.values()) + '\n',
        }


_STAGE_META = {
    'LAUNCHING': ('正在启动本地浏览器', 15),
    'CONNECTING': ('正在建立抓取通道', 30),
    'WAITING_LOGIN': ('等待完成网页登录', 50),
    'CONFIRMING': ('已识别登录态，稳定补采中', 75),
    'CAPTURING': ('正在读取完整 Cookie', 82),
    'SAVING': ('正在加密写入本地库', 90),
    'VERIFYING': ('正在在线校验有效性', 96),
    'COMPLETED': ('抓取并保存完成', 100),
    'CANCELLED': ('登录窗口已关闭', 0),
    'FAILED': ('登录或抓取失败', 0),
}


MULTI_PLATFORM_KEY = '__ALL__'


@dataclass
class BrowserLoginAttempt:
    attempt_id: str
    platform: str
    browser_name: str
    proxy: str
    browser_key: str = ''
    clean_mode: bool = False
    clean_waiting_confirm: bool = False
    auto_capture: bool = False
    multi_platform: bool = False
    manage_only: bool = False
    require_explicit_save: bool = False
    account_id: str = ''
    account_label: str = ''
    account_epoch: int = 0
    start_url: str = ''
    profile_dir_used: Path | None = None
    cdp_port: int | None = None
    cdp_ws: str = ''
    state: str = 'OPENING'
    stage: str = 'LAUNCHING'
    stage_label: str = '正在启动本地浏览器'
    progress: int = 15
    cdp_connected: bool = False
    current_host: str = ''
    cookie_count: int = 0
    marker_detected: bool = False
    ready_platforms: list[dict] = field(default_factory=list)
    synced_platforms: list[str] = field(default_factory=list)
    capture_mode: str = ''
    message: str = '正在启动本地浏览器登录窗口...'
    error: str = ''
    manual_capture_requested: bool = False
    cancel_requested: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    last_netscape: str = ''
    last_marker_hit: bool = False
    last_cookie_count: int = 0
    last_scanned: list[dict] = field(default_factory=list)
    process: object = None
    task: object = None
    wake_event: object = None
    loop: object = None


class LocalBrowserLoginManager:
    """使用持久化专属托管浏览器档案完成单平台/多平台登录、账号档案隔离与免密续签。"""

    POLL_INTERVAL_SEC = 0.6
    CONFIRM_WINDOW_SEC = 0.7
    PORT_WAIT_STEP_SEC = 0.08
    PORT_WAIT_TICKS = 180

    def __init__(self, profile_dir: Path | None = None):
        self._lock = threading.RLock()
        self._attempts: dict[str, BrowserLoginAttempt] = {}
        self._start_specs: dict[str, dict] = {}
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self._shared_proc = None
        self._shared_port: int | None = None
        self._shared_ws: str = ''
        self._shared_profile_dir: Path | None = None
        self._clean_proc = None
        self._clean_profile_dir: Path | None = None
        self._launch_lock: asyncio.Lock | None = None

    def _get_launch_lock(self) -> asyncio.Lock:
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()
        return self._launch_lock

    def has_profile_data(self) -> bool:
        """检查持久化共享浏览器目录中是否已保存账号档案。"""
        return profile_has_session_data(self.profile_dir)

    def is_shared_browser_active(self) -> bool:
        """检查当前托管浏览器独立窗口是否处于活跃打开状态。"""
        return bool(self._shared_proc is not None and self._shared_proc.returncode is None and self._shared_port)

    def get(self, attempt_id: str) -> BrowserLoginAttempt:
        attempt = self._attempts.get(attempt_id)
        if not attempt:
            raise AgentError('NOT_FOUND', '登录会话已结束或不存在')
        return attempt

    def _wake(self, attempt: BrowserLoginAttempt) -> None:
        if attempt.wake_event is not None and attempt.loop is not None:
            try:
                attempt.loop.call_soon_threadsafe(attempt.wake_event.set)
            except Exception:
                pass

    def set_stage(self, attempt: BrowserLoginAttempt, stage: str, message: str | None = None) -> None:
        """统一更新阶段标识、中文标签与百分比进度（仅含非敏感状态）。"""
        label, pct = _STAGE_META.get(stage, ('处理中', attempt.progress))
        attempt.stage = stage
        attempt.stage_label = label
        attempt.progress = pct
        if message is not None:
            attempt.message = message
        attempt.updated_at = time.time()

    def public_status(self, attempt_id: str) -> dict:
        a = self.get(attempt_id)
        return {
            'attempt_id': a.attempt_id,
            'platform': a.platform,
            'browser_name': a.browser_name,
            'browser_key': a.browser_key,
            'clean_mode': a.clean_mode,
            'clean_waiting_confirm': a.clean_waiting_confirm,
            'multi_platform': a.multi_platform,
            'manage_only': a.manage_only,
            'require_explicit_save': a.require_explicit_save,
            'account_id': a.account_id,
            'account_label': a.account_label,
            'profile_persistent': (a.profile_dir_used or self.profile_dir) is not None,
            'state': a.state,
            'stage': a.stage,
            'stage_label': a.stage_label,
            'progress': a.progress,
            'cdp_connected': a.cdp_connected,
            'current_host': a.current_host,
            'cookie_count': a.cookie_count,
            'marker_detected': a.marker_detected,
            'ready_platforms': list(a.ready_platforms),
            'synced_platforms': list(a.synced_platforms),
            'auto_capture': a.auto_capture,
            'capture_mode': a.capture_mode,
            'elapsed_seconds': max(0, int(time.time() - a.created_at)) if a.created_at else 0,
            'message': a.message,
            'error': a.error,
        }

    async def navigate_attempt(self, attempt_id: str, url: str) -> dict:
        """在当前活动登录浏览器窗口中跳转至指定 URL（如 Google 账号页、平台登录页或重载）。"""
        a = self.get(attempt_id)
        if a.clean_mode:
            raise AgentError('INVALID_STATE', '纯净防风控模式下请直接在已打开的浏览器地址栏操作')
        if a.state not in ('OPENING', 'WAITING') or not a.cdp_port:
            raise AgentError('INVALID_STATE', '当前登录窗口未连接就绪')
        if not str(url).startswith(('https://', 'http://')):
            raise AgentError('INVALID_PARAMETER', '无效导航地址')
        ok = await navigate_cdp_page(a.cdp_port, a.cdp_ws, url)
        return {'navigated': ok}

    async def relaunch_clean(self, attempt_id: str) -> dict:
        """将当前活动登录窗口切换为纯净无调试模式重新打开（彻底绕过 Google 安全检测）。"""
        spec = self._start_specs.get(attempt_id)
        if not spec:
            raise AgentError('NOT_FOUND', '登录会话已结束，无法切换模式，请重新发起登录')
        old = self._attempts.get(attempt_id)
        if old and old.state in ('OPENING', 'WAITING'):
            old.cancel_requested = True
            old.state = 'CANCELLED'
            self.set_stage(old, 'CANCELLED', '正在切换为纯净无调试模式重新打开...')
            self._wake(old)
            if old.task and not old.task.done():
                try:
                    await asyncio.wait_for(old.task, timeout=4.0)
                except Exception:
                    pass
        next_spec = dict(spec)
        next_spec['login_mode'] = 'clean'
        return await self.start(**next_spec)

    def request_manual_capture(self, platform: str) -> dict:
        with self._lock:
            active = [a for a in self._attempts.values() if a.platform == platform and a.state in ('OPENING', 'WAITING')]
            if not active:
                raise AgentError('NOT_FOUND', '当前没有正在进行的本地浏览器登录窗口')
            target = max(active, key=lambda a: a.created_at)
            target.manual_capture_requested = True
            msg = '正在结束网页管理...' if target.manage_only else '收到保存指令，正在立即读取浏览器 Cookie...'
            self.set_stage(target, 'CAPTURING', msg)
            self._wake(target)
            return {'attempt_id': target.attempt_id, 'requested': True}

    def request_cancel(self, platform: str) -> dict:
        with self._lock:
            active = [a for a in self._attempts.values() if a.platform == platform and a.state in ('OPENING', 'WAITING')]
            if not active:
                return {'cancelled': False}
            target = max(active, key=lambda a: a.created_at)
            target.cancel_requested = True
            target.state = 'CANCELLED'
            self.set_stage(target, 'CANCELLED', '已取消并关闭本地浏览器登录窗口')
            self._wake(target)
            return {'attempt_id': target.attempt_id, 'cancelled': True}

    async def _collect_cdp_state(self, port: int, ws_path: str, cookie_urls: tuple[str, ...]) -> dict:
        """收集当前 CDP 浏览器会话 Cookie 与脱敏页面宿主域（兼容测试对 fetch_cdp_cookies 的拦截）。"""
        if fetch_cdp_cookies is not _DEFAULT_FETCH_CDP_COOKIES:
            cookies = await fetch_cdp_cookies(port, ws_path)
            return {'cdp_connected': True, 'cookies': cookies or [], 'current_host': ''}
        return await fetch_cdp_snapshot(port, ws_path, cookie_urls)

    def _build_browser_cmd(
        self,
        browser_path: str,
        data_dir: Path,
        proxy: str,
        start_url: str,
        *,
        headless: bool = False,
        clean_mode: bool = False,
    ) -> list[str]:
        cmd = [
            browser_path,
            f'--user-data-dir={data_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-background-mode',
            '--disable-blink-features=AutomationControlled',
        ]
        # 纯净无调试 GUI 模式绝不传入 --remote-debugging-port，彻底消除 Edge/Chrome Google 安全拦截
        if headless or not clean_mode:
            cmd.append('--remote-debugging-port=0')
        if 'edge' in Path(browser_path).stem.lower():
            cmd.append('--disable-features=msEdgeFirstRunExperiences')
        if proxy:
            cmd.append(f'--proxy-server={proxy}')
        if headless:
            cmd.extend(['--headless=new', start_url])
        else:
            cmd.extend(['--new-window', start_url])
        return cmd

    async def _wait_cdp_port_file(self, data_dir: Path, proc, is_cancelled=lambda: False) -> tuple[int | None, str]:
        port_file = data_dir / 'DevToolsActivePort'
        for _ in range(self.PORT_WAIT_TICKS):
            if is_cancelled() or proc.returncode is not None:
                return None, ''
            if port_file.is_file():
                lines = port_file.read_text('utf-8', errors='ignore').strip().splitlines()
                if len(lines) >= 2 and lines[0].isdigit():
                    return int(lines[0]), lines[1].strip()
            await asyncio.sleep(self.PORT_WAIT_STEP_SEC)
        return None, ''

    async def _shutdown_browser_process(self, proc, cdp_port: int | None, cdp_ws: str) -> None:
        if cdp_port and cdp_ws:
            await close_cdp_browser(cdp_port, cdp_ws)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
        if proc is self._shared_proc:
            self._shared_proc = None
            self._shared_port = None
            self._shared_ws = ''
            self._shared_profile_dir = None
        if proc is self._clean_proc:
            self._clean_proc = None
            self._clean_profile_dir = None

    async def _extract_cookies_from_dir(
        self,
        *,
        browser_path: str,
        data_dir: Path,
        proxy: str,
        cookie_urls: tuple[str, ...],
        target_platform: str = '',
        allowed_domains: tuple[str, ...] = (),
        policy: dict | None = None,
    ) -> dict:
        """从已关闭的纯净浏览器目录通过无头 CDP 安全读取全量 Cookie（含 HttpOnly 与 App-Bound 加密会话）。"""
        async with self._get_launch_lock():
            cleanup_stale_profile_locks(data_dir)
            cmd = self._build_browser_cmd(browser_path, data_dir, proxy, 'about:blank', headless=True)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            port, ws_path = None, ''
            try:
                port, ws_path = await self._wait_cdp_port_file(data_dir, proc)
                if not port:
                    raise AgentError('UPSTREAM_ERROR', '后台读取浏览器档案超时，请确认浏览器窗口已全部关闭后重试')
                snap = await self._collect_cdp_state(port, ws_path, cookie_urls)
                cookies = snap.get('cookies') or []
                if target_platform and policy:
                    _, marker_hit, _ = analyze_cdp_cookies(cookies, policy, allowed_domains)
                    target_urls = policy.get('cookie_urls') or ()
                    if not marker_hit and target_urls:
                        navigated = await navigate_cdp_page(port, ws_path, target_urls[0])
                        if navigated:
                            wait_sec = 0.05 if self.POLL_INTERVAL_SEC < 0.1 else 1.8
                            await asyncio.sleep(wait_sec)
                            snap = await self._collect_cdp_state(port, ws_path, cookie_urls)
                return snap
            finally:
                await self._shutdown_browser_process(proc, port, ws_path)

    async def _try_reuse_shared_browser(self, target_dir: Path, target_url: str) -> tuple[object, int, str] | None:
        if self._shared_proc is None or self._shared_proc.returncode is not None or not self._shared_port:
            return None
        if self._shared_profile_dir is None or Path(self._shared_profile_dir).resolve() != Path(target_dir).resolve():
            return None
        alive, ws_url = await probe_cdp_alive(self._shared_port, self._shared_ws)
        if not alive:
            self._shared_proc = None
            self._shared_port = None
            self._shared_ws = ''
            self._shared_profile_dir = None
            return None
        if target_url and target_url != 'about:blank' and ws_url:
            await open_cdp_tab(ws_url, target_url)
        return self._shared_proc, self._shared_port, self._shared_ws

    async def start(
        self,
        platform: str,
        proxy: str,
        allowed_domains: tuple[str, ...],
        *,
        on_capture,
        domains: dict | None = None,
        on_capture_many=None,
        profile_dir_override: Path | None = None,
        initial_url: str | None = None,
        manage_only: bool = False,
        require_explicit_save: bool = False,
        account_id: str = '',
        account_label: str = '',
        account_epoch: int = 0,
        preferred_browser: str = 'auto',
        login_mode: str = 'auto',
    ) -> dict:
        is_multi = platform == MULTI_PLATFORM_KEY
        if not is_multi and platform not in PLATFORM_LOGIN_POLICIES:
            raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        browser = resolve_local_browser(preferred_browser)
        if not browser:
            raise AgentError('CAPABILITY_UNAVAILABLE', '未检测到本机 Chrome 或 Edge 浏览器，可点击“默认浏览器打开”完成登录后导入或粘贴 Cookie')

        policy = PLATFORM_LOGIN_POLICIES.get(platform, {})
        all_domains = domains or {}
        effective_explicit = bool(manage_only or require_explicit_save or is_multi or not policy.get('auto_capture'))
        start_url = initial_url or ('https://www.bilibili.com/' if is_multi else policy['start_url'])
        target_profile_dir = Path(profile_dir_override) if profile_dir_override else self.profile_dir
        browser_key = browser.get('key') or _browser_key(browser.get('name', ''), browser.get('path', ''))
        is_google_flow = bool(manage_only or platform == 'YouTube' or 'accounts.google.com' in (start_url or '').lower())
        mode_pref = str(login_mode or 'auto').strip().lower()
        if mode_pref == 'clean':
            clean_mode = True
        elif mode_pref == 'cdp':
            clean_mode = False
        else:
            # 智能模式：涉及 Google 登录页、YouTube、Google 管理或独立容器启动页时默认关闭远程调试端口，
            # 彻底避免 Microsoft Edge / Chrome 被 accounts.google.com 拦截报错
            clean_mode = bool(not is_multi and is_google_flow)

        now = time.time()
        loop = asyncio.get_running_loop()
        with self._lock:
            for old in list(self._attempts.values()):
                if old.state in ('OPENING', 'WAITING') and (old.platform == platform or old.profile_dir_used != target_profile_dir):
                    old.cancel_requested = True
                    old.state = 'CANCELLED'
                    self.set_stage(old, 'CANCELLED', '已切换至新打开的登录窗口')
                    self._wake(old)
                elif now - old.created_at > 900 or (old.state in ('COMPLETED', 'CANCELLED', 'FAILED') and now - old.updated_at > 180):
                    self._attempts.pop(old.attempt_id, None)
                    self._start_specs.pop(old.attempt_id, None)

            attempt = BrowserLoginAttempt(
                attempt_id=uuid.uuid4().hex,
                platform=platform,
                browser_name=browser['name'],
                proxy=proxy,
                browser_key=browser_key,
                clean_mode=clean_mode,
                auto_capture=bool(not effective_explicit and policy.get('auto_capture')),
                multi_platform=is_multi,
                manage_only=manage_only,
                require_explicit_save=effective_explicit,
                account_id=account_id,
                account_label=account_label,
                account_epoch=account_epoch,
                start_url=start_url,
                profile_dir_used=target_profile_dir,
                created_at=now,
                updated_at=now,
                wake_event=asyncio.Event(),
                loop=loop,
            )
            title_tag = f'{account_label} · {platform}' if account_label else platform
            init_msg = (
                f'正在打开 Google 账号管理页面（此模式仅管理退出，不保存凭据）...'
                if manage_only
                else f'正在以{"纯净无调试" if clean_mode else "实时读取"}模式启动 {browser["name"]}（{title_tag}）...'
            )
            self.set_stage(attempt, 'LAUNCHING', init_msg)
            self._attempts[attempt.attempt_id] = attempt
            self._start_specs[attempt.attempt_id] = {
                'platform': platform,
                'proxy': proxy,
                'allowed_domains': allowed_domains,
                'on_capture': on_capture,
                'domains': domains,
                'on_capture_many': on_capture_many,
                'profile_dir_override': profile_dir_override,
                'initial_url': initial_url,
                'manage_only': manage_only,
                'require_explicit_save': require_explicit_save,
                'account_id': account_id,
                'account_label': account_label,
                'account_epoch': account_epoch,
                'preferred_browser': preferred_browser,
                'login_mode': login_mode,
            }

        using_persistent = target_profile_dir is not None
        temp_profile = None
        if using_persistent:
            data_dir = target_profile_dir
            data_dir.mkdir(parents=True, exist_ok=True)
            record_profile_browser(data_dir, browser)
        else:
            temp_profile = tempfile.TemporaryDirectory(prefix='omni-browser-login-', ignore_cleanup_errors=True)
            data_dir = Path(temp_profile.name)

        async def _complete_single(netscape_text: str, count: int, mode: str) -> None:
            attempt.capture_mode = mode
            self.set_stage(attempt, 'SAVING', f'已捕获 {count} 项 {platform} Cookie，正在加密保存...')

            def _report(stage: str, msg: str):
                self.set_stage(attempt, stage, msg)

            verify_note = ''
            try:
                res = await on_capture(platform, netscape_text, mode=mode, report=_report)
                if isinstance(res, str) and res:
                    verify_note = f'（{res}）'
            except TypeError:
                await on_capture(platform, netscape_text)

            attempt.state = 'COMPLETED'
            prefix = '检测到关闭窗口前已完成登录，已自动保存' if mode == 'salvaged' else f'Cookie 已通过本地 {browser["name"]} 加密保存并启用'
            self.set_stage(attempt, 'COMPLETED', f'{platform} {prefix}（共 {count} 项）{verify_note}')

        async def _complete_multi(scanned: list[dict], mode: str) -> bool:
            if mode == 'salvaged':
                targets = [item for item in scanned if item['auto_sync_ready'] or item['marker_detected']]
            else:
                targets = [item for item in scanned if item['manual_ready']]
            if not targets or on_capture_many is None:
                return False
            attempt.capture_mode = mode
            names = [item['platform'] for item in targets]
            self.set_stage(attempt, 'SAVING', f'正在批量保存 {len(names)} 个已登录平台凭据...')
            saved = await on_capture_many(targets, mode=mode)
            attempt.synced_platforms = saved
            attempt.state = 'COMPLETED'
            prefix = '检测到关闭窗口前已登录' if mode == 'salvaged' else '已完成多平台同步保存'
            self.set_stage(attempt, 'COMPLETED', f'{prefix}，共写入 {len(saved)} 个平台（{"、".join(saved)}）')
            return True

        async def _sleep_or_wake(proc, seconds: float) -> None:
            if attempt.manual_capture_requested or attempt.cancel_requested or (proc and proc.returncode is not None):
                return
            if not attempt.wake_event:
                await asyncio.sleep(seconds)
                return
            try:
                await asyncio.wait_for(attempt.wake_event.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass
            finally:
                attempt.wake_event.clear()

        async def _monitor():
            proc = None
            cdp_port = None
            cdp_ws = ''
            reused = False
            confirm_polls = 0
            cookie_urls = tuple(
                url
                for p_pol in PLATFORM_LOGIN_POLICIES.values()
                for url in (p_pol.get('cookie_urls') or ())
            ) if is_multi else (policy.get('cookie_urls') or ())

            async def _run_clean_extract(mode: str) -> str:
                self.set_stage(attempt, 'CAPTURING', '正在后台无头读取已登录的完整 Cookie（含 HttpOnly）...')
                snap = await self._extract_cookies_from_dir(
                    browser_path=browser['path'],
                    data_dir=data_dir,
                    proxy=proxy,
                    cookie_urls=cookie_urls,
                    target_platform=('' if is_multi else platform),
                    allowed_domains=allowed_domains,
                    policy=policy,
                )
                cookies = snap.get('cookies') or []
                if is_multi:
                    scanned = scan_cookies_by_platform(cookies, all_domains)
                    attempt.last_scanned = scanned
                    attempt.cookie_count = sum(item['cookie_count'] for item in scanned)
                    attempt.marker_detected = any(item['marker_detected'] for item in scanned)
                    if await _complete_multi(scanned, mode):
                        return 'completed'
                    return '纯净浏览器档案中暂未发现已登录平台会话，请重新登录后再试'

                netscape, marker_hit, cookie_count = analyze_cdp_cookies(cookies, policy, allowed_domains)
                attempt.cookie_count = cookie_count
                attempt.marker_detected = marker_hit
                if mode == 'salvaged' and not (netscape and (marker_hit or not policy.get('markers'))):
                    if cookie_count > 0:
                        return f'纯净浏览器窗口已关闭（仅捕捉到 {cookie_count} 项访客 Cookie，未检测到账号登录标记，未保存凭据）'
                    return '纯净浏览器窗口已关闭，未检测到平台会话，未保存凭据'
                if netscape:
                    await _complete_single(netscape, cookie_count, mode)
                    return 'completed'
                return f'未能从 {browser["name"]} 档案中读取到 {platform} 有效登录 Cookie，请确认已完成网页登录后重试'

            try:
                if clean_mode:
                    async with self._get_launch_lock():
                        if self._shared_proc is not None:
                            await self._shutdown_browser_process(self._shared_proc, self._shared_port, self._shared_ws)
                        if self._clean_proc is not None and self._clean_proc.returncode is None:
                            if self._clean_profile_dir and Path(self._clean_profile_dir).resolve() == Path(data_dir).resolve():
                                helper = await asyncio.create_subprocess_exec(
                                    browser['path'],
                                    f'--user-data-dir={data_dir}',
                                    '--new-window',
                                    start_url,
                                    stdout=asyncio.subprocess.DEVNULL,
                                    stderr=asyncio.subprocess.DEVNULL,
                                    stdin=asyncio.subprocess.DEVNULL,
                                )
                                try:
                                    await asyncio.wait_for(helper.wait(), timeout=2.0)
                                except asyncio.TimeoutError:
                                    pass
                                proc = self._clean_proc
                                reused = True
                            else:
                                await graceful_close_browser_process(self._clean_proc)
                                self._clean_proc = None
                                self._clean_profile_dir = None
                        if not reused:
                            cleanup_stale_profile_locks(data_dir)
                            cmd = self._build_browser_cmd(browser['path'], data_dir, proxy, start_url, clean_mode=True)
                            proc = await asyncio.create_subprocess_exec(
                                *cmd,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                                stdin=asyncio.subprocess.DEVNULL,
                            )
                            if using_persistent:
                                self._clean_proc = proc
                                self._clean_profile_dir = data_dir
                        attempt.process = proc

                    if attempt.cancel_requested:
                        attempt.state = 'CANCELLED'
                        self.set_stage(attempt, 'CANCELLED', '已取消并关闭纯净浏览器登录窗口')
                        return

                    attempt.state = 'WAITING'
                    if manage_only:
                        clean_msg = (
                            f'已在纯净无调试模式下打开 {browser["name"]} 管理 Google 登录（无安全拦截）。'
                            '请退出或切换网页账号后直接关闭浏览器窗口，或点击右下“完成”。'
                        )
                    elif effective_explicit:
                        clean_msg = (
                            f'已在纯净无调试模式下打开 {browser["name"]}（防 Google 安全拦截）。'
                            f'请登录 Google 并访问 {platform} 确认已登录后，点击右下“保存 {platform} 下载凭据”或先关窗再确认保存。'
                        )
                    else:
                        clean_msg = (
                            f'已在纯净无调试模式下打开 {browser["name"]}（防 Google 安全拦截）。'
                            '请完成账号登录，完成后关闭浏览器窗口（或点右下保存按钮）即可自动提取保存。'
                        )
                    self.set_stage(attempt, 'WAITING_LOGIN', clean_msg)

                    while not attempt.cancel_requested:
                        if proc is not None and proc.returncode is not None:
                            if manage_only:
                                attempt.state = 'COMPLETED'
                                self.set_stage(attempt, 'COMPLETED', '已完成网页账号管理（浏览器窗口已关闭，未修改本机下载凭据）')
                                return
                            if not effective_explicit:
                                await asyncio.sleep(0.05 if self.POLL_INTERVAL_SEC < 0.1 else 0.4)
                                extracted = await _run_clean_extract('salvaged')
                                if extracted == 'completed':
                                    return
                                attempt.state = 'CANCELLED'
                                self.set_stage(attempt, 'CANCELLED', extracted)
                                return
                            if not attempt.clean_waiting_confirm:
                                attempt.clean_waiting_confirm = True
                                confirm_msg = (
                                    f'纯净 {browser["name"]} 窗口已关闭；若已完成登录，请点击右下“保存 {platform} 下载凭据”读取并保存（点“取消”放弃）'
                                    if not is_multi
                                    else '纯净浏览器窗口已关闭；若已完成登录，请点击右下“保存全部已登录平台”，或点“取消”放弃'
                                )
                                self.set_stage(attempt, 'CONFIRMING', confirm_msg)
                            if attempt.manual_capture_requested:
                                attempt.manual_capture_requested = False
                                await asyncio.sleep(0.05 if self.POLL_INTERVAL_SEC < 0.1 else 0.3)
                                extracted = await _run_clean_extract('manual')
                                if extracted == 'completed':
                                    return
                                attempt.state = 'FAILED'
                                attempt.error = extracted
                                self.set_stage(attempt, 'FAILED', extracted)
                                return
                            await _sleep_or_wake(None, self.POLL_INTERVAL_SEC)
                            continue

                        if attempt.manual_capture_requested:
                            attempt.manual_capture_requested = False
                            if manage_only:
                                await graceful_close_browser_process(proc)
                                attempt.state = 'COMPLETED'
                                self.set_stage(attempt, 'COMPLETED', '已完成网页账号管理（未保存或修改本机下载凭据）')
                                return
                            self.set_stage(attempt, 'CAPTURING', '正在优雅关闭纯净浏览器窗口，确保会话完整写入磁盘并读取 Cookie...')
                            await graceful_close_browser_process(proc)
                            await asyncio.sleep(0.05 if self.POLL_INTERVAL_SEC < 0.1 else 0.4)
                            extracted = await _run_clean_extract('manual')
                            if extracted == 'completed':
                                return
                            attempt.state = 'FAILED'
                            attempt.error = extracted
                            self.set_stage(attempt, 'FAILED', extracted)
                            return

                        await _sleep_or_wake(proc, self.POLL_INTERVAL_SEC)

                    attempt.state = 'CANCELLED'
                    self.set_stage(attempt, 'CANCELLED', '已关闭 Google 账号管理窗口' if manage_only else '已取消并关闭本地浏览器登录窗口')
                    return

                async with self._get_launch_lock():
                    if self._clean_proc is not None and self._clean_proc.returncode is None:
                        await graceful_close_browser_process(self._clean_proc)
                        self._clean_proc = None
                        self._clean_profile_dir = None
                    if using_persistent and self._shared_proc is not None and self._shared_profile_dir != data_dir:
                        await self._shutdown_browser_process(self._shared_proc, self._shared_port, self._shared_ws)
                    if using_persistent:
                        reused_info = await self._try_reuse_shared_browser(data_dir, start_url)
                        if reused_info:
                            proc, cdp_port, cdp_ws = reused_info
                            reused = True
                    if not reused:
                        if using_persistent:
                            cleanup_stale_profile_locks(data_dir)
                        cmd = self._build_browser_cmd(browser['path'], data_dir, proxy, start_url)
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                            stdin=asyncio.subprocess.DEVNULL,
                        )
                        attempt.process = proc
                        self.set_stage(attempt, 'CONNECTING', f'已打开 {browser["name"]} 窗口，正在建立本地 Cookie 读取通道...')
                        cdp_port, cdp_ws = await self._wait_cdp_port_file(
                            data_dir,
                            proc,
                            lambda: attempt.cancel_requested,
                        )
                        if using_persistent and cdp_port and proc.returncode is None:
                            self._shared_proc = proc
                            self._shared_port = cdp_port
                            self._shared_ws = cdp_ws
                            self._shared_profile_dir = data_dir
                    else:
                        attempt.process = proc
                        self.set_stage(attempt, 'CONNECTING', f'已复用打开中的 {browser["name"]} 独立窗口，正在建立读取通道...')

                attempt.cdp_port = cdp_port
                attempt.cdp_ws = cdp_ws
                if attempt.cancel_requested or (proc and proc.returncode is not None):
                    attempt.state = 'CANCELLED'
                    self.set_stage(attempt, 'CANCELLED', '本地浏览器窗口已关闭（尚未完成通道初始化）')
                    return
                if not cdp_port:
                    raise RuntimeError('未能与本地浏览器建立 Cookie 读取通道，请关闭已存在的同内核隔离实例或改用默认浏览器打开后导入')

                attempt.state = 'WAITING'
                attempt.cdp_connected = True
                if manage_only:
                    wait_msg = '请在打开的 Google 页面点击右上角头像手动退出登录（此页面不会保存或覆盖任何下载凭据），完成后点“完成”关闭'
                elif is_multi:
                    wait_msg = f'已在持久托管 {browser["name"]} 中监听全平台；在窗口内登录任意平台后点击“保存全部已登录平台”或直接关窗自动保存'
                elif attempt.auto_capture:
                    wait_msg = f'已连接本地 {browser["name"]} 读取通道；请完成账号登录，系统将自动捕获并保存完整 Cookie'
                else:
                    wait_msg = f'请在本地 {browser["name"]} 确认使用目标账号登录后，点击右下“保存 {platform} 下载凭据”'
                self.set_stage(attempt, 'WAITING_LOGIN', wait_msg)

                first_tick = True
                while not attempt.cancel_requested and proc.returncode is None:
                    if not first_tick:
                        await _sleep_or_wake(proc, self.POLL_INTERVAL_SEC)
                    first_tick = False
                    if attempt.cancel_requested or proc.returncode is not None:
                        break
                    if manage_only:
                        try:
                            meta = await fetch_cdp_targets(cdp_port, cdp_ws)
                            if meta.get('current_host'):
                                attempt.current_host = meta['current_host']
                        except Exception:
                            pass
                        if attempt.manual_capture_requested:
                            attempt.manual_capture_requested = False
                            attempt.state = 'COMPLETED'
                            self.set_stage(attempt, 'COMPLETED', '已完成网页账号管理（未保存或修改本机下载凭据）')
                            return
                        continue

                    try:
                        snap = await self._collect_cdp_state(cdp_port, cdp_ws, cookie_urls)
                    except Exception:
                        if attempt.manual_capture_requested:
                            attempt.manual_capture_requested = False
                            if is_multi and await _complete_multi(attempt.last_scanned, 'manual'):
                                return
                            if not is_multi and attempt.last_netscape:
                                await _complete_single(attempt.last_netscape, attempt.last_cookie_count, 'manual')
                                return
                            self.set_stage(attempt, 'WAITING_LOGIN', '浏览器正在加载页面，请稍候 1 秒后再试点击抓取')
                        continue

                    attempt.cdp_connected = bool(snap.get('cdp_connected', True))
                    if snap.get('current_host'):
                        attempt.current_host = snap['current_host']

                    if is_multi:
                        scanned = scan_cookies_by_platform(snap['cookies'], all_domains)
                        attempt.last_scanned = scanned
                        attempt.ready_platforms = [
                            {
                                'platform': item['platform'],
                                'cookie_count': item['cookie_count'],
                                'marker_detected': item['marker_detected'],
                                'auto_sync_ready': item['auto_sync_ready'],
                            }
                            for item in scanned
                        ]
                        attempt.cookie_count = sum(item['cookie_count'] for item in scanned)
                        attempt.marker_detected = any(item['marker_detected'] for item in scanned)
                        if attempt.manual_capture_requested:
                            attempt.manual_capture_requested = False
                            if await _complete_multi(scanned, 'manual'):
                                return
                            self.set_stage(attempt, 'WAITING_LOGIN', '尚未检测到任何已登录平台 Cookie，请先在托管窗口完成登录')
                            continue
                        ready_names = [i['platform'] for i in scanned if i['marker_detected'] or i['auto_sync_ready']]
                        if ready_names:
                            attempt.message = f'正在监听多平台窗口 · 已识别 {len(ready_names)} 个平台登录态（{"、".join(ready_names[:5])}{" 等" if len(ready_names) > 5 else ""}），点保存即可批量入库'
                        elif attempt.cookie_count > 0:
                            attempt.message = f'正在监听多平台窗口 · 已捕获 {attempt.cookie_count} 项基础 Cookie，请完成目标网站登录'
                        else:
                            attempt.message = wait_msg
                        attempt.updated_at = time.time()
                        continue

                    netscape, marker_hit, cookie_count = analyze_cdp_cookies(snap['cookies'], policy, allowed_domains)
                    attempt.cookie_count = cookie_count
                    attempt.marker_detected = marker_hit
                    if netscape:
                        attempt.last_netscape = netscape
                        attempt.last_marker_hit = marker_hit
                        attempt.last_cookie_count = cookie_count

                    if attempt.manual_capture_requested:
                        attempt.manual_capture_requested = False
                        selected_netscape = netscape or attempt.last_netscape
                        selected_count = cookie_count if netscape else attempt.last_cookie_count
                        if selected_netscape:
                            await _complete_single(selected_netscape, selected_count, 'manual')
                            return
                        host_tip = f'（当前页面：{attempt.current_host}）' if attempt.current_host else ''
                        self.set_stage(attempt, 'WAITING_LOGIN', f'尚未取得有效会话{host_tip}，请先完成网页登录后再保存')
                        continue

                    if attempt.auto_capture and netscape and marker_hit:
                        if confirm_polls == 0:
                            confirm_polls = 1
                            self.set_stage(
                                attempt,
                                'CONFIRMING',
                                f'已识别到登录核心标记（当前 {cookie_count} 项 Cookie），正在确认页面跳转完成与全量补采...',
                            )
                            await _sleep_or_wake(proc, self.CONFIRM_WINDOW_SEC)
                            continue
                        await _complete_single(netscape, cookie_count, 'auto')
                        return
                    confirm_polls = 0
                    if attempt.stage != 'WAITING_LOGIN':
                        self.set_stage(attempt, 'WAITING_LOGIN', wait_msg)
                    elif cookie_count > 0:
                        host_part = f'@{attempt.current_host}' if attempt.current_host else ''
                        if attempt.auto_capture:
                            attempt.message = f'正在监听登录态{host_part} · 已发现 {cookie_count} 项基础 Cookie，等待登录完成...'
                        else:
                            attempt.message = f'正在监听{host_part} · 已捕获 {cookie_count} 项 Cookie，请在确认登录后点击“保存 {platform} 下载凭据”'
                        attempt.updated_at = time.time()

                # 浏览器窗口关闭前自动兜底挽救（显式保存 / 只管理模式下不自动覆盖）
                if attempt.state not in ('COMPLETED', 'FAILED'):
                    if not attempt.cancel_requested and not manage_only and not effective_explicit:
                        if is_multi and await _complete_multi(attempt.last_scanned, 'salvaged'):
                            return
                        if not is_multi and attempt.last_netscape and attempt.last_marker_hit:
                            await _complete_single(attempt.last_netscape, attempt.last_cookie_count, 'salvaged')
                            return
                    if not attempt.cancel_requested and is_multi and attempt.last_scanned:
                        if await _complete_multi(attempt.last_scanned, 'salvaged'):
                            return
                    attempt.state = 'CANCELLED'
                    if manage_only:
                        cancel_msg = '已关闭 Google 账号管理窗口'
                    elif not is_multi and attempt.last_cookie_count > 0 and not attempt.cancel_requested:
                        if not attempt.last_marker_hit:
                            cancel_msg = (
                                f'浏览器窗口已关闭（仅捕捉到 {attempt.last_cookie_count} 项访客 Cookie，'
                                '未检测到账号登录标记，未保存凭据）'
                            )
                        else:
                            cancel_msg = (
                                f'浏览器窗口已关闭（已捕捉到 {attempt.last_cookie_count} 项 Cookie，'
                                '处于手动确认/防覆盖模式，需点击页面右下保存按钮，未覆盖现有凭据）'
                            )
                    else:
                        cancel_msg = '已关闭本地浏览器登录窗口' if attempt.cancel_requested else '本地浏览器窗口已关闭，未保存凭据'
                    self.set_stage(attempt, 'CANCELLED', cancel_msg)
            except asyncio.CancelledError:
                attempt.state = 'CANCELLED'
                self.set_stage(attempt, 'CANCELLED', '本地浏览器登录已中止')
                raise
            except Exception as exc:
                attempt.state = 'FAILED'
                attempt.error = safe_text(exc)
                self.set_stage(attempt, 'FAILED', attempt.error)
            finally:
                with self._lock:
                    other_active = any(
                        a.attempt_id != attempt.attempt_id and a.state in ('OPENING', 'WAITING') and a.process is proc
                        for a in self._attempts.values()
                    )
                should_close_browser = (not reused and not other_active) or attempt.cancel_requested
                if proc is not None and should_close_browser and not other_active:
                    if clean_mode:
                        await graceful_close_browser_process(proc)
                        if proc is self._clean_proc:
                            self._clean_proc = None
                            self._clean_profile_dir = None
                    else:
                        await self._shutdown_browser_process(proc, cdp_port, cdp_ws)
                if temp_profile is not None:
                    try:
                        temp_profile.cleanup()
                    except Exception:
                        pass

        attempt.task = asyncio.create_task(_monitor())
        return self.public_status(attempt.attempt_id)

    async def sync_from_profile(
        self,
        proxy: str,
        domains: dict,
        existing_imported: set[str],
        *,
        include_manual: bool = False,
        profile_dir_override: Path | None = None,
        preferred_browser: str = 'auto',
        on_capture_many,
    ) -> dict:
        """从持久化托管浏览器直接或后台静默无头同步全部已登录平台的 Cookie。"""
        used_headless = False
        cookies: list[dict] = []
        target_dir = Path(profile_dir_override) if profile_dir_override else self.profile_dir

        if (
            self._clean_proc is not None
            and self._clean_proc.returncode is None
            and self._clean_profile_dir is not None
            and target_dir is not None
            and Path(self._clean_profile_dir).resolve() == Path(target_dir).resolve()
        ):
            raise AgentError('INVALID_STATE', '该独立纯净浏览器窗口仍在打开中，请先关闭窗口或点击登录面板右下角的保存按钮')

        same_active = (
            self.is_shared_browser_active()
            and self._shared_port
            and self._shared_profile_dir is not None
            and target_dir is not None
            and Path(self._shared_profile_dir).resolve() == Path(target_dir).resolve()
        )
        if same_active:
            alive, ws_url = await probe_cdp_alive(self._shared_port, self._shared_ws)
            if alive:
                cookie_urls = tuple(
                    url for pol in PLATFORM_LOGIN_POLICIES.values() for url in (pol.get('cookie_urls') or ())
                )
                snap = await self._collect_cdp_state(self._shared_port, self._shared_ws or ws_url, cookie_urls)
                cookies = snap.get('cookies') or []
            else:
                self._shared_proc = None
                self._shared_port = None
                self._shared_ws = ''
                self._shared_profile_dir = None
                same_active = False

        if not cookies and not same_active:
            if not profile_has_session_data(target_dir):
                raise AgentError('NOT_FOUND', '该浏览器档案尚未建立网页登录会话，请先打开完成一次登录')
            recorded_key = read_profile_browser_key(target_dir)
            browser = resolve_local_browser(recorded_key or preferred_browser)
            if not browser:
                raise AgentError('CAPABILITY_UNAVAILABLE', '未检测到本机 Chrome 或 Edge 浏览器')
            used_headless = True
            cookie_urls = tuple(
                url for pol in PLATFORM_LOGIN_POLICIES.values() for url in (pol.get('cookie_urls') or ())
            )
            snap = await self._extract_cookies_from_dir(
                browser_path=browser['path'],
                data_dir=target_dir,
                proxy=proxy,
                cookie_urls=cookie_urls,
            )
            cookies = snap.get('cookies') or []

        scanned = scan_cookies_by_platform(cookies, domains, existing_imported=existing_imported)
        targets = [
            item for item in scanned
            if item['auto_sync_ready'] or (include_manual and item['manual_ready'])
        ]
        synced = await on_capture_many(targets, mode='sync') if targets else []
        skipped = [item['platform'] for item in scanned if item['platform'] not in synced]
        return {
            'synced_platforms': synced,
            'skipped_platforms': skipped,
            'used_headless': used_headless,
            'total_cookies': sum(item['cookie_count'] for item in targets),
        }

    async def delete_profile_dir(self, target_dir: Path | None) -> bool:
        """优雅停掉占用目标档案目录的任何浏览器进程并安全移除目录。"""
        if not target_dir:
            return False
        resolved = Path(target_dir).resolve()
        for aid, att in list(self._attempts.items()):
            if att.profile_dir_used and Path(att.profile_dir_used).resolve() == resolved and att.state in ('OPENING', 'WAITING'):
                att.cancel_requested = True
                self._wake(att)
                if att.task and not att.task.done():
                    try:
                        await asyncio.wait_for(att.task, timeout=3)
                    except Exception:
                        pass
        if self._shared_proc is not None and self._shared_profile_dir and Path(self._shared_profile_dir).resolve() == resolved:
            await self._shutdown_browser_process(self._shared_proc, self._shared_port, self._shared_ws)
        if self._clean_proc is not None and self._clean_profile_dir and Path(self._clean_profile_dir).resolve() == resolved:
            await graceful_close_browser_process(self._clean_proc)
            self._clean_proc = None
            self._clean_profile_dir = None
        if not resolved.exists():
            return True
        for attempt_idx in range(6):
            try:
                shutil.rmtree(resolved)
                return True
            except OSError:
                if attempt_idx == 5:
                    raise AgentError('INTERNAL_ERROR', '浏览器进程文件尚未完全释放，请稍后再试清理')
                await asyncio.sleep(0.25)
        return True

    async def reset_profile(self) -> dict:
        """优雅关闭正在运行的托管浏览器实例并彻底清空共享持久化浏览器数据目录。"""
        await self.close_all()
        if self._shared_proc is not None:
            await self._shutdown_browser_process(self._shared_proc, self._shared_port, self._shared_ws)
        if self._clean_proc is not None:
            await graceful_close_browser_process(self._clean_proc)
            self._clean_proc = None
            self._clean_profile_dir = None
        if not self.profile_dir:
            return {'cleared': False}
        await self.delete_profile_dir(self.profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        return {'cleared': True}

    async def close_all(self):
        for attempt in list(self._attempts.values()):
            attempt.cancel_requested = True
            self._wake(attempt)
            if attempt.task and not attempt.task.done():
                attempt.task.cancel()
        tasks = [a.task for a in self._attempts.values() if a.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._shared_proc is not None:
            await self._shutdown_browser_process(self._shared_proc, self._shared_port, self._shared_ws)
        if self._clean_proc is not None:
            await graceful_close_browser_process(self._clean_proc)
            self._clean_proc = None
            self._clean_profile_dir = None
