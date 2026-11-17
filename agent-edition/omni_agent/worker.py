"""JSON-line bridge to the existing TaskManager, inside a disposable process."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys

from .credentials import Vault
from .errors import AgentError, safe_text
from .security import install_network_guard, is_profile, source_url, validate_url


async def execute(request, emit):
    root = Path(request['state'])
    root.mkdir(parents=True, exist_ok=True)
    os.environ['OMNI_STATE_DIR'] = str(root)
    os.environ['OMNI_AUTOMATIC_BROWSER'] = 'off'
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(key, None)
    vault = Vault(request['data'])
    sessions, domains = vault.bind_core()
    # The Agent boundary removes ALL upstream URLs, including unfamiliar signed
    # parameter names, from legacy error logs as well as protocol responses.
    from engine import media_errors
    media_errors.sanitize = safe_text
    install_network_guard(request.get('proxy', ''))
    from core.config import config
    config.download_dir = str(root / 'output')
    Path(config.download_dir).mkdir(exist_ok=True)
    config.proxy_url = request.get('proxy', '')
    config.max_concurrent_tasks = 1
    from engine.url_sniffer import UrlSniffer
    from engine.ytdlp_engine import YtDlpEngine
    # Bound extraction before the core materializes playlist entries; disable
    # external media downloaders, unavailable browser fallback and DRM formats.
    original_opts = YtDlpEngine._build_ydl_opts.__func__

    @classmethod
    def opts(cls, *args, **kwargs):
        value = original_opts(cls, *args, **kwargs)
        value.update(playlistend=request.get('max_items', 20), max_downloads=request.get('max_items', 20),
                     max_filesize=request.get('max_file_bytes', 500 * 1024**2),
                     hls_prefer_native=True, allowed_extractors=['default'],
                     external_downloader=None, allow_unplayable_formats=False)
        from .credentials import native_runtime
        deno=native_runtime('deno')
        if deno: value['js_runtimes'] = {'deno': {'path':deno}}
        return value

    YtDlpEngine._build_ydl_opts = opts
    action = request['action']
    if action == 'verify_session':
        import requests
        site = request['platform']
        endpoints = {'哔哩哔哩': 'https://api.bilibili.com/x/web-interface/nav',
                     'Pixiv': 'https://www.pixiv.net/ajax/user/extra'}
        if site not in endpoints:
            raise AgentError('CAPABILITY_UNAVAILABLE', '此平台没有已验证的无头登录检测接口；导入状态不等于已登录')
        client = requests.Session()
        client.trust_env = False
        client.cookies.update(sessions.jar(site))
        try:
            response = client.get(endpoints[site], timeout=20, proxies={'https': config.proxy_url} if config.proxy_url else {})
            response.raise_for_status()
            data = response.json()
            valid = bool(data.get('data', {}).get('isLogin')) if site == '哔哩哔哩' else data.get('error') is False
            emit({'kind': 'result', 'result': {'state': 'VALID' if valid else 'INVALID'}})
            return
        finally: client.close()
    url = source_url(request['url'])
    validate_url(url, initial=True)
    url = await UrlSniffer.sanitize_and_resolve_url(url, config.proxy_url)
    validate_url(url, initial=True)
    profile = is_profile(url)
    if profile and not request.get('allow_profiles'):
        raise AgentError('FEATURE_DISABLED', '管理员未允许用户主页解析')
    site = UrlSniffer.identify_site(url)
    options = request.get('options') or {}
    image_request = options.get('download_type') in ('IMAGE', 'IMAGE_ZIP') or request.get('media_type') == 'images'
    if image_request:
        from engine.images import ImageEngine
        if profile:
            if site != 'X (Twitter)': raise AgentError('CAPABILITY_UNAVAILABLE','当前主页图文适配器仅提供X首个图片分页')
            from engine.media_resolver import resolve_x_profile_page
            def profile_page():
                with sessions.cookie_file(site) as cookie: return resolve_x_profile_page(url,cookie,config.proxy_url)
            post = await asyncio.to_thread(profile_page)
            post['resources'] = post['resources'][:request.get('max_items',10)]
            url = post['url']
            ImageEngine.remember(post)
        else: post = await ImageEngine.extract(url, site)
        if len(post['resources']) > request.get('max_items', 20):
            raise AgentError('LIMIT_EXCEEDED', '图文数量超过管理员限制')
        metadata = {'platform': site, 'title': safe_text(post['title']), 'author': safe_text(post.get('author', '')),
                    'media_type': 'images', 'file_count': len(post['resources']),
                    'options': ['IMAGE', 'IMAGE_ZIP']}
        if action == 'analyze':
            emit({'kind': 'result', 'result': metadata})
            return
    else:
        if profile and site != 'YouTube': raise AgentError('CAPABILITY_UNAVAILABLE','当前主页视频适配器仅提供有界的YouTube列表')
        if site in ('抖音', '小红书', '快手', '小黑盒', 'Pixiv'):
            from engine.platforms import PlatformEngine
            meta = await PlatformEngine.extract(url, site)
        elif site == '哔哩哔哩':
            from engine.bilibili_extractor import BilibiliDirectExtractor
            meta = await BilibiliDirectExtractor.extract(url)
        elif site == 'TikTok':
            from engine.tiktok_extractor import TikTokDirectExtractor
            meta = await TikTokDirectExtractor.extract(url)
        else: meta = await YtDlpEngine.extract(url, site)
        children = meta.multi_media_list
        children = children[:request.get('max_items',20)]
        metadata = {'platform': site, 'title': safe_text(meta.title), 'author': safe_text(meta.author),
                    'media_type': 'video', 'file_count': len(children) or 1,
                    'formats': [{'id': str(f.format_id) if not str(f.format_id).startswith('http') else 'source',
                                 'width': f.width or None, 'height': f.height or None, 'fps': f.fps or None,
                                 'container': f.ext or None, 'video_codec': f.vcodec or None,
                                 'audio_codec': f.acodec or None,
                                 'hdr': f.dynamic_range or None} for f in meta.available_video_formats],
                    'audio_formats': [f.ext for f in meta.available_audio_formats],
                    'collection': [{'index': i + 1, 'title': safe_text(m.title)} for i, m in enumerate(children)],
                    'options': ['VIDEO_WITH_AUDIO', 'VIDEO_ONLY', 'AUDIO_ONLY', 'GIF', 'COVER']}
        if action == 'analyze':
            emit({'kind': 'result', 'result': metadata})
            return
    from core.models import AudioFormat, DownloadType, QualitySelection, TaskStatus
    from core.task_manager import task_manager
    if image_request:
        available = post['resources']
        if action == 'collection': available = available[request['start']-1:request['start']-1+request['count']]
        selected = available if options['download_type'] == 'IMAGE_ZIP' or action=='collection' else available[:1]
        if not selected: raise AgentError('INVALID_RANGE','指定范围没有图片')
        if options['download_type']=='IMAGE_ZIP':items=[(url,post['title'],[r['id'] for r in selected])]
        else:items=[(url,post['title'],[resource['id']]) for resource in selected]
    else:
        if action == 'collection':
            start, count = request['start'], request['count']
            if not children: raise AgentError('NO_COLLECTION', '未取得可定位的合集内容')
            chosen = children[start - 1:start - 1 + count]
            if not chosen: raise AgentError('INVALID_RANGE', '指定范围没有可下载内容')
        else:
            if children: raise AgentError('COLLECTION_REQUIRED', '此链接包含多个媒体，请使用 download_collection 明确指定范围')
            chosen = [meta]
        items = [(item.url, item.title, []) for item in chosen]
    paths = []
    for index, (item_url, title, image_ids) in enumerate(items):
        validate_url(item_url, initial=True)
        task = await task_manager.add_task(url=item_url, title='media-' + str(index + 1),
            download_type=DownloadType(options['download_type']), audio_format=AudioFormat(options['audio_format']),
            quality_selection=QualitySelection(mode='MAX_HEIGHT', maxHeight=options['height'],
                                               container=options['container'], allowLowerQuality=True),
            image_platform=site if image_request else '', selected_image_ids=image_ids)
        while task.status in (TaskStatus.PENDING, TaskStatus.DOWNLOADING, TaskStatus.PROCESSING):
            emit({'kind': 'progress', 'state': task.status.value, 'progress': (index * 100 + task.progress) / len(items),
                  'speed': task.speed_text, 'files': len(paths)})
            await asyncio.sleep(.25)
        if task.status != TaskStatus.COMPLETED:
            raise AgentError(task.error_code or task.status.value, safe_text(task.error_message or '核心下载未完成'))
        validate_output(task.local_file_path, root, options, request['max_file_bytes'])
        paths.append(task.local_file_path)
    emit({'kind': 'result', 'result': {'paths': paths, 'metadata': metadata}})


def validate_output(name, root, options, maximum):
    from .security import controlled
    path = controlled(root / 'output', name)
    if not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise AgentError('FILE_INVALID','成品为空或超出管理员大小限制')
    suffix = path.suffix.lower()
    if suffix == '.zip':
        import zipfile
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist()) > maximum * 4 or archive.testzip():
                raise AgentError('FILE_INVALID','图片归档超过限制或校验失败')
    elif suffix in ('.jpg','.jpeg','.png','.webp','.gif','.avif'):
        from PIL import Image
        with Image.open(path) as image: image.verify()
    else:
        from engine.media_verifier import probe
        actual = probe(path)
        if not actual['width'] and not actual['audio']:
            raise AgentError('FILE_INVALID','成品未包含可验证的媒体流')
        if min(actual['width'],actual['height']) > options['height']:
            raise AgentError('LIMIT_EXCEEDED','实际成品画质超过管理员/请求限制')


def main():
    if os.name != 'nt' and os.environ.get('OMNI_AGENT_WORKER_PARENT'):
        import threading
        threading.Thread(target=watch_parent,args=(int(os.environ['OMNI_AGENT_WORKER_PARENT']),),daemon=True).start()
    output = sys.stdout
    def emit(value):
        output.write(json.dumps(value, ensure_ascii=True) + '\n')
        output.flush()
    request = json.loads(sys.stdin.readline())
    # Core prints and third-party logs must never corrupt the protocol or expose
    # cookies/signed URLs. Worker errors leave only structured, sanitized events.
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try: asyncio.run(execute(request, emit))
        except AgentError as error: emit({'kind': 'error', **error.result()['error']})
        except Exception as error:
            import re
            match = re.search(r'\[([A-Z_]+)\]', str(error))
            emit({'kind': 'error', 'code': match[1] if match else 'ENGINE_ERROR', 'message': safe_text(error)})


def watch_parent(expected):
    """Linux standalone workers must not outlive a crashed service process."""
    import signal
    import time
    while True:
        if os.getppid() != expected:
            if os.getpgrp()==os.getpid():os.killpg(os.getpgrp(),signal.SIGKILL)
            os._exit(70)
        time.sleep(.5)


if __name__ == '__main__': main()
