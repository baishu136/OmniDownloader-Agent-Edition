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


async def test_ordered_collection_merge_command_forward_batching_and_telegram_media_group(service, monkeypatch):
    import json
    from PIL import Image
    from omni_agent.delivery import OneBotAdapter

    # 1. OneBot: 35 张有序图片 + 全局 qq_forward_collection=False + 链接带 /merge（cmd_merge=True）
    #    应严格按 00..34 顺序拆成 30 + 5 两批 send_group_forward_msg（默认上限 30），附带标题，且只做 1 次 probe
    ob_cfg = BotConfig(
        adapter='onebot',
        enabled=True,
        base_url='http://127.0.0.1:3000',
        token='mock-ob',
        allowed_targets=['group:200'],
        shared_root=str(service.config.cache),
        remote_root='F:/NapCatShared',
    )
    service.vault.put('bot-onebot', ob_cfg.model_dump_json())
    service.update_settings({'qq_forward_collection': False, 'qq_forward_max_items': 30})

    row, _ = service.store.create_job(
        'agent',
        {'url': 'https://www.xiaohongshu.com/explore/35pics', 'cmd_all': True, 'cmd_merge': True},
        'merge-35-key',
        'merge-35-req',
    )
    out_dir = service.config.cache / 'jobs' / row['id'] / 'output'
    out_dir.mkdir(parents=True)
    paths = []
    for i in range(35):
        p = out_dir / f'img_{i:02d}.png'
        Image.new('RGB', (4, 4), (i * 5 % 255, 80, 120)).save(p)
        paths.append(p)
    service.store.finish(row['id'], paths, metadata={'title': '三十五图合集', 'media_type': 'images'})

    # 校验 Store.files 严格保持 00..34 原始下载顺序（不再被 UUID 随机打乱）
    stored_names = [Path(f['path']).name for f in service.store.files(row['id'])]
    assert stored_names == [f'img_{i:02d}.png' for i in range(35)]

    ob_calls = []

    def ob_handler(request):
        body = json.loads(request.content or b'{}')
        ob_calls.append((request.url.path, body))
        if request.url.path == '/get_version_info':
            data = {'app_name': 'NapCat', 'app_version': '4.2'}
        elif request.url.path == '/get_status':
            data = {'online': True}
        elif request.url.path == '/get_login_info':
            data = {'user_id': 10001, 'nickname': 'OmniBot'}
        elif request.url.path == '/send_group_forward_msg':
            data = {'message_id': 500 + len(ob_calls)}
        else:
            data = {}
        return httpx.Response(200, json={'status': 'ok', 'retcode': 0, 'data': data})

    monkeypatch.setattr('omni_agent.service.make_adapter', lambda cfg: OneBotAdapter(cfg, httpx.MockTransport(ob_handler)))
    delivery = await service.deliver('agent', row['id'], 'onebot', 'group:200', 'auto', 'merge-35-send')
    await service.run_delivery(service.store.delivery(delivery['id']))

    assert service.store.delivery(delivery['id'])['state'] == 'SENT'
    fwd_batches = [body['messages'] for path, body in ob_calls if path == '/send_group_forward_msg']
    assert len(fwd_batches) == 2
    # 每批首条为标题文本节点，后接 30 张与 5 张图片节点
    assert fwd_batches[0][0]['data']['content'][0]['type'] == 'text'
    assert '第 1/2 批' in fwd_batches[0][0]['data']['content'][0]['data']['text']
    assert len(fwd_batches[0]) == 31 and len(fwd_batches[1]) == 6
    sent_files = [
        node['data']['content'][0]['data']['file']
        for batch in fwd_batches
        for node in batch[1:]
    ]
    assert all(f.startswith('file:///F:/NapCatShared/') for f in sent_files)
    assert [f.rsplit('/', 1)[-1] for f in sent_files] == [f'img_{i:02d}.png' for i in range(35)]
    # 探针在单次交付中仅执行 1 次
    assert sum(1 for path, _ in ob_calls if path == '/get_version_info') == 1

    # 2. Telegram: 12 张 .webp 图片 + 1 张 .gif 动图：
    #    12 张 webp 内存转 JPEG/PNG 按 10 + 2 分两批 sendMediaGroup 发出，gif 走 sendAnimation，原盘文件零篡改并正常清理
    configure(service)
    row_tg, _ = service.store.create_job(
        'agent',
        {'url': 'https://www.xiaohongshu.com/explore/tg12', 'cmd_all': True},
        'tg-12-key',
        'tg-12-req',
    )
    tg_dir = service.config.cache / 'jobs' / row_tg['id'] / 'output'
    tg_dir.mkdir(parents=True)
    tg_paths = []
    for i in range(12):
        wp = tg_dir / f'p_{i:02d}.webp'
        Image.new('RGB', (8, 8), (10, i * 15, 200)).save(wp, format='WEBP')
        tg_paths.append(wp)
    gif_p = tg_dir / 'anim.gif'
    Image.new('RGB', (8, 8), 'purple').save(gif_p, format='GIF')
    tg_paths.append(gif_p)
    service.store.finish(row_tg['id'], tg_paths, metadata={'title': 'Telegram 相册测试', 'media_type': 'images'})

    tg_calls = []

    async def tg_handler(request):
        raw = await request.aread()
        tg_calls.append((request.url.path, raw))
        if request.url.path.endswith('/sendMediaGroup'):
            # 统计本次 attach 数量以返回等长回执
            count = raw.count(b'name="media_')
            return httpx.Response(200, json={
                'ok': True,
                'result': [{'message_id': 800 + idx, 'chat': {'id': 123}} for idx in range(count)],
            })
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 900, 'chat': {'id': 123}}})

    monkeypatch.setattr('omni_agent.service.make_adapter', lambda cfg: TelegramAdapter(cfg, httpx.MockTransport(tg_handler)))
    d_tg = await service.deliver('agent', row_tg['id'], 'telegram', '123', 'auto', 'tg-12-send')
    await service.run_delivery(service.store.delivery(d_tg['id']))

    assert service.store.delivery(d_tg['id'])['state'] == 'SENT'
    assert service.store.job(row_tg['id'])['state'] == 'CLEANED'
    tg_paths_called = [p.rsplit('/', 1)[-1] for p, _ in tg_calls]
    assert tg_paths_called == ['sendMediaGroup', 'sendMediaGroup', 'sendAnimation']
    # sendMediaGroup 中不应包含 .webp 扩展名（已在内存转换为 .jpg）
    assert b'.jpg' in tg_calls[0][1]

    # 3. Telegram sendMediaGroup 遭 BOT_REJECTED 时自动平滑降级逐条 sendPhoto 发送
    row_fb, _ = service.store.create_job('agent', {'url': 'https://x.com/u/status/fb'}, 'tg-fb-key', 'tg-fb-req')
    fb_dir = service.config.cache / 'jobs' / row_fb['id'] / 'output'
    fb_dir.mkdir(parents=True)
    fb_paths = []
    for i in range(2):
        fp = fb_dir / f'fb_{i}.png'
        Image.new('RGB', (4, 4), 'orange').save(fp)
        fb_paths.append(fp)
    service.store.finish(row_fb['id'], fb_paths, metadata={'title': '降级测试'})

    fb_calls = []

    def fb_handler(request):
        fb_calls.append(request.url.path.rsplit('/', 1)[-1])
        if request.url.path.endswith('/sendMediaGroup'):
            return httpx.Response(200, json={'ok': False, 'description': 'PHOTO_INVALID_DIMENSIONS'})
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 950 + len(fb_calls), 'chat': {'id': 123}}})

    monkeypatch.setattr('omni_agent.service.make_adapter', lambda cfg: TelegramAdapter(cfg, httpx.MockTransport(fb_handler)))
    d_fb = await service.deliver('agent', row_fb['id'], 'telegram', '123', 'auto', 'tg-fb-send')
    await service.run_delivery(service.store.delivery(d_fb['id']))
    assert service.store.delivery(d_fb['id'])['state'] == 'SENT'
    assert fb_calls == ['sendMediaGroup', 'sendPhoto', 'sendPhoto']


async def test_service_delivery_corrects_wrong_video_kind_and_preserves_mixed_order_and_oversize_fallback(service, monkeypatch):
    from PIL import Image
    configure(service)

    # 1. 单图任务即使用 video kind 请求交付，也不得走 sendVideo
    row_single, _ = service.store.create_job('agent', {'url': 'https://x.com/u/status/single1'}, 'tg-single-img-k', 'tg-single-img-r')
    s_dir = service.config.cache / 'jobs' / row_single['id'] / 'output'
    s_dir.mkdir(parents=True)
    img_only = s_dir / 'only.png'
    Image.new('RGB', (10, 10), 'cyan').save(img_only)
    service.store.finish(row_single['id'], [img_only], metadata={'title': '单图纠偏'})

    single_calls = []

    def single_handler(request):
        single_calls.append(request.url.path.rsplit('/', 1)[-1])
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1001, 'chat': {'id': 123}}})

    monkeypatch.setattr('omni_agent.service.make_adapter', lambda cfg: TelegramAdapter(cfg, httpx.MockTransport(single_handler)))
    d_single = await service.deliver('agent', row_single['id'], 'telegram', '123', 'video', 'tg-single-img-send')
    await service.run_delivery(service.store.delivery(d_single['id']))
    assert service.store.delivery(d_single['id'])['state'] == 'SENT'
    assert single_calls == ['sendPhoto']

    # 2. [img0, gif, img1, img2] 混排严格保留原始顺序：sendPhoto -> sendAnimation -> sendMediaGroup
    row_mix, _ = service.store.create_job('agent', {'url': 'https://x.com/u/status/mix1'}, 'tg-mix-k', 'tg-mix-r')
    m_dir = service.config.cache / 'jobs' / row_mix['id'] / 'output'
    m_dir.mkdir(parents=True)
    m0 = m_dir / 'm0.png'
    mg = m_dir / 'm1.gif'
    m2 = m_dir / 'm2.png'
    m3 = m_dir / 'm3.png'
    Image.new('RGB', (8, 8), 'red').save(m0)
    Image.new('RGB', (8, 8), 'blue').save(mg, format='GIF', save_all=True, append_images=[Image.new('RGB', (8, 8), 'white')], duration=50)
    Image.new('RGB', (8, 8), 'green').save(m2)
    Image.new('RGB', (8, 8), 'yellow').save(m3)
    service.store.finish(row_mix['id'], [m0, mg, m2, m3], metadata={'title': '混排保序'})

    mix_calls = []

    async def mix_handler(request):
        raw = await request.aread()
        mix_calls.append(request.url.path.rsplit('/', 1)[-1])
        if request.url.path.endswith('/sendMediaGroup'):
            count = raw.count(b'name="media_')
            return httpx.Response(200, json={'ok': True, 'result': [{'message_id': 1100 + i, 'chat': {'id': 123}} for i in range(count)]})
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1150, 'chat': {'id': 123}}})

    monkeypatch.setattr('omni_agent.service.make_adapter', lambda cfg: TelegramAdapter(cfg, httpx.MockTransport(mix_handler)))
    d_mix = await service.deliver('agent', row_mix['id'], 'telegram', '123', 'auto', 'tg-mix-send')
    await service.run_delivery(service.store.delivery(d_mix['id']))
    assert mix_calls == ['sendPhoto', 'sendAnimation', 'sendMediaGroup']

    # 3. 媒体组内含转换后超 photo 上限的图片时，不崩溃，自动降级单发并走 sendDocument
    small_cfg = BotConfig(adapter='telegram', enabled=True, base_url='https://api.telegram.org', token='mock-token', allowed_targets=['123'], max_upload_bytes=1024**2)
    service.vault.put('bot-telegram', small_cfg.model_dump_json())
    row_big, _ = service.store.create_job('agent', {'url': 'https://x.com/u/status/big'}, 'tg-big-k', 'tg-big-r')
    b_dir = service.config.cache / 'jobs' / row_big['id'] / 'output'
    b_dir.mkdir(parents=True)
    bw = b_dir / 'huge.webp'
    bp = b_dir / 'ok.png'
    Image.new('RGB', (120, 120), 'navy').save(bw, format='WEBP')
    Image.new('RGB', (4, 4), 'pink').save(bp)
    service.store.finish(row_big['id'], [bw, bp], metadata={'title': '超限相册降级'})

    big_calls = []
    adapter_obj = TelegramAdapter(small_cfg, httpx.MockTransport(lambda r: (big_calls.append(r.url.path.rsplit('/', 1)[-1]), httpx.Response(200, json={'ok': True, 'result': {'message_id': 1200 + len(big_calls), 'chat': {'id': 123}}}))[1]))
    monkeypatch.setattr(adapter_obj, 'limit', lambda kind: 200 if kind == 'photo' else 1024**2)
    monkeypatch.setattr('omni_agent.service.make_adapter', lambda cfg: adapter_obj)
    d_big = await service.deliver('agent', row_big['id'], 'telegram', '123', 'auto', 'tg-big-send')
    await service.run_delivery(service.store.delivery(d_big['id']))
    assert service.store.delivery(d_big['id'])['state'] == 'SENT'
    assert big_calls == ['sendDocument', 'sendPhoto']
