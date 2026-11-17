import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest


@pytest.fixture(scope='module')
def live_server(tmp_path_factory):
    data=tmp_path_factory.mktemp('agent-live')
    root=Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=dict(os.environ,PYTHONPATH=str(root),OMNI_AGENT_DATA=str(data),OMNI_AGENT_ADMIN_TOKEN='admin-'+ 'a'*32,OMNI_AGENT_TOKEN='agent-'+'b'*32)
    log=open(data/'server-test.log','w')
    proc=subprocess.Popen([sys.executable,'-m','omni_agent','--serve','--no-browser','--port',str(port)],env=env,stdout=log,stderr=log)
    url=f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            if proc.poll() is not None:pytest.fail('Live service exited: '+(data/'server-test.log').read_text())
            try:
                with httpx.Client(trust_env=False) as client:
                    if client.get(url+'/health').status_code==200:break
            except httpx.HTTPError:time.sleep(.1)
        else:pytest.fail('Live service startup timeout')
        yield {'url':url,'data':data,'env':env,'process':proc}
    finally:
        proc.terminate()
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        log.close()


async def test_streamable_http_initialize_discovery_and_structured_results(live_server):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+live_server['env']['OMNI_AGENT_TOKEN']},trust_env=False) as client:
        async with streamable_http_client(live_server['url']+'/mcp',http_client=client) as streams:
            async with ClientSession(streams[0],streams[1]) as session:
                initialized=await session.initialize()
                assert initialized.server_info.name=='OmniDownloader Agent Edition'
                tools=await session.list_tools()
                names={tool.name for tool in tools.tools}
                assert {'analyze_media','download_media','download_collection','get_task_status','list_tasks','cancel_task','get_media_file','get_service_capabilities','deliver_media','get_delivery_status'}==names
                result=await session.call_tool('get_service_capabilities',{})
                assert result.structured_content['ok'] is True
                result=await session.call_tool('get_task_status',{'task_id':'missing'})
                assert result.structured_content['error']['code']=='NOT_FOUND'
                assert result.is_error
                result=await session.call_tool('get_task_status',{'task_id':{'token':'must-not-be-echoed'}})
                assert result.is_error and result.structured_content['error']['code']=='INVALID_PARAMETER'
                assert 'must-not-be-echoed' not in str(result)
                result=await session.call_tool('download_media',{'url':'http://127.0.0.1/private'})
                assert result.structured_content['ok'] is False


async def test_stdio_initialize_discovery_and_api_bridge(live_server):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env=dict(live_server['env'],OMNI_AGENT_SERVER=live_server['url'])
    params=StdioServerParameters(command=sys.executable,args=['-m','omni_agent','--stdio'],env=env)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools)==10
            result=await session.call_tool('list_tasks',{})
            assert result.structured_content['ok'] is True


def test_http_mcp_requires_authentication(live_server):
    with httpx.Client(trust_env=False) as client:
        response=client.post(live_server['url']+'/mcp',json={'jsonrpc':'2.0','method':'tools/list','id':1})
        assert response.status_code==401


async def test_actual_worker_starts_and_rejects_unsupported_url(tmp_path):
    from omni_agent.service import Service
    from omni_agent.errors import AgentError
    service=Service(tmp_path)
    with pytest.raises(AgentError) as error:
        await service.runner.call({'action':'analyze','url':'http://127.0.0.1/secret'})
    assert error.value.code in ('UNSUPPORTED_PLATFORM','SSRF_BLOCKED')
    assert not service.runner.processes
    service.store.db.close()
