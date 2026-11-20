import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import quote, urlsplit
import webbrowser


def base_command():
    return [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-m', 'omni_agent']


def stdio(server_url, directory):
    import httpx
    from .errors import AgentError
    from .mcp_server import create_mcp

    parts = urlsplit(server_url)
    if parts.scheme not in ('http', 'https') or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('MCP桥接地址无效')
    if parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('远程stdio桥必须使用HTTPS')

    base_url = server_url.rstrip('/')
    bridge_id = f'mcp-stdio-{os.getpid()}'
    bridge_state = {'name': 'MCP stdio 桥接', 'client_name': '', 'client_version': ''}
    stop_heartbeat = threading.Event()

    def ready():
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                return client.get(base_url + '/health').status_code == 200
        except httpx.HTTPError:
            return False

    if not ready() and parts.hostname in ('127.0.0.1', 'localhost'):
        flags = {'creationflags': 0x08000000 | 0x00000200} if os.name == 'nt' else {'start_new_session': True}
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        command = base_command() + ['--serve', '--no-browser', '--host', '127.0.0.1', '--port', str(parts.port or 58080)]
        if directory:
            command += ['--data', directory]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env, **flags)
        deadline = time.monotonic() + 20
        while not ready() and time.monotonic() < deadline:
            time.sleep(.2)

    def bridge_headers():
        return {
            'X-Omni-Bridge-Id': bridge_id,
            'X-Omni-Bridge-Type': 'mcp_stdio',
            'X-Omni-Bridge-Name': quote(bridge_state['client_name'] or 'MCP stdio', safe=' -_.'),
            'X-Omni-Bridge-Client': quote(bridge_state['client_name'], safe=' -_.'),
            'X-Omni-Bridge-Version': quote(bridge_state['client_version'], safe=' -_.'),
            'X-Omni-Bridge-Pid': str(os.getpid()),
        }

    def post_bridge(endpoint: str):
        try:
            payload = {
                'bridge_id': bridge_id,
                'kind': 'mcp_stdio',
                'name': bridge_state['client_name'] or bridge_state['name'],
                'transport': 'stdio',
                'client_name': bridge_state['client_name'],
                'client_version': bridge_state['client_version'],
                'pid': os.getpid(),
            } if endpoint != 'unregister' else {'bridge_id': bridge_id}
            with httpx.Client(trust_env=False, timeout=2) as client:
                client.post(f'{base_url}/api/bridges/{endpoint}', json=payload)
        except Exception:
            pass

    def heartbeat_loop():
        post_bridge('register')
        while not stop_heartbeat.wait(30):
            post_bridge('heartbeat')

    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()

    async def on_mcp_session(client_name: str, client_version: str):
        changed = (client_name != bridge_state['client_name']) or (client_version != bridge_state['client_version'])
        bridge_state['client_name'] = client_name
        bridge_state['client_version'] = client_version
        if changed:
            await asyncio.to_thread(post_bridge, 'register')

    async def invoke(name, arguments):
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=1900, follow_redirects=False) as client:
                response = await client.post(base_url + '/api/tools/' + name, json=arguments, headers=bridge_headers())
                if response.status_code != 200:
                    return AgentError('SERVICE_REJECTED', '服务接口调用失败').result()
                return response.json()
        except httpx.HTTPError:
            return AgentError('SERVICE_UNAVAILABLE', '无法连接 Agent Edition 服务').result()

    # stdout 专用于 MCP JSON-RPC 通讯，不能打印任何杂散日志
    try:
        import asyncio
        create_mcp(invoke, on_session=on_mcp_session).run('stdio')
    finally:
        stop_heartbeat.set()
        post_bridge('unregister')


def serve(args):
    import httpx
    import uvicorn
    from .api import create_app
    from .service import Service

    if not args.no_browser and args.host in ('127.0.0.1', 'localhost'):
        try:
            with httpx.Client(trust_env=False, timeout=0.6) as client:
                if client.get(f'http://127.0.0.1:{args.port}/health').status_code == 200:
                    webbrowser.open(f'http://127.0.0.1:{args.port}/')
                    return
        except httpx.HTTPError:
            pass

    service = Service(args.data)
    service.runtime_endpoint = {'host': args.host, 'port': int(args.port)}
    first_start = not (service.config.directory / 'has-started').exists()
    config = uvicorn.Config(
        create_app(service),
        host=args.host,
        port=args.port,
        log_level='warning',
        access_log=False,
        proxy_headers=False,
        limit_concurrency=64,
        backlog=128,
    )
    server = uvicorn.Server(config)

    def open_when_ready():
        for _ in range(100):
            if server.started:
                webbrowser.open(f'http://127.0.0.1:{args.port}/')
                return
            time.sleep(.1)

    if first_start and not args.no_browser and args.host in ('127.0.0.1', 'localhost'):
        threading.Thread(target=open_when_ready, daemon=True).start()
    (service.config.directory / 'has-started').touch()
    if args.tray:
        import pystray
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (64, 64), '#0969b7')
        draw = ImageDraw.Draw(image)
        draw.line([(16, 20), (32, 40), (48, 20)], fill='white', width=6)

        def _autostart_checked(_item):
            try:
                return bool(service.get_autostart_status().get('enabled'))
            except Exception:
                return False

        def _autostart_supported(_item):
            try:
                return bool(service.get_autostart_status().get('supported'))
            except Exception:
                return False

        def _toggle_autostart(icon_obj, _item):
            try:
                current = bool(service.get_autostart_status().get('enabled'))
                service.set_autostart(not current)
                icon_obj.update_menu()
            except Exception:
                pass

        icon = pystray.Icon(
            'omni-agent',
            image,
            'OmniDownloader Agent Edition',
            menu=pystray.Menu(
                pystray.MenuItem('打开管理页面', lambda: webbrowser.open(f'http://127.0.0.1:{args.port}/'), default=True),
                pystray.MenuItem('开机自启动', _toggle_autostart, checked=_autostart_checked, enabled=_autostart_supported),
                pystray.MenuItem('安全退出', lambda: (setattr(server, 'should_exit', True), icon.stop())),
            ),
        )
        threading.Thread(target=icon.run, daemon=True).start()
    server.run()


def main():
    # PyInstaller 窗口模式启动时 stdout/stderr 为 None；stdio 桥使用单独的控制台入口
    for name in ('stdout', 'stderr'):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, 'w'))
    if '--worker' in sys.argv:
        from .worker import main as worker_main
        worker_main()
        return
    if '--pick-dir' in sys.argv:
        p = argparse.ArgumentParser()
        p.add_argument('--pick-dir', action='store_true')
        p.add_argument('--initial', default='')
        p.add_argument('--title', default='选择文件夹')
        p.add_argument('--out', required=True)
        pick_args = p.parse_args()
        from .desktop_tools import run_pick_dir_cli
        run_pick_dir_cli(pick_args.initial, pick_args.title, pick_args.out)
        return
    parser = argparse.ArgumentParser(description='OmniDownloader Agent Edition')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--stdio', action='store_true', help='Official MCP stdio bridge to the persistent service')
    modes.add_argument('--serve', action='store_true', help='Serve management HTTP and MCP at /mcp')
    parser.add_argument('--host', default=os.environ.get('OMNI_AGENT_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('OMNI_AGENT_PORT', '58080')))
    parser.add_argument('--data', default=os.environ.get('OMNI_AGENT_DATA'))
    parser.add_argument('--server', default=os.environ.get('OMNI_AGENT_SERVER', 'http://127.0.0.1:58080'))
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--tray', action='store_true')
    args = parser.parse_args()
    if args.stdio:
        stdio(args.server.rstrip('/'), args.data)
    else:
        if getattr(sys, 'frozen', False) and os.name == 'nt':
            args.tray = True
        serve(args)


if __name__ == '__main__':
    main()
