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


async def test_onebot_video_and_image_message_vs_file_and_zip_modes(tmp_path):
    video = tmp_path / 'clip.mp4'
    video.write_bytes(b'vid')
    image = tmp_path / 'pic.png'
    image.write_bytes(b'img')
    archive = tmp_path / 'bundle.zip'
    archive.write_bytes(b'zip')
    calls = []

    def handler(request):
        import json
        calls.append((request.url.path, json.loads(request.content or b'{}')))
        if request.url.path == '/get_version_info':
            data = {'app_name': 'NapCat', 'app_version': '4.0.0'}
        elif request.url.path == '/get_status':
            data = {'online': True}
        elif request.url.path == '/get_login_info':
            data = {'user_id': 10086, 'nickname': 'OmniBot'}
        elif request.url.path.startswith('/send_'):
            data = {'message_id': 888}
        elif request.url.path.startswith('/upload_'):
            data = {'file_id': 'f99'}
        else:
            data = {}
        return httpx.Response(200, json={'status': 'ok', 'retcode': 0, 'data': data})

    bot = OneBotAdapter(onebot(tmp_path), httpx.MockTransport(handler))
    probe = await bot.probe()
    assert probe['implementation'] == 'NapCat' and probe['bot_id'] == '10086' and probe['nickname'] == 'OmniBot'

    # 视频/图片消息模式走 send_group_msg
    r1 = await bot.send(video, 'group:1', 'auto', video_mode='message', image_mode='message')
    assert r1['mode'] == 'message' and r1['message_id'] == 888
    assert any(p == '/send_group_msg' and body['message'][0]['type'] == 'video' for p, body in calls)

    calls.clear()
    r2 = await bot.send(image, 'private:2', 'auto', video_mode='file', image_mode='message')
    assert r2['mode'] == 'message'
    assert any(p == '/send_private_msg' and body['message'][0]['type'] == 'image' for p, body in calls)

    # 压缩包即使在消息模式下也强制以群文件/私聊文件上传
    calls.clear()
    r3 = await bot.send(archive, 'group:1', 'auto', video_mode='message', image_mode='message')
    assert r3['mode'] == 'file' and r3['file_id'] == 'f99'
    assert any(p == '/upload_group_file' for p, _ in calls)

    # 合并转发聊天记录
    calls.clear()
    r4 = await bot.send_forward([image, video], 'group:1', title='合集测试')
    assert r4['mode'] == 'forward' and r4['count'] == 2 and r4['message_id'] == 888
    fwd_call = next(body for p, body in calls if p == '/send_group_forward_msg')
    assert len(fwd_call['messages']) == 3
    assert fwd_call['messages'][0]['data']['content'][0]['type'] == 'text'
    assert fwd_call['messages'][1]['data']['content'][0]['type'] == 'image'
    assert fwd_call['messages'][2]['data']['content'][0]['type'] == 'video'


async def test_telegram_probe_returns_bot_identity():
    bot = TelegramAdapter(telegram(), httpx.MockTransport(lambda r: httpx.Response(200, json={
        'ok': True, 'result': {'id': 555, 'username': 'omni_dl_bot', 'first_name': 'Omni'}
    })))
    info = await bot.probe()
    assert info['connected'] is True and info['username'] == 'omni_dl_bot' and info['bot_id'] == 555


async def test_image_files_never_route_to_video_send_even_with_video_kind(tmp_path):
    from PIL import Image

    jpg = tmp_path / 'photo.jpg'
    Image.new('RGB', (6, 4), 'red').save(jpg, format='JPEG')
    webp = tmp_path / 'photo.webp'
    Image.new('RGB', (8, 12), 'blue').save(webp, format='WEBP')
    gif = tmp_path / 'anim.gif'
    Image.new('RGB', (4, 4), 'green').save(gif, format='GIF', save_all=True, append_images=[Image.new('RGB', (4, 4), 'yellow')], duration=80)
    archive = tmp_path / 'bundle.zip'
    archive.write_bytes(b'PK\x03\x04fake-zip')
    misnamed_mp4 = tmp_path / 'fake_video.mp4'
    misnamed_mp4.write_bytes(jpg.read_bytes())

    methods = []

    async def handler(request):
        methods.append(request.url.path.rsplit('/', 1)[-1])
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 700 + len(methods), 'chat': {'id': 123}}})

    tg = TelegramAdapter(telegram(), httpx.MockTransport(handler))
    await tg.send(jpg, '123', 'video')
    await tg.send(webp, '123', 'video')
    await tg.send(gif, '123', 'video')
    await tg.send(archive, '123', 'video')
    await tg.send(misnamed_mp4, '123', 'video')
    assert methods == ['sendPhoto', 'sendPhoto', 'sendAnimation', 'sendDocument', 'sendPhoto']

    # OneBot 对 .jfif 与魔数为图片的错标 .mp4 均归类为 image 而非 video
    jfif = tmp_path / 'pic.jfif'
    jfif.write_bytes(jpg.read_bytes())
    assert OneBotAdapter.media_category(jfif) == 'image'
    assert OneBotAdapter.media_category(misnamed_mp4) == 'image'
    assert OneBotAdapter.media_category(archive) == 'file'
