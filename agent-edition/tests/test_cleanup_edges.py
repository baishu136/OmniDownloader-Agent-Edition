from pathlib import Path

import httpx
import pytest

from conftest import ready_task
from omni_agent.config import Config, Settings
from omni_agent.delivery import BotConfig, TelegramAdapter
from omni_agent.errors import AgentError


async def test_cleanup_failure_never_changes_confirmed_sent_state(service,monkeypatch):
    config=BotConfig(adapter='telegram',enabled=True,base_url='https://api.telegram.org',token='mock-token',allowed_targets=['123'])
    service.vault.put('bot-telegram',config.model_dump_json())
    job=ready_task(service,'agent')
    path=Path(service.store.files(job['id'])[0]['path'])
    transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':True,'result':{'message_id':22,'chat':{'id':123}}}))
    monkeypatch.setattr('omni_agent.service.make_adapter',lambda cfg:TelegramAdapter(cfg,transport))
    response=await service.deliver('agent',job['id'],'telegram','123','document','cleanup-failure')
    def fail_cleanup():raise PermissionError('File temporarily locked')
    monkeypatch.setattr(service.store,'cleanup',fail_cleanup)
    await service.run_delivery(service.store.delivery(response['id']))
    assert service.store.delivery(response['id'])['state']=='SENT'
    assert service.store.job(job['id'])['state']=='SENT'
    assert service.last_cleanup_error=='CLEANUP_DEFERRED' and path.exists()


def test_empty_cache_directories_do_not_prevent_admin_path_change(service,tmp_path):
    (service.config.cache/'jobs').mkdir()
    settings=service.config.settings.model_copy(update={'cache_dir':str(tmp_path/'new-cache')})
    service.config.save(settings)
    assert service.config.cache==tmp_path/'new-cache'/'omni-agent-managed'
    assert (service.config.cache/'.owner').is_file()


def test_cache_cannot_be_shared_by_separate_state_directories(tmp_path):
    from omni_agent.config import atomic_write
    first=tmp_path/'one';second=tmp_path/'two'
    first.mkdir();second.mkdir()
    settings=Settings(cache_dir=str(tmp_path/'shared-cache'))
    for directory in (first,second):atomic_write(directory/'settings.json',settings.model_dump_json().encode())
    Config(first)
    with pytest.raises(ValueError,match='另一个'):Config(second)


async def test_disabled_bot_probe_returns_clear_error():
    with pytest.raises(AgentError) as error:await TelegramAdapter(BotConfig(adapter='telegram')).probe()
    assert error.value.code=='BOT_DISABLED'
