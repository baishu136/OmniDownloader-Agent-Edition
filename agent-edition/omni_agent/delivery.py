"""Authenticated outgoing API calls are the only source of send receipts."""
import asyncio
import mimetypes
from pathlib import Path
import time
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import AgentError

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.flv'}


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

    async def request(self, path, **kwargs):
        # 禁止跟随重定向，防止恶意节点窃取机器人请求头或令牌
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), transport=self.transport,
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
                    delay = min(5, max(0, int(response.headers.get('Retry-After', '1'))))
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

    async def send(self, path, target, kind='auto', **_kwargs):
        self.check_target(target)
        if kind == 'auto':
            suffix = path.suffix.lower()
            kind = 'photo' if suffix in ('.jpg', '.jpeg', '.png') else 'video' if suffix == '.mp4' else 'audio' if suffix in ('.mp3', '.m4a') else 'document'
            if path.stat().st_size > self.limit(kind):
                kind = 'document'
        if kind not in ('video', 'audio', 'photo', 'document'):
            raise AgentError('INVALID_PARAMETER', '不支持的发送类型')
        if path.stat().st_size > self.limit(kind):
            raise AgentError('BOT_FILE_TOO_LARGE', '文件超过当前 Telegram 发送方式的限制')
        method = {'video': 'sendVideo', 'audio': 'sendAudio', 'photo': 'sendPhoto', 'document': 'sendDocument'}[kind]
        with path.open('rb') as stream:
            data = await self.request(
                '/bot' + self.config.token + '/' + method,
                data={'chat_id': target},
                files={kind: (path.name, stream, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')},
            )
        result = data.get('result') or {}
        if data.get('ok') is not True:
            raise AgentError('BOT_REJECTED', 'Telegram 未确认发送成功，文件已保留')
        if not result.get('message_id') or str(result.get('chat', {}).get('id')) != target:
            raise AgentError('DELIVERY_UNCERTAIN', 'Telegram 回执缺少消息或匹配的会话标识')
        return {'platform': 'telegram', 'message_id': result['message_id'], 'chat_id': target}


class OneBotAdapter(APIAdapter):
    async def api(self, action, payload=None):
        data = await self.request('/' + action, json=payload or {}, headers={'Authorization': 'Bearer ' + self.config.token})
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
    def media_category(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return 'image'
        if suffix in VIDEO_EXTENSIONS:
            return 'video'
        return 'file'

    async def probe(self):
        if not self.config.enabled:
            raise AgentError('BOT_DISABLED', '请先保存并启用 OneBot 连接')
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
        return result

    async def send(self, path, target, kind='auto', *, video_mode='file', image_mode='message'):
        self.check_target(target)
        chat_type, number = self._parse_target(target)
        remote = self._map_remote(path)
        probe_info = await self.probe()

        category = self.media_category(path)
        use_message = (
            kind != 'document'
            and path.suffix.lower() != '.zip'
            and ((category == 'image' and image_mode == 'message') or (category == 'video' and video_mode == 'message'))
        )
        if use_message:
            action = 'send_group_msg' if chat_type == 'group' else 'send_private_msg'
            id_key = 'group_id' if chat_type == 'group' else 'user_id'
            segment = {'type': category, 'data': {'file': remote}}
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

        if chat_type == 'group':
            await self.api('get_group_file_system_info', {'group_id': number})
        else:
            await self.api('get_stranger_info', {'user_id': number, 'no_cache': True})
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
        probe_info = await self.probe()
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
                    'content': [{'type': category, 'data': {'file': remote}}],
                },
            })
        action = 'send_group_forward_msg' if chat_type == 'group' else 'send_private_forward_msg'
        id_key = 'group_id' if chat_type == 'group' else 'user_id'
        result = await self.api(action, {id_key: number, 'messages': nodes})
        if not isinstance(result, dict) or not any(result.get(k) for k in ('message_id', 'id', 'res_id')):
            raise AgentError('DELIVERY_UNCERTAIN', '协议端未返回可核对的转发记录标识；保留文件等待核对')
        return {
            'platform': 'onebot',
            'mode': 'forward',
            'count': len(paths),
            'target': target,
            **{k: result[k] for k in ('message_id', 'id', 'res_id') if k in result},
        }


def make_adapter(config, transport=None):
    return (TelegramAdapter if config.adapter == 'telegram' else OneBotAdapter)(config, transport)
