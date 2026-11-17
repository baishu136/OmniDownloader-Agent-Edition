"""Android opt-in request pacing. State/record locks match PixivRequestGuard.kt.

No credentials are stored. All local Pixiv accounts share a conservative budget.
Only SharedPython sets OMNI_PIXIV_GUARD_DIR; Windows behavior is unchanged.
"""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from requests.adapters import HTTPAdapter
from urllib.parse import urlsplit

_thread_lock = threading.RLock()
BASE_COOLDOWN = 900_000


def kind(url):
    host = (urlsplit(url).hostname or '').lower()
    for domain, name in (('pixiv.net', 'api'), ('pximg.net', 'media')):
        if host == domain or host.endswith('.' + domain): return name
    return None


def retry_after(value, at):
    try:
        text = (value or '').strip()
        if text.isascii() and text.isdecimal(): return int(text) * 1000
        return max(0, int(parsedate_to_datetime(text).timestamp() * 1000) - at)
    except (ValueError, TypeError, OverflowError): return 0


class GuardError(IOError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'[{code}] {message}')


class PixivGuard:
    state_name = 'pixiv'
    label = 'Pixiv'
    gaps = {'api': 2000, 'media': 1000}
    global_gap = 500
    def __init__(self, directory, now=None, sleep=time.sleep):
        self.directory = Path(directory)
        self.now = now or (lambda: int(time.time() * 1000))
        self.sleep = sleep

    @contextmanager
    def locked(self):
        # lockf uses fcntl record locks, compatible with Java FileChannel.lock.
        # flock is deliberately not used (a different lock namespace on Linux).
        if os.name == 'nt': import msvcrt
        else: import fcntl
        with _thread_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / (self.state_name + '.lock')).open('a+b') as lock:
                if os.name == 'nt':
                    if lock.seek(0, 2) == 0: lock.write(b'0'); lock.flush()
                    lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                else: fcntl.lockf(lock, fcntl.LOCK_EX)
                try:
                    path = self.directory / (self.state_name + '.json')
                    valid = True
                    try:
                        if not path.exists(): state = {}
                        else:
                            if path.stat().st_size > 4096: raise ValueError('state too large')
                            state = json.loads(path.read_text('utf-8'))
                            if not isinstance(state, dict): raise ValueError('state type')
                    except (ValueError, OSError):
                        valid = False
                        state = {'policyVersion': 2, 'until': 0, 'reason': 'RATE_LIMITED',
                                 'warningId': str(uuid.uuid4()), 'warningPending': True}
                    before = dict(state)
                    if state.get('policyVersion', 0) < 2:
                        old_until = state.get('until', 0)
                        last = state.get('lastFailure', 0)
                        default = BASE_COOLDOWN * 2 ** (min(3, max(1, state.get('strikes', 1))) - 1)
                        state.update(policyVersion=2, until=old_until if last > 0 and old_until - last > default else 0)
                        if old_until > self.now(): state.update(warningId=str(uuid.uuid4()), warningPending=True)
                    try: yield state
                    finally:
                        if state != before or not path.exists() or not valid:
                            temporary = self.directory / (self.state_name + '.json.tmp')
                            temporary.write_text(json.dumps(state, separators=(',', ':')), encoding='utf-8')
                            temporary.replace(path)
                finally:
                    if os.name == 'nt': lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else: fcntl.lockf(lock, fcntl.LOCK_UN)

    def acquire(self, lane):
        while True:
            with self.locked() as state:
                at = self.now()
                remaining = state.get('until', 0) - at
                if state.get('warningPending'):
                    raise GuardError(state.get('reason', 'RATE_LIMITED'),
                                     f'{self.label} 返回请求限制或身份验证要求，请查看应用内警告后手动重试；已解析结果保留')
                if remaining > 0:
                    raise GuardError(state.get('reason', 'RATE_LIMITED'),
                                     f'{self.label} 平台要求等待 {(remaining + 999) // 1000} 秒；已解析结果保留')
                due = max(state.get('global', 0), state.get(lane, 0))
                if due <= at:
                    state['global'] = at + self.global_gap
                    state[lane] = at + self.gaps.get(lane, 1000)
                    return
                wait = due - at
            self.sleep(min(wait / 1000, .1))

    def block(self, status, retry=None, challenge=False):
        with self.locked() as state:
            at = self.now()
            strikes = 1 if at - state.get('lastFailure', 0) > 86_400_000 else min(3, state.get('strikes', 0) + 1)
            duration = retry_after(retry, at)
            state.update(until=max(state.get('until', 0), min(2**63 - 1, at + duration)), lastFailure=at,
                         strikes=strikes, reason='CHALLENGE_REQUIRED' if challenge else
                         'AUTH_REQUIRED' if status in (401, 403) else 'RATE_LIMITED',
                         warningId=str(uuid.uuid4()), warningPending=True)

    def acknowledge(self, identifier):
        with self.locked() as state:
            if state.get('warningId') == identifier: state['warningPending'] = False


class GuardedAdapter(HTTPAdapter):
    def __init__(self, guard, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard = guard
        self.direct = HTTPAdapter(max_retries=0)

    def send(self, request, **kwargs):
        lane = kind(request.url)
        if lane is None: return super().send(request, **kwargs)
        self.guard.acquire(lane)
        response = self.direct.send(request, **kwargs)
        if 300 <= response.status_code < 400 and any(word in response.headers.get('Location', '').lower() for word in ('/login', '/captcha', '/challenge')):
            self.guard.block(401)
            response.close()
            self.guard.acquire(lane)
        if response.status_code in (401, 403, 429) or (response.status_code == 503 and response.headers.get('Retry-After')):
            self.guard.block(response.status_code, response.headers.get('Retry-After'))
            response.close()
            self.guard.acquire(lane)
        if 'text/html' in response.headers.get('Content-Type', '').lower():
            # Pixiv API/image endpoints should not return HTML. Treat unexpected
            # HTML as a verification stop rather than consuming/retrying it.
            self.guard.block(response.status_code, challenge=True)
            response.close()
            self.guard.acquire(lane)
        return response

    def close(self):
        self.direct.close()
        super().close()


def attach(client, retry):
    directory = os.environ.get('OMNI_PIXIV_GUARD_DIR')
    if directory:
        for scheme in ('https://', 'http://'):
            client.mount(scheme, GuardedAdapter(PixivGuard(directory), max_retries=retry))


def cache_path(client, url):
    directory = os.environ.get('OMNI_PIXIV_GUARD_DIR')
    if not directory or kind(url) != 'api' or not urlsplit(url).path.startswith('/ajax/'): return None
    from requests import Request
    request = client.prepare_request(Request('GET', url))
    scope = json.dumps([request.url, request.headers.get('Cookie'), client.proxies], sort_keys=True)
    return Path(directory) / 'metadata' / (hashlib.sha256(scope.encode()).hexdigest() + '.json')


def cached_page(client, url):
    path = cache_path(client, url)
    if path is None: return None
    try:
        if path.stat().st_size > 600_000: return None
        entry = json.loads(path.read_text('utf-8'))
        if not 0 <= int(time.time() * 1000) - entry['at'] < 180_000: return None
        from requests import Response
        result = Response(); result.status_code = 200; result.url = url
        result._content = entry['body'].encode('utf-8'); result._content_consumed = True
        result.headers['Content-Type'] = 'application/json; charset=utf-8'; result.encoding = 'utf-8'
        return result
    except (OSError, ValueError, KeyError, TypeError): return None


def cache_page(client, url, response):
    path = cache_path(client, url)
    if path is None or response.status_code != 200 or len(response.content) > 512 * 1024: return
    if 'json' not in response.headers.get('Content-Type', '').lower() or 'no-store' in response.headers.get('Cache-Control', '').lower(): return
    try:
        root = response.json()
        if not isinstance(root, dict) or root.get('error', True): return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Cache content is account-scoped; no Cookie/header/URL is saved in it.
        temporary = path.with_suffix(f'.{os.getpid()}.tmp')
        temporary.write_text(json.dumps({'at': int(time.time() * 1000), 'body': response.content.decode('utf-8')}), encoding='utf-8')
        temporary.replace(path)
        entries = sorted(path.parent.glob('*'), key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for index, item in enumerate(entries):
            total += item.stat().st_size
            if index >= 64 or total > 8 * 1024 * 1024: item.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError): pass  # Cache failure never repeats a request.
