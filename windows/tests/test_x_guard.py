import io
import json
import requests
import pytest
from engine import x_guard as x
from engine.pixiv_guard import PixivGuard


def active(tmp_path):
    clock = [10000]
    return x.XGuard(tmp_path, lambda: clock[0], lambda delay: clock.__setitem__(0, clock[0] + round(delay * 1000))), clock


def test_x_lanes_share_budget_and_do_not_reset_pixiv(tmp_path):
    guard, clock = active(tmp_path)
    for lane in ('api', 'media', 'image', 'image', 'api'): guard.acquire(lane)
    assert clock[0] == 12000
    guard.defer(429, {'x-rate-limit-reset': '5000'})
    assert json.loads((tmp_path / 'x.json').read_text())['until'] == 5_000_000
    with pytest.raises(x.GuardError): x.XGuard(tmp_path, lambda: clock[0]).acquire('image')
    PixivGuard(tmp_path, lambda: clock[0]).acquire('api')


def test_host_matching_and_expired_media_are_not_account_bans(tmp_path):
    guard, _ = active(tmp_path)
    assert x.kind('https://api.twitter.com/1.1/') == 'api'
    assert x.kind('https://cdn.syndication.twimg.com/tweet-result') == 'api'
    assert x.kind('https://video.twimg.com/a') == 'media'
    assert x.kind('https://x.com.evil.test/a') is None
    guard.headers('media', 403, {})
    guard.acquire('api')


@pytest.mark.parametrize('code,expected', [(88, 'RATE_LIMITED'), ('88', 'RATE_LIMITED'), (32, 'AUTH_REQUIRED'), (89, 'AUTH_REQUIRED'), (64, 'AUTH_REQUIRED'), (326, 'AUTH_REQUIRED')])
def test_json_errors_stop_even_when_http_status_is_200(tmp_path, code, expected):
    guard, _ = active(tmp_path)
    with pytest.raises(x.GuardError, match=expected):
        guard.body('api', json.dumps({'errors': [{'code': code}]}), 'application/json', {})
    with pytest.raises(x.GuardError): guard.acquire('media')


def test_remaining_zero_returns_last_response_but_stops_future_requests(tmp_path):
    guard, _ = active(tmp_path)
    guard.headers('api', 200, {'X-Rate-Limit-Remaining': '0', 'X-Rate-Limit-Reset': '9000'})
    with pytest.raises(x.GuardError): guard.acquire('api')
    assert json.loads((tmp_path / 'x.json').read_text())['until'] == 9_000_000


def test_automatic_login_redirect_stops_subsequent_media(tmp_path):
    guard, _ = active(tmp_path)
    with pytest.raises(x.GuardError, match='AUTH_REQUIRED'):
        guard.redirected('api', 'https://x.com/i/flow/login?redirect_after_login=a', {})
    with pytest.raises(x.GuardError): guard.acquire('media')


def test_requests_paces_before_sending_and_never_retries_429(tmp_path, monkeypatch):
    guard, _ = active(tmp_path);calls = []
    def send(self, request, **kwargs):
        calls.append(request)
        assert self.get_adapter(request.url).max_retries.total == 0
        response = requests.Response();response.status_code = 429;response._content = b'{}';response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    x.install_requests(guard)
    client = requests.Session()
    for url in ('https://x.com/i/api/a', 'https://pbs.twimg.com/media/a'):
        with pytest.raises(x.GuardError): client.get(url)
    assert len(calls) == 1


def test_ytdlp_http_and_json_denials_share_the_same_gate(tmp_path, monkeypatch):
    from yt_dlp import YoutubeDL
    from yt_dlp.downloader.external import ExternalFD
    from yt_dlp.networking.common import Response
    guard, _ = active(tmp_path);calls = []
    def urlopen(self, request):
        calls.append(request)
        return Response(io.BytesIO(b'{"errors":[{"code":88}]}'), request, {'Content-Type': 'application/json'})
    monkeypatch.setattr(YoutubeDL, 'urlopen', urlopen)
    monkeypatch.setattr(ExternalFD, 'real_download', ExternalFD.real_download)
    x.install_ytdlp(guard)
    engine = YoutubeDL({'quiet': True}, auto_init=False)
    with pytest.raises(x.GuardError, match='RATE_LIMITED'): engine.urlopen('https://api.x.com/graphql').read()
    with pytest.raises(x.GuardError): engine.urlopen('https://video.twimg.com/a.mp4')
    assert len(calls) == 1


def test_x_unsupported_hls_cannot_fall_back_to_external_network(tmp_path, monkeypatch):
    from yt_dlp import YoutubeDL
    from yt_dlp.downloader.external import ExternalFD
    guard, _ = active(tmp_path);calls = []
    monkeypatch.setattr(YoutubeDL, 'urlopen', lambda *_: None)
    monkeypatch.setattr(ExternalFD, 'real_download', lambda *args: calls.append(args[-1]['url']))
    x.install_ytdlp(guard)
    for url in ('https://video.twimg.com/a.m3u8', 'https://pbs.twimg.com/media/a'):
        with pytest.raises(x.GuardError, match='ENGINE_UNAVAILABLE'):
            ExternalFD.real_download(None, 'fixture', {'url': url})
    ExternalFD.real_download(None, 'fixture', {'url': 'https://other.example/a'})
    assert calls == ['https://other.example/a']
