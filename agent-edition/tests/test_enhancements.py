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
from omni_agent.cookie_login import PLATFORM_LOGIN_POLICIES, cdp_cookies_to_netscape, normalize_cookie_text
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

    # CDP Cookie 过滤与转换应保留 HttpOnly 并检测关键登录标记
    cdp_netscape, marker_hit = cdp_cookies_to_netscape(
        [
            {'name': 'auth_token', 'value': 'x-cdp-secret', 'domain': '.x.com', 'path': '/', 'httpOnly': True, 'secure': True, 'expires': int(time.time()) + 3600},
            {'name': 'guest', 'value': '1', 'domain': '.other.com', 'path': '/'},
        ],
        PLATFORM_LOGIN_POLICIES['X (Twitter)'],
        service.domains['X (Twitter)'],
    )
    assert marker_hit is True
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
        assert ui_info['local_browser_available'] is False
        assert ui_info['login_url'].startswith('https://')

        # Agent 视角绝不能包含任何 Cookie 键名、明文或 UI 元字段
        agent_resp = client.post('/api/tools/list_cookie_statuses', json={}).json()
        assert agent_resp['ok']
        assert 'SESSDATA' not in json.dumps(agent_resp)
        assert 'json-secret-sess' not in json.dumps(agent_resp)
        assert 'ui' not in agent_resp['result']['platforms'][0]

        # 默认浏览器打开接口与无本地浏览器错误提示
        opened = client.post('/api/admin/sessions/X (Twitter)/open-login')
        assert opened.status_code == 200 and opened.json()['opened'] is True and opened_urls == ['https://x.com/i/flow/login']
        no_browser = client.post('/api/admin/sessions/X (Twitter)/browser-login')
        assert no_browser.status_code == 400 and no_browser.json()['error']['code'] == 'CAPABILITY_UNAVAILABLE'

        # 模拟本地已安装 Chrome：验证 CDP 自动抓取、加密保存、手动抓取和关闭窗口取消
        fake_browser = {'name': 'Google Chrome', 'path': 'mock-chrome'}
        monkeypatch.setattr('omni_agent.cookie_login.find_local_browser', lambda: fake_browser)
        monkeypatch.setattr('omni_agent.service.find_local_browser', lambda: fake_browser)

        real_sleep = asyncio.sleep
        real_exec = asyncio.create_subprocess_exec
        monkeypatch.setattr('omni_agent.cookie_login.asyncio.sleep', lambda sec: real_sleep(0.01 if sec >= 0.9 else sec))

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
                return FakeProc()
            return await real_exec(*cmd, **kwargs)

        poll_counter = {'n': 0}

        async def fake_fetch(port, ws_path):
            assert port == 9229
            poll_counter['n'] += 1
            if poll_counter['n'] == 1:
                return [{'name': 'guest_id', 'value': 'guest-1', 'domain': '.x.com', 'path': '/'}]
            return [{'name': 'auth_token', 'value': 'x-cdp-secret', 'domain': '.x.com', 'path': '/', 'httpOnly': True, 'secure': True}]

        async def fake_close(port, ws_path):
            return None

        monkeypatch.setattr('omni_agent.cookie_login.asyncio.create_subprocess_exec', fake_exec)
        monkeypatch.setattr('omni_agent.cookie_login.fetch_cdp_cookies', fake_fetch)
        monkeypatch.setattr('omni_agent.cookie_login.close_cdp_browser', fake_close)

        started = client.post('/api/admin/sessions/X (Twitter)/browser-login')
        assert started.status_code == 200
        attempt_id = started.json()['attempt_id']

        final_status = {}
        for _ in range(40):
            final_status = client.get(f'/api/admin/sessions/browser-status/{attempt_id}').json()
            if final_status['state'] == 'COMPLETED':
                break
            time.sleep(0.02)

        assert final_status['state'] == 'COMPLETED'
        assert 'x-cdp-secret' not in json.dumps(final_status)
        assert service.sessions.status('X (Twitter)')['state'] == 'IMPORTED'

        # 测试等待期间的手动抓取与取消
        async def empty_cookies(port, ws_path):
            return [{'name': 'PHPSESSID', 'value': 'px-manual-secret', 'domain': '.pixiv.net', 'path': '/'}]

        monkeypatch.setattr('omni_agent.cookie_login.fetch_cdp_cookies', empty_cookies)
        px = client.post('/api/admin/sessions/Pixiv/browser-login')
        assert px.status_code == 200
        px_id = px.json()['attempt_id']
        time.sleep(0.03)
        cancel_res = client.post('/api/admin/sessions/Pixiv/browser-cancel')
        assert cancel_res.status_code == 200 and cancel_res.json()['cancelled'] is True
        assert client.get(f'/api/admin/sessions/browser-status/{px_id}').json()['state'] == 'CANCELLED'

        # 模拟 B 站二维码生成与扫码确认流程
        monkeypatch.setattr(service.qr_logins, 'generate', lambda proxy='': {'qr_id': 'mock-qr', 'matrix': [[1, 0], [0, 1]], 'expires_in': 180})
        monkeypatch.setattr(service.qr_logins, 'poll', lambda qr_id: {'status': 'confirmed', 'netscape': 'SESSDATA=qr-secret-val'})
        qr = client.post('/api/admin/sessions/bilibili/qr')
        assert qr.status_code == 200 and qr.json()['matrix'] == [[1, 0], [0, 1]]
        polled = client.post('/api/admin/sessions/bilibili/qr/mock-qr/poll')
        assert polled.status_code == 200 and polled.json()['status'] == 'confirmed'
        assert 'qr-secret-val' not in polled.text


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
