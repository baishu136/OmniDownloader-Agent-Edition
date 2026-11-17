"""Android-only X pacing across requests, gallery-dl and yt-dlp's URL opener.

The installed yt-dlp plugin also covers CLI extraction and native HLS fragments.
No account/IP rotation; a denial stops all local X routes until cooldown expires.
"""
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

if __package__:
    from .pixiv_guard import PixivGuard, GuardError, retry_after
else:
    from omni_pixiv_guard import PixivGuard, GuardError, retry_after

_local = threading.local()


def kind(url):
    host = (urlsplit(url).hostname or '').lower()
    if any(host == domain or host.endswith('.' + domain) for domain in ('x.com', 'twitter.com', 'syndication.twimg.com')) or host == 't.co': return 'api'
    if host == 'pbs.twimg.com': return 'image'
    if host == 'twimg.com' or host.endswith('.twimg.com'): return 'media'
    return None


class XGuard(PixivGuard):
    state_name = 'x'
    label = 'X'
    gaps = {'api': 2000, 'image': 1000, 'media': 250}
    global_gap = 250

    def defer(self, status, headers, challenge=False):
        headers = {k.lower(): v for k, v in headers.items()}
        wait = retry_after(headers.get('retry-after'), self.now())
        try: wait = max(wait, int(headers.get('x-rate-limit-reset', 0)) * 1000 - self.now())
        except (ValueError, TypeError): pass
        self.block(status, str(max(0, (wait + 999) // 1000)), challenge)

    def headers(self, lane, status, headers):
        values = {k.lower(): v for k, v in headers.items()}
        location = values.get('location', '')
        login = 300 <= status < 400 and any(p in location for p in ('/i/flow/login', '/account/access'))
        if status == 429 or lane == 'api' and status in (401, 403) or login or status == 503 and 'retry-after' in values:
            self.defer(401 if login else status, values)
            self.acquire(lane)
        # Let the last successful response finish; stop subsequent requests.
        if lane == 'api' and 200 <= status < 300 and values.get('x-rate-limit-remaining') == '0':
            self.defer(429, values)

    def body(self, lane, data, content_type, headers):
        if lane != 'api': return
        text = data.decode('utf-8', 'replace') if isinstance(data, bytes) else data
        codes = set()
        if 'json' in content_type:
            try:
                root = json.loads(text)
                if isinstance(root, dict):
                    codes = {int(e.get('code')) for e in root.get('errors', [])
                             if isinstance(e, dict) and str(e.get('code', '')).isdigit()}
            except (ValueError, TypeError): pass
        challenge = 'html' in content_type and any(marker in text.lower() for marker in ('cf-chl-', 'challenge-platform', 'verify you are human', 'complete a security check'))
        status = 429 if 88 in codes else 401 if codes.intersection((32, 64, 89, 326)) else 0
        if status or challenge:
            self.defer(status, headers, challenge)
            self.acquire(lane)

    def redirected(self, lane, url, headers):
        parts = urlsplit(url or '')
        if kind(url) == 'api' and parts.path.startswith(('/i/flow/login', '/account/access')):
            self.defer(401, headers)
            self.acquire(lane)


def directory():
    explicit = os.environ.get('OMNI_X_GUARD_DIR')
    if explicit: return explicit
    config = Path(__file__).with_name('.omni-x-guard.json')
    if not config.exists(): return None
    # The file is written by Android installation code in private app storage.
    return json.loads(config.read_text('utf-8'))['directory']


def install_requests(guard):
    import requests
    original = requests.Session.send
    if getattr(original, '_omni_x_guard', False): return

    def send(self, request, **kwargs):
        lane = kind(request.url)
        if lane is None: return original(self, request, **kwargs)
        ticket = getattr(_local, 'ticket', None)
        inherited = ticket is not None and ticket['url'] == request.url and not ticket['used']
        if inherited: ticket['used'] = True
        else: guard.acquire(lane)
        # Every Requests redirect re-enters send. No adapter-internal wire retry.
        parts = urlsplit(request.url)
        prefix = f'{parts.scheme}://{parts.netloc}/'
        adapter = self.get_adapter(request.url)
        if getattr(adapter, '_omni_x_no_retry', False) is False:
            adapter = requests.adapters.HTTPAdapter(max_retries=0)
            adapter._omni_x_no_retry = True
            self.mount(prefix, adapter)
        response = original(self, request, **kwargs)
        try:
            guard.headers(lane, response.status_code, response.headers)
            guard.redirected(lane, response.url, response.headers)
            if response._content is not False and len(response.content) <= 65536:
                guard.body(lane, response.content, response.headers.get('Content-Type', '').lower(), response.headers)
            if ticket is not None: ticket['observed'] = True
            return response
        except BaseException:
            response.close()
            raise

    send._omni_x_guard = True
    requests.Session.send = send


def install_ytdlp(guard):
    from yt_dlp import YoutubeDL
    from yt_dlp.downloader.external import ExternalFD
    from yt_dlp.networking.common import Response
    from yt_dlp.networking.exceptions import HTTPError
    original = YoutubeDL.urlopen
    if getattr(original, '_omni_x_guard', False): return
    external = ExternalFD.real_download

    def external_download(self, filename, info_dict):
        # HlsFD can delegate unsupported streams to FFmpeg even with 'native'.
        # External HTTP stacks cannot share the Python/native admission gate.
        if kind(info_dict.get('url', '')):
            raise GuardError('ENGINE_UNAVAILABLE', '此 X 媒体需要外部网络下载器，已停止以保留请求保护')
        return external(self, filename, info_dict)

    external_download._omni_x_guard = True
    ExternalFD.real_download = external_download

    class CheckedResponse(Response):
        def __init__(self, response, lane):
            super().__init__(response, response.url, response.headers, response.status, response.reason, response.extensions)
            self.lane = lane
            self.buffer = bytearray()

        def read(self, amt=None):
            value = self.fp.read(amt)
            if len(self.buffer) <= 65536:
                self.buffer.extend(value[:65537-len(self.buffer)])
                if len(self.buffer) <= 65536 and (amt is None or amt < 0 or not value):
                    guard.body(self.lane, bytes(self.buffer), self.headers.get('Content-Type', '').lower(), self.headers)
            return value

    def urlopen(self, request):
        url = request if isinstance(request, str) else getattr(request, 'url', None) or request.get_full_url()
        lane = kind(url)
        if lane is None: return original(self, request)
        guard.acquire(lane)
        previous = getattr(_local, 'ticket', None)
        ticket = {'url': url, 'used': False, 'observed': False}
        _local.ticket = ticket
        try:
            try: response = original(self, request)
            except HTTPError as error:
                if not ticket['observed']: guard.headers(lane, error.status, error.response.headers)
                raise
            try:
                if not ticket['observed']: guard.headers(lane, response.status, response.headers)
                guard.redirected(lane, response.url, response.headers)
                content_type = response.headers.get('Content-Type', '').lower()
                return CheckedResponse(response, lane) if lane == 'api' and any(t in content_type for t in ('json', 'html')) else response
            except BaseException:
                response.close(); raise
        finally: _local.ticket = previous

    urlopen._omni_x_guard = True
    YoutubeDL.urlopen = urlopen


def install():
    root = directory()
    if not root: return False
    active = XGuard(root)
    install_requests(active)
    install_ytdlp(active)
    return True
