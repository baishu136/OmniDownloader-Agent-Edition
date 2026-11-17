import argparse
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser


def base_command():
    return [sys.executable] if getattr(sys,'frozen',False) else [sys.executable,'-m','omni_agent']


def stdio(server_url, directory):
    import httpx
    from .config import Config
    from .errors import AgentError
    from .mcp_server import create_mcp
    token = os.environ.get('OMNI_AGENT_TOKEN')
    if not token:
        config = Config(directory)
        token = config.tokens['agent']
    parts = urlsplit(server_url)
    if parts.scheme not in ('http','https') or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('MCP桥接地址无效')
    if parts.scheme == 'http' and parts.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('远程stdio桥必须使用HTTPS')
    def ready():
        try:
            with httpx.Client(trust_env=False,timeout=2) as client:
                return client.get(server_url + '/health').status_code == 200
        except httpx.HTTPError: return False
    if not ready() and parts.hostname in ('127.0.0.1','localhost'):
        flags = {'creationflags':0x08000000 | 0x00000200} if os.name == 'nt' else {'start_new_session':True}
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        command = base_command() + ['--serve','--no-browser','--host','127.0.0.1','--port',str(parts.port or 58080)]
        if directory: command += ['--data',directory]
        subprocess.Popen(command, stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,env=env,**flags)
        deadline = time.monotonic() + 20
        while not ready() and time.monotonic() < deadline: time.sleep(.2)
    async def invoke(name, arguments):
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=1900,follow_redirects=False) as client:
                response = await client.post(server_url + '/api/tools/' + name,
                    headers={'Authorization':'Bearer ' + token}, json=arguments)
                if response.status_code != 200: return AgentError('SERVICE_REJECTED','服务认证或接口调用失败').result()
                return response.json()
        except httpx.HTTPError: return AgentError('SERVICE_UNAVAILABLE','无法连接 Agent Edition 服务').result()
    # Stdout belongs exclusively to MCP JSON-RPC.
    create_mcp(invoke).run('stdio')


def serve(args):
    import uvicorn
    from .api import create_app
    from .service import Service
    service = Service(args.data)
    first_start = not (service.config.directory / 'has-started').exists()
    config = uvicorn.Config(create_app(service), host=args.host, port=args.port,
                            log_level='warning', access_log=False, proxy_headers=False, limit_concurrency=64, backlog=128)
    server = uvicorn.Server(config)
    def open_when_ready():
        for _ in range(100):
            if server.started:
                webbrowser.open(f'http://127.0.0.1:{args.port}/')
                return
            time.sleep(.1)
    if first_start and not args.no_browser and args.host in ('127.0.0.1','localhost'):
        threading.Thread(target=open_when_ready,daemon=True).start()
    (service.config.directory / 'has-started').touch()
    if args.tray:
        import pystray
        from PIL import Image, ImageDraw
        image = Image.new('RGB',(64,64),'#0969b7')
        draw = ImageDraw.Draw(image)
        draw.line([(16,20),(32,40),(48,20)],fill='white',width=6)
        icon = pystray.Icon('omni-agent', image, 'OmniDownloader Agent Edition',
            menu=pystray.Menu(pystray.MenuItem('打开管理页面',lambda: webbrowser.open(f'http://127.0.0.1:{args.port}/')),
                             pystray.MenuItem('安全退出',lambda: (setattr(server,'should_exit',True),icon.stop()))))
        threading.Thread(target=icon.run,daemon=True).start()
    server.run()


def main():
    # Windowed PyInstaller starts with None streams. Stdio has a separate console
    # executable; normal service startup always keeps console windows hidden.
    for name in ('stdout','stderr'):
        if getattr(sys,name) is None: setattr(sys,name,open(os.devnull,'w'))
    if '--worker' in sys.argv:
        from .worker import main as worker_main
        worker_main()
        return
    parser = argparse.ArgumentParser(description='OmniDownloader Agent Edition')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--stdio',action='store_true',help='Official MCP stdio bridge to the persistent service')
    modes.add_argument('--serve',action='store_true',help='Serve management HTTP and MCP at /mcp')
    parser.add_argument('--host',default=os.environ.get('OMNI_AGENT_HOST','127.0.0.1'))
    parser.add_argument('--port',type=int,default=int(os.environ.get('OMNI_AGENT_PORT','58080')))
    parser.add_argument('--data',default=os.environ.get('OMNI_AGENT_DATA'))
    parser.add_argument('--server',default=os.environ.get('OMNI_AGENT_SERVER','http://127.0.0.1:58080'))
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--tray',action='store_true')
    args = parser.parse_args()
    if args.stdio: stdio(args.server.rstrip('/'),args.data)
    else:
        if getattr(sys,'frozen',False) and os.name == 'nt': args.tray = True
        serve(args)


if __name__ == '__main__': main()
