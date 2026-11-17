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
    row = ready_task(service, 'agent')
    first = await service.deliver('agent', row['id'], 'telegram', '123', 'document', 'same')
    second = await service.deliver('agent', row['id'], 'telegram', '123', 'document', 'same')
    assert first['id'] == second['id']
    assert service.store.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == 1


async def test_onebot_service_honors_qq_message_and_forward_collection_settings(service, monkeypatch):
    from PIL import Image
    from omni_agent.delivery import OneBotAdapter

    ob_cfg = BotConfig(
        adapter='onebot',
        enabled=True,
        base_url='http://127.0.0.1:3000',
        token='mock-ob',
        allowed_targets=['group:100'],
        shared_root=str(service.config.cache),
        remote_root='/shared',
    )
    service.vault.put('bot-onebot', ob_cfg.model_dump_json())
    service.update_settings({'qq_image_mode': 'message', 'qq_forward_collection': True})

    row, _ = service.store.create_job('agent', {'url': 'https://x.com/u/status/1'}, 'fwd-key', 'fwd-req')
    p1 = service.config.cache / 'jobs' / row['id'] / 'output' / 'a.png'
    p2 = service.config.cache / 'jobs' / row['id'] / 'output' / 'b.png'
    p1.parent.mkdir(parents=True)
    Image.new('RGB', (4, 4), 'red').save(p1)
    Image.new('RGB', (4, 4), 'green').save(p2)
    service.store.finish(row['id'], [p1, p2])

    actions = []

    def handler(request):
        actions.append(request.url.path)
        if request.url.path == '/get_version_info':
            data = {'app_name': 'NapCat', 'app_version': '4.1'}
        elif request.url.path == '/get_status':
            data = {'online': True}
        elif request.url.path == '/get_login_info':
            data = {'user_id': 999, 'nickname': 'Bot'}
        elif request.url.path == '/send_group_forward_msg':
            data = {'message_id': 777}
        else:
            data = {}
        return httpx.Response(200, json={'status': 'ok', 'retcode': 0, 'data': data})

    monkeypatch.setattr('omni_agent.service.make_adapter', lambda cfg: OneBotAdapter(cfg, httpx.MockTransport(handler)))
    delivery = await service.deliver('agent', row['id'], 'onebot', 'group:100', 'auto', 'fwd-send')
    await service.run_delivery(service.store.delivery(delivery['id']))

    assert '/send_group_forward_msg' in actions
    assert '/upload_group_file' not in actions
    assert service.store.delivery(delivery['id'])['state'] == 'SENT'
    assert service.store.job(row['id'])['state'] == 'CLEANED'
    assert not p1.exists() and not p2.exists()
