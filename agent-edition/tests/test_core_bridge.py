import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock

from omni_agent.credentials import core_root
from omni_agent.worker import execute


async def test_real_task_manager_is_used_for_media_job(tmp_path,monkeypatch):
    """Mock ONLY platform I/O; actually run the original scheduler and file checks."""
    sys.path.insert(0,str(core_root()))
    monkeypatch.setenv('OMNI_STATE_DIR',str(tmp_path/'core-state'))
    monkeypatch.setattr('omni_agent.worker.install_network_guard',lambda *args:None)
    monkeypatch.setattr('omni_agent.worker.validate_url',lambda url,**kw:url)
    from core.config import config
    from core.models import FormatOption, VideoMetadata
    from core.task_manager import task_manager
    from engine.bilibili_extractor import BilibiliDirectExtractor
    from engine.url_sniffer import UrlSniffer
    url='https://www.bilibili.com/video/BV17x411w7KC'
    metadata=VideoMetadata(url=url,title='fixture',siteName='哔哩哔哩',availableVideoFormats=[FormatOption(formatId='f1',resolutionLabel='720p',width=1280,height=720)])
    monkeypatch.setattr(UrlSniffer,'sanitize_and_resolve_url',AsyncMock(return_value=url))
    monkeypatch.setattr(BilibiliDirectExtractor,'extract',AsyncMock(return_value=metadata))
    async def download(task,progress,cancel):
        from PIL import Image
        path=Path(config.download_dir)/'cover.png'
        Image.new('RGB',(16,16),'red').save(path)
        return str(path)
    monkeypatch.setattr(BilibiliDirectExtractor,'download',download)
    events=[]
    await execute({'action':'download','url':url,'state':str(tmp_path/'work'),'data':str(tmp_path/'data'),
        'max_file_bytes':1024**2,'options':{'download_type':'COVER','height':1080,'container':'mp4','audio_format':'mp3'}},events.append)
    result=events[-1]
    assert result['kind']=='result' and len(result['result']['paths'])==1
    assert any(task.status.value=='COMPLETED' for task in task_manager.tasks.values())
    assert Path(result['result']['paths'][0]).exists()


async def test_real_core_analyze_preserves_truthful_formats(tmp_path,monkeypatch):
    monkeypatch.setattr('omni_agent.worker.install_network_guard',lambda *args:None)
    monkeypatch.setattr('omni_agent.worker.validate_url',lambda url,**kw:url)
    from core.models import FormatOption,VideoMetadata
    from engine.bilibili_extractor import BilibiliDirectExtractor
    from engine.url_sniffer import UrlSniffer
    url='https://www.bilibili.com/video/BV17x411w7KC'
    monkeypatch.setattr(UrlSniffer,'sanitize_and_resolve_url',AsyncMock(return_value=url))
    monkeypatch.setattr(BilibiliDirectExtractor,'extract',AsyncMock(return_value=VideoMetadata(url=url,title='fixture',siteName='哔哩哔哩',
        availableVideoFormats=[FormatOption(formatId='f1',resolutionLabel='720p',width=1280,height=720,vcodec='avc1',dynamicRange='SDR')])))
    events=[]
    await execute({'action':'analyze','url':url,'state':str(tmp_path/'work'),'data':str(tmp_path/'data')},events.append)
    result=events[-1]['result']
    assert result['formats'][0]['height']==720 and result['formats'][0]['hdr']=='SDR'
    assert 'url' not in result


async def test_collection_of_images_creates_one_core_task_per_selected_image(tmp_path,monkeypatch):
    monkeypatch.setattr('omni_agent.worker.install_network_guard',lambda *args:None)
    monkeypatch.setattr('omni_agent.worker.validate_url',lambda url,**kw:url)
    from core.config import config
    from engine.images import ImageEngine
    from engine.url_sniffer import UrlSniffer
    url='https://www.xiaohongshu.com/explore/fixture'
    post={'url':url,'platform':'小红书','title':'fixture','resources':[{'id':'i1'},{'id':'i2'},{'id':'i3'}]}
    monkeypatch.setattr(UrlSniffer,'sanitize_and_resolve_url',AsyncMock(return_value=url))
    monkeypatch.setattr(ImageEngine,'extract',AsyncMock(return_value=post))
    selected=[]
    async def download(task,progress,cancel):
        from PIL import Image
        selected.extend(task.selected_image_ids)
        path=Path(config.download_dir)/(task.id+'.png')
        Image.new('RGB',(8,8),'green').save(path)
        return str(path)
    monkeypatch.setattr(ImageEngine,'download',download)
    events=[]
    await execute({'action':'collection','url':url,'state':str(tmp_path/'work'),'data':str(tmp_path/'data'),
        'start':2,'count':2,'max_file_bytes':1024**2,
        'options':{'download_type':'IMAGE','height':1080,'container':'mp4','audio_format':'mp3'}},events.append)
    assert selected==['i2','i3']
    assert len(events[-1]['result']['paths'])==2


async def test_all_items_separate_images_and_zip_packing_for_video_collection(tmp_path, monkeypatch):
    import zipfile
    monkeypatch.setattr('omni_agent.worker.install_network_guard', lambda *args: None)
    monkeypatch.setattr('omni_agent.worker.validate_url', lambda url, **kw: url)
    from core.config import config
    from core.models import FormatOption, VideoMetadata
    from engine.bilibili_extractor import BilibiliDirectExtractor
    from engine.images import ImageEngine
    from engine.url_sniffer import UrlSniffer

    # 1. /us（all_items=True）且 pack_zip=False 时，下载全部独立图片而非打压缩包
    img_url = 'https://www.xiaohongshu.com/explore/all-imgs'
    post = {'url': img_url, 'platform': '小红书', 'title': 'pics', 'resources': [{'id': 'p1'}, {'id': 'p2'}, {'id': 'p3'}]}
    monkeypatch.setattr(UrlSniffer, 'sanitize_and_resolve_url', AsyncMock(return_value=img_url))
    monkeypatch.setattr(ImageEngine, 'extract', AsyncMock(return_value=post))
    dl_calls = []

    async def img_dl(task, progress, cancel):
        from PIL import Image
        dl_calls.append((task.download_type.value, list(task.selected_image_ids)))
        p = Path(config.download_dir) / (task.id + '.png')
        Image.new('RGB', (4, 4), 'yellow').save(p)
        return str(p)

    monkeypatch.setattr(ImageEngine, 'download', img_dl)
    events = []
    await execute({
        'action': 'download',
        'url': '/us ' + img_url,
        'all_items': True,
        'state': str(tmp_path / 'work_img'),
        'data': str(tmp_path / 'data_img'),
        'max_file_bytes': 1024**2,
        'options': {'download_type': 'IMAGE', 'height': 1080, 'container': 'mp4', 'audio_format': 'mp3', 'pack_zip': False},
    }, events.append)
    assert len(dl_calls) == 3 and all(dt == 'IMAGE' for dt, _ in dl_calls)
    assert len(events[-1]['result']['paths']) == 3

    # 2. /us + /zip 对合集视频下载全部子集并打包成一个 ZIP 文件
    vid_url = 'https://www.bilibili.com/video/BV1multi'
    child1 = VideoMetadata(url=vid_url + '?p=1', title='ep1', siteName='哔哩哔哩')
    child2 = VideoMetadata(url=vid_url + '?p=2', title='ep2', siteName='哔哩哔哩')
    parent = VideoMetadata(
        url=vid_url,
        title='合集作品',
        siteName='哔哩哔哩',
        availableVideoFormats=[FormatOption(formatId='f1', resolutionLabel='720p', width=1280, height=720)],
        multiMediaList=[child1, child2],
    )
    monkeypatch.setattr(UrlSniffer, 'sanitize_and_resolve_url', AsyncMock(return_value=vid_url))
    monkeypatch.setattr(BilibiliDirectExtractor, 'extract', AsyncMock(return_value=parent))

    async def vid_dl(task, progress, cancel):
        from PIL import Image
        p = Path(config.download_dir) / f'{task.title}_{task.id[:6]}.png'
        Image.new('RGB', (8, 8), 'red').save(p)
        return str(p)

    monkeypatch.setattr(BilibiliDirectExtractor, 'download', vid_dl)
    monkeypatch.setattr('engine.media_verifier.probe', lambda p: {'width': 8, 'height': 8, 'audio': False})
    events2 = []
    await execute({
        'action': 'download',
        'url': '/us /zip ' + vid_url,
        'all_items': True,
        'state': str(tmp_path / 'work_vid'),
        'data': str(tmp_path / 'data_vid'),
        'max_file_bytes': 1024**2,
        'options': {'download_type': 'VIDEO_WITH_AUDIO', 'height': 1080, 'container': 'mp4', 'audio_format': 'mp3', 'pack_zip': True},
    }, events2.append)
    out_paths = events2[-1]['result']['paths']
    assert len(out_paths) == 1 and out_paths[0].endswith('.zip')
    with zipfile.ZipFile(out_paths[0]) as zf:
        assert zf.testzip() is None and len(zf.namelist()) == 2


async def test_auto_image_fallback_first_image_without_us_and_bilibili_single_video(tmp_path, monkeypatch):
    import zipfile
    monkeypatch.setattr('omni_agent.worker.install_network_guard', lambda *args: None)
    monkeypatch.setattr('omni_agent.worker.validate_url', lambda url, **kw: url)
    from core.config import config
    from core.models import FormatOption, VideoMetadata
    from engine.bilibili_extractor import BilibiliDirectExtractor
    from engine.images import ImageEngine
    from engine.platforms import PlatformEngine
    from engine.url_sniffer import UrlSniffer

    # 1. 默认 VIDEO_WITH_AUDIO 下发小红书图文链接（原帖 25 张图 > max_items=20）：
    #    自动回退 ImageEngine，且不加 /us 时仅下载首图（1 张），不提前报 LIMIT_EXCEEDED
    xhs_url = 'https://www.xiaohongshu.com/explore/auto-note'
    post = {
        'url': xhs_url,
        'platform': '小红书',
        'title': '自动图文笔记',
        'resources': [{'id': f'img_{i}'} for i in range(1, 26)],
    }
    monkeypatch.setattr(UrlSniffer, 'sanitize_and_resolve_url', AsyncMock(return_value=xhs_url))
    monkeypatch.setattr(PlatformEngine, 'extract', AsyncMock(side_effect=RuntimeError('[MEDIA_UNCONFIRMED] 当前会话未取得笔记视频')))
    monkeypatch.setattr(ImageEngine, 'extract', AsyncMock(return_value=post))
    dl_ids = []

    async def fake_img_dl(task, progress, cancel):
        from PIL import Image
        dl_ids.append((task.download_type.value, list(task.selected_image_ids)))
        p = Path(config.download_dir) / f'{task.id}.png'
        Image.new('RGB', (4, 4), 'cyan').save(p)
        return str(p)

    monkeypatch.setattr(ImageEngine, 'download', fake_img_dl)

    ev_single = []
    await execute({
        'action': 'download',
        'url': xhs_url,
        'all_items': False,
        'allow_images': True,
        'max_items': 20,
        'state': str(tmp_path / 'w_auto1'),
        'data': str(tmp_path / 'd_auto1'),
        'max_file_bytes': 1024**2,
        'options': {'download_type': 'VIDEO_WITH_AUDIO', 'height': 1080, 'container': 'mp4', 'audio_format': 'mp3', 'pack_zip': False},
    }, ev_single.append)
    assert dl_ids == [('IMAGE', ['img_1'])]
    assert len(ev_single[-1]['result']['paths']) == 1
    assert ev_single[-1]['result']['metadata']['media_type'] == 'images'

    # 2. 不加 /us 仅加 /zip（pack_zip=True, image_zip_all=False）仍只打包首图；显式 image_zip_all=True 才打包全部
    dl_ids.clear()
    ev_zip_one = []
    await execute({
        'action': 'download',
        'url': '/zip ' + xhs_url,
        'all_items': False,
        'allow_images': True,
        'max_items': 5,
        'state': str(tmp_path / 'w_zip1'),
        'data': str(tmp_path / 'd_zip1'),
        'max_file_bytes': 1024**2,
        'options': {'download_type': 'VIDEO_WITH_AUDIO', 'height': 1080, 'container': 'mp4', 'audio_format': 'mp3', 'pack_zip': True, 'image_zip_all': False},
    }, ev_zip_one.append)
    assert dl_ids == [('IMAGE_ZIP', ['img_1'])]
    assert ev_zip_one[-1]['result']['paths'][0].endswith('.zip')

    # 3. 关闭图文权限（allow_images=False）时绝不自动回退图文
    ev_denied = []
    try:
        await execute({
            'action': 'download',
            'url': xhs_url,
            'all_items': False,
            'allow_images': False,
            'state': str(tmp_path / 'w_noimg'),
            'data': str(tmp_path / 'd_noimg'),
            'max_file_bytes': 1024**2,
            'options': {'download_type': 'VIDEO_WITH_AUDIO', 'height': 1080, 'container': 'mp4', 'audio_format': 'mp3', 'pack_zip': False},
        }, ev_denied.append)
        assert False, 'Expected video extraction failure when allow_images=False'
    except RuntimeError as exc:
        assert 'MEDIA_UNCONFIRMED' in str(exc)

    # 4. B 站带合集 multiMediaList 的普通单视频，不加 /us 直接下载当前单集（不报 COLLECTION_REQUIRED）
    bili_url = 'https://www.bilibili.com/video/BV1singleP2?p=2'
    c1 = VideoMetadata(url='https://www.bilibili.com/video/BV1singleP2', title='P1', siteName='哔哩哔哩')
    c2 = VideoMetadata(url=bili_url, title='P2', siteName='哔哩哔哩')
    bili_meta = VideoMetadata(
        url=bili_url,
        title='合集当前P2',
        siteName='哔哩哔哩',
        availableVideoFormats=[FormatOption(formatId='f1', resolutionLabel='720p', width=1280, height=720)],
        multiMediaList=[c1, c2],
    )
    monkeypatch.setattr(UrlSniffer, 'sanitize_and_resolve_url', AsyncMock(return_value=bili_url))
    monkeypatch.setattr(BilibiliDirectExtractor, 'extract', AsyncMock(return_value=bili_meta))
    downloaded_urls = []

    async def fake_bili_dl(task, progress, cancel):
        from PIL import Image
        downloaded_urls.append(task.url)
        p = Path(config.download_dir) / f'{task.id}.png'
        Image.new('RGB', (8, 8), 'blue').save(p)
        return str(p)

    monkeypatch.setattr(BilibiliDirectExtractor, 'download', fake_bili_dl)
    ev_bili = []
    await execute({
        'action': 'download',
        'url': bili_url,
        'all_items': False,
        'state': str(tmp_path / 'w_bili'),
        'data': str(tmp_path / 'd_bili'),
        'max_file_bytes': 1024**2,
        'options': {'download_type': 'VIDEO_WITH_AUDIO', 'height': 1080, 'container': 'mp4', 'audio_format': 'mp3', 'pack_zip': False},
    }, ev_bili.append)
    assert downloaded_urls == [bili_url]
    assert len(ev_bili[-1]['result']['paths']) == 1


async def test_image_format_conversion_preserves_animated_gif_and_updates_zip_manifest(tmp_path, monkeypatch):
    import hashlib
    import json
    import zipfile
    from PIL import Image

    monkeypatch.setattr('omni_agent.worker.install_network_guard', lambda *args: None)
    monkeypatch.setattr('omni_agent.worker.validate_url', lambda url, **kw: url)
    from core.config import config
    from engine.images import ImageEngine
    from engine.url_sniffer import UrlSniffer

    url = 'https://www.xiaohongshu.com/explore/fmt-test'
    post = {
        'url': url,
        'platform': '小红书',
        'title': '图片格式测试',
        'resources': [{'id': 'i1'}, {'id': 'i2'}, {'id': 'i3'}],
    }
    monkeypatch.setattr(UrlSniffer, 'sanitize_and_resolve_url', AsyncMock(return_value=url))
    monkeypatch.setattr(ImageEngine, 'extract', AsyncMock(return_value=post))

    # 1. 多图下载指定 image_format='jpg'：
    #    - 透明 RGBA PNG 白色背景合成转为 JPG 并清理旧后缀；
    #    - 静态 WebP 转为 JPG；
    #    - 多帧动态 GIF 自动保留动态（维持 .gif 多帧，不拆帧）；
    async def mixed_img_dl(task, progress, cancel):
        out = Path(config.download_dir)
        idx = task.selected_image_ids[0]
        if idx == 'i1':
            p = out / f'{task.id}_alpha.png'
            rgba = Image.new('RGBA', (8, 8), (200, 0, 0, 0))
            rgba.putpixel((0, 0), (10, 20, 30, 255))
            rgba.save(p, format='PNG')
            return str(p)
        if idx == 'i2':
            p = out / f'{task.id}_static.webp'
            Image.new('RGB', (8, 8), (40, 120, 200)).save(p, format='WEBP')
            return str(p)
        p = out / f'{task.id}_anim.gif'
        f1 = Image.new('RGB', (8, 8), 'red')
        f2 = Image.new('RGB', (8, 8), 'blue')
        f1.save(p, format='GIF', save_all=True, append_images=[f2], duration=80, loop=0)
        return str(p)

    monkeypatch.setattr(ImageEngine, 'download', mixed_img_dl)
    ev_jpg = []
    await execute({
        'action': 'download',
        'url': '/us ' + url,
        'all_items': True,
        'allow_images': True,
        'max_items': 20,
        'state': str(tmp_path / 'w_jpg'),
        'data': str(tmp_path / 'd_jpg'),
        'max_file_bytes': 1024**2,
        'options': {
            'download_type': 'IMAGE',
            'height': 1080,
            'container': 'mp4',
            'audio_format': 'mp3',
            'pack_zip': False,
            'image_format': 'jpg',
        },
    }, ev_jpg.append)
    out_paths = [Path(p) for p in ev_jpg[-1]['result']['paths']]
    assert [p.suffix.lower() for p in out_paths] == ['.jpg', '.jpg', '.gif']
    assert all(p.is_file() for p in out_paths)
    # 透明像素合成为纯白底 JPEG
    with Image.open(out_paths[0]) as im_rgba_jpg:
        assert im_rgba_jpg.format == 'JPEG'
        r, g, b = im_rgba_jpg.getpixel((5, 5))
        assert r >= 245 and g >= 245 and b >= 245
    # 动图保留全部动画帧
    with Image.open(out_paths[2]) as im_gif:
        assert getattr(im_gif, 'is_animated', False) and im_gif.n_frames == 2

    # 2. 原图已是匹配格式（.jpg 在 image_format='jpg' 下）保持原始字节零重编码；切换 image_format='webp' 才转换
    exact_bytes = b''

    async def exact_jpg_dl(task, progress, cancel):
        nonlocal exact_bytes
        p = Path(config.download_dir) / f'{task.id}_keep.jpg'
        Image.new('RGB', (6, 6), 'green').save(p, format='JPEG', quality=90)
        exact_bytes = p.read_bytes()
        return str(p)

    monkeypatch.setattr(ImageEngine, 'download', exact_jpg_dl)
    ev_keep = []
    await execute({
        'action': 'download',
        'url': url,
        'all_items': False,
        'allow_images': True,
        'max_items': 20,
        'state': str(tmp_path / 'w_keep'),
        'data': str(tmp_path / 'd_keep'),
        'max_file_bytes': 1024**2,
        'options': {'download_type': 'IMAGE', 'height': 1080, 'container': 'mp4', 'audio_format': 'mp3', 'pack_zip': False, 'image_format': 'jpg'},
    }, ev_keep.append)
    kept_path = Path(ev_keep[-1]['result']['paths'][0])
    assert kept_path.suffix == '.jpg' and kept_path.read_bytes() == exact_bytes

    ev_webp = []
    await execute({
        'action': 'download',
        'url': url,
        'all_items': False,
        'allow_images': True,
        'max_items': 20,
        'state': str(tmp_path / 'w_webp'),
        'data': str(tmp_path / 'd_webp'),
        'max_file_bytes': 1024**2,
        'options': {'download_type': 'IMAGE', 'height': 1080, 'container': 'mp4', 'audio_format': 'mp3', 'pack_zip': False, 'image_format': 'webp'},
    }, ev_webp.append)
    webp_path = Path(ev_webp[-1]['result']['paths'][0])
    assert webp_path.suffix == '.webp'
    with Image.open(webp_path) as im_w:
        assert im_w.format == 'WEBP'

    # 3. 图集 ZIP（ImageEngine IMAGE_ZIP 带 manifest.json）在 image_format='png' 时自动转换包内静态图、保留动图 GIF 并刷新 manifest.json
    async def zip_with_manifest_dl(task, progress, cancel):
        work = Path(config.download_dir)
        w1 = work / 'raw1.webp'
        Image.new('RGB', (10, 10), (12, 34, 56)).save(w1, format='WEBP')
        g1 = work / 'raw2.gif'
        Image.new('RGB', (6, 6), 'red').save(g1, format='GIF', save_all=True, append_images=[Image.new('RGB', (6, 6), 'yellow')], duration=90)
        zpath = work / 'fmt_images.zip'
        with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_STORED) as zf:
            zf.write(w1, 'p1.webp')
            zf.write(g1, 'p2.gif')
            manifest = {
                'platform': '小红书',
                'contentId': 'fmt-test',
                'images': [
                    {'id': 'i1', 'file': 'p1.webp', 'bytes': w1.stat().st_size, 'sha256': hashlib.sha256(w1.read_bytes()).hexdigest(), 'source': 'PLATFORM_SOURCE'},
                    {'id': 'i2', 'file': 'p2.gif', 'bytes': g1.stat().st_size, 'sha256': hashlib.sha256(g1.read_bytes()).hexdigest(), 'source': 'PLATFORM_SOURCE'},
                ],
            }
            zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        w1.unlink()
        g1.unlink()
        return str(zpath)

    monkeypatch.setattr(ImageEngine, 'download', zip_with_manifest_dl)
    ev_zip = []
    await execute({
        'action': 'download',
        'url': '/us /zip ' + url,
        'all_items': True,
        'allow_images': True,
        'max_items': 20,
        'state': str(tmp_path / 'w_zipfmt'),
        'data': str(tmp_path / 'd_zipfmt'),
        'max_file_bytes': 1024**2,
        'options': {'download_type': 'IMAGE', 'height': 1080, 'container': 'mp4', 'audio_format': 'mp3', 'pack_zip': True, 'image_format': 'png'},
    }, ev_zip.append)
    res_zip = Path(ev_zip[-1]['result']['paths'][0])
    assert res_zip.suffix == '.zip'
    with zipfile.ZipFile(res_zip) as zf:
        assert zf.testzip() is None
        names = set(zf.namelist())
        assert names == {'p1.png', 'p2.gif', 'manifest.json'}
        converted_manifest = json.loads(zf.read('manifest.json').decode('utf-8'))
        entry1 = converted_manifest['images'][0]
        assert entry1['file'] == 'p1.png'
        assert entry1['bytes'] == len(zf.read('p1.png'))
        assert entry1['sha256'] == hashlib.sha256(zf.read('p1.png')).hexdigest()
