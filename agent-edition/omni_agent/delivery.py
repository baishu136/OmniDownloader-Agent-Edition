"""Authenticated outgoing API calls are the only source of send receipts."""
import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import AgentError


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
        if len(self.token)>1024 or any(c in self.token for c in '\r\n\t'):
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
        # No redirect following: a compromised endpoint must not capture tokens.
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), transport=self.transport,
                                     trust_env=False, follow_redirects=False) as client:
            for attempt in range(3):
                # Uploaded stream must be rewound when an explicit 429 allows retry.
                for value in kwargs.get('files', {}).values():
                    if isinstance(value, tuple) and hasattr(value[1], 'seek'): value[1].seek(0)
                try:
                    response = await client.post(self.config.base_url.rstrip('/') + path, **kwargs)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if attempt < 2:
                        await asyncio.sleep(.2 * (attempt + 1))
                        continue
                    raise AgentError('BOT_UNREACHABLE', '无法连接机器人 API，文件已保留') from None
                except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
                    raise AgentError('DELIVERY_UNCERTAIN', '连接中断，发送结果不确定；请管理员核对后决定重试') from None
                if response.status_code == 429:
                    if attempt == 2: raise AgentError('BOT_RATE_LIMITED', '机器人发送限流，文件已保留')
                    delay = min(5, max(0, int(response.headers.get('Retry-After', '1'))))
                    await asyncio.sleep(delay)
                    continue
                if response.status_code >= 500:
                    raise AgentError('DELIVERY_UNCERTAIN', '机器人 API 服务异常，发送结果待核对')
                if response.status_code != 200:
                    raise AgentError('BOT_REJECTED', '机器人 API 拒绝请求，文件已保留')
                try: return response.json()
                except ValueError: raise AgentError('DELIVERY_UNCERTAIN', '机器人 API 返回无效回执') from None

    def check_target(self, target):
        if not self.config.enabled: raise AgentError('BOT_DISABLED', '管理员未启用此发送适配器')
        if target not in self.config.allowed_targets: raise AgentError('TARGET_DENIED', '发送目标不在管理员许可名单')


class TelegramAdapter(APIAdapter):
    async def probe(self):
        if not self.config.enabled: raise AgentError('BOT_DISABLED','请先保存并启用 Telegram 连接')
        data = await self.request('/bot' + self.config.token + '/getMe')
        if data.get('ok') is not True or not data.get('result', {}).get('id'):
            raise AgentError('BOT_AUTH_FAILED', 'Telegram 身份验证失败')
        return {'connected': True, 'adapter': 'telegram', 'mode': 'local' if self.config.local_api else 'cloud',
                'max_document_bytes': self.limit('document')}

    def limit(self, kind):
        # Limits differ by API/method. Photos retain a 10MB method limit.
        method_limit = 10 * 1024**2 if kind == 'photo' else (2000 * 1024**2 if self.config.local_api else 50 * 1024**2)
        return min(method_limit, self.config.max_upload_bytes)

    async def send(self, path, target, kind='auto'):
        self.check_target(target)
        if kind == 'auto':
            suffix = path.suffix.lower()
            kind = 'photo' if suffix in ('.jpg', '.jpeg', '.png') else 'video' if suffix == '.mp4' else 'audio' if suffix in ('.mp3', '.m4a') else 'document'
            if path.stat().st_size > self.limit(kind): kind = 'document'
        if kind not in ('video', 'audio', 'photo', 'document'): raise AgentError('INVALID_PARAMETER', '不支持的发送类型')
        if path.stat().st_size > self.limit(kind): raise AgentError('BOT_FILE_TOO_LARGE', '文件超过当前 Telegram 发送方式的限制')
        method = {'video': 'sendVideo', 'audio': 'sendAudio', 'photo': 'sendPhoto', 'document': 'sendDocument'}[kind]
        with path.open('rb') as stream:
            data = await self.request('/bot' + self.config.token + '/' + method,
                data={'chat_id': target}, files={kind: (path.name, stream, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')})
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

    async def probe(self):
        if not self.config.enabled: raise AgentError('BOT_DISABLED','请先保存并启用 OneBot 连接')
        version = await self.api('get_version_info')
        status = await self.api('get_status')
        if not isinstance(version, dict) or not isinstance(status, dict) or status.get('online') is not True:
            raise AgentError('BOT_OFFLINE', 'OneBot 未确认在线状态')
        return {'connected': True, 'adapter': 'onebot', 'implementation': str(version.get('app_name', 'unknown'))[:80],
                'max_upload_bytes': self.config.max_upload_bytes,
                'file_upload': 'checked_per_target_at_send'}

    async def send(self, path, target, kind='auto'):
        self.check_target(target)
        chat_type, separator, number = target.partition(':')
        if not separator or chat_type not in ('group', 'private') or not number.isdigit():
            raise AgentError('INVALID_TARGET', 'QQ目标格式须为 group:群号 或 private:QQ号')
        if path.stat().st_size > self.config.max_upload_bytes:
            raise AgentError('BOT_FILE_TOO_LARGE', '文件超过管理员配置的 QQ 上传上限')
        root = Path(self.config.shared_root).resolve()
        if not path.resolve().is_relative_to(root): raise AgentError('PATH_DENIED', '文件不在 NapCat 共享目录映射内')
        # Real capability probes; upload_* are protocol implementation extensions.
        await self.probe()
        if chat_type == 'group': await self.api('get_group_file_system_info', {'group_id': int(number)})
        else: await self.api('get_stranger_info', {'user_id': int(number), 'no_cache': True})
        remote = self.config.remote_root.rstrip('/\\') + '/' + path.resolve().relative_to(root).as_posix()
        payload = {'file': remote, 'name': path.name, ('group_id' if chat_type == 'group' else 'user_id'): int(number)}
        result = await self.api('upload_group_file' if chat_type == 'group' else 'upload_private_file', payload)
        # Some implementations return only retcode=0. That means API accepted,
        # but is insufficient for destructive auto-cleanup without a file/message ID.
        if not isinstance(result, dict) or not any(result.get(k) for k in ('file_id', 'id', 'message_id')):
            raise AgentError('DELIVERY_UNCERTAIN', '协议端未返回可核对的文件或消息标识；保留文件等待管理员核对')
        return {'platform': 'onebot', 'target': target,
                **{k: result[k] for k in ('file_id', 'id', 'message_id', 'busid') if k in result}}


def make_adapter(config, transport=None):
    return (TelegramAdapter if config.adapter == 'telegram' else OneBotAdapter)(config, transport)
