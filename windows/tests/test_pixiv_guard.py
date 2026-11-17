import json
import pytest
import requests
from engine import pixiv_guard as guard


def clock_guard(path):
    clock = [10000]
    return guard.PixivGuard(path, lambda: clock[0], lambda seconds: clock.__setitem__(0, clock[0] + round(seconds * 1000))), clock


def test_shared_pacing_and_restart(tmp_path):
    first, clock = clock_guard(tmp_path)
    first.acquire('api'); first.acquire('api'); first.acquire('media'); first.acquire('media'); first.acquire('api')
    assert clock[0] == 14000
    restarted = guard.PixivGuard(tmp_path, lambda: clock[0], lambda seconds: clock.__setitem__(0, clock[0] + round(seconds * 1000)))
    restarted.acquire('api'); assert clock[0] == 16000


def test_cooldown_respects_retry_after_and_never_sleeps_or_resets_on_restart(tmp_path):
    first, clock = clock_guard(tmp_path)
    first.block(429, '3600')
    assert json.loads((tmp_path / 'pixiv.json').read_text())['until'] == clock[0] + 3_600_000
    restarted = guard.PixivGuard(tmp_path, lambda: clock[0], lambda _: pytest.fail('must not wait'))
    for lane in ('api', 'media'):
        with pytest.raises(guard.GuardError, match='RATE_LIMITED'): restarted.acquire(lane)
    restarted.acknowledge(json.loads((tmp_path / 'pixiv.json').read_text())['warningId'])
    with pytest.raises(guard.GuardError): restarted.acquire('api')
    clock[0] += 3_600_001; restarted.acquire('api'); restarted.block(429)
    assert json.loads((tmp_path / 'pixiv.json').read_text())['until'] == clock[0]


def test_warning_can_be_acknowledged_without_fixed_wait_but_not_for_newer_denial(tmp_path):
    active, clock = clock_guard(tmp_path)
    active.block(401)
    old = json.loads((tmp_path / 'pixiv.json').read_text())['warningId']
    with pytest.raises(guard.GuardError): active.acquire('api')
    active.block(429)
    active.acknowledge(old)
    with pytest.raises(guard.GuardError): active.acquire('api')
    active.acknowledge(json.loads((tmp_path / 'pixiv.json').read_text())['warningId'])
    active.acquire('api'); assert clock[0] == 10000


@pytest.mark.parametrize('duration', [900000, 3600000])
def test_legacy_local_wait_migrates_to_warning_while_longer_deadline_is_preserved(tmp_path, duration):
    (tmp_path / 'pixiv.json').write_text(json.dumps({'until': 10000 + duration, 'lastFailure': 10000, 'strikes': 1, 'reason': 'RATE_LIMITED'}))
    active, _ = clock_guard(tmp_path)
    with pytest.raises(guard.GuardError): active.acquire('api')
    state = json.loads((tmp_path / 'pixiv.json').read_text())
    assert state['until'] == (0 if duration == 900000 else 10000 + duration)
    active.acknowledge(state['warningId'])
    if duration == 900000: active.acquire('api')
    else:
        with pytest.raises(guard.GuardError): active.acquire('api')


def test_corrupt_state_stops_instead_of_removing_previous_protection(tmp_path):
    (tmp_path / 'pixiv.json').write_text('broken')
    first, clock = clock_guard(tmp_path)
    with pytest.raises(guard.GuardError): first.acquire('api')
    until = json.loads((tmp_path / 'pixiv.json').read_text())['until']
    clock[0] += 1000
    with pytest.raises(guard.GuardError): first.acquire('api')
    assert json.loads((tmp_path / 'pixiv.json').read_text())['until'] == until


@pytest.mark.parametrize('status,content_type', [(429, 'application/json'), (401, 'application/json'), (403, 'application/json'), (200, 'text/html')])
def test_adapter_stops_without_anonymous_or_http_status_retries(tmp_path, status, content_type):
    active, _ = clock_guard(tmp_path)
    adapter = guard.GuardedAdapter(active)
    calls = []
    def send(request, **kwargs):
        calls.append(request)
        response = requests.Response(); response.status_code = status
        response.headers['Content-Type'] = content_type; response._content = b'{}'; response._content_consumed = True
        return response
    adapter.direct.send = send
    request = requests.Request('GET', 'https://www.pixiv.net/ajax/test').prepare()
    with pytest.raises(guard.GuardError): adapter.send(request)
    with pytest.raises(guard.GuardError): adapter.send(request)
    assert len(calls) == 1 and adapter.direct.max_retries.total == 0


def test_retry_after_and_host_boundaries():
    assert guard.retry_after('120', 0) == guard.retry_after('Thu, 01 Jan 1970 00:02:00 GMT', 0) == 120000
    assert guard.retry_after('-2', 0) == guard.retry_after('invalid', 0) == 0
    assert guard.kind('https://i.pximg.net/a') == 'media'
    assert guard.kind('https://pixiv.net.evil.test/a') is None


def test_cache_is_opt_in_and_cookie_scoped_and_skips_errors(tmp_path, monkeypatch):
    client = requests.Session(); url = 'https://www.pixiv.net/ajax/illust/42'
    monkeypatch.delenv('OMNI_PIXIV_GUARD_DIR', raising=False)
    assert guard.cache_path(client, url) is None
    monkeypatch.setenv('OMNI_PIXIV_GUARD_DIR', str(tmp_path))
    response = requests.Response(); response.status_code = 200
    response.headers['Content-Type'] = 'application/json'; response._content = b'{"error":false,"body":{"id":42}}'
    guard.cache_page(client, url, response)
    assert guard.cached_page(client, url).json()['body']['id'] == 42
    client.cookies.set('PHPSESSID', 'fixture-only')
    assert guard.cached_page(client, url) is None
    response._content = b'{"error":true}'
    guard.cache_page(client, url, response); assert guard.cached_page(client, url) is None
