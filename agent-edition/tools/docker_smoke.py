"""Run INSIDE the actual Docker container; no platform credentials required."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-persistence', action='store_true')
    args = parser.parse_args()
    data = Path(os.environ['OMNI_AGENT_DATA'])
    marker = data / 'docker-smoke.json'
    with httpx.Client(base_url='http://127.0.0.1:58080', trust_env=False) as client:
        assert client.get('/health').status_code == 200
        assert not (data / 'auth.json').exists()
        settings = client.get('/api/admin/config').json()
        if args.check_persistence:
            assert settings['default_height'] == 720
            assert marker.exists()
            state = json.loads(marker.read_text())
            job = client.post('/api/tools/get_task_status', json={'task_id': state['task_id']}).json()
            assert job['ok'] and job['result']['state'] == 'FAILED'
            cached = client.post('/api/tools/get_task_status', json={'task_id': state['ready_task']}).json()
            assert cached['ok'] and cached['result']['state'] == 'READY'
            assert client.get('/api/files/' + state['file_id']).content.startswith(b'\x89PNG')
            mock_delivery(client, state)
        else:
            settings['default_height'] = 720
            assert client.put('/api/admin/config', json=settings).status_code == 200
            response = client.post('/api/tools/download_media', json={'url': 'https://youtube.com/watch?v=agent-smoke-invalid', 'request_key': 'docker-recovery'}).json()
            assert response['ok']
            fixture = seed_cache_fixture(data)
            marker.write_text(json.dumps({'task_id': response['result']['id'], **fixture}))
        result = client.post('/api/tools/get_service_capabilities', json={}).json()
        assert result['ok'] and result['result']['ffmpeg']
        assert result['result']['deno']
    asyncio.run(mcp_check())
    with tempfile.TemporaryDirectory() as state_dir:
        request = {'action': 'verify_session', 'platform': 'YouTube', 'state': state_dir, 'data': str(data)}
        proc = subprocess.run(['python', '-m', 'omni_agent', '--worker'], input=json.dumps(request), text=True, capture_output=True, timeout=30)
        assert json.loads(proc.stdout.splitlines()[-1])['code'] == 'CAPABILITY_UNAVAILABLE'
    print('Docker service, MCP, core worker and persistence checks passed; delivery used ONLY a local mock API and generated PNG fixture.')


def seed_cache_fixture(directory):
    from omni_agent.service import Service
    from PIL import Image
    service = Service(directory)
    identifier = uuid.uuid4().hex
    now = time.time()
    with service.store.transaction():
        service.store.db.execute(
            'INSERT INTO jobs(id,owner,request_key,cache_key,payload,state,created,updated) VALUES(?,?,?,?,?,?,?,?)',
            (identifier, 'agent', 'docker-persistence-fixture', 'docker-fixture', service.vault.protect(b'{}'), 'READY', now, now),
        )
    path = service.config.cache / 'jobs' / identifier / 'output' / 'fixture.png'
    path.parent.mkdir(parents=True)
    Image.new('RGB', (8, 8), 'blue').save(path)
    service.store.finish(identifier, [path])
    file = service.store.files(identifier)[0]
    service.store.db.close()
    return {'ready_task': identifier, 'file_id': file['id']}


def mock_delivery(client, state):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            result = json.dumps({'ok': True, 'result': {'message_id': 101, 'chat': {'id': 123}}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, *args):
            pass

    api = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=api.serve_forever, daemon=True)
    thread.start()
    try:
        config = {'adapter': 'telegram', 'enabled': True, 'local_api': True, 'base_url': f'http://127.0.0.1:{api.server_port}',
                  'token': 'local-mock-only', 'allowed_targets': ['123']}
        response = client.put('/api/admin/bots', json=config)
        assert response.status_code == 200, response.text
        result = client.post('/api/tools/deliver_media', json={'task_id': state['ready_task'], 'adapter': 'telegram', 'target': '123', 'request_key': 'docker-mock-send', 'kind': 'document'}).json()
        assert result['ok'], result
        delivery_id = result['result']['id']
        for _ in range(100):
            delivery = client.post('/api/tools/get_delivery_status', json={'delivery_id': delivery_id}).json()
            if delivery['result']['state'] == 'SENT':
                break
            if delivery['result']['state'] in ('FAILED', 'UNCERTAIN'):
                raise AssertionError(delivery)
            time.sleep(.1)
        else:
            raise AssertionError('Mock delivery timeout')
        assert client.get('/api/files/' + state['file_id']).status_code == 400
    finally:
        api.shutdown()
        api.server_close()
        thread.join(timeout=2)


async def mcp_check():
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async with httpx2.AsyncClient(trust_env=False) as client:
        async with streamable_http_client('http://127.0.0.1:58080/mcp', http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 23


if __name__ == '__main__':
    main()
