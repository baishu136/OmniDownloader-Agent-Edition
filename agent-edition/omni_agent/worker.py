"""JSON-line bridge to the existing TaskManager, inside a disposable process."""
import asyncio
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import zipfile

from .credentials import Vault
from .errors import AgentError, safe_text
from .security import controlled, install_network_guard, is_profile, source_url, validate_url

_IMAGE_PLATFORMS = {'抖音', '快手', '哔哩哔哩', 'Pixiv', '小红书', 'X (Twitter)', 'Pinterest'}
_IMAGE_FALLBACK_HINTS = (
    'NO_MEDIA',
    'MEDIA_UNCONFIRMED',
    '图文',
    'no video could be found',
    '未取得笔记视频',
    '图片/漫画',
    '未取得真实视频格式',
    '不适用视频导出',
    '未提供视频',
)


def _is_explicit_image_url(url: str, site: str) -> bool:
    """识别无需先走视频提取的明确图文链接（如抖音图文 /note/、B 站动态图文 /opus/）。"""
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    path = (parts.path or '').lower()
    if site == '抖音' and '/note/' in path:
        return True
    if site == '哔哩哔哩' and (host == 't.bilibili.com' or '/opus/' in path or '/dynamic/' in path):
        return True
    return False


def _should_fallback_to_images(exc: Exception) -> bool:
    """判断视频提取失败是否因目标实际为图文帖子，从而允许在默认模式下平滑回退图文引擎。"""
    text = str(exc or '')
    low = text.lower()
    return any(hint.lower() in low for hint in _IMAGE_FALLBACK_HINTS)


def _safe_archive_name(title, fallback='omni_archive'):
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', str(title or '')).strip(' .')
    return (cleaned[:80] or fallback)


_IMAGE_SUFFIXES_BY_FORMAT = {
    'jpg': ('.jpg', '.jpeg'),
    'png': ('.png',),
    'webp': ('.webp',),
}
_CONVERTIBLE_IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'}


def convert_image_bytes(raw_bytes: bytes, source_suffix: str, target_format: str) -> tuple[bytes, str] | None:
    """按指定目标格式转换静态图片字节；目标为 original、后缀已匹配或属多帧动图时返回 None 保持原样。"""
    fmt = str(target_format or 'original').lower()
    if fmt == 'original' or fmt not in _IMAGE_SUFFIXES_BY_FORMAT:
        return None
    if source_suffix.lower() not in _CONVERTIBLE_IMAGE_SUFFIXES:
        return None
    if source_suffix.lower() in _IMAGE_SUFFIXES_BY_FORMAT[fmt]:
        return None
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(raw_bytes)) as opened:
        if getattr(opened, 'is_animated', False) and getattr(opened, 'n_frames', 1) > 1:
            return None
        img = ImageOps.exif_transpose(opened)
        has_alpha = img.mode in ('RGBA', 'LA', 'PA') or (img.mode == 'P' and 'transparency' in img.info)
        buf = io.BytesIO()
        if fmt == 'jpg':
            if has_alpha:
                rgba = img.convert('RGBA')
                bg = Image.new('RGB', rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel('A'))
                out_img = bg
            elif img.mode != 'RGB':
                out_img = img.convert('RGB')
            else:
                out_img = img
            out_img.save(buf, format='JPEG', quality=95, subsampling=0)
            return buf.getvalue(), '.jpg'
        if fmt == 'png':
            if has_alpha:
                out_img = img.convert('RGBA') if img.mode != 'RGBA' else img
            elif img.mode not in ('RGB', 'L'):
                out_img = img.convert('RGB')
            else:
                out_img = img
            out_img.save(buf, format='PNG', optimize=True)
            return buf.getvalue(), '.png'
        if has_alpha:
            out_img = img.convert('RGBA') if img.mode != 'RGBA' else img
        elif img.mode != 'RGB':
            out_img = img.convert('RGB')
        else:
            out_img = img
        out_img.save(buf, format='WEBP', quality=92)
        return buf.getvalue(), '.webp'


def convert_image_file(path_str: str, output_dir: Path, target_format: str, max_file_bytes: int) -> str:
    """将散图文件按目标格式原子替换保存，保持原有文件名主干并校验大小限制。"""
    src = controlled(output_dir, path_str)
    converted = convert_image_bytes(src.read_bytes(), src.suffix, target_format)
    if converted is None:
        return str(src)
    payload, new_ext = converted
    if not 0 < len(payload) <= max_file_bytes:
        raise AgentError('FILE_TOO_LARGE', '转换后的图片为空或超出单文件大小上限')
    target = src.with_suffix(new_ext)
    idx = 1
    while target.exists() and target.resolve() != src.resolve():
        target = output_dir / f'{src.stem}_{idx}{new_ext}'
        idx += 1
    temporary = target.with_suffix(f'{new_ext}.part')
    try:
        temporary.write_bytes(payload)
        temporary.replace(target)
        if target.resolve() != src.resolve():
            src.unlink(missing_ok=True)
        return str(target)
    finally:
        temporary.unlink(missing_ok=True)


def convert_zip_images(zip_path_str: str, output_dir: Path, target_format: str, max_file_bytes: int) -> str:
    """按目标图片格式重建图集 ZIP 压缩包内容，并同步更新其中的 manifest.json。"""
    fmt = str(target_format or 'original').lower()
    if fmt == 'original' or fmt not in _IMAGE_SUFFIXES_BY_FORMAT:
        return zip_path_str
    src = controlled(output_dir, zip_path_str)
    with zipfile.ZipFile(src) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist() if not info.is_dir()]
    rewrites: list[tuple[str, str, bytes, bool]] = []
    used_names: set[str] = set()
    changed = False
    manifest_raw: bytes | None = None
    for name, raw in entries:
        if name == 'manifest.json':
            manifest_raw = raw
            continue
        p = Path(name)
        converted = convert_image_bytes(raw, p.suffix, fmt)
        if converted is not None:
            new_bytes, new_ext = converted
            changed = True
            candidate = str(p.with_suffix(new_ext)).replace('\\', '/')
        else:
            new_bytes = raw
            candidate = name
        final_name = candidate
        idx = 1
        while final_name in used_names:
            cp = Path(candidate)
            final_name = str(cp.with_name(f'{cp.stem}_{idx}{cp.suffix}')).replace('\\', '/')
            idx += 1
        used_names.add(final_name)
        rewrites.append((name, final_name, new_bytes, converted is not None))
    if not changed:
        return str(src)

    temporary = src.with_suffix('.zip.part')
    try:
        rename_map = {old: (new, len(data), hashlib.sha256(data).hexdigest()) for old, new, data, _ in rewrites}
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_STORED) as bundle:
            for _, final_name, data, _ in rewrites:
                bundle.writestr(final_name, data)
            if manifest_raw is not None:
                try:
                    manifest = json.loads(manifest_raw.decode('utf-8'))
                    for item in manifest.get('images') or []:
                        old_file = item.get('file')
                        if old_file in rename_map:
                            new_file, size_val, digest = rename_map[old_file]
                            item['file'] = new_file
                            item['bytes'] = size_val
                            item['sha256'] = digest
                    bundle.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                except (ValueError, UnicodeDecodeError):
                    bundle.writestr('manifest.json', manifest_raw)
        if not temporary.is_file() or not 0 < temporary.stat().st_size <= max_file_bytes:
            raise AgentError('FILE_TOO_LARGE', '转换打包后的压缩包为空或超出单文件大小上限')
        with zipfile.ZipFile(temporary) as bundle:
            if sum(i.file_size for i in bundle.infolist()) > max_file_bytes * 4 or bundle.testzip():
                raise AgentError('FILE_INVALID', '转换后的压缩包超出大小或完整性校验失败')
        temporary.replace(src)
        return str(src)
    finally:
        temporary.unlink(missing_ok=True)


def pack_paths_to_zip(paths, output_dir, title, max_file_bytes):
    """将输出文件列表打包为 ZIP 压缩包并校验大小及完整性。"""
    output_dir = Path(output_dir)
    archive_name = _safe_archive_name(title)
    target = output_dir / f'{archive_name}.zip'
    index = 1
    while target.exists():
        target = output_dir / f'{archive_name}_{index}.zip'
        index += 1
    temporary = target.with_suffix('.zip.part')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_STORED) as bundle:
            used_names = set()
            for idx, item in enumerate(paths):
                src = controlled(output_dir, item)
                arcname = src.name
                if arcname in used_names:
                    arcname = f'{idx + 1}_{arcname}'
                used_names.add(arcname)
                bundle.write(src, arcname)
        if not temporary.is_file() or not 0 < temporary.stat().st_size <= max_file_bytes:
            raise AgentError('FILE_TOO_LARGE', '打包后的压缩包为空或超出单文件大小上限')
        with zipfile.ZipFile(temporary) as bundle:
            if bundle.testzip():
                raise AgentError('FILE_INVALID', '打包压缩包完整性校验失败')
        temporary.replace(target)
        for item in paths:
            Path(item).unlink(missing_ok=True)
        return str(target)
    finally:
        temporary.unlink(missing_ok=True)


async def execute(request, emit):
    root = Path(request['state'])
    root.mkdir(parents=True, exist_ok=True)
    os.environ['OMNI_STATE_DIR'] = str(root)
    os.environ['OMNI_AUTOMATIC_BROWSER'] = 'off'
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(key, None)
    vault = Vault(request['data'])
    sessions, domains = vault.bind_core()
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
    original_opts = YtDlpEngine._build_ydl_opts.__func__

    @classmethod
    def opts(cls, *args, **kwargs):
        value = original_opts(cls, *args, **kwargs)
        value.update(playlistend=request.get('max_items', 20), max_downloads=request.get('max_items', 20),
                     max_filesize=request.get('max_file_bytes', 500 * 1024**2),
                     hls_prefer_native=True, allowed_extractors=['default'],
                     external_downloader=None, allow_unplayable_formats=False)
        from .credentials import native_runtime
        deno = native_runtime('deno')
        if deno:
            value['js_runtimes'] = {'deno': {'path': deno}}
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
        finally:
            client.close()
    url = source_url(request['url'])
    using_proxy = bool(config.proxy_url)
    validate_url(url, initial=True, using_proxy=using_proxy)
    url = await UrlSniffer.sanitize_and_resolve_url(url, config.proxy_url)
    validate_url(url, initial=True, using_proxy=using_proxy)
    profile = is_profile(url)
    if profile and not request.get('allow_profiles'):
        raise AgentError('FEATURE_DISABLED', '当前设置未允许用户主页解析')
    site = UrlSniffer.identify_site(url)
    options = request.get('options') or {}
    all_items = bool(request.get('all_items'))
    allow_images = bool(request.get('allow_images', True))
    pack_zip = bool(options.get('pack_zip') or options.get('download_type') == 'IMAGE_ZIP')
    image_request = options.get('download_type') in ('IMAGE', 'IMAGE_ZIP') or request.get('media_type') == 'images'
    can_auto_image = (
        allow_images
        and site in _IMAGE_PLATFORMS
        and (action == 'analyze' or options.get('download_type', 'VIDEO_WITH_AUDIO') == 'VIDEO_WITH_AUDIO')
    )
    if not image_request and can_auto_image and _is_explicit_image_url(url, site):
        image_request = True

    post = None
    meta = None
    children = []

    async def _extract_images():
        nonlocal url
        from engine.images import ImageEngine
        if profile:
            if site != 'X (Twitter)':
                raise AgentError('CAPABILITY_UNAVAILABLE', '当前主页图文适配器仅提供X首个图片分页')
            from engine.media_resolver import resolve_x_profile_page

            def profile_page():
                with sessions.cookie_file(site) as cookie:
                    return resolve_x_profile_page(url, cookie, config.proxy_url)

            resolved_post = await asyncio.to_thread(profile_page)
            resolved_post['resources'] = resolved_post['resources'][:request.get('max_items', 10)]
            url = resolved_post['url']
            ImageEngine.remember(resolved_post)
        else:
            resolved_post = await ImageEngine.extract(url, site)
        meta_dict = {
            'platform': site,
            'title': safe_text(resolved_post['title']),
            'author': safe_text(resolved_post.get('author', '')),
            'media_type': 'images',
            'file_count': len(resolved_post['resources']),
            'options': ['IMAGE'],
        }
        return resolved_post, meta_dict

    if image_request:
        post, metadata = await _extract_images()
        if action == 'analyze':
            emit({'kind': 'result', 'result': metadata})
            return
    else:
        if profile and site != 'YouTube':
            raise AgentError('CAPABILITY_UNAVAILABLE', '当前主页视频适配器仅提供有界的YouTube列表')
        try:
            if site in ('抖音', '小红书', '快手', '小黑盒', 'Pixiv'):
                from engine.platforms import PlatformEngine
                meta = await PlatformEngine.extract(url, site)
            elif site == '哔哩哔哩':
                from engine.bilibili_extractor import BilibiliDirectExtractor
                meta = await BilibiliDirectExtractor.extract(url)
            elif site == 'TikTok':
                from engine.tiktok_extractor import TikTokDirectExtractor
                meta = await TikTokDirectExtractor.extract(url)
            else:
                meta = await YtDlpEngine.extract(url, site)
        except Exception as video_exc:
            if can_auto_image and _should_fallback_to_images(video_exc):
                try:
                    post, metadata = await _extract_images()
                    image_request = True
                except Exception:
                    raise video_exc
            else:
                raise

        if image_request:
            if action == 'analyze':
                emit({'kind': 'result', 'result': metadata})
                return
        else:
            children = meta.multi_media_list
            children = children[:request.get('max_items', 20)]
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
    max_items = request.get('max_items', 20)
    if image_request:
        available = post['resources']
        if action == 'collection':
            selected = available[request['start'] - 1:request['start'] - 1 + request['count']]
        elif all_items or request.get('image_zip_all') or options.get('image_zip_all') or options.get('download_type') == 'IMAGE_ZIP':
            selected = available[:max_items]
        else:
            selected = available[:1]
        if not selected:
            raise AgentError('INVALID_RANGE', '指定范围没有图片')
        core_download_type = DownloadType.IMAGE_ZIP if pack_zip else DownloadType.IMAGE
        if pack_zip:
            items = [(url, post['title'], [r['id'] for r in selected])]
        else:
            items = [(url, f'{post["title"]}_{idx + 1}' if len(selected) > 1 else post['title'], [resource['id']])
                     for idx, resource in enumerate(selected)]
    else:
        if action == 'collection':
            start, count = request['start'], request['count']
            if not children:
                raise AgentError('NO_COLLECTION', '未取得可定位的合集内容')
            chosen = children[start - 1:start - 1 + count]
            if not chosen:
                raise AgentError('INVALID_RANGE', '指定范围没有可下载内容')
        elif all_items and children:
            chosen = children[:max_items]
        else:
            if children and not meta.available_video_formats:
                raise AgentError('COLLECTION_REQUIRED', '此链接包含多个媒体，请使用 download_collection 或在链接前加 /us 指令')
            chosen = [meta]
        core_download_type = DownloadType(options['download_type'])
        items = [(item.url, item.title, []) for item in chosen]
    emit({'kind': 'metadata', 'metadata': metadata})
    paths = []
    for index, (item_url, title, image_ids) in enumerate(items):
        validate_url(item_url, initial=True, using_proxy=using_proxy)
        task = await task_manager.add_task(
            url=item_url,
            title=title or ('media-' + str(index + 1)),
            download_type=core_download_type,
            audio_format=AudioFormat(options['audio_format']),
            quality_selection=QualitySelection(mode='MAX_HEIGHT', maxHeight=options['height'],
                                               container=options['container'], allowLowerQuality=True),
            image_platform=site if image_request else '',
            selected_image_ids=image_ids,
        )
        while task.status in (TaskStatus.PENDING, TaskStatus.DOWNLOADING, TaskStatus.PROCESSING):
            emit({'kind': 'progress', 'state': task.status.value, 'progress': (index * 100 + task.progress) / len(items),
                  'speed': task.speed_text, 'files': len(paths)})
            await asyncio.sleep(.25)
        if task.status != TaskStatus.COMPLETED:
            raise AgentError(task.error_code or task.status.value, safe_text(task.error_message or '核心下载未完成'))
        local_path = task.local_file_path
        validate_output(local_path, root, options, request['max_file_bytes'])
        image_format = str(options.get('image_format') or 'original').lower()
        if image_request and image_format != 'original':
            if Path(local_path).suffix.lower() == '.zip':
                local_path = convert_zip_images(local_path, root / 'output', image_format, request['max_file_bytes'])
            else:
                local_path = convert_image_file(local_path, root / 'output', image_format, request['max_file_bytes'])
            validate_output(local_path, root, options, request['max_file_bytes'])
        paths.append(local_path)
    if pack_zip and not (len(paths) == 1 and Path(paths[0]).suffix.lower() == '.zip'):
        zip_path = pack_paths_to_zip(paths, root / 'output', metadata.get('title') or 'omni_media', request['max_file_bytes'])
        validate_output(zip_path, root, options, request['max_file_bytes'])
        paths = [zip_path]
    emit({'kind': 'result', 'result': {'paths': paths, 'metadata': metadata}})


def validate_output(name, root, options, maximum):
    from .security import controlled
    path = controlled(root / 'output', name)
    if not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise AgentError('FILE_INVALID', '成品为空或超出大小限制')
    suffix = path.suffix.lower()
    if suffix == '.zip':
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist()) > maximum * 4 or archive.testzip():
                raise AgentError('FILE_INVALID', '压缩包超过限制或校验失败')
    elif suffix in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'):
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
    else:
        from engine.media_verifier import probe
        actual = probe(path)
        if not actual['width'] and not actual['audio']:
            raise AgentError('FILE_INVALID', '成品未包含可验证的媒体流')
        if min(actual['width'], actual['height']) > options['height']:
            raise AgentError('LIMIT_EXCEEDED', '实际成品画质超过请求或上限设置')


def main():
    if os.name != 'nt' and os.environ.get('OMNI_AGENT_WORKER_PARENT'):
        import threading
        threading.Thread(target=watch_parent, args=(int(os.environ['OMNI_AGENT_WORKER_PARENT']),), daemon=True).start()
    output = sys.stdout

    def emit(value):
        output.write(json.dumps(value, ensure_ascii=True) + '\n')
        output.flush()

    request = json.loads(sys.stdin.readline())
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            asyncio.run(execute(request, emit))
        except AgentError as error:
            emit({'kind': 'error', **error.result()['error']})
        except Exception as error:
            match = re.search(r'\[([A-Z_]+)\]', str(error))
            emit({'kind': 'error', 'code': match[1] if match else 'ENGINE_ERROR', 'message': safe_text(error)})


def watch_parent(expected):
    """Linux 独立工作进程防止在服务崩溃后遗留孤儿进程。"""
    import signal
    import time
    while True:
        if os.getppid() != expected:
            if os.getpgrp() == os.getpid():
                os.killpg(os.getpgrp(), signal.SIGKILL)
            os._exit(70)
        time.sleep(.5)


if __name__ == '__main__':
    main()
