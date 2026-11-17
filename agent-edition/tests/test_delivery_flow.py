from pathlib import Path

import httpx
import pytest

from conftest import ready_task
from omni_agent.delivery import BotConfig, TelegramAdapter


def configure(service):
    config=BotConfig(adapter='telegram',enabled=True,base_url='https://api.telegram.org',token='mock-token',allowed_targets=['123'])
    service.vault.put('bot-telegram',config.model_dump_json())


async def test_service_success_upload_receipt_and_cleanup(service,monkeypatch):
    configure(service)
    row=ready_task(service,'agent')
    path=Path(service.store.files(row['id'])[0]['path'])
    transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':True,'result':{'message_id':101,'chat':{'id':123}}}))
    monkeypatch.setattr('omni_agent.service.make_adapter',lambda config:TelegramAdapter(config,transport))
    response=await service.deliver('agent',row['id'],'telegram','123','document','send')
    await service.run_delivery(service.store.delivery(response['id']))
    assert service.store.delivery(response['id'])['state']=='SENT'
    assert not path.exists()
    assert service.store.job(row['id'])['state']=='CLEANED'


async def test_service_failure_keeps_file_for_retry(service,monkeypatch):
    configure(service)
    row=ready_task(service,'agent')
    path=Path(service.store.files(row['id'])[0]['path'])
    rejected=httpx.MockTransport(lambda r:httpx.Response(403,json={'ok':False}))
    monkeypatch.setattr('omni_agent.service.make_adapter',lambda config:TelegramAdapter(config,rejected))
    response=await service.deliver('agent',row['id'],'telegram','123','document','send')
    await service.run_delivery(service.store.delivery(response['id']))
    assert service.store.delivery(response['id'])['state']=='FAILED' and path.exists()
    service.retry_delivery(response['id'])
    accepted=httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':True,'result':{'message_id':102,'chat':{'id':123}}}))
    monkeypatch.setattr('omni_agent.service.make_adapter',lambda config:TelegramAdapter(config,accepted))
    await service.run_delivery(service.store.delivery(response['id']))
    assert service.store.delivery(response['id'])['state']=='SENT' and not path.exists()


async def test_auto_delete_off_and_manual_cleanup(service,monkeypatch):
    configure(service)
    service.config.settings.auto_delete=False
    job=ready_task(service,'agent')
    path=Path(service.store.files(job['id'])[0]['path'])
    transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':True,'result':{'message_id':101,'chat':{'id':123}}}))
    monkeypatch.setattr('omni_agent.service.make_adapter',lambda config:TelegramAdapter(config,transport))
    response=await service.deliver('agent',job['id'],'telegram','123','document','send')
    await service.run_delivery(service.store.delivery(response['id']))
    assert path.exists()
    assert service.store.cleanup()['files_removed']==1


async def test_duplicate_delivery_request_does_not_send_twice(service):
    configure(service)
    row=ready_task(service,'agent')
    first=await service.deliver('agent',row['id'],'telegram','123','document','same')
    second=await service.deliver('agent',row['id'],'telegram','123','document','same')
    assert first['id']==second['id']
    assert service.store.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]==1
