import httpx
import pytest

from omni_agent.delivery import BotConfig, OneBotAdapter, TelegramAdapter
from omni_agent.errors import AgentError


def telegram(**kwargs):
    return BotConfig(adapter='telegram',enabled=True,base_url='https://api.telegram.org',token='secret',allowed_targets=['123'],**kwargs)


@pytest.fixture
def media(tmp_path):
    path=tmp_path/'media.mp4'
    path.write_bytes(b'media')
    return path


@pytest.mark.parametrize('kind,method',[('video','sendVideo'),('audio','sendAudio'),('photo','sendPhoto'),('document','sendDocument'),('auto','sendVideo')])
async def test_telegram_multipart_ack(media,kind,method):
    async def handler(request):
        assert request.url.path.endswith('/'+method)
        assert 'multipart/form-data' in request.headers['content-type']
        assert b'media' in await request.aread()
        return httpx.Response(200,json={'ok':True,'result':{'message_id':42,'chat':{'id':123}}})
    result=await TelegramAdapter(telegram(),httpx.MockTransport(handler)).send(media,'123',kind)
    assert result['message_id']==42


async def test_telegram_rejection_preserves_file(media):
    bot=TelegramAdapter(telegram(),httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':False,'description':'token=secret'})))
    with pytest.raises(AgentError) as error:await bot.send(media,'123','document')
    assert error.value.code=='BOT_REJECTED'
    assert media.exists() and 'secret' not in error.value.message


@pytest.mark.parametrize('result',[{'message_id':2},{'message_id':2,'chat':{'id':999}},{'chat':{'id':123}}])
async def test_telegram_incomplete_receipt_is_uncertain(media,result):
    bot=TelegramAdapter(telegram(),httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':True,'result':result})))
    with pytest.raises(AgentError) as error:await bot.send(media,'123','document')
    assert error.value.code=='DELIVERY_UNCERTAIN'


async def test_timeout_is_not_blindly_retried(media):
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('sensitive URL',request=request)
    with pytest.raises(AgentError) as error:await TelegramAdapter(telegram(),httpx.MockTransport(handler)).send(media,'123','document')
    assert error.value.code=='DELIVERY_UNCERTAIN' and len(calls)==1


async def test_connect_failure_has_bounded_retries(media):
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError('secret',request=request)
    with pytest.raises(AgentError) as error:await TelegramAdapter(telegram(),httpx.MockTransport(handler)).send(media,'123','document')
    assert error.value.code=='BOT_UNREACHABLE' and len(calls)==3


def test_limits_are_transport_and_method_specific():
    cloud=TelegramAdapter(telegram(max_upload_bytes=2000*1024**2))
    local=TelegramAdapter(BotConfig(adapter='telegram',enabled=True,base_url='http://localhost:8081',token='secret',allowed_targets=['123'],local_api=True,max_upload_bytes=2000*1024**2))
    assert cloud.limit('document')==50*1024**2
    assert local.limit('document')==2000*1024**2
    assert local.limit('photo')==10*1024**2


def onebot(root):
    return BotConfig(adapter='onebot',enabled=True,base_url='http://localhost:3000',token='secret',allowed_targets=['group:1','private:2'],shared_root=str(root),remote_root='/shared')


@pytest.mark.parametrize('target,action',[('group:1','upload_group_file'),('private:2','upload_private_file')])
async def test_onebot_real_actions_and_receipts(media,target,action):
    calls=[]
    def handler(request):
        import json
        calls.append(request.url.path)
        assert request.headers['authorization']=='Bearer secret'
        value={'app_name':'NapCat'} if request.url.path=='/get_version_info' else {'online':True} if request.url.path=='/get_status' else {'file_id':'f1'} if request.url.path.startswith('/upload_') else {}
        if request.url.path.startswith('/upload_'):
            payload=json.loads(request.content)
            assert payload['file']=='/shared/media.mp4'
            assert ('group_id' if target.startswith('group') else 'user_id') in payload
        return httpx.Response(200,json={'status':'ok','retcode':0,'data':value})
    receipt=await OneBotAdapter(onebot(media.parent),httpx.MockTransport(handler)).send(media,target,'document')
    assert receipt['file_id']=='f1' and '/'+action in calls


async def test_onebot_missing_file_id_cannot_trigger_cleanup(media):
    def handler(request):
        value={'app_name':'NapCat'} if request.url.path=='/get_version_info' else {'online':True} if request.url.path=='/get_status' else {}
        return httpx.Response(200,json={'status':'ok','retcode':0,'data':value})
    with pytest.raises(AgentError) as error:await OneBotAdapter(onebot(media.parent),httpx.MockTransport(handler)).send(media,'group:1')
    assert error.value.code=='DELIVERY_UNCERTAIN' and media.exists()


async def test_onebot_unimplemented_action_is_rejected(media):
    bot=OneBotAdapter(onebot(media.parent),httpx.MockTransport(lambda r:httpx.Response(200,json={'status':'failed','retcode':1404})))
    with pytest.raises(AgentError) as error:await bot.send(media,'group:1')
    assert error.value.code=='CAPABILITY_UNAVAILABLE'


async def test_target_allowlist(media):
    with pytest.raises(AgentError) as error:await TelegramAdapter(telegram()).send(media,'999','document')
    assert error.value.code=='TARGET_DENIED'
