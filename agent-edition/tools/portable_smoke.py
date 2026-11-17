"""Launch the built Windows EXE and connect using the official SDK."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import httpx


EXPECTED_TOOLS = {
    'analyze_media', 'download_media', 'download_collection', 'get_task_status',
    'list_tasks', 'cancel_task', 'get_media_file', 'get_service_capabilities',
    'deliver_media', 'get_delivery_status', 'get_settings', 'update_settings',
    'reset_settings', 'get_bot_configs', 'update_bot_config', 'probe_bot',
    'list_approvals', 'approve_request', 'cleanup_cache', 'list_cookie_statuses',
    'disable_platform_cookie', 'remove_platform_cookie',
}


async def mcp_check(exe, url, env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=str(exe), args=['--stdio', '--server', url], env=env)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert names == EXPECTED_TOOLS, names
            result = await session.call_tool('get_service_capabilities', {})
            assert result.structured_content['ok'] and result.structured_content['result']['ffmpeg']
            assert result.structured_content['result']['deno']
            assert result.structured_content['result']['auth_required'] is False
            updated = await session.call_tool('update_settings', {'settings': {'default_height': 1080}})
            assert updated.structured_content['ok'] and updated.structured_content['result']['default_height'] == 1080
            cookies = await session.call_tool('list_cookie_statuses', {})
            assert cookies.structured_content['ok'] and len(cookies.structured_content['result']['platforms']) >= 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    with tempfile.TemporaryDirectory(prefix='omni-agent-portable-') as data:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, OMNI_AGENT_DATA=data)
        server = subprocess.Popen(
            [str(directory / 'OmniDownloaderAgent.exe'), '--serve', '--no-browser', '--port', str(port)],
            env=env,
            creationflags=0x08000000,
        )
        url = f'http://127.0.0.1:{port}'
        try:
            for _ in range(150):
                if server.poll() is not None:
                    raise RuntimeError('Portable service exited during launch')
                try:
                    with httpx.Client(trust_env=False, timeout=1) as client:
                        if client.get(url + '/health').status_code == 200:
                            break
                except httpx.HTTPError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Portable service did not become ready')
            with httpx.Client(trust_env=False) as client:
                assert not (Path(data) / 'auth.json').exists()
                settings = client.get(url + '/api/admin/config').json()
                settings['default_height'] = 720
                assert client.put(url + '/api/admin/config', json=settings).status_code == 200
                assert client.get(url + '/').status_code == 200
            asyncio.run(mcp_check(directory / 'OmniDownloaderAgentMCP.exe', url, env))
            request = {'action': 'verify_session', 'platform': 'YouTube', 'state': str(Path(data) / 'worker'), 'data': data}
            result = subprocess.run(
                [str(directory / 'OmniDownloaderAgentMCP.exe'), '--worker'],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                env=env,
                timeout=30,
                creationflags=0x08000000,
            )
            events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            assert events and events[-1].get('code') == 'CAPABILITY_UNAVAILABLE', events
            print('Portable EXE startup, tokenless management, stdio MCP, settings update, FFmpeg/Deno discovery and core worker imports passed.')
        finally:
            subprocess.run(['taskkill', '/PID', str(server.pid), '/T', '/F'], capture_output=True, creationflags=0x08000000)
            server.wait(timeout=10)


if __name__ == '__main__':
    main()
