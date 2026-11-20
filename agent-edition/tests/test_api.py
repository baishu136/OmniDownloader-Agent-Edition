import asyncio
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

from conftest import ready_task
from omni_agent.api import create_app, invoke
from omni_agent.config import Config, default_data_dir
from omni_agent.errors import AgentError
from omni_agent.service import DownloadOptions


class FakeRunner:
    """Fixture for lifecycle tests. No real platform success is claimed."""

    def __init__(self, config, store):
        self.config, self.store = config, store

    async def call(self, payload, identifier=None, progress=None):
        if payload['action'] == 'analyze':
            return {'platform': 'fixture', 'title': 'mock media', 'media_type': 'video', 'options': ['VIDEO_WITH_AUDIO']}
        await asyncio.sleep(.05)
        from PIL import Image
        path = self.config.cache / 'jobs' / identifier / 'output' / 'fixture.png'
        path.parent.mkdir(parents=True)
        Image.new('RGB', (2, 2)).save(path)
        return {'paths': [str(path)]}

    async def close(self):
        pass


def test_default_data_dir_is_independent_and_tokenless(tmp_path, monkeypatch):
    monkeypatch.delenv('OMNI_AGENT_DATA', raising=False)
    workspace_root = Path(__file__).resolve().parents[2]
    assert default_data_dir() == workspace_root / 'data'

    # 模拟打包模式位于工作区内部
    fake_exe = workspace_root / 'agent-edition' / 'dist' / 'OmniDownloaderAgent' / 'OmniDownloaderAgent.exe'
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(fake_exe))
    assert default_data_dir() == workspace_root / 'data'

    # 模拟打包模式被独立复制到其他路径
    portable_exe = tmp_path / 'Portable' / 'OmniDownloaderAgent.exe'
    portable_exe.parent.mkdir(parents=True)
    monkeypatch.setattr(sys, 'executable', str(portable_exe))
    assert default_data_dir() == portable_exe.parent / 'data'

    cfg = Config(tmp_path / 'state')
    assert not (cfg.directory / 'auth.json').exists()
    assert not hasattr(cfg, 'tokens')


def test_tokenless_admin_api_save_and_ui_no_login(service):
    assert not (service.config.directory / 'auth.json').exists()
    with TestClient(create_app(service)) as client:
        assert client.get('/health').json() == {'ok': True}
        page = client.get('/').text
        assert 'id="login"' not in page
        assert 'auth.json' not in page
        assert 'id="workspace"' in page

        original = client.get('/api/admin/config').json()
        original['video'] = False
        assert client.put('/api/admin/config', json=original).status_code == 200
        assert json.loads(service.config.path.read_text('utf-8'))['video'] is False

        result = client.post('/api/tools/download_media', json={'url': 'https://youtube.com/watch?v=test'}).json()
        assert result['error']['code'] == 'FEATURE_DISABLED'
        assert 'token' not in client.get('/api/admin/config').text

        mcp_cfg = client.get('/api/admin/mcp-config').json()
        assert 'headers' not in mcp_cfg['http']['mcpServers']['omni-agent']
        assert 'OMNI_AGENT_TOKEN' not in mcp_cfg['stdio']['mcpServers']['omni-agent']['env']


def test_origin_host_and_body_boundary(service):
    with TestClient(create_app(service)) as client:
        assert client.get('/api/admin/config', headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.get('/health', headers={'Host': 'evil.example'}).status_code == 403
        assert client.post('/api/tools/list_tasks', content=b'x' * (2 * 1024**2 + 1)).status_code == 413


def test_file_reference_isolation_and_no_raw_paths(service):
    agent_job = ready_task(service, 'agent')
    other_row, _ = service.store.create_job('other-owner', {'url': 'https://www.youtube.com/watch?v=other'}, 'other-key', 'other-req')
    other_path = service.config.cache / 'jobs' / other_row['id'] / 'output' / 'other.png'
    other_path.parent.mkdir(parents=True)
    from PIL import Image
    Image.new('RGB', (4, 4), 'red').save(other_path)
    service.store.finish(other_row['id'], [other_path])
    with TestClient(create_app(service)) as client:
        agent_file = service.store.files(agent_job['id'])[0]['id']
        other_file = service.store.files(other_row['id'])[0]['id']
        assert client.post('/api/tools/get_task_status', json={'task_id': other_row['id']}).json()['error']['code'] == 'NOT_FOUND'
        reference = client.post('/api/tools/get_media_file', json={'file_id': agent_file}).json()
        assert reference['ok'] and 'path' not in reference['result']
        assert client.get('/api/files/' + agent_file).content.startswith(b'\x89PNG')
        assert client.get('/api/files/' + other_file).status_code == 400


@pytest.mark.asyncio
async def test_parameter_validation_and_errors(service):
    result = await invoke(service, 'agent', 'download_collection', {'url': 'https://youtube.com/playlist?list=test', 'count': 0})
    assert result['error']['code'] == 'INVALID_PARAMETER'
    result = await invoke(service, 'agent', 'get_media_file', {'file_id': '../../etc/passwd'})
    assert result['error']['code'] == 'NOT_FOUND'
    assert (await invoke(service, 'agent', 'not_a_tool', {}))['error']['code'] == 'UNKNOWN_TOOL'


async def test_limits_and_confirmation_can_be_approved_by_agent(service):
    for feature, typ in [('video', 'VIDEO_ONLY'), ('audio', 'AUDIO_ONLY'), ('images', 'IMAGE_ZIP'), ('covers', 'COVER')]:
        setattr(service.config.settings, feature, False)
        with pytest.raises(AgentError):
            service.resolve_options(DownloadOptions(use_defaults=False, download_type=typ))
        setattr(service.config.settings, feature, True)

    with pytest.raises(AgentError) as error:
        await service.download('agent', 'https://youtube.com/playlist?list=test', DownloadOptions(), collection=True, count=6)
    assert error.value.code == 'CONFIRMATION_REQUIRED'
    approval = error.value.details['approval_id']

    with TestClient(create_app(service)) as client:
        approvals = client.post('/api/tools/list_approvals', json={}).json()
        assert approvals['ok'] and any(a['id'] == approval for a in approvals['result']['approvals'])
        approved = client.post('/api/tools/approve_request', json={'approval_id': approval}).json()
        assert approved['ok']

        result = await service.download('agent', 'https://youtube.com/playlist?list=test', DownloadOptions(), collection=True, count=6, approval_id=approval)
        assert result['state'] == 'QUEUED'
        with pytest.raises(AgentError):
            await service.download('bob', 'https://youtube.com/playlist?list=test', DownloadOptions(), collection=True, count=6, approval_id=approval)


async def test_async_download_status_and_cancel(service):
    job = await service.download('alice', 'https://youtube.com/watch?v=test', DownloadOptions(), request_key='same')
    again = await service.download('alice', 'https://youtube.com/watch?v=test', DownloadOptions(), request_key='same')
    assert job['id'] == again['id'] and job['state'] == 'QUEUED'
    cancelled = await service.cancel('alice', job['id'])
    assert cancelled['state'] == 'CANCELLED'


def test_async_core_completion_in_background(service):
    service.runner = FakeRunner(service.config, service.store)
    with TestClient(create_app(service)) as client:
        result = client.post('/api/tools/download_media', json={'url': 'https://youtube.com/watch?v=test'}).json()
        assert result['ok'] and result['result']['state'] == 'QUEUED'
        identifier = result['result']['id']
        import time
        for _ in range(100):
            status = client.post('/api/tools/get_task_status', json={'task_id': identifier}).json()['result']
            if status['state'] == 'READY':
                break
            time.sleep(.02)
        assert status['state'] == 'READY' and status['file_count'] == 1


def test_agent_can_manage_settings_and_bots_with_redacted_secrets(service):
    with TestClient(create_app(service)) as client:
        res = client.post('/api/tools/get_settings', json={}).json()
        assert res['ok'] and res['result']['default_height'] == 1080 and res['result']['image_format'] == 'original'

        upd = client.post('/api/tools/update_settings', json={
            'settings': {'default_height': 720, 'profiles': True, 'proxy': 'http://127.0.0.1:7890', 'theme': 'dark', 'image_format': 'jpg'}
        }).json()
        assert upd['ok']
        assert upd['result']['default_height'] == 720
        assert upd['result']['profiles'] is True
        assert upd['result']['proxy'] == 'http://127.0.0.1:7890'
        assert upd['result']['image_format'] == 'jpg'
        assert json.loads(service.config.path.read_text('utf-8'))['image_format'] == 'jpg'
        assert json.loads(service.config.path.read_text('utf-8'))['theme'] == 'dark'
        assert client.post('/api/tools/get_service_capabilities', json={}).json()['result']['defaults']['image_format'] == 'jpg'

        # 非法代理或默认画质高于最高画质或非法图片格式仍须拒绝
        bad = client.post('/api/tools/update_settings', json={'settings': {'default_height': 4320}}).json()
        assert not bad['ok'] and bad['error']['code'] == 'INVALID_PARAMETER'
        bad_img_fmt = client.post('/api/tools/update_settings', json={'settings': {'image_format': 'tiff'}}).json()
        assert not bad_img_fmt['ok'] and bad_img_fmt['error']['code'] == 'INVALID_PARAMETER'
        bad_proxy = client.post('/api/tools/update_settings', json={'settings': {'proxy': 'http://user:pass@127.0.0.1:7890'}}).json()
        assert not bad_proxy['ok'] and bad_proxy['error']['code'] == 'INVALID_PARAMETER'

        # 机器人配置可管理，但始终不回传密钥明文
        bot_upd = client.post('/api/tools/update_bot_config', json={
            'config': {'adapter': 'telegram', 'enabled': True, 'token': 'super-bot-secret', 'allowed_targets': ['12345']}
        }).json()
        assert bot_upd['ok'] and bot_upd['result']['has_token'] is True
        assert 'super-bot-secret' not in json.dumps(bot_upd)

        bots = client.post('/api/tools/get_bot_configs', json={}).json()
        assert bots['ok'] and 'super-bot-secret' not in json.dumps(bots)
        tg = next(b for b in bots['result']['bots'] if b['adapter'] == 'telegram')
        assert tg['enabled'] is True and tg['allowed_targets'] == ['12345'] and tg['has_token'] is True

        # 清理缓存工具可直接执行
        clean = client.post('/api/tools/cleanup_cache', json={}).json()
        assert clean['ok'] and 'files_removed' in clean['result']


def test_agent_cookie_permissions_strictly_restricted_to_status_disable_remove(service):
    with TestClient(create_app(service)) as client:
        # 仅用户通过管理页导入 Cookie
        assert client.post('/api/admin/sessions', json={'platform': '哔哩哔哩', 'text': 'SESSDATA=super-secret-session'}).status_code == 200
        assert client.post('/api/admin/sessions', json={'platform': 'Pixiv', 'text': 'PHPSESSID=pixiv-secret'}).status_code == 200

        # 验证底层文件已加密且无明文
        admin_sessions = client.get('/api/admin/sessions').text
        assert 'super-secret-session' not in admin_sessions
        assert all(b'super-secret-session' not in p.read_bytes() for p in service.vault.directory.rglob('*.bin'))

        # Agent 只能读取非敏感状态
        agent_statuses = client.post('/api/tools/list_cookie_statuses', json={}).json()
        assert agent_statuses['ok']
        raw = json.dumps(agent_statuses, ensure_ascii=False)
        assert 'super-secret-session' not in raw and 'pixiv-secret' not in raw and 'SESSDATA' not in raw
        bili = next(p for p in agent_statuses['result']['platforms'] if p['platform'] == '哔哩哔哩')
        assert bili['state'] == 'IMPORTED' and bili['enabled'] is True

        # Agent 绝对没有导入、填写、读取、启用或检测 Cookie 的工具权限
        for forbidden_tool in ('import_cookie', 'set_cookie', 'get_cookie', 'read_cookie', 'enable_platform_cookie', 'verify_cookie'):
            denied = client.post('/api/tools/' + forbidden_tool, json={'platform': '哔哩哔哩'}).json()
            assert not denied['ok'] and denied['error']['code'] == 'UNKNOWN_TOOL'

        # Agent 可以禁用单个平台 Cookie，且禁用后底层不再读取该 Cookie
        dis = client.post('/api/tools/disable_platform_cookie', json={'platform': '哔哩哔哩'}).json()
        assert dis['ok'] and dis['result']['platforms'][0]['enabled'] is False
        assert service.sessions.is_enabled('哔哩哔哩') is False
        with service.sessions.cookie_file('哔哩哔哩') as cookie_path:
            assert cookie_path is None
        assert client.post('/api/admin/sessions/哔哩哔哩/verify').status_code == 400

        # 仅用户可在管理页重新启用
        en = client.post('/api/admin/sessions/哔哩哔哩/enable')
        assert en.status_code == 200 and en.json()['enabled'] is True
        assert service.sessions.is_enabled('哔哩哔哩') is True
        with service.sessions.cookie_file('哔哩哔哩') as cookie_path:
            assert cookie_path is not None

        # Agent 可以批量禁用全部平台 Cookie
        dis_all = client.post('/api/tools/disable_platform_cookie', json={'platform': 'all'}).json()
        assert dis_all['ok'] and all(p['enabled'] is False for p in dis_all['result']['platforms'])

        # Agent 可以移除单个或全部平台 Cookie
        rem = client.post('/api/tools/remove_platform_cookie', json={'platform': '哔哩哔哩'}).json()
        assert rem['ok'] and rem['result']['platforms'][0]['state'] == 'MISSING'
        assert service.sessions.status('哔哩哔哩')['state'] == 'MISSING'

        rem_all = client.post('/api/tools/remove_platform_cookie', json={'platform': 'all'}).json()
        assert rem_all['ok'] and all(p['state'] == 'MISSING' for p in rem_all['result']['platforms'])
        assert service.sessions.status('Pixiv')['state'] == 'MISSING'


async def test_policy_change_before_dispatch_is_enforced(service):
    job = await service.download('alice', 'https://youtube.com/watch?v=test', DownloadOptions())
    service.config.settings.video = False
    await service.run_job(service.store.job(job['id']))
    assert service.store.job(job['id'])['error'] == 'FEATURE_DISABLED'


async def test_shared_active_cancellation_does_not_kill_other_users(service):
    first = await service.download('alice', 'https://youtube.com/watch?v=test', DownloadOptions())
    second = await service.download('bob', 'https://youtube.com/watch?v=test', DownloadOptions())
    assert (await service.cancel('alice', first['id']))['state'] == 'CANCELLED'
    assert service.store.job(second['id'])['state'] == 'QUEUED'
    assert (await service.cancel('bob', second['id']))['state'] == 'CANCELLED'


async def test_single_zip_confirmation_round_trip_uses_public_tool_schema(service):
    arguments = {'url': 'https://www.xiaohongshu.com/explore/test', 'options': {'use_defaults': False, 'download_type': 'IMAGE_ZIP'}, 'request_key': 'zip-one'}
    first = await invoke(service, 'alice', 'download_media', arguments)
    second = await invoke(service, 'alice', 'download_media', arguments)
    assert first['error']['code'] == 'CONFIRMATION_REQUIRED'
    approval = first['error']['approval_id']
    assert second['error']['approval_id'] == approval
    service.store.db.execute('UPDATE approvals SET approved=1 WHERE id=?', (approval,))
    result = await invoke(service, 'alice', 'download_media', dict(arguments, approval_id=approval))
    assert result['ok'] and result['result']['state'] == 'QUEUED'


def test_patch_and_reset_settings_and_storage_directories(service, tmp_path):
    dl_dir = tmp_path / 'downloads'
    img_dir = tmp_path / 'images'
    service.runner = FakeRunner(service.config, service.store)
    with TestClient(create_app(service)) as client:
        # PATCH 部分更新（自动保存用）
        res = client.patch('/api/admin/config', json={
            'theme': 'dark',
            'download_dir': str(dl_dir),
            'image_dir': str(img_dir),
            'qq_video_mode': 'message',
            'qq_image_mode': 'file',
            'qq_forward_collection': True,
            'image_format': 'webp',
            'pack_zip': True,
        })
        assert res.status_code == 200 and res.json()['settings']['theme'] == 'dark'
        assert res.json()['settings']['qq_forward_collection'] is True
        assert res.json()['settings']['image_format'] == 'webp'

        # 不合法的相对路径或内部目录重叠须拒绝
        bad = client.patch('/api/admin/config', json={'download_dir': 'relative/path'})
        assert bad.status_code == 400
        overlap = client.patch('/api/admin/config', json={'download_dir': str(service.config.directory)})
        assert overlap.status_code == 400

        # 状态接口回传存储目录与 bot 连接信息
        st = client.get('/api/admin/status').json()
        assert st['download_dir'] == str(dl_dir) and st['image_dir'] == str(img_dir)
        assert len(st['bots']) == 2 and 'status' in st['bots'][0]

        # 执行一次下载：验证永久文件确实导出到 download_dir / image_dir，且清理缓存不会删掉它
        dl = client.post('/api/tools/download_media', json={'url': 'https://youtube.com/watch?v=test', 'options': {'use_defaults': False, 'download_type': 'VIDEO_WITH_AUDIO', 'pack_zip': False}}).json()
        assert dl['ok']
        import time
        for _ in range(100):
            job_st = client.post('/api/tools/get_task_status', json={'task_id': dl['result']['id']}).json()['result']
            if job_st['state'] == 'READY':
                break
            time.sleep(.02)
        assert job_st['state'] == 'READY'
        assert (dl_dir / 'fixture.png').is_file()

        # 重置默认设置
        reset = client.post('/api/admin/config/reset', json={'reset_cache_dir': False})
        assert reset.status_code == 200
        reset_cfg = reset.json()['settings']
        assert reset_cfg['theme'] == 'auto'
        assert reset_cfg['download_dir'] == ''
        assert reset_cfg['qq_video_mode'] == 'file'
        assert reset_cfg['qq_forward_collection'] is False
        assert reset_cfg['pack_zip'] is False
        assert reset_cfg['image_format'] == 'original'


async def test_download_command_prefixes_us_and_zip(service):
    from omni_agent.security import parse_download_command, parse_download_commands

    assert parse_download_command('/us https://www.bilibili.com/video/BV1xx') == ('https://www.bilibili.com/video/BV1xx', True, False)
    assert parse_download_command('/zip https://www.xiaohongshu.com/explore/1') == ('https://www.xiaohongshu.com/explore/1', False, True)
    assert parse_download_command('/us /zip 看看 https://x.com/u/status/1') == ('看看 https://x.com/u/status/1', True, True)
    assert parse_download_command('/zip/us https://youtube.com/watch?v=1') == ('/zip/us https://youtube.com/watch?v=1', False, False)
    assert parse_download_command('/ushttps://youtube.com/watch?v=1') == ('https://youtube.com/watch?v=1', True, False)

    # /merge 前缀指令及其任意组合
    assert parse_download_commands('/merge https://www.xiaohongshu.com/explore/1') == ('https://www.xiaohongshu.com/explore/1', False, False, True)
    assert parse_download_commands('/us /merge https://www.bilibili.com/video/BV1xx') == ('https://www.bilibili.com/video/BV1xx', True, False, True)
    assert parse_download_commands('/merge /us /zip https://x.com/u/status/1') == ('https://x.com/u/status/1', True, True, True)

    # 默认合并转发单批上限应为 30 条，并可通过 update_settings 修改
    assert service.config.settings.qq_forward_max_items == 30
    updated = service.update_settings({'qq_forward_max_items': 25})
    assert updated['qq_forward_max_items'] == 25
    assert service.capabilities()['qq_delivery']['forward_max_items'] == 25
    assert '/merge' in service.capabilities()['download_commands']

    # 通过服务层调用，/us + /zip + /merge 能正确写入 payload 的 all_items、pack_zip 与 cmd_merge
    service.update_settings({'confirmation_threshold': 50})
    job = await service.download('agent', '/us /zip /merge https://www.bilibili.com/video/BV17x411w7KC', DownloadOptions())
    stored = service.store.payload(service.store.job(job['id']))
    assert stored['all_items'] is True
    assert stored['cmd_all'] is True and stored['cmd_zip'] is True and stored['cmd_merge'] is True
    assert stored['options']['pack_zip'] is True
    assert stored['url'] == 'https://www.bilibili.com/video/BV17x411w7KC'
    assert '合并转发 /merge' in job['metadata']['command_summary']

    # 9 张图文下载不再因 500 MiB * 9 误报 CACHE_FULL，3 张图文在默认阈值 5 下也不误报 CONFIRMATION_REQUIRED
    service.update_settings({'confirmation_threshold': 5, 'max_collection': 20})
    small_img_col = await service.download(
        'agent',
        'https://www.xiaohongshu.com/explore/9pics',
        DownloadOptions(use_defaults=False, download_type='IMAGE'),
        collection=True,
        start=1,
        count=3,
    )
    assert small_img_col['state'] == 'QUEUED'
    service.update_settings({'confirmation_threshold': 10})
    nine_img_col = await service.download(
        'agent',
        'https://www.xiaohongshu.com/explore/9pics',
        DownloadOptions(use_defaults=False, download_type='IMAGE'),
        collection=True,
        start=1,
        count=9,
    )
    assert nine_img_col['state'] == 'QUEUED'


def test_autostart_command_api_and_boundary(service, tmp_path, monkeypatch):
    import sys
    from fastapi.testclient import TestClient
    from omni_agent import autostart
    from omni_agent.api import create_app

    # 1. 命令构建：默认与自定义端口/数据目录，且冻结模式优先使用 GUI 托盘入口
    cmd, argv = autostart.build_autostart_command(data_dir=tmp_path / 'custom data', host='127.0.0.1', port=59001)
    assert '--serve' in argv and '--no-browser' in argv and '--port' in argv and '59001' in argv
    assert '--data' in argv and str((tmp_path / 'custom data').resolve()) in cmd

    fake_dir = tmp_path / 'portable'
    fake_dir.mkdir()
    gui_exe = fake_dir / 'OmniDownloaderAgent.exe'
    mcp_exe = fake_dir / 'OmniDownloaderAgentMCP.exe'
    gui_exe.write_bytes(b'')
    mcp_exe.write_bytes(b'')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(mcp_exe))
    monkeypatch.setattr(autostart, '_is_windows', lambda: True)
    _, frozen_argv = autostart.build_autostart_command()
    assert frozen_argv[0] == str(gui_exe) and '--serve' in frozen_argv and '--no-browser' in frozen_argv and '--tray' in frozen_argv
    monkeypatch.setattr(sys, 'frozen', False, raising=False)

    # 2. Windows/桌面支持环境下的管理接口读写与陈旧路径识别
    reg_store = {}
    monkeypatch.setattr(autostart, 'is_autostart_supported', lambda: (True, ''))
    monkeypatch.setattr(autostart, '_is_windows', lambda: True)
    monkeypatch.setattr(autostart, '_read_windows_run_value', lambda: reg_store.get('val'))

    def fake_write(value):
        if value is None:
            reg_store.pop('val', None)
        else:
            reg_store['val'] = value

    monkeypatch.setattr(autostart, '_write_windows_run_value', fake_write)
    service.runtime_endpoint = {'host': '127.0.0.1', 'port': 58080}

    with TestClient(create_app(service)) as client:
        assert client.get('/api/admin/autostart').json()['enabled'] is False
        on_res = client.put('/api/admin/autostart', json={'enabled': True})
        assert on_res.status_code == 200
        assert on_res.json()['enabled'] is True and on_res.json()['stale'] is False
        assert '--no-browser' in reg_store['val'] and '--tray' in reg_store['val']
        assert client.get('/api/admin/status').json()['autostart']['enabled'] is True

        # 模拟安装目录迁移导致注册表命令陈旧
        reg_store['val'] = '"C:\\Old\\OmniDownloaderAgent.exe" --serve --no-browser'
        assert client.get('/api/admin/autostart').json()['stale'] is True

        # 关闭自启与重复关闭均保持幂等
        off_res = client.put('/api/admin/autostart', json={'enabled': False})
        assert off_res.status_code == 200 and off_res.json()['enabled'] is False and 'val' not in reg_store
        assert client.put('/api/admin/autostart', json={'enabled': False}).json()['enabled'] is False

        # Agent 端工具边界隔离：不能通过 Settings 或 MCP 修改系统开机启动项
        bad_agent = client.post('/api/tools/update_settings', json={'settings': {'autostart': True}}).json()
        assert bad_agent['ok'] is False

        # 3. 无桌面/容器环境：诚实返回 supported=False，拒绝伪开启
        monkeypatch.setattr(autostart, 'is_autostart_supported', lambda: (False, 'HEADLESS'))
        unsupported = client.get('/api/admin/autostart').json()
        assert unsupported['supported'] is False and unsupported['enabled'] is False
        rejected = client.put('/api/admin/autostart', json={'enabled': True})
        assert rejected.status_code == 400 and rejected.json()['error']['code'] == 'CAPABILITY_UNAVAILABLE'
        assert client.put('/api/admin/autostart', json={'enabled': False}).status_code == 200

