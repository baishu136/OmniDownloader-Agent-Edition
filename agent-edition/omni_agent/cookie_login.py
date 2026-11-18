"""完善平台 Cookie 导入格式兼容、B站扫码登录、以及本地桌面浏览器（Chrome / Edge）隔离登录流程。"""
import asyncio
from dataclasses import dataclass
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
        'cookie_urls': ('https://www.youtube.com/', 'https://youtube.com/', 'https://accounts.google.com/'),
        'markers': ('SID', 'SAPISID', '__Secure-3PAPISID', 'LOGIN_INFO'),
        'auto_capture': True,
        'hint': '将启动本机真实浏览器独立窗口登录 Google / YouTube；登录完成后自动捕获完整 Cookie，也可从常用浏览器导出 JSON / cookies.txt 导入。',
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


def find_local_browser() -> dict | None:
    """探测本机已安装的 Chrome / Microsoft Edge / Chromium 可执行文件。"""
    override = os.environ.get('OMNI_BROWSER_PATH', '').strip()
    if override and Path(override).is_file():
        return {'path': override, 'name': Path(override).stem}

    if os.name != 'nt' and sys.platform != 'darwin' and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return None

    candidates: list[tuple[str, str]] = []
    if os.name == 'nt':
        try:
            import winreg
            for exe, label in (('msedge.exe', 'Microsoft Edge'), ('chrome.exe', 'Google Chrome'), ('brave.exe', 'Brave')):
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
            ('Microsoft Edge', r'Microsoft\Edge\Application\msedge.exe'),
            ('Google Chrome', r'Google\Chrome\Application\chrome.exe'),
            ('Brave', r'BraveSoftware\Brave-Browser\Application\brave.exe'),
        )
        for base in env_dirs:
            if not base:
                continue
            for label, rel in rel_paths:
                candidates.append((label, str(Path(base) / rel)))
        for label, cmd in (('Microsoft Edge', 'msedge'), ('Google Chrome', 'chrome')):
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
    for label, raw_path in candidates:
        norm = str(raw_path or '').strip()
        if not norm or norm.lower() in seen:
            continue
        seen.add(norm.lower())
        if Path(norm).is_file():
            return {'path': norm, 'name': label}
    return None


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
        return '', False
    return '# Netscape HTTP Cookie File\n' + '\n'.join(dedup.values()) + '\n', marker_hit


async def fetch_cdp_cookies(port: int, browser_ws_path: str) -> list[dict]:
    """通过本机环回 CDP 端口读取隔离浏览器实例的完整 Cookie（含 HttpOnly）。"""
    ws_url = f'ws://127.0.0.1:{port}{browser_ws_path}'
    async with websockets.connect(ws_url, proxy=None, max_size=8 * 1024 * 1024, open_timeout=3) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Storage.getCookies'}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if msg.get('id') != 1:
                continue
            if 'result' in msg and isinstance(msg['result'].get('cookies'), list):
                return msg['result']['cookies']
            break

    # 兼容不支持浏览器级 Storage.getCookies 的旧版 Chromium 内核：退回页面会话 Network.getAllCookies
    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
        targets = (await client.get(f'http://127.0.0.1:{port}/json/list')).json()
    for target in targets:
        page_ws = target.get('webSocketDebuggerUrl')
        if page_ws:
            async with websockets.connect(page_ws, proxy=None, max_size=8 * 1024 * 1024, open_timeout=3) as ws:
                await ws.send(json.dumps({'id': 2, 'method': 'Network.getAllCookies'}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    if msg.get('id') == 2:
                        return (msg.get('result') or {}).get('cookies') or []
    return []


async def close_cdp_browser(port: int, browser_ws_path: str) -> None:
    try:
        ws_url = f'ws://127.0.0.1:{port}{browser_ws_path}'
        async with websockets.connect(ws_url, proxy=None, open_timeout=2) as ws:
            await ws.send(json.dumps({'id': 9, 'method': 'Browser.close'}))
    except Exception:
        pass


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
        if code == 86101:
            return {'status': 'waiting', 'message': '等待手机哔哩哔哩 App 扫码...'}
        if code == 86090:
            return {'status': 'scanned', 'message': '已扫码，请在手机端点击确认登录'}
        if code == 86038:
            with self._lock:
                self._sessions.pop(qr_id, None)
            client.close()
            return {'status': 'expired', 'message': '二维码已失效，请重新生成'}
        if code != 0:
            return {'status': 'error', 'message': safe_text(data.get('message') or 'B 站扫码登录返回异常')}

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
        return {'status': 'confirmed', 'netscape': '# Netscape HTTP Cookie File\n' + '\n'.join(rows.values()) + '\n'}


@dataclass
class BrowserLoginAttempt:
    attempt_id: str
    platform: str
    browser_name: str
    proxy: str
    state: str = 'OPENING'
    message: str = '正在启动本地浏览器登录窗口...'
    error: str = ''
    manual_capture_requested: bool = False
    cancel_requested: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    process: object = None
    task: object = None


class LocalBrowserLoginManager:
    """使用本机 Chrome / Edge 独立隔离窗口完成平台登录并经由环回 CDP 捕获完整 Cookie。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._attempts: dict[str, BrowserLoginAttempt] = {}

    def get(self, attempt_id: str) -> BrowserLoginAttempt:
        attempt = self._attempts.get(attempt_id)
        if not attempt:
            raise AgentError('NOT_FOUND', '登录会话已结束或不存在')
        return attempt

    def public_status(self, attempt_id: str) -> dict:
        a = self.get(attempt_id)
        return {
            'attempt_id': a.attempt_id,
            'platform': a.platform,
            'browser_name': a.browser_name,
            'state': a.state,
            'message': a.message,
            'error': a.error,
        }

    def request_manual_capture(self, platform: str) -> dict:
        with self._lock:
            active = [a for a in self._attempts.values() if a.platform == platform and a.state in ('OPENING', 'WAITING')]
            if not active:
                raise AgentError('NOT_FOUND', '当前没有正在进行的本地浏览器登录窗口')
            target = max(active, key=lambda a: a.created_at)
            target.manual_capture_requested = True
            target.updated_at = time.time()
            return {'attempt_id': target.attempt_id, 'requested': True}

    def request_cancel(self, platform: str) -> dict:
        with self._lock:
            active = [a for a in self._attempts.values() if a.platform == platform and a.state in ('OPENING', 'WAITING')]
            if not active:
                return {'cancelled': False}
            target = max(active, key=lambda a: a.created_at)
            target.cancel_requested = True
            target.state = 'CANCELLED'
            target.message = '已关闭本地浏览器登录窗口'
            target.updated_at = time.time()
            return {'attempt_id': target.attempt_id, 'cancelled': True}

    async def start(self, platform: str, proxy: str, allowed_domains: tuple[str, ...], *, on_capture) -> dict:
        if platform not in PLATFORM_LOGIN_POLICIES:
            raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        browser = find_local_browser()
        if not browser:
            raise AgentError('CAPABILITY_UNAVAILABLE', '未检测到本机 Chrome 或 Edge 浏览器，可点击“在默认浏览器打开”完成登录后导入或粘贴 Cookie')

        now = time.time()
        with self._lock:
            for aid, item in list(self._attempts.items()):
                if now - item.created_at > 900 or item.state in ('COMPLETED', 'CANCELLED', 'FAILED'):
                    if now - item.updated_at > 60:
                        self._attempts.pop(aid, None)
            attempt = BrowserLoginAttempt(
                attempt_id=uuid.uuid4().hex,
                platform=platform,
                browser_name=browser['name'],
                proxy=proxy,
                created_at=now,
                updated_at=now,
            )
            self._attempts[attempt.attempt_id] = attempt

        policy = PLATFORM_LOGIN_POLICIES[platform]
        temp_profile = tempfile.TemporaryDirectory(prefix='omni-browser-login-', ignore_cleanup_errors=True)
        cmd = [
            browser['path'],
            f'--user-data-dir={temp_profile.name}',
            '--remote-debugging-port=0',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-default-apps',
        ]
        if proxy:
            cmd.append(f'--proxy-server={proxy}')
        cmd.extend(['--new-window', policy['start_url']])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        attempt.process = proc

        async def _monitor():
            cdp_port = None
            cdp_ws = ''
            try:
                port_file = Path(temp_profile.name) / 'DevToolsActivePort'
                for _ in range(150):
                    if attempt.cancel_requested or proc.returncode is not None:
                        break
                    if port_file.is_file():
                        lines = port_file.read_text('utf-8', errors='ignore').strip().splitlines()
                        if len(lines) >= 2 and lines[0].isdigit():
                            cdp_port = int(lines[0])
                            cdp_ws = lines[1].strip()
                            break
                    await asyncio.sleep(0.1)

                if attempt.cancel_requested or proc.returncode is not None:
                    attempt.state = 'CANCELLED'
                    attempt.message = '本地浏览器窗口已关闭'
                    attempt.updated_at = time.time()
                    return
                if not cdp_port:
                    raise RuntimeError('未能与本地浏览器建立 Cookie 读取通道，请关闭已存在的同内核隔离实例或改用默认浏览器打开后导入')

                attempt.state = 'WAITING'
                attempt.message = (
                    f'已启动本地 {browser["name"]} 独立登录窗口；登录后将自动保存 Cookie'
                    if policy.get('auto_capture')
                    else f'已启动本地 {browser["name"]} 独立登录窗口；请完成账号登录后点击“我已登录，抓取保存”'
                )
                attempt.updated_at = time.time()

                while not attempt.cancel_requested and proc.returncode is None:
                    await asyncio.sleep(1.0)
                    if attempt.cancel_requested or proc.returncode is not None:
                        break
                    try:
                        cookies = await fetch_cdp_cookies(cdp_port, cdp_ws)
                    except Exception:
                        continue
                    netscape, marker_hit = cdp_cookies_to_netscape(cookies, policy, allowed_domains)
                    force = attempt.manual_capture_requested
                    if force:
                        attempt.manual_capture_requested = False
                    if netscape and (force or (policy.get('auto_capture') and marker_hit)):
                        await on_capture(platform, netscape)
                        attempt.state = 'COMPLETED'
                        attempt.message = f'{platform} Cookie 已通过本地 {browser["name"]} 加密保存并启用'
                        attempt.updated_at = time.time()
                        return
                    if force:
                        attempt.message = '尚未检测到有效平台 Cookie，请确认已在打开的浏览器窗口完成登录后再点抓取'
                        attempt.updated_at = time.time()

                if attempt.state not in ('COMPLETED', 'FAILED'):
                    attempt.state = 'CANCELLED'
                    attempt.message = '本地浏览器窗口已关闭'
                    attempt.updated_at = time.time()
            except asyncio.CancelledError:
                attempt.state = 'CANCELLED'
                attempt.message = '本地浏览器登录已中止'
                raise
            except Exception as exc:
                attempt.state = 'FAILED'
                attempt.error = safe_text(exc)
                attempt.message = attempt.error
                attempt.updated_at = time.time()
            finally:
                if cdp_port and cdp_ws:
                    await close_cdp_browser(cdp_port, cdp_ws)
                if proc.returncode is None:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                            await proc.wait()
                        except Exception:
                            pass
                try:
                    temp_profile.cleanup()
                except Exception:
                    pass

        attempt.task = asyncio.create_task(_monitor())
        return self.public_status(attempt.attempt_id)

    async def close_all(self):
        for attempt in list(self._attempts.values()):
            attempt.cancel_requested = True
            if attempt.task and not attempt.task.done():
                attempt.task.cancel()
        tasks = [a.task for a in self._attempts.values() if a.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
