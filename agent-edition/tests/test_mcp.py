import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest


EXPECTED_TOOLS = {
    'analyze_media', 'download_media', 'download_collection', 'get_task_status',
    'list_tasks', 'cancel_task', 'get_media_file', 'get_service_capabilities',
    'deliver_media', 'get_delivery_status', 'get_settings', 'update_settings',
    'get_bot_configs', 'update_bot_config', 'probe_bot', 'list_approvals',
    'approve_request', 'cleanup_cache', 'list_cookie_statuses',
    'disable_platform_cookie', 'remove_platform_cookie',
}


@pytest.fixture(scope='module')
def live_server(tmp_path_factory):
    data = tmp_path_factory.mktemp('agent-live')
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, PYTHONPATH=str(root), OMNI_AGENT_DATA=str(data))
    log = open(data / 'server-test.log', 'w')
    proc = subprocess.Popen([sys.executable, '-m', 'omni_agent', '--serve', '--no-browser', '--port', str(port)], env=env, stdout=log, stderr=log)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail('Live service exited: ' + (data / 'server-test.log').read_text())
            try:
                with httpx.Client(trust_env=False) as client:
                    if client.get(url + '/health').status_code == 200:
                        break
            except httpx.HTTPError:
                time.sleep(.1)
        else:
            pytest.fail('Live service startup timeout')
        yield {'url': url, 'data': data, 'env': env, 'process': proc}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


async def test_streamable_http_initialize_discovery_and_settings_and_cookie_tools(live_server):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    assert not (live_server['data'] / 'auth.json').exists()
    # 先由管理接口导入平台 Cookie，供后续检验 MCP 权限隔离
    with httpx.Client(trust_env=False) as http:
        assert http.post(live_server['url'] + '/api/admin/sessions', json={'platform': '哔哩哔哩', 'text': 'SESSDATA=mcp-secret-cookie'}).status_code == 200

    async with httpx2.AsyncClient(trust_env=False) as client:
        async with streamable_http_client(live_server['url'] + '/mcp', http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == 'OmniDownloader Agent Edition'
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert names == EXPECTED_TOOLS

                cap = await session.call_tool('get_service_capabilities', {})
                assert cap.structured_content['ok'] is True
                assert cap.structured_content['result']['auth_required'] is False
                assert cap.structured_content['result']['cookie_agent_permissions'] == ['read_status', 'disable', 'remove']

                # 直接修改与读取设置
                upd = await session.call_tool('update_settings', {'settings': {'default_height': 720, 'theme': 'dark'}})
                assert upd.structured_content['ok'] is True and upd.structured_content['result']['default_height'] == 720
                got = await session.call_tool('get_settings', {})
                assert got.structured_content['ok'] is True and got.structured_content['result']['theme'] == 'dark'

                # Cookie 只能读取非敏感状态、禁用与移除，不可获取明文
                statuses = await session.call_tool('list_cookie_statuses', {})
                assert statuses.structured_content['ok'] is True
                assert 'mcp-secret-cookie' not in str(statuses)
                disabled = await session.call_tool('disable_platform_cookie', {'platform': '哔哩哔哩'})
                assert disabled.structured_content['ok'] is True
                assert disabled.structured_content['result']['platforms'][0]['enabled'] is False
                removed = await session.call_tool('remove_platform_cookie', {'platform': '哔哩哔哩'})
                assert removed.structured_content['ok'] is True
                assert removed.structured_content['result']['platforms'][0]['state'] == 'MISSING'

                # 恢复默认设置供后续检查
                await session.call_tool('update_settings', {'settings': {'default_height': 1080, 'theme': 'auto'}})

                result = await session.call_tool('get_task_status', {'task_id': 'missing'})
                assert result.structured_content['error']['code'] == 'NOT_FOUND'
                assert result.is_error
                result = await session.call_tool('get_task_status', {'task_id': {'token': 'must-not-be-echoed'}})
                assert result.is_error and result.structured_content['error']['code'] == 'INVALID_PARAMETER'
                assert 'must-not-be-echoed' not in str(result)
                result = await session.call_tool('download_media', {'url': 'http://127.0.0.1/private'})
                assert result.structured_content['ok'] is False


async def test_stdio_initialize_discovery_and_api_bridge(live_server):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = dict(live_server['env'], OMNI_AGENT_SERVER=live_server['url'])
    params = StdioServerParameters(command=sys.executable, args=['-m', 'omni_agent', '--stdio'], env=env)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            assert {t.name for t in (await session.list_tools()).tools} == EXPECTED_TOOLS
            result = await session.call_tool('list_tasks', {})
            assert result.structured_content['ok'] is True
            settings = await session.call_tool('get_settings', {})
            assert settings.structured_content['ok'] is True


def test_http_mcp_is_tokenless_but_keeps_host_and_origin_guards(live_server):
    with httpx.Client(trust_env=False) as client:
        ok = client.post(
            live_server['url'] + '/mcp',
            headers={'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'method': 'initialize', 'id': 1, 'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'probe', 'version': '1.0'}}},
        )
        assert ok.status_code == 200
        bad_host = client.post(live_server['url'] + '/mcp', headers={'Host': 'evil.example'}, json={'jsonrpc': '2.0', 'method': 'tools/list', 'id': 1})
        assert bad_host.status_code == 403
        bad_origin = client.post(live_server['url'] + '/mcp', headers={'Origin': 'https://evil.example'}, json={'jsonrpc': '2.0', 'method': 'tools/list', 'id': 1})
        assert bad_origin.status_code == 403


async def test_actual_worker_starts_and_rejects_unsupported_url(tmp_path):
    from omni_agent.service import Service
    from omni_agent.errors import AgentError
    service = Service(tmp_path)
    with pytest.raises(AgentError) as error:
        await service.runner.call({'action': 'analyze', 'url': 'http://127.0.0.1/secret'})
    assert error.value.code in ('UNSUPPORTED_PLATFORM', 'SSRF_BLOCKED')
    assert not service.runner.processes
    service.store.db.close()
