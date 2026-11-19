"""Authenticated outgoing API calls are the only source of send receipts."""
import asyncio
import contextlib
import io
import json
import mimetypes
from pathlib import Path
import re
import time
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import AgentError

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif', '.jfif'}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.flv'}
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.flac', '.opus', '.ogg', '.wav', '.aac'}
_TELEGRAM_CONVERT_PHOTO_EXTS = {'.webp', '.avif', '.bmp', '.jfif'}
_WIN_DRIVE_RE = re.compile(r'^[A-Za-z]:[/\\]')


def _magic_media_kind(path: Path) -> str:
    """读取文件头魔数识别真实媒体类别，防止扩展名错配导致图片按视频发送。"""
    try:
        with path.open('rb') as stream:
            head = stream.read(64)
    except OSError:
        return ''
    if len(head) < 4:
        return ''
    if head.startswith(b'\xff\xd8\xff') or head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'photo'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'animation'
    if len(head) >= 12 and head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return 'photo'
    if len(head) >= 12 and head[4:8] == b'ftyp' and any(brand in head[8:40] for brand in (b'avif', b'avis')):
        return 'photo'
    if len(head) >= 18 and head.startswith(b'BM'):
        return 'photo'
    if head[:4] in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'):
        return 'document'
    if head.startswith(b'ID3') or head.startswith(b'fLaC') or head.startswith(b'OggS'):
        return 'audio'
    if len(head) >= 16 and head[4:8] == b'ftyp' and b'M4A ' in head[8:24]:
        return 'audio'
    return ''


def detect_media_kind(path: Path) -> str:
    """结合二进制文件头魔数与扩展名推断真实发送类型（photo/animation/video/audio/document）。"""
    magic = _magic_media_kind(path)
    if magic:
        return magic
    suffix = path.suffix.lower()
    if suffix == '.zip':
        return 'document'
    if suffix == '.gif':
        return 'animation'
    if suffix in IMAGE_EXTENSIONS:
        return 'photo'
    if suffix in VIDEO_EXTENSIONS:
        return 'video'
    if suffix in AUDIO_EXTENSIONS:
        return 'audio'
    return ''


class BotConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    adapter: Literal['telegram', 'onebot']
    enabled: bool = False
    base_url: str = ''
    token: str = ''
    local_api: bool = False
    max_upload_bytes: int = Field(50 * 1024**2, ge=1024, le=2000 * 1024**2)
    allowed_targets: list[str] = Field(default_factory=list, max_length=200)
    shared_root: str = ''
    remote_root: str = ''

    @model_validator(mode='after')
    def validate_endpoint(self):
        if len(self.token) > 1024 or any(c in self.token for c in '\r\n\t'):
            raise ValueError('机器人令牌格式无效')
        if self.enabled:
            p = urlsplit(self.base_url)
            if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment:
                raise ValueError('机器人 API 地址必须是无凭据、无查询参数的HTTP(S)地址')
            if not self.token or not self.allowed_targets:
                raise ValueError('启用机器人需要令牌和允许发送的目标名单')
            if self.adapter == 'telegram' and not self.local_api and self.base_url.rstrip('/') != 'https://api.telegram.org':
                raise ValueError('公共 Telegram 模式仅允许官方 API；自建端点请选择本地 Bot API')
            if self.adapter == 'onebot' and (not self.shared_root or not self.remote_root):
                raise ValueError('OneBot 文件上传需配置双方共享目录映射')
        return self


class BotDeliveryAdapter(Protocol):
    async def probe(self) -> dict: ...
    async def send(self, path: Path, target: str, kind: str) -> dict: ...


class APIAdapter:
    def __init__(self, config, transport=None):
        self.config, self.transport = config, transport

    async def request(self, path, *, timeout: float | None = None, **kwargs):
        # 禁止跟随重定向，防止恶意节点窃取机器人请求头或令牌
        read_timeout = float(timeout) if timeout is not None else 120.0
        async with httpx.AsyncClient(timeout=httpx.Timeout(read_timeout, connect=10), transport=self.transport,
                                     trust_env=False, follow_redirects=False) as client:
            for attempt in range(3):
                for value in kwargs.get('files', {}).values():
                    if isinstance(value, tuple) and hasattr(value[1], 'seek'):
                        value[1].seek(0)
                try:
                    response = await client.post(self.config.base_url.rstrip('/') + path, **kwargs)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if attempt < 2:
                        await asyncio.sleep(.2 * (attempt + 1))
                        continue
                    raise AgentError('BOT_UNREACHABLE', '无法连接机器人 API，文件已保留') from None
                except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
                    raise AgentError('DELIVERY_UNCERTAIN', '连接中断，发送结果不确定；请核对后决定重试') from None
                if response.status_code == 429:
                    if attempt == 2:
                        raise AgentError('BOT_RATE_LIMITED', '机器人发送限流，文件已保留')
                    try:
                        retry_after = int(response.headers.get('Retry-After', '1'))
                    except ValueError:
                        retry_after = 1
                    delay = min(30, max(1, retry_after))
                    await asyncio.sleep(delay)
                    continue
                if response.status_code >= 500:
                    raise AgentError('DELIVERY_UNCERTAIN', '机器人 API 服务异常，发送结果待核对')
                if response.status_code != 200:
                    raise AgentError('BOT_REJECTED', '机器人 API 拒绝请求，文件已保留')
                try:
                    return response.json()
                except ValueError:
                    raise AgentError('DELIVERY_UNCERTAIN', '机器人 API 返回无效回执') from None

    def check_target(self, target):
        if not self.config.enabled:
            raise AgentError('BOT_DISABLED', '未启用此发送适配器')
        if target not in self.config.allowed_targets:
            raise AgentError('TARGET_DENIED', '发送目标不在许可名单')


class TelegramAdapter(APIAdapter):
    async def probe(self):
        if not self.config.enabled:
            raise AgentError('BOT_DISABLED', '请先保存并启用 Telegram 连接')
        data = await self.request('/bot' + self.config.token + '/getMe')
        user = data.get('result') or {}
        if data.get('ok') is not True or not user.get('id'):
            raise AgentError('BOT_AUTH_FAILED', 'Telegram 身份验证失败')
        return {
            'connected': True,
            'adapter': 'telegram',
            'mode': 'local' if self.config.local_api else 'cloud',
            'bot_id': user.get('id'),
            'username': user.get('username', ''),
            'first_name': user.get('first_name', ''),
            'max_document_bytes': self.limit('document'),
            'checked_at': time.time(),
        }

    def limit(self, kind):
        method_limit = 10 * 1024**2 if kind == 'photo' else (2000 * 1024**2 if self.config.local_api else 50 * 1024**2)
        return min(method_limit, self.config.max_upload_bytes)

    def _convert_photo_in_memory(self, path: Path) -> tuple[str, io.BytesIO, str] | None:
        """将 WebP / AVIF / BMP / JFIF 或错标扩展名图片在内存中转为 Telegram 兼容 JPEG/PNG，不改动磁盘受控原文件。"""
        try:
            from PIL import Image, ImageOps
            with Image.open(path) as opened:
                img = ImageOps.exif_transpose(opened)
                has_alpha = img.mode in ('RGBA', 'LA', 'PA') or (img.mode == 'P' and 'transparency' in img.info)
                buf = io.BytesIO()
                if has_alpha:
                    if img.mode != 'RGBA':
                        img = img.convert('RGBA')
                    img.save(buf, format='PNG', optimize=True)
                    ext, mime = '.png', 'image/png'
                else:
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    img.save(buf, format='JPEG', quality=92, optimize=True)
                    ext, mime = '.jpg', 'image/jpeg'
                if buf.tell() <= 0 or buf.tell() > self.limit('photo'):
                    return None
                buf.seek(0)
                return f'{path.stem}{ext}', buf, mime
        except Exception:
            return None

    def media_group_kind(self, path: Path) -> str | None:
        """判断文件是否可纳入 Telegram sendMediaGroup（photo / video）。"""
        suffix = path.suffix.lower()
        actual = detect_media_kind(path)
        size = path.stat().st_size
        if actual == 'animation' or suffix == '.gif':
            return None
        if actual == 'photo':
            if suffix in ('.jpg', '.jpeg', '.png') and _magic_media_kind(path) in ('', 'photo'):
                return 'photo' if size <= self.limit('photo') else None
            return 'photo' if size <= self.limit('document') else None
        if actual == 'video' and suffix == '.mp4':
            return 'video' if size <= self.limit('video') else None
        return None

    async def send(self, path, target, kind='auto', **_kwargs):
        self.check_target(target)
        suffix = path.suffix.lower()
        magic_kind = _magic_media_kind(path)
        actual_kind = magic_kind or detect_media_kind(path) or 'document'
        is_image_or_anim = magic_kind in ('photo', 'animation') or suffix in IMAGE_EXTENSIONS
        is_zip = magic_kind == 'document' or suffix == '.zip'
        converted_photo = None
        if kind != 'document' and (kind == 'auto' or is_image_or_anim or is_zip):
            if actual_kind == 'animation':
                kind = 'animation'
            elif actual_kind == 'photo':
                if suffix in _TELEGRAM_CONVERT_PHOTO_EXTS or (magic_kind == 'photo' and suffix not in ('.jpg', '.jpeg', '.png')):
                    converted_photo = self._convert_photo_in_memory(path)
                    kind = 'photo' if converted_photo else 'document'
                else:
                    kind = 'photo'
            elif actual_kind in ('video', 'audio'):
                kind = actual_kind
            else:
                kind = 'document'
            if converted_photo is None and path.stat().st_size > self.limit(kind):
                kind = 'document'
        elif kind == 'photo' and suffix in _TELEGRAM_CONVERT_PHOTO_EXTS:
            converted_photo = self._convert_photo_in_memory(path)
        if kind not in ('video', 'audio', 'photo', 'animation', 'document'):
            raise AgentError('INVALID_PARAMETER', '不支持的发送类型')
        if converted_photo is None and path.stat().st_size > self.limit(kind):
            raise AgentError('BOT_FILE_TOO_LARGE', '文件超过当前 Telegram 发送方式的限制')
        method = {
            'video': 'sendVideo',
            'audio': 'sendAudio',
            'photo': 'sendPhoto',
            'animation': 'sendAnimation',
            'document': 'sendDocument',
        }[kind]
        form_data = {'chat_id': target}
        if kind == 'video':
            form_data['supports_streaming'] = 'true'
        if converted_photo is not None:
            upload_name, upload_buf, upload_mime = converted_photo
            data = await self.request(
                '/bot' + self.config.token + '/' + method,
                data=form_data,
                files={kind: (upload_name, upload_buf, upload_mime)},
            )
        else:
            with path.open('rb') as stream:
                data = await self.request(
                    '/bot' + self.config.token + '/' + method,
                    data=form_data,
                    files={kind: (path.name, stream, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')},
                )
        result = data.get('result') or {}
        if data.get('ok') is not True:
            raise AgentError('BOT_REJECTED', 'Telegram 未确认发送成功，文件已保留')
        if not result.get('message_id') or str(result.get('chat', {}).get('id')) != target:
            raise AgentError('DELIVERY_UNCERTAIN', 'Telegram 回执缺少消息或匹配的会话标识')
        return {'platform': 'telegram', 'message_id': result['message_id'], 'chat_id': target}

    async def send_media_group(self, paths: list[Path], target: str, *, caption: str = '') -> list[dict]:
        """以 Telegram sendMediaGroup 聚合发送 2~10 个图片/视频，减少刷屏并返回逐条回执。"""
        self.check_target(target)
        if not 2 <= len(paths) <= 10:
            raise AgentError('INVALID_PARAMETER', 'Telegram 媒体组仅支持单批 2 至 10 个媒体')
        media_items = []
        files_payload = {}
        with contextlib.ExitStack() as stack:
            for idx, path in enumerate(paths):
                key = f'media_{idx}'
                suffix = path.suffix.lower()
                actual = detect_media_kind(path)
                if actual == 'video' and suffix == '.mp4':
                    if path.stat().st_size > self.limit('video'):
                        raise AgentError('BOT_FILE_TOO_LARGE', '视频超过 Telegram 发送上限')
                    stream = stack.enter_context(path.open('rb'))
                    files_payload[key] = (path.name, stream, 'video/mp4')
                    entry = {'type': 'video', 'media': f'attach://{key}', 'supports_streaming': True}
                elif actual == 'photo':
                    if suffix in _TELEGRAM_CONVERT_PHOTO_EXTS or (_magic_media_kind(path) == 'photo' and suffix not in ('.jpg', '.jpeg', '.png')):
                        converted = self._convert_photo_in_memory(path)
                        if converted is None:
                            raise AgentError('BOT_FILE_TOO_LARGE', '转换后的图片超过 Telegram 照片发送上限')
                        files_payload[key] = converted
                    else:
                        if path.stat().st_size > self.limit('photo'):
                            raise AgentError('BOT_FILE_TOO_LARGE', '图片超过 Telegram 照片发送上限')
                        stream = stack.enter_context(path.open('rb'))
                        files_payload[key] = (path.name, stream, mimetypes.guess_type(path.name)[0] or 'image/jpeg')
                    entry = {'type': 'photo', 'media': f'attach://{key}'}
                else:
                    raise AgentError('BOT_REJECTED', '媒体类型不支持打包为 Telegram 媒体组')
                if idx == 0 and caption:
                    entry['caption'] = str(caption).strip()[:1024]
                media_items.append(entry)

            data = await self.request(
                '/bot' + self.config.token + '/sendMediaGroup',
                timeout=min(300.0, max(120.0, 30.0 * len(paths))),
                data={'chat_id': target, 'media': json.dumps(media_items, ensure_ascii=False)},
                files=files_payload,
            )
        results = data.get('result')
        if data.get('ok') is not True:
            raise AgentError('BOT_REJECTED', 'Telegram 媒体组未确认发送成功，文件已保留')
        if not isinstance(results, list) or len(results) != len(paths):
            raise AgentError('DELIVERY_UNCERTAIN', 'Telegram 媒体组回执数量不完整；请核对后决定重试')
        receipts = []
        for item in results:
            if not isinstance(item, dict) or not item.get('message_id') or str((item.get('chat') or {}).get('id')) != target:
                raise AgentError('DELIVERY_UNCERTAIN', 'Telegram 媒体组回执缺少消息或匹配的会话标识')
            receipts.append({
                'platform': 'telegram',
                'mode': 'media_group',
                'message_id': item['message_id'],
                'chat_id': target,
            })
        return receipts

    async def send_text(self, target: str, text: str, *, message_id=None) -> dict:
        """向已许可的 Telegram 会话发送文本状态消息，支持在已有 message_id 上原地更新。"""
        self.check_target(target)
        clean_text = str(text or '').strip()[:4000]
        if not clean_text:
            raise AgentError('INVALID_PARAMETER', '状态消息文本不能为空')
        if message_id:
            try:
                edited = await self.request(
                    '/bot' + self.config.token + '/editMessageText',
                    json={'chat_id': target, 'message_id': message_id, 'text': clean_text},
                )
                if edited.get('ok') is True:
                    res = edited.get('result') or {}
                    return {'platform': 'telegram', 'message_id': res.get('message_id', message_id), 'chat_id': target, 'edited': True}
            except AgentError:
                pass
        data = await self.request(
            '/bot' + self.config.token + '/sendMessage',
            json={'chat_id': target, 'text': clean_text},
        )
        result = data.get('result') or {}
        if data.get('ok') is not True or not result.get('message_id'):
            raise AgentError('BOT_REJECTED', 'Telegram 文本通知发送失败')
        return {'platform': 'telegram', 'message_id': result['message_id'], 'chat_id': target, 'edited': False}


class OneBotAdapter(APIAdapter):
    def __init__(self, config, transport=None):
        super().__init__(config, transport)
        self._probe_cache: dict | None = None
        self._checked_targets: set[str] = set()

    async def api(self, action, payload=None, *, timeout: float | None = None):
        data = await self.request('/' + action, timeout=timeout, json=payload or {}, headers={'Authorization': 'Bearer ' + self.config.token})
        if data.get('status') != 'ok' or data.get('retcode') != 0:
            code = 'CAPABILITY_UNAVAILABLE' if data.get('retcode') in (1404, 404) else 'BOT_REJECTED'
            raise AgentError(code, 'OneBot 未确认操作成功；请检查协议端 API 能力或权限')
        return data.get('data')

    def _parse_target(self, target):
        chat_type, separator, number = target.partition(':')
        if not separator or chat_type not in ('group', 'private') or not number.isdigit():
            raise AgentError('INVALID_TARGET', 'QQ目标格式须为 group:群号 或 private:QQ号')
        return chat_type, int(number)

    def _map_remote(self, path: Path):
        if path.stat().st_size > self.config.max_upload_bytes:
            raise AgentError('BOT_FILE_TOO_LARGE', '文件超过配置的 QQ 上传上限')
        root = Path(self.config.shared_root).resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise AgentError('PATH_DENIED', '文件不在 NapCat 共享目录映射内')
        return self.config.remote_root.rstrip('/\\') + '/' + resolved.relative_to(root).as_posix()

    @staticmethod
    def _media_file_uri(remote: str) -> str:
        """Windows 带盘符路径转为 file:/// URI 以兼容 OneBot 媒体消息段，Linux 容器路径保持原样。"""
        if _WIN_DRIVE_RE.match(remote):
            return 'file:///' + remote.replace('\\', '/')
        return remote

    @staticmethod
    def media_category(path: Path) -> str:
        actual = detect_media_kind(path)
        if actual in ('photo', 'animation'):
            return 'image'
        if actual == 'video':
            return 'video'
        return 'file'

    async def probe(self, *, use_cache: bool = False):
        if not self.config.enabled:
            raise AgentError('BOT_DISABLED', '请先保存并启用 OneBot 连接')
        if use_cache and self._probe_cache is not None:
            return self._probe_cache
        version = await self.api('get_version_info')
        status = await self.api('get_status')
        if not isinstance(version, dict) or not isinstance(status, dict) or status.get('online') is not True:
            raise AgentError('BOT_OFFLINE', 'OneBot 未确认在线状态')
        result = {
            'connected': True,
            'adapter': 'onebot',
            'implementation': str(version.get('app_name', 'unknown'))[:80],
            'app_version': str(version.get('app_version', ''))[:40],
            'online': True,
            'max_upload_bytes': self.config.max_upload_bytes,
            'file_upload': 'checked_per_target_at_send',
            'checked_at': time.time(),
        }
        try:
            login = await self.api('get_login_info')
            if isinstance(login, dict) and login.get('user_id'):
                result['bot_id'] = str(login['user_id'])
                result['nickname'] = str(login.get('nickname', ''))[:40]
        except AgentError:
            pass
        self._probe_cache = result
        return result

    async def send(self, path, target, kind='auto', *, video_mode='file', image_mode='message'):
        self.check_target(target)
        chat_type, number = self._parse_target(target)
        remote = self._map_remote(path)
        probe_info = await self.probe(use_cache=True)

        category = self.media_category(path)
        use_message = (
            kind != 'document'
            and path.suffix.lower() != '.zip'
            and ((category == 'image' and image_mode == 'message') or (category == 'video' and video_mode == 'message'))
        )
        if use_message:
            action = 'send_group_msg' if chat_type == 'group' else 'send_private_msg'
            id_key = 'group_id' if chat_type == 'group' else 'user_id'
            segment = {'type': category, 'data': {'file': self._media_file_uri(remote)}}
            result = await self.api(action, {id_key: number, 'message': [segment]})
            if not isinstance(result, dict) or not any(result.get(k) for k in ('message_id', 'id')):
                raise AgentError('DELIVERY_UNCERTAIN', '协议端未返回可核对的消息标识；保留文件等待核对')
            return {
                'platform': 'onebot',
                'mode': 'message',
                'media_type': category,
                'target': target,
                **{k: result[k] for k in ('message_id', 'id') if k in result},
            }

        target_key = f'file:{target}'
        if target_key not in self._checked_targets:
            if chat_type == 'group':
                await self.api('get_group_file_system_info', {'group_id': number})
            else:
                await self.api('get_stranger_info', {'user_id': number, 'no_cache': True})
            self._checked_targets.add(target_key)
        payload = {'file': remote, 'name': path.name, ('group_id' if chat_type == 'group' else 'user_id'): number}
        result = await self.api('upload_group_file' if chat_type == 'group' else 'upload_private_file', payload)
        if not isinstance(result, dict) or not any(result.get(k) for k in ('file_id', 'id', 'message_id')):
            raise AgentError('DELIVERY_UNCERTAIN', '协议端未返回可核对的文件或消息标识；保留文件等待核对')
        return {
            'platform': 'onebot',
            'mode': 'file',
            'target': target,
            'implementation': probe_info.get('implementation', ''),
            **{k: result[k] for k in ('file_id', 'id', 'message_id', 'busid') if k in result},
        }

    async def send_forward(self, paths: list[Path], target: str, title: str = '') -> dict:
        """将合集或图集的多个媒体文件打包为 OneBot 合并转发聊天记录发出。"""
        self.check_target(target)
        chat_type, number = self._parse_target(target)
        probe_info = await self.probe(use_cache=True)
        bot_uid = str(probe_info.get('bot_id') or '10000')
        bot_name = probe_info.get('nickname') or 'OmniDownloader'

        nodes = []
        if title:
            nodes.append({
                'type': 'node',
                'data': {'user_id': bot_uid, 'nickname': bot_name, 'content': [{'type': 'text', 'data': {'text': title}}]},
            })
        for path in paths:
            remote = self._map_remote(path)
            category = self.media_category(path)
            if category not in ('image', 'video'):
                raise AgentError('INVALID_PARAMETER', '仅图片和视频支持打包进转发聊天记录')
            nodes.append({
                'type': 'node',
                'data': {
                    'user_id': bot_uid,
                    'nickname': bot_name,
                    'content': [{'type': category, 'data': {'file': self._media_file_uri(remote)}}],
                },
            })
        action = 'send_group_forward_msg' if chat_type == 'group' else 'send_private_forward_msg'
        id_key = 'group_id' if chat_type == 'group' else 'user_id'
        timeout = min(300.0, max(120.0, 30.0 * len(paths)))
        result = await self.api(action, {id_key: number, 'messages': nodes}, timeout=timeout)
        if not isinstance(result, dict) or not any(result.get(k) for k in ('message_id', 'id', 'res_id')):
            raise AgentError('DELIVERY_UNCERTAIN', '协议端未返回可核对的转发记录标识；保留文件等待核对')
        return {
            'platform': 'onebot',
            'mode': 'forward',
            'count': len(paths),
            'target': target,
            **{k: result[k] for k in ('message_id', 'id', 'res_id') if k in result},
        }

    async def send_text(self, target: str, text: str, *, message_id=None) -> dict:
        """向已许可的 QQ 群聊或私聊发送文本状态通知（忽略 message_id 编辑要求，始终新发）。"""
        self.check_target(target)
        chat_type, number = self._parse_target(target)
        clean_text = str(text or '').strip()[:4500]
        if not clean_text:
            raise AgentError('INVALID_PARAMETER', '状态消息文本不能为空')
        action = 'send_group_msg' if chat_type == 'group' else 'send_private_msg'
        id_key = 'group_id' if chat_type == 'group' else 'user_id'
        result = await self.api(action, {id_key: number, 'message': [{'type': 'text', 'data': {'text': clean_text}}]})
        return {
            'platform': 'onebot',
            'mode': 'text',
            'target': target,
            'edited': False,
            **({k: result[k] for k in ('message_id', 'id') if k in result} if isinstance(result, dict) else {}),
        }


def make_adapter(config, transport=None):
    return (TelegramAdapter if config.adapter == 'telegram' else OneBotAdapter)(config, transport)
