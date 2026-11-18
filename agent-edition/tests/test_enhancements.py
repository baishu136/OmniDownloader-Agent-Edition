import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import types

from fastapi.testclient import TestClient
import pytest

from omni_agent.api import create_app
from omni_agent.cookie_login import (
    PLATFORM_LOGIN_POLICIES,
    analyze_cdp_cookies,
    cdp_cookies_to_netscape,
    fetch_cdp_snapshot,
    merge_cdp_cookies,
    normalize_cookie_text,
)
from omni_agent.errors import AgentError
from omni_agent.visualization import build_visualization


def test_directory_browser_create_and_pick_fallback(service, tmp_path, monkeypatch):
    root = tmp_path / 'storage'
    root.mkdir()
    (root / 'existing_dir').mkdir()

    with TestClient(create_app(service)) as client:
        listed = client.post('/api/admin/fs/list', json={'path': str(root)})
        assert listed.status_code == 200
        body = listed.json()
        assert body['current'] == str(root.resolve())
        assert [e['name'] for e in body['entries']] == ['existing_dir']

        created = client.post('/api/admin/fs/mkdir', json={'parent': str(root), 'name': 'new_dl'})
        assert created.status_code == 200
        assert (root / 'new_dl').is_dir()

        bad = client.post('/api/admin/fs/mkdir', json={'parent': str(root), 'name': '../escape'})
        assert bad.status_code == 400

        async def fake_pick(initial_dir='', title=''):
            return {'supported': False, 'reason': 'HEADLESS', 'path': ''}

        monkeypatch.setattr('omni_agent.api.pick_directory', fake_pick)
        picked = client.post('/api/admin/pick-directory', json={'initial_dir': str(root), 'title': '选择'})
        assert picked.status_code == 200 and picked.json()['supported'] is False


def test_proxy_connectivity_check_and_validation(service, monkeypatch):
    class OkHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with TestClient(create_app(service)) as client:
            bad = client.post('/api/admin/proxy/test', json={'proxy': 'http://user:pass@127.0.0.1:7890'})
            assert bad.status_code == 400 and bad.json()['error']['code'] == 'INVALID_PARAMETER'

            # 本地未监听端口快速报错
            dead = client.post('/api/admin/proxy/test', json={'proxy': 'http://127.0.0.1:1'})
            assert dead.status_code == 200
            assert dead.json()['ok'] is False and dead.json()['tcp_ok'] is False

            # 允许直连本测试模拟目标验证 RTT 与状态码汇总（绕过公网 SSRF 以测 HTTP 状态收集）
            monkeypatch.setattr('omni_agent.desktop_tools.validate_url', lambda *a, **k: None)
            ok = client.post('/api/admin/proxy/test', json={'proxy': '', 'target': f'http://127.0.0.1:{server.server_port}/generate_204'})
            assert ok.status_code == 200
            data = ok.json()
            assert data['ok'] is True and data['targets'][0]['status_code'] == 204
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cookie_multi_format_local_browser_cdp_and_qr_login(service, monkeypatch):
    # JSON Cookie 数组格式导入并正确加密，不泄漏给 Agent
    json_cookies = json.dumps([
        {'name': 'SESSDATA', 'value': 'json-secret-sess', 'domain': '.bilibili.com', 'path': '/', 'httpOnly': True, 'secure': True},
        {'name': 'bili_jct', 'value': 'csrf-secret', 'domain': '.bilibili.com', 'path': '/', 'secure': True},
        {'name': 'other_site', 'value': 'ignored', 'domain': '.evil.example', 'path': '/'},
    ])
    netscape = normalize_cookie_text('哔哩哔哩', json_cookies, service.domains)
    assert '#HttpOnly_.bilibili.com' in netscape and 'json-secret-sess' in netscape and 'evil.example' not in netscape

    # 多路 CDP Cookie 合并应去重并保留 HttpOnly、非空值和更大过期时间
    future_ts = int(time.time()) + 3600
    merged = merge_cdp_cookies(
        [{'name': 'auth_token', 'value': 'x-cdp-secret', 'domain': '.x.com', 'path': '/', 'httpOnly': False, 'expires': 0}],
        [{'name': 'auth_token', 'value': '', 'domain': '.x.com', 'path': '/', 'httpOnly': True, 'secure': True, 'expires': future_ts}],
        [{'name': 'guest', 'value': '1', 'domain': '.other.com', 'path': '/'}],
    )
    assert len(merged) == 2
    x_token = next(c for c in merged if c['name'] == 'auth_token')
    assert x_token['value'] == 'x-cdp-secret' and x_token['httpOnly'] is True and x_token['expires'] == future_ts

    # CDP Cookie 过滤与转换应保留 HttpOnly、检测关键登录标记并返回精确条目数
    cdp_netscape, marker_hit = cdp_cookies_to_netscape(merged, PLATFORM_LOGIN_POLICIES['X (Twitter)'], service.domains['X (Twitter)'])
    analyzed_text, analyzed_marker, count = analyze_cdp_cookies(merged, PLATFORM_LOGIN_POLICIES['X (Twitter)'], service.domains['X (Twitter)'])
    assert marker_hit is True and analyzed_marker is True and count == 1 and analyzed_text == cdp_netscape
    assert '#HttpOnly_.x.com' in cdp_netscape and 'x-cdp-secret' in cdp_netscape and 'other.com' not in cdp_netscape

    # 本地未检测到浏览器时应优雅降级为 CAPABILITY_UNAVAILABLE (400)，并保留默认浏览器打开能力
    monkeypatch.setattr('omni_agent.cookie_login.find_local_browser', lambda: None)
    monkeypatch.setattr('omni_agent.service.find_local_browser', lambda: None)
    opened_urls = []
    monkeypatch.setattr('omni_agent.cookie_login.webbrowser.open', lambda url, new=2: opened_urls.append(url) or True)

    with TestClient(create_app(service)) as client:
        res = client.post('/api/admin/sessions', json={'platform': '哔哩哔哩', 'text': json_cookies})
        assert res.status_code == 200
        ui_info = next(s for s in client.get('/api/admin/sessions').json() if s['platform'] == '哔哩哔哩')
        assert ui_info['ui']['cookie_count'] == 2
        assert ui_info['ui']['marker_present'] is True
        assert ui_info['ui']['source'] == 'import'
        assert ui_info['local_browser_available'] is False
        assert ui_info['login_url'].startswith('https://')

        # Agent 视角绝不能包含任何 Cookie 键名、明文、来源或 UI 元字段
        agent_resp = client.post('/api/tools/list_cookie_statuses', json={}).json()
        assert agent_resp['ok']
        dumped_agent = json.dumps(agent_resp)
        assert 'SESSDATA' not in dumped_agent and 'json-secret-sess' not in dumped_agent
        assert 'source' not in dumped_agent and 'ui' not in agent_resp['result']['platforms'][0]

        # 默认浏览器打开接口与无本地浏览器错误提示
        opened = client.post('/api/admin/sessions/X (Twitter)/open-login')
        assert opened.status_code == 200 and opened.json()['opened'] is True and opened_urls == ['https://x.com/i/flow/login']
        no_browser = client.post('/api/admin/sessions/X (Twitter)/browser-login')
        assert no_browser.status_code == 400 and no_browser.json()['error']['code'] == 'CAPABILITY_UNAVAILABLE'

        # 模拟本地 Chrome：验证三阶段状态展示、防抖二次补采、手动即时抓取与关窗自动兜底
        fake_browser = {'name': 'Google Chrome', 'path': 'mock-chrome'}
        monkeypatch.setattr('omni_agent.cookie_login.find_local_browser', lambda: fake_browser)
        monkeypatch.setattr('omni_agent.service.find_local_browser', lambda: fake_browser)
        monkeypatch.setattr(service.browser_logins, 'POLL_INTERVAL_SEC', 0.01)
        monkeypatch.setattr(service.browser_logins, 'CONFIRM_WINDOW_SEC', 0.01)
        monkeypatch.setattr(service.browser_logins, 'PORT_WAIT_STEP_SEC', 0.005)

        real_exec = asyncio.create_subprocess_exec
        procs = []

        class FakeProc:
            def __init__(self):
                self.returncode = None

            def terminate(self):
                self.returncode = 0

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        async def fake_exec(*cmd, **kwargs):
            if cmd and str(cmd[0]) == 'mock-chrome':
                for arg in cmd:
                    if str(arg).startswith('--user-data-dir='):
                        profile_dir = Path(str(arg).split('=', 1)[1])
                        (profile_dir / 'DevToolsActivePort').write_text('9229\n/devtools/browser/mock\n', encoding='utf-8')
                proc = FakeProc()
                procs.append(proc)
                return proc
            return await real_exec(*cmd, **kwargs)

        async def fake_close(port, ws_path):
            return None

        poll_counter = {'n': 0}

        async def fake_snapshot(port, ws_path, cookie_urls=()):
            assert port == 9229
            poll_counter['n'] += 1
            if poll_counter['n'] == 1:
                return {'cdp_connected': True, 'current_host': 'x.com', 'cookies': [{'name': 'guest_id', 'value': 'guest-1', 'domain': '.x.com', 'path': '/'}]}
            if poll_counter['n'] == 2:
                return {'cdp_connected': True, 'current_host': 'x.com', 'cookies': [{'name': 'auth_token', 'value': 'x-cdp-secret', 'domain': '.x.com', 'path': '/', 'httpOnly': True, 'secure': True}]}
            return {
                'cdp_connected': True,
                'current_host': 'x.com',
                'cookies': [
                    {'name': 'auth_token', 'value': 'x-cdp-secret', 'domain': '.x.com', 'path': '/', 'httpOnly': True, 'secure': True},
                    {'name': 'ct0', 'value': 'x-ct0-secret', 'domain': '.x.com', 'path': '/'},
                ],
            }

        monkeypatch.setattr('omni_agent.cookie_login.asyncio.create_subprocess_exec', fake_exec)
        monkeypatch.setattr('omni_agent.cookie_login.fetch_cdp_snapshot', fake_snapshot)
        monkeypatch.setattr('omni_agent.cookie_login.close_cdp_browser', fake_close)

        started = client.post('/api/admin/sessions/X (Twitter)/browser-login')
        assert started.status_code == 200
        start_body = started.json()
        attempt_id = start_body['attempt_id']
        assert start_body['stage'] == 'LAUNCHING' and start_body['progress'] == 15

        final_status = {}
        seen_stages = []
        for _ in range(50):
            final_status = client.get(f'/api/admin/sessions/browser-status/{attempt_id}').json()
            seen_stages.append(final_status['stage'])
            if final_status['state'] == 'COMPLETED':
                break
            time.sleep(0.01)

        assert final_status['state'] == 'COMPLETED'
        assert final_status['progress'] == 100
        assert final_status['capture_mode'] == 'auto'
        assert final_status['cookie_count'] == 2
        assert final_status['marker_detected'] is True
        assert final_status['current_host'] == 'x.com'
        assert 'x-cdp-secret' not in json.dumps(final_status)
        assert service.sessions.status('X (Twitter)')['state'] == 'IMPORTED'
        x_ui = next(s for s in client.get('/api/admin/sessions').json() if s['platform'] == 'X (Twitter)')
        assert x_ui['ui']['cookie_count'] == 2 and x_ui['ui']['source'] == 'browser_auto'

        # 验证手动点击抓取即时响应（Pixiv）
        async def pixiv_snapshot(port, ws_path, cookie_urls=()):
            return {'cdp_connected': True, 'current_host': 'www.pixiv.net', 'cookies': [{'name': 'PHPSESSID', 'value': 'px-manual-secret', 'domain': '.pixiv.net', 'path': '/'}]}

        monkeypatch.setattr('omni_agent.cookie_login.fetch_cdp_snapshot', pixiv_snapshot)
        monkeypatch.setattr(service, 'verify_cookie', lambda p: asyncio.sleep(0, result={'state': 'VALID'}))
        px = client.post('/api/admin/sessions/Pixiv/browser-login')
        assert px.status_code == 200
        px_id = px.json()['attempt_id']
        for _ in range(30):
            if client.get(f'/api/admin/sessions/browser-status/{px_id}').json()['cookie_count'] == 1:
                break
            time.sleep(0.01)
        cap_res = client.post('/api/admin/sessions/Pixiv/browser-capture')
        assert cap_res.status_code == 200 and cap_res.json()['requested'] is True
        px_done = {}
        for _ in range(40):
            px_done = client.get(f'/api/admin/sessions/browser-status/{px_id}').json()
            if px_done['state'] == 'COMPLETED':
                break
            time.sleep(0.01)
        assert px_done['state'] == 'COMPLETED' and px_done['capture_mode'] == 'manual'

        # 验证窗口被用户提前关闭时：若已出现核心登录标记则自动兜底挽救（salvaged）
        monkeypatch.setattr(service.browser_logins, 'CONFIRM_WINDOW_SEC', 2.0)

        async def ig_snapshot(port, ws_path, cookie_urls=()):
            if procs:
                procs[-1].returncode = 0
            return {'cdp_connected': True, 'current_host': 'www.instagram.com', 'cookies': [{'name': 'sessionid', 'value': 'ig-salvage-secret', 'domain': '.instagram.com', 'path': '/', 'httpOnly': True}]}

        monkeypatch.setattr('omni_agent.cookie_login.fetch_cdp_snapshot', ig_snapshot)
        ig = client.post('/api/admin/sessions/Instagram/browser-login')
        ig_id = ig.json()['attempt_id']
        ig_done = {}
        for _ in range(40):
            ig_done = client.get(f'/api/admin/sessions/browser-status/{ig_id}').json()
            if ig_done['state'] == 'COMPLETED':
                break
            time.sleep(0.01)
        assert ig_done['state'] == 'COMPLETED' and ig_done['capture_mode'] == 'salvaged'
        assert service.sessions.status('Instagram')['state'] == 'IMPORTED'

        # 验证仅有访客 Cookie 时手动关闭窗口不会误覆盖凭据，且给出清晰取消说明
        async def guest_only_snapshot(port, ws_path, cookie_urls=()):
            return {'cdp_connected': True, 'current_host': 'www.douyin.com', 'cookies': [{'name': 'ttwid', 'value': 'guest-ttwid', 'domain': '.douyin.com', 'path': '/'}]}

        monkeypatch.setattr('omni_agent.cookie_login.fetch_cdp_snapshot', guest_only_snapshot)
        dy = client.post('/api/admin/sessions/抖音/browser-login')
        dy_id = dy.json()['attempt_id']
        for _ in range(30):
            if client.get(f'/api/admin/sessions/browser-status/{dy_id}').json()['cookie_count'] == 1:
                procs[-1].returncode = 0
                break
            time.sleep(0.01)
        dy_done = {}
        for _ in range(40):
            dy_done = client.get(f'/api/admin/sessions/browser-status/{dy_id}').json()
            if dy_done['state'] == 'CANCELLED':
                break
            time.sleep(0.01)
        assert dy_done['state'] == 'CANCELLED' and '访客' in dy_done['message']
        assert service.sessions.status('抖音')['state'] == 'MISSING'

        # 模拟 B 站二维码生成、扫码中与扫码确认状态字段
        monkeypatch.setattr(service.qr_logins, 'generate', lambda proxy='': {'qr_id': 'mock-qr', 'matrix': [[1, 0], [0, 1]], 'expires_in': 180})
        monkeypatch.setattr(
            service.qr_logins,
            'poll',
            lambda qr_id: {'status': 'confirmed', 'stage': 'COMPLETED', 'stage_label': '扫码登录完成', 'progress': 100, 'netscape': 'SESSDATA=qr-secret-val'},
        )
        qr = client.post('/api/admin/sessions/bilibili/qr')
        assert qr.status_code == 200 and qr.json()['matrix'] == [[1, 0], [0, 1]]
        polled = client.post('/api/admin/sessions/bilibili/qr/mock-qr/poll')
        assert polled.status_code == 200 and polled.json()['status'] == 'confirmed' and polled.json()['stage'] == 'COMPLETED'
        assert 'qr-secret-val' not in polled.text


async def test_cdp_snapshot_loopback_multi_source_and_host_extraction():
    from http import HTTPStatus
    import websockets

    port_holder = {'port': 0}

    def handle_http(connection, request):
        path = request.path
        port = port_holder['port']
        if path == '/json/version':
            body = json.dumps({'webSocketDebuggerUrl': f'ws://127.0.0.1:{port}/devtools/browser/b1'}).encode()
            return connection.respond(HTTPStatus.OK, body.decode())
        if path == '/json/list':
            body = json.dumps([
                {
                    'type': 'page',
                    'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&token=secret',
                    'webSocketDebuggerUrl': f'ws://127.0.0.1:{port}/devtools/page/p1',
                }
            ]).encode()
            return connection.respond(HTTPStatus.OK, body.decode())
        return None

    async def handle_ws(ws):
        async for raw in ws:
            req = json.loads(raw)
            method = req.get('method')
            rid = req['id']
            if method == 'Storage.getCookies':
                cookies = [{'name': 'SID', 'value': 'sid-val', 'domain': '.youtube.com', 'path': '/', 'httpOnly': False}]
            elif method == 'Network.getAllCookies':
                cookies = [{'name': 'SID', 'value': 'sid-val', 'domain': '.youtube.com', 'path': '/', 'httpOnly': True}]
            elif method == 'Network.getCookies':
                cookies = [{'name': 'LOGIN_INFO', 'value': 'login-val', 'domain': '.youtube.com', 'path': '/', 'httpOnly': True}]
            else:
                cookies = []
            await ws.send(json.dumps({'id': rid, 'result': {'cookies': cookies}}))

    async with websockets.serve(handle_ws, '127.0.0.1', 0, process_request=handle_http) as server:
        port = server.sockets[0].getsockname()[1]
        port_holder['port'] = port
        snap = await fetch_cdp_snapshot(port, '/devtools/browser/fallback', ('https://www.youtube.com/',))
        assert snap['cdp_connected'] is True
        # 严格脱敏：仅保留宿主域名，绝不含 path/query 参数
        assert snap['current_host'] == 'www.youtube.com'
        names = {c['name']: c for c in snap['cookies']}
        assert set(names) == {'SID', 'LOGIN_INFO'}
        assert names['SID']['httpOnly'] is True


def test_persistent_managed_browser_workspace_sync_and_reset(service, monkeypatch):
    profile_dir = service.browser_logins.profile_dir
    assert profile_dir is not None and profile_dir.name == 'browser-profile'

    fake_browser = {'name': 'Microsoft Edge', 'path': 'mock-edge'}
    monkeypatch.setattr('omni_agent.cookie_login.find_local_browser', lambda: fake_browser)
    monkeypatch.setattr('omni_agent.service.find_local_browser', lambda: fake_browser)
    monkeypatch.setattr(service.browser_logins, 'POLL_INTERVAL_SEC', 0.01)
    monkeypatch.setattr(service.browser_logins, 'CONFIRM_WINDOW_SEC', 0.01)
    monkeypatch.setattr(service.browser_logins, 'PORT_WAIT_STEP_SEC', 0.005)
    monkeypatch.setattr(service, 'verify_cookie', lambda p: asyncio.sleep(0, result={'state': 'VALID'}))

    launched_cmds = []

    class FakeProc:
        def __init__(self):
            self.returncode = None

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def fake_exec(*cmd, **kwargs):
        if cmd and str(cmd[0]) == 'mock-edge':
            launched_cmds.append(list(cmd))
            # 在持久 profile 内写入会话标记文件，模拟真实浏览器保存登录态档案
            (profile_dir / 'Local State').write_text('{"os_crypt":{}}', encoding='utf-8')
            (profile_dir / 'DevToolsActivePort').write_text('9333\n/devtools/browser/persist\n', encoding='utf-8')
            return FakeProc()
        return await asyncio.create_subprocess_exec(*cmd, **kwargs)

    multi_cookies = [
        {'name': 'auth_token', 'value': 'x-persist-secret', 'domain': '.x.com', 'path': '/', 'httpOnly': True, 'secure': True},
        {'name': 'SESSDATA', 'value': 'bili-persist-secret', 'domain': '.bilibili.com', 'path': '/', 'httpOnly': True, 'secure': True},
        # 匿名访问小红书/抖音可能带的未登录 Cookie，不应被后台免密自动同步误收
        {'name': 'ttwid', 'value': 'dy-guest-cookie', 'domain': '.douyin.com', 'path': '/'},
        {'name': 'PHPSESSID', 'value': 'px-guest-or-user', 'domain': '.pixiv.net', 'path': '/'},
    ]

    async def fake_snapshot(port, ws_path, cookie_urls=()):
        assert port == 9333
        return {'cdp_connected': True, 'current_host': 'x.com', 'cookies': multi_cookies}

    monkeypatch.setattr('omni_agent.cookie_login.asyncio.create_subprocess_exec', fake_exec)
    monkeypatch.setattr('omni_agent.cookie_login.fetch_cdp_snapshot', fake_snapshot)
    monkeypatch.setattr('omni_agent.cookie_login.close_cdp_browser', lambda p, w: asyncio.sleep(0))

    with TestClient(create_app(service)) as client:
        # 1. 启动多平台托管工作窗，实时预览已抓到的平台矩阵并一键批量保存
        ws = client.post('/api/admin/sessions/browser-workspace/start')
        assert ws.status_code == 200 and ws.json()['multi_platform'] is True
        attempt_id = ws.json()['attempt_id']

        st = {}
        for _ in range(40):
            st = client.get(f'/api/admin/sessions/browser-status/{attempt_id}').json()
            if st['ready_platforms']:
                break
            time.sleep(0.01)
        ready_names = {r['platform'] for r in st['ready_platforms']}
        assert {'X (Twitter)', '哔哩哔哩', 'Pixiv'}.issubset(ready_names)

        # 验证打开后即使切走，持久化 profile 命令使用了 service.browser_logins.profile_dir
        assert any(f'--user-data-dir={profile_dir}' in arg for arg in launched_cmds[0])

        cap = client.post('/api/admin/sessions/browser-workspace/capture')
        assert cap.status_code == 200 and cap.json()['requested'] is True
        done = {}
        for _ in range(40):
            done = client.get(f'/api/admin/sessions/browser-status/{attempt_id}').json()
            if done['state'] == 'COMPLETED':
                break
            time.sleep(0.01)
        assert done['state'] == 'COMPLETED'
        assert set(done['synced_platforms']) == {'X (Twitter)', '哔哩哔哩', 'Pixiv'}
        assert profile_dir.exists() and (profile_dir / 'Local State').is_file()

        # 2. 关闭窗口后，直接用 --headless=new 一键免密同步持久档案中的已登录平台（且不误入未登录抖音）
        client.delete('/api/admin/sessions/X (Twitter)')
        assert service.sessions.status('X (Twitter)')['state'] == 'MISSING'

        sync_res = client.post('/api/admin/sessions/browser-profile/sync', json={'include_manual': False})
        assert sync_res.status_code == 200
        sync_body = sync_res.json()
        assert sync_body['used_headless'] is True
        assert '--headless=new' in launched_cmds[-1]
        # 之前已在库确认过的 Pixiv 会跟着免密续签，可靠标记 X 和 B 站同步入库，未登录抖音不会误存
        assert {'X (Twitter)', '哔哩哔哩', 'Pixiv'}.issubset(set(sync_body['synced_platforms']))
        assert service.sessions.status('X (Twitter)')['state'] == 'IMPORTED'
        assert service.sessions.status('抖音')['state'] == 'MISSING'

        # 3. 验证 UI 暴露持久档案标志而 Agent API 严格不暴露
        ui_item = next(s for s in client.get('/api/admin/sessions').json() if s['platform'] == 'X (Twitter)')
        assert ui_item['browser_profile_persistent'] is True and ui_item['browser_profile_exists'] is True
        assert ui_item['ui']['source'] in ('browser_sync', 'browser_manual')
        agent_resp = client.post('/api/tools/list_cookie_statuses', json={}).json()
        assert 'browser_profile' not in json.dumps(agent_resp) and 'x-persist-secret' not in json.dumps(agent_resp)

        # 4. 清理托管浏览器档案只清空档案，绝不误删已加密保存到会话库中的凭据
        reset_res = client.post('/api/admin/sessions/browser-profile/reset')
        assert reset_res.status_code == 200 and reset_res.json()['cleared'] is True
        assert not (profile_dir / 'Local State').exists()
        assert service.sessions.status('X (Twitter)')['state'] == 'IMPORTED'


def test_bridge_auto_discovery_and_scan(service):
    with TestClient(create_app(service)) as client:
        # 注册模拟 AstrBot 桥接心跳
        reg = client.post('/api/bridges/register', json={
            'bridge_id': 'astrbot-test-1',
            'kind': 'astrbot',
            'name': 'AstrBot 测试插件',
            'transport': 'http',
            'client_name': 'AstrBot',
            'client_version': '0.2.0',
            'routes': ['onebot:group:12345'],
        })
        assert reg.status_code == 200

        # HTTP /api/tools 调用应自动识别请求桥接身份与递增调用次数
        listed = client.post('/api/tools/list_bridges', json={}, headers={
            'X-Omni-Bridge-Id': 'astrbot-test-1',
            'X-Omni-Bridge-Type': 'astrbot',
            'X-Omni-Bridge-Name': 'AstrBot Plugin',
        }).json()
        assert listed['ok']
        bridges = listed['result']['bridges']
        astr = next(b for b in bridges if b['id'] == 'astrbot-test-1')
        assert astr['status'] == 'online' and astr['call_count'] == 1 and astr['routes'] == ['onebot:group:12345']

        scanned = client.post('/api/admin/bridges/scan')
        assert scanned.status_code == 200
        assert any(b['id'] == 'astrbot-test-1' for b in scanned.json()['bridges'])

        client.post('/api/bridges/unregister', json={'bridge_id': 'astrbot-test-1'})
        after = client.post('/api/tools/list_bridges', json={}).json()['result']['bridges']
        assert next(b for b in after if b['id'] == 'astrbot-test-1')['status'] == 'offline'


class StageRunner:
    """模拟真实多阶段媒体下载与报错行为以验证下载可视化与报错直出。"""

    def __init__(self, config, store, *, fail_error=None):
        self.config, self.store, self.fail_error = config, store, fail_error

    async def call(self, payload, identifier=None, progress=None):
        if self.fail_error:
            raise self.fail_error
        if progress:
            progress({'kind': 'metadata', 'metadata': {'platform': '哔哩哔哩', 'title': '可视化测试视频', 'author': 'Tester'}})
            progress({'kind': 'progress', 'state': 'DOWNLOADING', 'progress': 35, 'speed': '4.2 MiB/s'})
        await asyncio.sleep(.05)
        from PIL import Image
        path = self.config.cache / 'jobs' / identifier / 'output' / 'vis.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (4, 4)).save(path)
        return {'paths': [str(path)], 'metadata': {'platform': '哔哩哔哩', 'title': '可视化测试视频', 'author': 'Tester'}}

    async def close(self):
        pass


def test_download_visualization_custom_agent_silent_and_direct_error(service, tmp_path):
    # 1. 自定义模板渲染与各阶段开关测试
    service.update_settings({
        'confirmation_threshold': 50,
        'visual_enabled': True,
        'visual_mode': 'custom',
        'visual_on_receive': True,
        'visual_on_start': True,
        'visual_on_complete': True,
        'visual_receive_template': '收到！模式={command_summary}',
        'visual_start_template': '开下：{platform}-{title}',
        'visual_complete_template': '完成{file_count}个，正发往{target}',
        'visual_error_template': '出错了：{error}',
    })
    service.update_bot_config({'adapter': 'onebot', 'enabled': True, 'base_url': 'http://127.0.0.1:3000',
                              'token': 'ob-secret', 'allowed_targets': ['group:999'],
                              'shared_root': str(tmp_path), 'remote_root': '/app'})
    service.runner = StageRunner(service.config, service.store)

    with TestClient(create_app(service)) as client:
        job = client.post('/api/tools/download_media', json={'url': '/us https://www.bilibili.com/video/BV1xx411c7mD'}).json()['result']
        assert job['visualization']['current_stage'] == 'receive'
        assert job['visualization']['should_reply'] is True
        assert '合集全量 /us' in job['visualization']['message']

        import time
        for _ in range(100):
            st = client.post('/api/tools/get_task_status', json={'task_id': job['id']}).json()['result']
            if st['state'] == 'READY':
                break
            time.sleep(.02)
        assert st['state'] == 'READY'
        assert st['metadata']['title'] == '可视化测试视频'

        d = client.post('/api/tools/deliver_media', json={
            'task_id': job['id'], 'adapter': 'onebot', 'target': 'group:999', 'request_key': 'vis-deliv',
        }).json()['result']
        assert d['visualization']['current_stage'] == 'complete'
        assert d['visualization']['should_reply'] is True
        assert d['visualization']['message'] == '完成1个，正发往group:999'

        # 2. 关闭视觉通知后“不返回”过程状态，但报错时仍然必须直接输出完整报错内容
        service.update_settings({'visual_enabled': False})
        service.runner = StageRunner(service.config, service.store, fail_error=AgentError('ACCESS_DENIED', '该视频仅限大会员观看'))
        err_job = client.post('/api/tools/download_media', json={'url': 'https://www.bilibili.com/video/BV1fail'}).json()['result']
        assert err_job['visualization']['should_reply'] is False
        for _ in range(100):
            err_st = client.post('/api/tools/get_task_status', json={'task_id': err_job['id']}).json()['result']
            if err_st['state'] == 'FAILED':
                break
            time.sleep(.02)
        assert err_st['state'] == 'FAILED'
        assert err_st['error'] == 'ACCESS_DENIED'
        assert '该视频仅限大会员观看' in err_st['error_message']
        assert err_st['visualization']['should_reply'] is True
        assert '该视频仅限大会员观看' in err_st['visualization']['message']

    # 3. 测试 Agent 模式引导语及防止模板注入
    service.update_settings({'visual_enabled': True, 'visual_mode': 'agent', 'visual_start_template': '{0.__class__}'})
    vis = build_visualization(service.config.settings, 'start', context={'platform': 'Pixiv', 'title': '插画'})
    assert vis['mode'] == 'agent' and vis['should_reply'] is True
    assert 'agent_decides' in vis['agent_instruction']
    assert 'object' not in vis['message']


@pytest.mark.asyncio
async def test_astrbot_plugin_stage_visualization_and_direct_error(tmp_path, monkeypatch):
    # 动态注入最小 AstrBot stub 模块后验证 examples/astrbot_plugin/main.py 阶段状态与报错直出
    astr_pkg = types.ModuleType('astrbot')
    astr_api = types.ModuleType('astrbot.api')
    astr_event = types.ModuleType('astrbot.api.event')
    astr_star = types.ModuleType('astrbot.api.star')

    class DummyEvent:
        unified_msg_origin = 'napcat:GroupMessage:123'

        class MsgObj:
            message_id = 'm1'

        message_obj = MsgObj()

        def plain_result(self, text):
            return text

    astr_event.AstrMessageEvent = DummyEvent
    astr_event.filter = types.SimpleNamespace(
        command=lambda *a, **k: (lambda fn: fn),
        llm_tool=lambda *a, **k: (lambda fn: fn),
    )
    astr_star.Context = object
    astr_star.Star = type('Star', (), {'__init__': lambda self, ctx=None: None})
    astr_star.register = lambda *a, **k: (lambda cls: cls)
    for name, mod in (('astrbot', astr_pkg), ('astrbot.api', astr_api), ('astrbot.api.event', astr_event), ('astrbot.api.star', astr_star)):
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location(
        'test_astrbot_plugin_mod',
        Path(__file__).resolve().parents[1] / 'examples' / 'astrbot_plugin' / 'main.py',
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    routes = tmp_path / 'chats.json'
    routes.write_text(json.dumps({'napcat:GroupMessage:123': {'adapter': 'onebot', 'target': 'group:123'}}), 'utf-8')
    monkeypatch.setenv('OMNI_AGENT_CHAT_IDENTITIES', str(routes))

    plugin = module.OmniAgentPlugin(context=None)
    monkeypatch.setattr(plugin, '_ensure_heartbeat', lambda: None)
    seq = [
        {'id': 't1', 'state': 'QUEUED', 'visualization': {'stages': {'receive': True, 'start': True, 'complete': True}, 'message': '已收到链接'}},
        {'id': 't1', 'state': 'DOWNLOADING', 'metadata': {'title': '测试作品'}, 'visualization': {'stages': {'receive': True, 'start': True, 'complete': True}, 'message': '开始下载：测试作品'}},
        {'id': 't1', 'state': 'READY', 'file_count': 2, 'visualization': {'stages': {'receive': True, 'start': True, 'complete': True}, 'message': '下载完成'}},
        {'id': 'd1', 'state': 'QUEUED', 'visualization': {'stages': {'receive': True, 'start': True, 'complete': True}, 'message': '下载完成（共 2 个文件），正在发送至 group:123'}},
        {'id': 'd1', 'state': 'SENT'},
    ]
    idx = 0

    async def fake_call(name, **kwargs):
        nonlocal idx
        item = seq[idx]
        idx += 1
        return item

    monkeypatch.setattr(plugin, 'call', fake_call)
    emitted = []

    async def capture_emit(msg):
        emitted.append(msg)

    res = await plugin.process(DummyEvent(), 'https://www.bilibili.com/video/BV1test', 'default', emit=capture_emit)
    assert emitted == ['已收到链接', '开始下载：测试作品', '下载完成（共 2 个文件），正在发送至 group:123']
    assert '交付编号：d1' in res

    # 验证报错时直接输出报错内容
    err_seq = [
        {'id': 't2', 'state': 'QUEUED', 'visualization': {'stages': {'receive': False, 'start': False, 'complete': False}}},
        {'id': 't2', 'state': 'FAILED', 'error': 'COOKIE_EXPIRED', 'error_message': '登录凭证已过期，请扫码重新登录'},
    ]
    err_idx = 0

    async def fake_fail_call(name, **kwargs):
        nonlocal err_idx
        item = err_seq[err_idx]
        err_idx += 1
        return item

    monkeypatch.setattr(plugin, 'call', fake_fail_call)
    err_emitted = []
    err_res = await plugin.process(DummyEvent(), 'https://www.bilibili.com/video/BV1fail', 'default', emit=lambda m: asyncio.sleep(0, result=err_emitted.append(m)))
    assert '登录凭证已过期，请扫码重新登录' in err_emitted[-1]
    assert '登录凭证已过期，请扫码重新登录' in err_res


@pytest.mark.asyncio
async def test_android_style_explicit_reverify_manage_only_and_multi_account_profiles(service, monkeypatch):
    """验证对齐 Android 版的防自动覆盖重验证、Google manage_only、页内导航及多账号隔离退出闭环。"""
    from starlette.testclient import TestClient
    from omni_agent.api import create_app, invoke
    from omni_agent.cookie_login import LocalBrowserLoginManager

    fake_browser = {'name': 'Chrome', 'path': 'mock-chrome'}
    monkeypatch.setattr('omni_agent.cookie_login.find_local_browser', lambda: fake_browser)
    monkeypatch.setattr('omni_agent.service.find_local_browser', lambda: fake_browser)
    monkeypatch.setattr(service.browser_logins, 'POLL_INTERVAL_SEC', 0.01)
    monkeypatch.setattr(service.browser_logins, 'PORT_WAIT_STEP_SEC', 0.01)
    monkeypatch.setattr(service.browser_logins, 'CONFIRM_WINDOW_SEC', 0.02)

    cfg = service.config
    app = create_app(service)

    launch_records = []
    nav_records = []
    procs = []
    real_exec = asyncio.create_subprocess_exec

    class FakeProc:
        def __init__(self):
            self.returncode = None

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def fake_exec(*cmd, **kwargs):
        if cmd and str(cmd[0]) == 'mock-chrome':
            data_dir = None
            for arg in cmd:
                if str(arg).startswith('--user-data-dir='):
                    data_dir = Path(str(arg).split('=', 1)[1])
                    data_dir.mkdir(parents=True, exist_ok=True)
                    (data_dir / 'DevToolsActivePort').write_text('9333\n/devtools/browser/mock\n', encoding='utf-8')
                    (data_dir / 'Local State').write_text('{"os_crypt":{}}', encoding='utf-8')
            launch_records.append({'cmd': list(cmd), 'profile_dir': data_dir, 'url': str(cmd[-1])})
            proc = FakeProc()
            procs.append(proc)
            return proc
        return await real_exec(*cmd, **kwargs)

    async def fake_close(port, ws_path):
        return None

    async def fake_probe(port, ws_path):
        return (bool(procs and procs[-1].returncode is None), 'ws://127.0.0.1:9333/devtools/browser/mock')

    async def fake_open_tab(ws_url, target_url):
        launch_records.append({'reuse_tab': True, 'url': target_url})
        return True

    async def fake_navigate(port, ws_path, target_url):
        nav_records.append(target_url)
        return True

    async def fake_targets(port, ws_path):
        return {'current_host': 'accounts.google.com', 'page_ws': ''}

    snap_state = {
        'host': 'x.com',
        'cookies': [
            {'name': 'auth_token', 'value': 'new-x-auth', 'domain': '.x.com', 'path': '/', 'httpOnly': True, 'secure': True},
            {'name': 'ct0', 'value': 'new-x-ct0', 'domain': '.x.com', 'path': '/'},
        ],
    }

    async def fake_snapshot(port, ws_path, cookie_urls=()):
        return {'cdp_connected': True, 'current_host': snap_state['host'], 'cookies': list(snap_state['cookies'])}

    monkeypatch.setattr('omni_agent.cookie_login.asyncio.create_subprocess_exec', fake_exec)
    monkeypatch.setattr('omni_agent.cookie_login.close_cdp_browser', fake_close)
    monkeypatch.setattr('omni_agent.cookie_login.probe_cdp_alive', fake_probe)
    monkeypatch.setattr('omni_agent.cookie_login.open_cdp_tab', fake_open_tab)
    monkeypatch.setattr('omni_agent.cookie_login.navigate_cdp_page', fake_navigate)
    monkeypatch.setattr('omni_agent.cookie_login.fetch_cdp_targets', fake_targets)
    monkeypatch.setattr('omni_agent.cookie_login.fetch_cdp_snapshot', fake_snapshot)
    monkeypatch.setattr(service, 'verify_cookie', lambda p: asyncio.sleep(0, result={'state': 'VALID'}))

    def read_cookie_text(plat):
        with service.sessions.cookie_file(plat) as path:
            return Path(path).read_text('utf-8') if path else ''

    with TestClient(app) as client:
        # 1. X (Twitter) 已有凭据时，重新验证登录必须 require_explicit_save=True，不自动覆盖，关窗也不覆盖
        service.import_cookie('X (Twitter)', 'auth_token=old-x-auth; ct0=old-x-ct0')
        relogin = client.post('/api/admin/sessions/X%20(Twitter)/browser-login').json()
        assert relogin['require_explicit_save'] is True
        attempt_1 = relogin['attempt_id']
        for _ in range(25):
            st = client.get(f'/api/admin/sessions/browser-status/{attempt_1}').json()
            if st['marker_detected'] and st['cookie_count'] == 2:
                break
            time.sleep(0.01)
        assert client.get(f'/api/admin/sessions/browser-status/{attempt_1}').json()['state'] in ('OPENING', 'WAITING')
        assert 'old-x-auth' in read_cookie_text('X (Twitter)')

        # 关闭窗口而不点保存 -> 绝对不覆盖 old-x-auth
        procs[-1].returncode = 0
        closed_st = {}
        for _ in range(30):
            closed_st = client.get(f'/api/admin/sessions/browser-status/{attempt_1}').json()
            if closed_st['state'] == 'CANCELLED':
                break
            time.sleep(0.01)
        assert closed_st['state'] == 'CANCELLED'
        assert 'old-x-auth' in read_cookie_text('X (Twitter)')

        # 再次重新验证，测试页内导航 + 显式点击“保存下载凭据”才真正覆盖
        relogin_2 = client.post('/api/admin/sessions/X%20(Twitter)/browser-login').json()
        attempt_2 = relogin_2['attempt_id']
        for _ in range(25):
            if client.get(f'/api/admin/sessions/browser-status/{attempt_2}').json()['cdp_connected']:
                break
            time.sleep(0.01)
        assert client.post(f'/api/admin/sessions/browser-login/{attempt_2}/navigate', json={'target': 'google'}).status_code == 200
        assert client.post(f'/api/admin/sessions/browser-login/{attempt_2}/navigate', json={'target': 'platform'}).status_code == 200
        assert client.post(f'/api/admin/sessions/browser-login/{attempt_2}/navigate', json={'target': 'reload'}).status_code == 200
        assert nav_records[:2] == ['https://accounts.google.com/', 'https://x.com/i/flow/login']
        assert client.post('/api/admin/sessions/X%20(Twitter)/browser-capture').status_code == 200
        for _ in range(40):
            if client.get(f'/api/admin/sessions/browser-status/{attempt_2}').json()['state'] == 'COMPLETED':
                break
            time.sleep(0.01)
        assert 'new-x-auth' in read_cookie_text('X (Twitter)')

        # 2. manage-google 模式打开 Google 账号页，只管理网页登录，不修改已保存凭据
        manage = client.post('/api/admin/sessions/manage-google', json={}).json()
        assert manage['manage_only'] is True and manage['require_explicit_save'] is True
        manage_id = manage['attempt_id']
        for _ in range(25):
            if client.get(f'/api/admin/sessions/browser-status/{manage_id}').json()['cdp_connected']:
                break
            time.sleep(0.01)
        assert launch_records[-1]['url'] == 'https://accounts.google.com/'
        assert client.post('/api/admin/sessions/YouTube/browser-capture').status_code == 200
        manage_done = {}
        for _ in range(30):
            manage_done = client.get(f'/api/admin/sessions/browser-status/{manage_id}').json()
            if manage_done['state'] == 'COMPLETED':
                break
            time.sleep(0.01)
        assert manage_done['state'] == 'COMPLETED' and '未保存或修改' in manage_done['message']
        assert service.sessions.status('YouTube')['state'] == 'MISSING'

        # 3. 独立登录档案多账号隔离：新建、激活、打开隔离容器保存 YouTube + Pixiv、覆写保护、同步及退出清理
        acc_main = client.post('/api/admin/sessions/accounts', json={'label': '主号 Google'}).json()
        main_id = acc_main['accounts'][0]['id']
        acc_alt = client.post('/api/admin/sessions/accounts', json={'label': '工作号'}).json()
        assert len(acc_alt['accounts']) == 2
        alt_id = next(a['id'] for a in acc_alt['accounts'] if a['id'] != main_id)
        act_res = client.post(f'/api/admin/sessions/accounts/{main_id}/activate').json()
        assert act_res['current_id'] == main_id

        # 打开主号保存 YouTube（启动地址应为 Google 账号页且使用主号隔离目录 _e0）
        snap_state['host'] = 'www.youtube.com'
        snap_state['cookies'] = [
            {'name': 'SID', 'value': 'yt-main-sid', 'domain': '.youtube.com', 'path': '/', 'httpOnly': True, 'secure': True},
            {'name': 'SAPISID', 'value': 'yt-main-sapisid', 'domain': '.youtube.com', 'path': '/', 'httpOnly': False, 'secure': True},
        ]
        open_yt = client.post(f'/api/admin/sessions/accounts/{main_id}/open', json={'platform': 'YouTube'}).json()
        assert open_yt['account_id'] == main_id and open_yt['require_explicit_save'] is True
        main_dir_e0 = cfg.directory / 'browser-profiles' / f'{main_id}_e0'
        yt_attempt = open_yt['attempt_id']
        for _ in range(30):
            if client.get(f'/api/admin/sessions/browser-status/{yt_attempt}').json()['cookie_count'] == 2:
                break
            time.sleep(0.01)
        assert launch_records[-1]['url'] == 'https://accounts.google.com/'
        assert any(r.get('profile_dir') == main_dir_e0 for r in launch_records)
        client.post('/api/admin/sessions/YouTube/browser-capture')
        for _ in range(40):
            if client.get(f'/api/admin/sessions/browser-status/{yt_attempt}').json()['state'] == 'COMPLETED':
                break
            time.sleep(0.01)

        # 同时在主号下保存 Pixiv 凭据
        snap_state['host'] = 'www.pixiv.net'
        snap_state['cookies'] = [
            {'name': 'SID', 'value': 'yt-main-sid', 'domain': '.youtube.com', 'path': '/', 'httpOnly': True, 'secure': True},
            {'name': 'SAPISID', 'value': 'yt-main-sapisid', 'domain': '.youtube.com', 'path': '/', 'httpOnly': False, 'secure': True},
            {'name': 'PHPSESSID', 'value': 'px-main-1', 'domain': '.pixiv.net', 'path': '/', 'httpOnly': True, 'secure': True},
        ]
        open_px = client.post(f'/api/admin/sessions/accounts/{main_id}/open', json={'platform': 'Pixiv'}).json()
        px_attempt = open_px['attempt_id']
        for _ in range(30):
            if client.get(f'/api/admin/sessions/browser-status/{px_attempt}').json()['cookie_count'] == 1:
                break
            time.sleep(0.01)
        client.post('/api/admin/sessions/Pixiv/browser-capture')
        for _ in range(40):
            if client.get(f'/api/admin/sessions/browser-status/{px_attempt}').json()['state'] == 'COMPLETED':
                break
            time.sleep(0.01)

        # 验证同步接口可工作且账号列表中记录已绑定平台
        synced = client.post(f'/api/admin/sessions/accounts/{main_id}/sync', json={'include_manual': False}).json()
        assert 'YouTube' in synced['synced_platforms']
        listed_before = client.get('/api/admin/sessions/accounts').json()
        main_entry = next(a for a in listed_before['accounts'] if a['id'] == main_id)
        assert set(main_entry['bound_platforms']) == {'YouTube', 'Pixiv'}

        # 关闭活动窗口后模拟用户手动覆写 Pixiv（使用其他账号/导入） -> owner_token 变更
        procs[-1].returncode = 0
        service.import_cookie('Pixiv', 'PHPSESSID=px-overwritten-by-manual', source='import')

        # 退出主号：仅清除仍归属主号的 YouTube，保留被覆盖的 Pixiv，递增 epoch=1 并删除旧 _e0 目录
        logout_res = client.post('/api/admin/sessions/accounts/logout', json={'account_ids': [main_id]}).json()
        assert 'YouTube' in logout_res['cleared_platforms']
        assert 'Pixiv' not in logout_res['cleared_platforms']
        assert service.sessions.status('YouTube')['state'] == 'MISSING'
        assert service.sessions.status('Pixiv')['state'] == 'IMPORTED'
        assert 'px-overwritten-by-manual' in read_cookie_text('Pixiv')
        assert not main_dir_e0.exists()
        main_after = next(a for a in logout_res['accounts']['accounts'] if a['id'] == main_id)
        assert main_after['epoch'] == 1 and main_after['signed_out'] is True and main_after['bound_platforms'] == []

        # 删除工作号，确认状态 clean
        del_res = client.delete(f'/api/admin/sessions/accounts/{alt_id}').json()
        assert len(del_res['accounts']) == 1

        # 4. 严格隔离检查：Agent MCP list_cookie_statuses 绝不泄漏 accounts / ui / owner_token / browser_profile
        agent_res = await invoke(service, 'agent', 'list_cookie_statuses', {})
        assert agent_res['ok'] is True
        agent_statuses = agent_res['result']['platforms']
        serialized = json.dumps(agent_statuses, ensure_ascii=False)
        for item in agent_statuses:
            assert set(item.keys()) == {'platform', 'state', 'enabled', 'validation'}
            assert set(item['validation'].keys()).issubset({'state', 'checked_at'})
        for forbidden in ('owner_token', 'source', 'browser_profile', 'browser_accounts', '主号', 'yt-main', 'px-overwritten'):
            assert forbidden not in serialized
