import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

from .errors import AgentError
from .security import controlled


def worker_command():
    if getattr(sys, 'frozen', False):
        # PyInstaller windowed bootloaders set stdin/stdout to None even when
        # redirected. The bundled console bootloader provides real worker pipes.
        executable = Path(sys.executable).with_name('OmniDownloaderAgentMCP.exe')
        return [str(executable), '--worker']
    return [sys.executable, '-m', 'omni_agent', '--worker']


async def kill_tree(process):
    if os.name == 'nt':
        if process.returncode is not None: return
        killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=0x08000000)
        await killer.wait()
    else:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    try: process.kill()
    except ProcessLookupError: pass
    await process.wait()


class Runner:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.processes = {}

    async def call(self, payload, identifier=None, progress=None):
        identifier = identifier or uuid.uuid4().hex
        settings = self.config.settings
        state = controlled(self.config.cache, self.config.cache / 'jobs' / identifier)
        state.mkdir(parents=True, exist_ok=True)
        request = dict(payload, data=str(self.config.directory), state=str(state), proxy=settings.proxy,
            max_file_bytes=settings.max_file_bytes, max_items=settings.max_profile if payload.get('profile') else settings.max_collection,
            allow_profiles=settings.profiles, allow_images=settings.images, allow_collections=settings.collections)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        env['PYTHONIOENCODING'] = 'utf-8'
        env['OMNI_AGENT_WORKER_PARENT'] = str(os.getpid())
        # Credentials remain in encrypted storage, never subprocess argv/logs.
        for key in ('OMNI_AGENT_ADMIN_TOKEN', 'OMNI_AGENT_TOKEN'):
            env.pop(key, None)
        kwargs = {'creationflags': 0x08000000} if os.name == 'nt' else {'start_new_session': True}
        process = await asyncio.create_subprocess_exec(*worker_command(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env, limit=2 * 1024**2, **kwargs)
        from .process_guard import bind_job, close_job
        try: job_handle = bind_job(process.pid)
        except OSError:
            await kill_tree(process)
            raise AgentError('PROCESS_ISOLATION_FAILED','无法建立工作进程生命周期隔离') from None
        self.processes[identifier] = process
        started = time.monotonic()
        timeout = min(settings.task_timeout,120) if payload['action'] in ('analyze','verify_session') else settings.task_timeout
        read = None
        try:
            process.stdin.write(json.dumps(request).encode() + b'\n')
            await process.stdin.drain()
            process.stdin.close()
            result = None
            while True:
                read = asyncio.create_task(process.stdout.readline())
                while not read.done():
                    await asyncio.wait({read}, timeout=.25)
                    if time.monotonic() - started > timeout:
                        read.cancel()
                        raise AgentError('TIMEOUT', '媒体任务超过管理员时限')
                    if self.store.usage() > settings.max_cache_bytes:
                        read.cancel()
                        raise AgentError('CACHE_FULL', '缓存空间达到管理员限制')
                    oversized = any(p.stat().st_size > settings.max_file_bytes for p in state.rglob('*') if p.is_file())
                    if oversized:
                        read.cancel()
                        raise AgentError('FILE_TOO_LARGE', '文件达到管理员大小上限')
                line = await read
                if not line: break
                event = json.loads(line)
                if event['kind'] == 'error':
                    raise AgentError(
                        event['code'],
                        event['message'],
                        stage=event.get('stage'),
                        retryable=event.get('retryable'),
                        suggested_action=event.get('suggested_action'),
                    )
                if event['kind'] in ('progress', 'metadata') and progress: progress(event)
                if event['kind'] == 'result': result = event['result']
            await process.wait()
            if process.returncode or result is None:
                raise AgentError('WORKER_FAILED', '下载核心进程异常退出')
            return result
        finally:
            if read and not read.done():
                read.cancel()
                await asyncio.gather(read, return_exceptions=True)
            close_job(job_handle)
            await kill_tree(process)
            self.processes.pop(identifier, None)
            if payload['action'] in ('analyze','verify_session'):
                import shutil
                for child in state.rglob('*'): controlled(state,child)
                shutil.rmtree(state)

    async def close(self):
        await asyncio.gather(*(kill_tree(p) for p in list(self.processes.values())), return_exceptions=True)
