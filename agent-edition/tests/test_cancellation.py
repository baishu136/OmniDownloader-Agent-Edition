import asyncio
import json
import os
from pathlib import Path
import sys

import pytest


def alive(pid):
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return False
        code=wintypes.DWORD()
        try:return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally:kernel.CloseHandle(handle)
    try:
        status=Path(f'/proc/{pid}/stat')
        if status.exists():
            parts=status.read_text().split()
            if len(parts)>2 and parts[2]=='Z':return False
        os.kill(pid,0)
        return True
    except (ProcessLookupError,FileNotFoundError):return False


async def test_cancel_really_terminates_worker_and_child_process(service,monkeypatch):
    code="""import sys,json,subprocess,time
json.loads(sys.stdin.readline())
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
print(json.dumps({'kind':'progress','state':'DOWNLOADING','progress':1,'child_pid':child.pid}),flush=True)
time.sleep(60)
"""
    monkeypatch.setattr('omni_agent.runner.worker_command',lambda:[sys.executable,'-u','-c',code])
    child=asyncio.get_running_loop().create_future()
    task=asyncio.create_task(service.runner.call({'action':'download','url':'https://youtube.com/watch?v=test'},progress=lambda event:child.set_result(event['child_pid']) if not child.done() else None))
    pid=await asyncio.wait_for(child,timeout=10)
    assert alive(pid)
    task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    for _ in range(20):
        if not alive(pid):break
        await asyncio.sleep(.05)
    assert not alive(pid)
    assert not service.runner.processes


def test_orphan_guard_targets_only_its_own_process_group(monkeypatch):
    from omni_agent.worker import watch_parent
    import signal
    monkeypatch.setattr(signal,'SIGKILL',9,raising=False)
    calls=[]
    monkeypatch.setattr(os,'getppid',lambda:999)
    monkeypatch.setattr(os,'getpid',lambda:123)
    monkeypatch.setattr(os,'getpgrp',lambda:123,raising=False)
    def kill(group,signal):
        calls.append(group)
        raise RuntimeError('guard stopped test fixture')
    monkeypatch.setattr(os,'killpg',kill,raising=False)
    with pytest.raises(RuntimeError):watch_parent(100)
    assert calls==[123]


async def test_shutdown_marks_inflight_job_as_restart_interrupted_failed(tmp_path, monkeypatch):
    from omni_agent.service import DownloadOptions, Service

    started = asyncio.Event()

    class BlockingRunner:
        def __init__(self, config, store):
            pass

        async def call(self, payload, identifier=None, progress=None):
            started.set()
            await asyncio.sleep(30)
            return {'paths': []}

        async def close(self):
            pass

    monkeypatch.setattr('omni_agent.service.Service.check_url', lambda self, u: (u, False, False))
    svc1 = Service(tmp_path, runner_factory=BlockingRunner)
    await svc1.start()
    try:
        job = await svc1.download('agent', 'https://youtube.com/watch?v=restart-test', DownloadOptions(), request_key='shutdown-check')
        await asyncio.wait_for(started.wait(), timeout=5)
    finally:
        await svc1.close()

    svc2 = Service(tmp_path, runner_factory=BlockingRunner)
    await svc2.start()
    try:
        persisted = svc2.store.job(job['id'])
        assert persisted['state'] == 'FAILED'
        assert persisted['error'] == 'RESTART_INTERRUPTED'
    finally:
        await svc2.close()
