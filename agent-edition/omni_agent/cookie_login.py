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
    auto_capture: bool = False
    multi_platform: bool = False
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
    """使用持久化专属托管浏览器档案完成单平台/多平台登录与免密续签。"""

    POLL_INTERVAL_SEC = 0.6
    CONFIRM_WINDOW_SEC = 0.7
    PORT_WAIT_STEP_SEC = 0.08
    PORT_WAIT_TICKS = 180

    def __init__(self, profile_dir: Path | None = None):
        self._lock = threading.RLock()
        self._attempts: dict[str, BrowserLoginAttempt] = {}
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self._shared_proc = None
        self._shared_port: int | None = None
        self._shared_ws: str = ''
        self._launch_lock: asyncio.Lock | None = None

    def _get_launch_lock(self) -> asyncio.Lock:
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()
        return self._launch_lock

    def has_profile_data(self) -> bool:
        """检查持久化专用浏览器目录中是否已保存账号档案。"""
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
            'multi_platform': a.multi_platform,
            'profile_persistent': self.profile_dir is not None,
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

    def request_manual_capture(self, platform: str) -> dict:
        with self._lock:
            active = [a for a in self._attempts.values() if a.platform == platform and a.state in ('OPENING', 'WAITING')]
            if not active:
                raise AgentError('NOT_FOUND', '当前没有正在进行的本地浏览器登录窗口')
            target = max(active, key=lambda a: a.created_at)
            target.manual_capture_requested = True
            self.set_stage(target, 'CAPTURING', '收到抓取指令，正在立即读取浏览器 Cookie...')
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

    def _build_browser_cmd(self, browser_path: str, data_dir: Path, proxy: str, start_url: str, *, headless: bool = False) -> list[str]:
        cmd = [
            browser_path,
            f'--user-data-dir={data_dir}',
            '--remote-debugging-port=0',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-default-apps',
            '--disable-background-networking',
            '--disable-sync',
            '--disable-features=msEdgeFirstRunExperiences',
        ]
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

    async def _try_reuse_shared_browser(self, target_url: str) -> tuple[object, int, str] | None:
        if self._shared_proc is None or self._shared_proc.returncode is not None or not self._shared_port:
            return None
        alive, ws_url = await probe_cdp_alive(self._shared_port, self._shared_ws)
        if not alive:
            self._shared_proc = None
            self._shared_port = None
            self._shared_ws = ''
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
    ) -> dict:
        is_multi = platform == MULTI_PLATFORM_KEY
        if not is_multi and platform not in PLATFORM_LOGIN_POLICIES:
            raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        browser = find_local_browser()
        if not browser:
            raise AgentError('CAPABILITY_UNAVAILABLE', '未检测到本机 Chrome 或 Edge 浏览器，可点击“默认浏览器打开”完成登录后导入或粘贴 Cookie')

        policy = PLATFORM_LOGIN_POLICIES.get(platform, {})
        all_domains = domains or {}
        now = time.time()
        loop = asyncio.get_running_loop()
        with self._lock:
            # 同一目标如已有未关闭监听尝试，平滑替换旧尝试（但不强制杀死复用中的共享浏览器）
            for old in list(self._attempts.values()):
                if old.platform == platform and old.state in ('OPENING', 'WAITING'):
                    old.cancel_requested = True
                    old.state = 'CANCELLED'
                    self.set_stage(old, 'CANCELLED', '已切换至新打开的登录任务')
                    self._wake(old)
                elif now - old.created_at > 900 or (old.state in ('COMPLETED', 'CANCELLED', 'FAILED') and now - old.updated_at > 180):
                    self._attempts.pop(old.attempt_id, None)

            attempt = BrowserLoginAttempt(
                attempt_id=uuid.uuid4().hex,
                platform=platform,
                browser_name=browser['name'],
                proxy=proxy,
                auto_capture=False if is_multi else bool(policy.get('auto_capture')),
                multi_platform=is_multi,
                created_at=now,
                updated_at=now,
                wake_event=asyncio.Event(),
                loop=loop,
            )
            init_msg = (
                f'正在启动本地 {browser["name"]} 多平台托管工作窗（自动保存历史登录档案）...'
                if is_multi
                else f'正在启动本地 {browser["name"]} 独立登录窗口...'
            )
            self.set_stage(attempt, 'LAUNCHING', init_msg)
            self._attempts[attempt.attempt_id] = attempt

        start_url = 'https://www.bilibili.com/' if is_multi else policy['start_url']
        using_persistent = self.profile_dir is not None
        temp_profile = None
        if using_persistent:
            data_dir = self.profile_dir
            data_dir.mkdir(parents=True, exist_ok=True)
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
            try:
                async with self._get_launch_lock():
                    if using_persistent:
                        reused_info = await self._try_reuse_shared_browser(start_url)
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
                    else:
                        attempt.process = proc
                        self.set_stage(attempt, 'CONNECTING', f'已复用打开中的 {browser["name"]} 独立窗口，正在建立读取通道...')

                if attempt.cancel_requested or (proc and proc.returncode is not None):
                    attempt.state = 'CANCELLED'
                    self.set_stage(attempt, 'CANCELLED', '本地浏览器窗口已关闭（尚未完成通道初始化）')
                    return
                if not cdp_port:
                    raise RuntimeError('未能与本地浏览器建立 Cookie 读取通道，请关闭已存在的同内核隔离实例或改用默认浏览器打开后导入')

                attempt.state = 'WAITING'
                attempt.cdp_connected = True
                if is_multi:
                    wait_msg = f'已在持久托管 {browser["name"]} 中监听全平台；在窗口内登录任意平台后点击“我已登录，抓取保存”或直接关窗自动保存'
                elif policy.get('auto_capture'):
                    wait_msg = f'已连接本地 {browser["name"]} 读取通道；请完成账号登录，系统将自动捕获并保存完整 Cookie'
                else:
                    wait_msg = f'已连接本地 {browser["name"]} 读取通道；请完成账号登录后点击“我已登录，抓取保存”'
                self.set_stage(attempt, 'WAITING_LOGIN', wait_msg)

                cookie_urls = tuple(
                    url
                    for p_pol in PLATFORM_LOGIN_POLICIES.values()
                    for url in (p_pol.get('cookie_urls') or ())
                ) if is_multi else (policy.get('cookie_urls') or ())

                first_tick = True
                while not attempt.cancel_requested and proc.returncode is None:
                    if not first_tick:
                        await _sleep_or_wake(proc, self.POLL_INTERVAL_SEC)
                    first_tick = False
                    if attempt.cancel_requested or proc.returncode is not None:
                        break
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
                            attempt.message = f'正在监听多平台窗口 · 已识别 {len(ready_names)} 个平台登录态（{"、".join(ready_names[:5])}{" 等" if len(ready_names) > 5 else ""}），点“我已登录，抓取保存”即可批量入库'
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
                        self.set_stage(attempt, 'WAITING_LOGIN', f'尚未扫描到属于 {platform} 的有效 Cookie{host_tip}，请先登录网页后再点击抓取')
                        continue

                    if netscape and policy.get('auto_capture') and marker_hit:
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
                        if policy.get('auto_capture'):
                            attempt.message = f'正在监听登录态{host_part} · 已发现 {cookie_count} 项基础 Cookie，等待登录完成...'
                        else:
                            attempt.message = f'正在监听{host_part} · 已捕获 {cookie_count} 项 Cookie，请在确认登录后点击“我已登录，抓取保存”'
                        attempt.updated_at = time.time()

                # 浏览器窗口关闭前自动兜底挽救
                if attempt.state not in ('COMPLETED', 'FAILED'):
                    if not attempt.cancel_requested:
                        if is_multi and await _complete_multi(attempt.last_scanned, 'salvaged'):
                            return
                        if not is_multi and attempt.last_netscape and attempt.last_marker_hit:
                            await _complete_single(attempt.last_netscape, attempt.last_cookie_count, 'salvaged')
                            return
                    attempt.state = 'CANCELLED'
                    if not is_multi and attempt.last_cookie_count > 0 and not attempt.cancel_requested:
                        cancel_msg = (
                            f'浏览器窗口已关闭（已捕捉到 {attempt.last_cookie_count} 项访客/前置 Cookie，'
                            '但未识别到核心登录标记，未覆盖现有凭据）'
                        )
                    else:
                        cancel_msg = '已关闭本地浏览器登录窗口' if attempt.cancel_requested else '本地浏览器窗口已关闭，未检测到可保存登录凭据'
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
        on_capture_many,
    ) -> dict:
        """从持久化托管浏览器直接或后台静默无头同步全部已登录平台的 Cookie。"""
        used_headless = False
        cookies: list[dict] = []

        if self.is_shared_browser_active() and self._shared_port:
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

        if not cookies and not self.is_shared_browser_active():
            if not self.has_profile_data():
                raise AgentError('NOT_FOUND', '本地托管浏览器尚未建立持久化登录档案，请先通过“本地浏览器登录”或“多平台托管登录”完成一次登录')
            browser = find_local_browser()
            if not browser:
                raise AgentError('CAPABILITY_UNAVAILABLE', '未检测到本机 Chrome 或 Edge 浏览器')
            used_headless = True
            async with self._get_launch_lock():
                cleanup_stale_profile_locks(self.profile_dir)
                cmd = self._build_browser_cmd(browser['path'], self.profile_dir, proxy, 'about:blank', headless=True)
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    stdin=asyncio.subprocess.DEVNULL,
                )
                port, ws_path = None, ''
                try:
                    port, ws_path = await self._wait_cdp_port_file(self.profile_dir, proc)
                    if not port:
                        raise AgentError('UPSTREAM_ERROR', '后台读取托管浏览器档案超时，请点击“本地浏览器登录”唤醒同步')
                    cookie_urls = tuple(
                        url for pol in PLATFORM_LOGIN_POLICIES.values() for url in (pol.get('cookie_urls') or ())
                    )
                    snap = await self._collect_cdp_state(port, ws_path, cookie_urls)
                    cookies = snap.get('cookies') or []
                finally:
                    await self._shutdown_browser_process(proc, port, ws_path)

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

    async def reset_profile(self) -> dict:
        """优雅关闭正在运行的托管浏览器实例并彻底清空持久化浏览器数据目录。"""
        await self.close_all()
        if self._shared_proc is not None:
            await self._shutdown_browser_process(self._shared_proc, self._shared_port, self._shared_ws)
        if not self.profile_dir:
            return {'cleared': False}
        if self.profile_dir.exists():
            for attempt_idx in range(5):
                try:
                    shutil.rmtree(self.profile_dir)
                    break
                except OSError:
                    if attempt_idx == 4:
                        raise AgentError('INTERNAL_ERROR', '浏览器进程文件尚未完全释放，请稍后再点清理')
                    await asyncio.sleep(0.25)
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
