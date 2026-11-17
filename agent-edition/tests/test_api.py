import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from conftest import ready_task
from omni_agent.api import create_app, invoke
from omni_agent.errors import AgentError
from omni_agent.service import DownloadOptions


class FakeRunner:
    """Fixture for lifecycle tests. No real platform success is claimed."""
    def __init__(self, config, store):self.config,self.store=config,store
    async def call(self, payload, identifier=None, progress=None):
        if payload['action']=='analyze':return {'platform':'fixture','title':'mock media','media_type':'video','options':['VIDEO_WITH_AUDIO']}
        await asyncio.sleep(.05)
        from PIL import Image
        path=self.config.cache/'jobs'/identifier/'output'/'fixture.png'
        path.parent.mkdir(parents=True)
        Image.new('RGB',(2,2)).save(path)
        return {'paths':[str(path)]}
    async def close(self):pass


def headers(service,admin=False):return {'Authorization':'Bearer '+service.config.tokens['admin' if admin else 'agent']}


def test_admin_api_auth_save_and_secrets(service):
    with TestClient(create_app(service)) as client:
        assert client.get('/health').json()=={'ok':True}
        assert client.get('/api/admin/config').status_code==401
        assert client.get('/api/admin/config',headers=headers(service)).status_code==401
        original=client.get('/api/admin/config',headers=headers(service,True)).json()
        original['video']=False
        assert client.put('/api/admin/config',headers=headers(service,True),json=original).status_code==200
        assert json.loads(service.config.path.read_text())['video'] is False
        result=client.post('/api/tools/download_media',headers=headers(service),json={'url':'https://youtube.com/watch?v=test'}).json()
        assert result['error']['code']=='FEATURE_DISABLED'
        assert 'token' not in client.get('/api/admin/config',headers=headers(service,True)).text


def test_origin_host_and_body_boundary(service):
    with TestClient(create_app(service)) as client:
        assert client.get('/api/admin/config',headers=headers(service,True)|{'Origin':'https://evil.example'}).status_code==403
        assert client.get('/health',headers={'Host':'evil.example'}).status_code==403
        assert client.post('/api/tools/list_tasks',headers=headers(service),content=b'x'*(2*1024**2+1)).status_code==413


def test_identity_isolation_and_no_raw_paths(service):
    job=ready_task(service,'alice')
    with TestClient(create_app(service)) as client:
        identity=client.post('/api/admin/identities',headers=headers(service,True),json={'name':'alice','targets':['123']}).json()
        alice={'Authorization':'Bearer '+identity['token']}
        file_id=service.store.files(job['id'])[0]['id']
        assert client.post('/api/tools/get_task_status',headers=headers(service),json={'task_id':job['id']}).json()['error']['code']=='NOT_FOUND'
        reference=client.post('/api/tools/get_media_file',headers=alice,json={'file_id':file_id}).json()
        assert reference['ok'] and 'path' not in reference['result']
        assert client.get('/api/files/'+file_id,headers=alice).content.startswith(b'\x89PNG')
        assert client.get('/api/files/'+file_id,headers=headers(service)).status_code==400


@pytest.mark.asyncio
async def test_parameter_validation_and_errors(service):
    result=await invoke(service,'agent','download_collection',{'url':'https://youtube.com/playlist?list=test','count':0})
    assert result['error']['code']=='INVALID_PARAMETER'
    result=await invoke(service,'agent','get_media_file',{'file_id':'../../etc/passwd'})
    assert result['error']['code']=='NOT_FOUND'
    assert (await invoke(service,'agent','not_a_tool',{}))['error']['code']=='UNKNOWN_TOOL'


async def test_admin_limits_and_confirmation(service):
    for feature,typ in [('video','VIDEO_ONLY'),('audio','AUDIO_ONLY'),('images','IMAGE_ZIP'),('covers','COVER')]:
        setattr(service.config.settings,feature,False)
        with pytest.raises(AgentError):service.resolve_options(DownloadOptions(use_defaults=False,download_type=typ))
        setattr(service.config.settings,feature,True)
    with pytest.raises(AgentError) as error:await service.download('alice','https://youtube.com/playlist?list=test',DownloadOptions(),collection=True,count=6)
    assert error.value.code=='CONFIRMATION_REQUIRED'
    approval=error.value.details['approval_id']
    service.store.db.execute('UPDATE approvals SET approved=1 WHERE id=?',(approval,))
    result=await service.download('alice','https://youtube.com/playlist?list=test',DownloadOptions(),collection=True,count=6,approval_id=approval)
    assert result['state']=='QUEUED'
    with pytest.raises(AgentError):await service.download('bob','https://youtube.com/playlist?list=test',DownloadOptions(),collection=True,count=6,approval_id=approval)


async def test_async_download_status_and_cancel(service):
    job=await service.download('alice','https://youtube.com/watch?v=test',DownloadOptions(),request_key='same')
    again=await service.download('alice','https://youtube.com/watch?v=test',DownloadOptions(),request_key='same')
    assert job['id']==again['id'] and job['state']=='QUEUED'
    cancelled=await service.cancel('alice',job['id'])
    assert cancelled['state']=='CANCELLED'


def test_async_core_completion_in_background(service):
    service.runner=FakeRunner(service.config,service.store)
    with TestClient(create_app(service)) as client:
        result=client.post('/api/tools/download_media',headers=headers(service),json={'url':'https://youtube.com/watch?v=test'}).json()
        assert result['ok'] and result['result']['state']=='QUEUED'
        identifier=result['result']['id']
        import time
        for _ in range(100):
            status=client.post('/api/tools/get_task_status',headers=headers(service),json={'task_id':identifier}).json()['result']
            if status['state']=='READY':break
            time.sleep(.02)
        assert status['state']=='READY' and status['file_count']==1


def test_sessions_are_encrypted_and_never_echoed(service):
    with TestClient(create_app(service)) as client:
        assert client.post('/api/admin/sessions',headers=headers(service,True),json={'platform':'哔哩哔哩','text':'SESSDATA=super-secret-session'}).status_code==200
        public=client.get('/api/admin/sessions',headers=headers(service,True)).text
        assert 'super-secret-session' not in public
        assert all(b'super-secret-session' not in p.read_bytes() for p in service.vault.directory.rglob('*.bin'))
        assert client.delete('/api/admin/sessions/哔哩哔哩',headers=headers(service,True)).status_code==200


async def test_policy_change_before_dispatch_is_enforced(service):
    job=await service.download('alice','https://youtube.com/watch?v=test',DownloadOptions())
    service.config.settings.video=False
    await service.run_job(service.store.job(job['id']))
    assert service.store.job(job['id'])['error']=='FEATURE_DISABLED'


async def test_shared_active_cancellation_does_not_kill_other_users(service):
    first=await service.download('alice','https://youtube.com/watch?v=test',DownloadOptions())
    second=await service.download('bob','https://youtube.com/watch?v=test',DownloadOptions())
    assert (await service.cancel('alice',first['id']))['state']=='CANCELLED'
    assert service.store.job(second['id'])['state']=='QUEUED'
    assert (await service.cancel('bob',second['id']))['state']=='CANCELLED'


async def test_single_zip_confirmation_round_trip_uses_public_tool_schema(service):
    arguments={'url':'https://www.xiaohongshu.com/explore/test','options':{'use_defaults':False,'download_type':'IMAGE_ZIP'},'request_key':'zip-one'}
    first=await invoke(service,'alice','download_media',arguments)
    second=await invoke(service,'alice','download_media',arguments)
    assert first['error']['code']=='CONFIRMATION_REQUIRED'
    approval=first['error']['approval_id']
    assert second['error']['approval_id']==approval
    service.store.db.execute('UPDATE approvals SET approved=1 WHERE id=?',(approval,))
    result=await invoke(service,'alice','download_media',dict(arguments,approval_id=approval))
    assert result['ok'] and result['result']['state']=='QUEUED'
