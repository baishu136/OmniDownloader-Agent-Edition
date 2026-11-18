"""完善平台 Cookie 导入格式兼容、B站扫码登录、以及桌面 pywebview 内置浏览器登录。"""
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx
import qrcode
import requests

from .errors import AgentError, safe_text

DESKTOP_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0'
)
MOBILE_UA = (
    'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36'
)

# 参照 Android PlatformLoginPolicy.kt 统一维护各平台登录入口与关键会话字段
PLATFORM_LOGIN_POLICIES = {
    'YouTube': {
        'start_url': 'https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/',
        'cookie_urls': ('https://www.youtube.com/', 'https://youtube.com/', 'https://accounts.google.com/'),
        'markers': ('SID', 'SAPISID', '__Secure-3PAPISID', 'LOGIN_INFO'),
        'auto_capture': True,
        'desktop': True,
        'hint': 'Google 可能限制部分嵌入式环境，若被拒绝登录可点击“关闭窗口”后使用浏览器导出的 cookies.txt / JSON 导入。',
    },
    'X (Twitter)': {
        'start_url': 'https://x.com/i/flow/login',
        'cookie_urls': ('https://x.com/', 'https://twitter.com/'),
        'markers': ('auth_token',),
        'auto_capture': True,
        'desktop': True,
        'hint': '在内置窗口完成 X 登录后将自动捕获 auth_token 等完整会话 Cookie。',
    },
    '小红书': {
        'start_url': 'https://www.xiaohongshu.com/explore',
        'cookie_urls': ('https://www.xiaohongshu.com/',),
        'markers': ('web_session',),
        'auto_capture': False,
        'desktop': True,
        'hint': '小红书访客态也可能生成 web_session，请在内置窗口确认登录账号后点击“我已登录，保存 Cookie”。',
    },
    '小黑盒': {
        'start_url': 'https://www.xiaoheihe.cn/',
        'cookie_urls': ('https://www.xiaoheihe.cn/', 'https://www.heybox.cn/'),
        'markers': ('heybox_id', 'user_id', 'sessionid'),
        'auto_capture': True,
        'desktop': True,
        'hint': '在内置窗口完成小黑盒登录后将自动捕获并加密保存会话。',
    },
    'Pixiv': {
        'start_url': 'https://accounts.pixiv.net/login',
        'cookie_urls': ('https://www.pixiv.net/', 'https://accounts.pixiv.net/'),
        'markers': ('PHPSESSID',),
        'auto_capture': False,
        'desktop': True,
        'hint': 'Pixiv 匿名访问也存在 PHPSESSID；请在内置窗口完成账号登录后点击“我已登录，保存 Cookie”，随后可点“检测”。',
    },
    '抖音': {
        'start_url': 'https://www.douyin.com/',
        'cookie_urls': ('https://www.douyin.com/',),
        'markers': ('sessionid', 'sessionid_ss'),
        'auto_capture': False,
        'desktop': True,
        'hint': '请在内置窗口扫码或登录后点击“我已登录，保存 Cookie”。',
    },
    '哔哩哔哩': {
        'start_url': 'https://passport.bilibili.com/login',
        'cookie_urls': ('https://www.bilibili.com/', 'https://passport.bilibili.com/', 'https://api.bilibili.com/'),
        'markers': ('SESSDATA',),
        'auto_capture': True,
        'desktop': True,
        'hint': '支持“官方扫码登录”与“内置浏览器登录”；取得 SESSDATA 后会自动加密保存并验证登录态。',
    },
    'Pinterest': {
        'start_url': 'https://www.pinterest.com/login/',
        'cookie_urls': ('https://www.pinterest.com/',),
        'markers': ('_pinterest_sess',),
        'auto_capture': True,
        'desktop': True,
        'hint': '在内置窗口登录 Pinterest 后将自动保存会话。',
    },
    'Instagram': {
        'start_url': 'https://www.instagram.com/accounts/login/',
        'cookie_urls': ('https://www.instagram.com/',),
        'markers': ('sessionid',),
        'auto_capture': True,
        'desktop': True,
        'hint': '在内置窗口登录 Instagram 后将自动捕获 sessionid 并保存。',
    },
    'Facebook': {
        'start_url': 'https://www.facebook.com/login/',
        'cookie_urls': ('https://www.facebook.com/',),
        'markers': ('c_user',),
        'auto_capture': True,
        'desktop': True,
        'hint': '在内置窗口登录 Facebook 后将自动捕获 c_user 等凭据并保存。',
    },
    'TikTok': {
        'start_url': 'https://www.tiktok.com/login',
        'cookie_urls': ('https://www.tiktok.com/',),
        'markers': ('sessionid', 'sessionid_ss'),
        'auto_capture': True,
        'desktop': True,
        'hint': '在内置窗口登录 TikTok 后将自动捕获会话并保存。',
    },
    '快手': {
        'start_url': 'https://www.kuaishou.com/new-reco',
        'cookie_urls': ('https://www.kuaishou.com/',),
        'markers': ('userId',),
        'auto_capture': False,
        'desktop': True,
        'hint': '请在内置窗口完成快手登录后点击“我已登录，保存 Cookie”。',
    },
}


def webview_available() -> bool:
    """检查当前环境是否支持启动 pywebview 桌面内置登录窗口。"""
    if importlib.util.find_spec('webview') is None:
        return False
    if os.name == 'nt' or sys.platform == 'darwin':
        return True
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def _domain_matches(domain: str, allowed_domains: tuple[str, ...]) -> bool:
    clean = str(domain or '').lstrip('.').lower()
    if not clean:
        return False
    return any(clean == d or clean.endswith('.' + d) for d in allowed_domains)


def _expiry_to_epoch(raw_expiry) -> int:
    if raw_expiry in (None, '', 0, '0'):
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
        # WebView2 会将退出清理的会话 Cookie 设为 1970/0001 等过去默认值，这里按 0 处理
        return ts if ts > 1_000_000 else 0
    except (TypeError, ValueError, OSError, OverflowError):
        return 0


def format_netscape_row(domain: str, path: str, secure: bool, expires: int, name: str, value: str, *, httponly: bool = False) -> str:
    """格式化单条标准 Netscape Cookie 行（含 #HttpOnly_ 前缀以保留 HttpOnly 属性）。"""
    clean_domain = str(domain or '').strip()
    if not clean_domain.startswith('.'):
        include_subdomains = 'FALSE'
    else:
        include_subdomains = 'TRUE'
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
    # 1. 标准 Netscape 头部
    if text.startswith(('# Netscape HTTP Cookie File', '# HTTP Cookie File')):
        return text

    # 2. JSON 数组格式（Cookie-Editor / EditThisCookie / CDP）
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

    # 3. 缺省文件头的 7 列 Netscape 行
    lines = [ln for ln in text.splitlines() if ln.strip() and not (ln.startswith('#') and not ln.startswith('#HttpOnly_'))]
    if lines and all(len(ln.split('\t')) == 7 for ln in lines):
        return '# Netscape HTTP Cookie File\n' + '\n'.join(lines) + '\n'

    # 4. HTTP Cookie 请求头字符串（允许带 "Cookie:" 前缀）
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

        # 登录确认成功：收集 Set-Cookie 与回跳 URL 中的会话字段并补足首页 Cookie
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

        # 回跳 URL 有时额外携带 SESSDATA / bili_jct / DedeUserID
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
class WebViewAttempt:
    attempt_id: str
    platform: str
    nonce: str
    proxy: str
    state: str = 'OPENING'
    message: str = '正在打开内置登录浏览器...'
    error: str = ''
    manual_capture_requested: bool = False
    cancel_requested: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    process: object = None


class WebViewLoginManager:
    """协调主服务与独立 --webview-login 子进程间的隔离认证通信。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._attempts: dict[str, WebViewAttempt] = {}

    def create(self, platform: str, proxy: str = '') -> WebViewAttempt:
        if platform not in PLATFORM_LOGIN_POLICIES:
            raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        if not webview_available():
            raise AgentError('CAPABILITY_UNAVAILABLE', '当前环境不支持桌面内置浏览器（请使用扫码登录或粘贴/导入 Cookie）')
        now = time.time()
        with self._lock:
            # 清理同一平台或过期的旧尝试标记
            for aid, item in list(self._attempts.items()):
                if now - item.created_at > 900 or item.state in ('COMPLETED', 'CANCELLED', 'FAILED'):
                    if now - item.updated_at > 60:
                        self._attempts.pop(aid, None)
            attempt = WebViewAttempt(
                attempt_id=uuid.uuid4().hex,
                platform=platform,
                nonce=uuid.uuid4().hex,
                proxy=proxy,
                created_at=now,
                updated_at=now,
            )
            self._attempts[attempt.attempt_id] = attempt
            return attempt

    def get(self, attempt_id: str) -> WebViewAttempt:
        attempt = self._attempts.get(attempt_id)
        if not attempt:
            raise AgentError('NOT_FOUND', '登录会话已结束或不存在')
        return attempt

    def verify_internal(self, attempt_id: str, nonce: str) -> WebViewAttempt:
        attempt = self.get(attempt_id)
        if not nonce or nonce != attempt.nonce:
            raise AgentError('PERMISSION_DENIED', '内部登录校验令牌无效')
        return attempt

    def public_status(self, attempt_id: str) -> dict:
        a = self.get(attempt_id)
        return {
            'attempt_id': a.attempt_id,
            'platform': a.platform,
            'state': a.state,
            'message': a.message,
            'error': a.error,
        }

    def request_manual_capture(self, platform: str) -> dict:
        with self._lock:
            active = [a for a in self._attempts.values() if a.platform == platform and a.state in ('OPENING', 'WAITING')]
            if not active:
                raise AgentError('NOT_FOUND', '当前没有正在进行的内置浏览器登录窗口')
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
            target.message = '已取消内置浏览器登录'
            target.updated_at = time.time()
            return {'attempt_id': target.attempt_id, 'cancelled': True}


def _collect_cookies_from_window(window, policy: dict, allowed_domains: tuple[str, ...]) -> tuple[list[tuple[str, str, str]], bool]:
    """从 WebView2 CookieManager 或 pywebview API 提取完整 Cookie（含 HttpOnly）并转为 Netscape 行。"""
    raw_items = []
    # 优先直接访问 Windows Edge WebView2 CoreWebView2CookieManager 以跨越跳转子域名获取 Cookie
    native = getattr(window, 'native', None)
    browser = getattr(native, 'browser', None) if native is not None else None
    core_wv2 = getattr(getattr(browser, 'webview', None), 'CoreWebView2', None) if browser is not None else None

    if native is not None and core_wv2 is not None:
        try:
            from System import Action, Func, Object
            from System.Collections.Generic import List
            from System.Globalization import CultureInfo
            from System.Threading.Tasks import Task
            from Microsoft.Web.WebView2.Core import CoreWebView2Cookie

            done = threading.Event()
            extracted = []
            scheduler = browser.syncContextTaskScheduler

            def _fetch():
                def _on_done(task):
                    try:
                        for c in task.Result:
                            extracted.append({
                                'name': c.Name,
                                'value': c.Value or '',
                                'domain': c.Domain or '',
                                'path': c.Path or '/',
                                'secure': bool(c.IsSecure),
                                'httponly': bool(c.IsHttpOnly),
                                'expires_raw': c.Expires.ToString('r', CultureInfo.GetCultureInfo('en-US')),
                            })
                    finally:
                        done.set()

                core_wv2.CookieManager.GetCookiesAsync('').ContinueWith(
                    Action[Task[List[CoreWebView2Cookie]]](_on_done),
                    scheduler,
                )

            native.Invoke(Func[Object](_fetch))
            done.wait(timeout=4.0)
            raw_items = extracted
        except Exception:
            raw_items = []

    if not raw_items:
        # 通用回退：使用 pywebview 跨平台 get_cookies()
        try:
            simple_cookies = window.get_cookies() or []
        except Exception:
            simple_cookies = []
        for cookie_obj in simple_cookies:
            for name, morsel in cookie_obj.items():
                raw_items.append({
                    'name': name,
                    'value': morsel.value or '',
                    'domain': morsel.get('domain') or ('.' + allowed_domains[0]),
                    'path': morsel.get('path') or '/',
                    'secure': bool(morsel.get('secure')),
                    'httponly': bool(morsel.get('httponly')),
                    'expires_raw': morsel.get('expires') or '',
                })

    dedup = {}
    marker_set = set(policy.get('markers') or ())
    marker_hit = False
    now = time.time()
    for item in raw_items:
        name = str(item.get('name') or '').strip()
        value = str(item.get('value') or '')
        domain = str(item.get('domain') or '').strip()
        if not name or not _domain_matches(domain, allowed_domains):
            continue
        exp = _expiry_to_epoch(item.get('expires_raw'))
        if exp and exp <= now:
            continue
        if name in marker_set and value:
            marker_hit = True
        norm_domain = domain if domain.startswith('.') else '.' + domain
        try:
            row = format_netscape_row(
                norm_domain,
                item.get('path') or '/',
                bool(item.get('secure', True)),
                exp,
                name,
                value,
                httponly=bool(item.get('httponly', False)),
            )
            dedup[(norm_domain, item.get('path') or '/', name)] = row
        except ValueError:
            continue
    return list(dedup.items()), marker_hit


def run_webview_login_cli(server_url: str, attempt_id: str, nonce: str) -> None:
    """运行隔离桌面 pywebview 登录窗口并在登录后经由本机回环回传 Cookie。"""
    headers = {'X-Omni-Webview-Nonce': nonce}
    base = server_url.rstrip('/')
    with httpx.Client(trust_env=False, timeout=10) as client:
        resp = client.get(f'{base}/api/internal/webview-login/{attempt_id}', headers=headers)
        resp.raise_for_status()
        cfg = resp.json()

    platform = cfg['platform']
    policy = PLATFORM_LOGIN_POLICIES[platform]
    allowed_domains = tuple(cfg['domains'])
    proxy = str(cfg.get('proxy') or '').strip()
    if proxy:
        existing_args = os.environ.get('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '')
        os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = f'{existing_args} --proxy-server={proxy}'.strip()

    import webview
    from webview.menu import Menu, MenuAction

    state = {'done': False, 'desktop': bool(policy.get('desktop', True))}

    def _post(action: str, payload: dict | None = None) -> dict:
        try:
            with httpx.Client(trust_env=False, timeout=15) as client:
                r = client.post(f'{base}/api/internal/webview-login/{attempt_id}/{action}', headers=headers, json=payload or {})
                return r.json() if r.content else {}
        except Exception:
            return {}

    def _save_now(force: bool) -> bool:
        if state['done']:
            return True
        entries, marker_hit = _collect_cookies_from_window(window, policy, allowed_domains)
        if not entries:
            return False
        if not force and not (policy.get('auto_capture') and marker_hit):
            return False
        netscape = '# Netscape HTTP Cookie File\n' + '\n'.join(row for _, row in entries) + '\n'
        result = _post('capture', {'netscape': netscape})
        if result.get('ok'):
            state['done'] = True
            try:
                window.destroy()
            except Exception:
                pass
            return True
        return False

    def _manual_save():
        if not _save_now(force=True):
            window.set_title(f'登录 {platform} · 尚未检测到可用 Cookie，请先完成登录')

    def _reload_start():
        window.load_url(policy['start_url'])

    def _close_window():
        state['done'] = True
        _post('cancel')
        window.destroy()

    def _on_closed():
        if not state['done']:
            _post('cancel')

    def _watcher():
        _post('heartbeat', {'state': 'WAITING', 'message': f'请在打开的内置浏览器中完成 {platform} 登录...'})
        time.sleep(1.5)
        while not state['done']:
            # 1. 自动识别关键登录标志 Cookie
            if policy.get('auto_capture') and _save_now(force=False):
                return
            # 2. 响应管理页发起的远程“立即抓取”或“关闭窗口”指令
            try:
                with httpx.Client(trust_env=False, timeout=5) as client:
                    cmd_resp = client.get(f'{base}/api/internal/webview-login/{attempt_id}/commands', headers=headers)
                    if cmd_resp.status_code == 200:
                        cmd = cmd_resp.json()
                        if cmd.get('cancel'):
                            state['done'] = True
                            window.destroy()
                            return
                        if cmd.get('capture') and _save_now(force=True):
                            return
            except Exception:
                pass
            time.sleep(1.0)

    menu_items = [
        Menu('Cookie 登录操作', [
            MenuAction('我已登录，保存当前 Cookie', _manual_save),
            MenuAction('重新加载登录首页', _reload_start),
            MenuAction('关闭登录窗口', _close_window),
        ])
    ]

    with tempfile.TemporaryDirectory(prefix='omni-wv-') as profile_dir:
        icon_candidate = Path(__file__).resolve().parents[2] / 'windows' / 'app.ico'
        if getattr(sys, 'frozen', False):
            icon_candidate = Path(sys._MEIPASS) / 'app.ico'
        icon_arg = str(icon_candidate) if icon_candidate.is_file() else None
        window = webview.create_window(
            title=f'登录 {platform} · OmniDownloader Cookie 登录',
            url=policy['start_url'],
            width=1020,
            height=760,
            min_size=(760, 560),
            text_select=True,
        )
        window.events.closed += _on_closed
        webview.start(
            func=_watcher,
            gui='edgechromium' if os.name == 'nt' else None,
            user_agent=DESKTOP_UA if state['desktop'] else MOBILE_UA,
            private_mode=True,
            storage_path=profile_dir,
            menu=menu_items,
            icon=icon_arg,
        )
