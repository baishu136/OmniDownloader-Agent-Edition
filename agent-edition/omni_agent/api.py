import contextvars
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .errors import AgentError
from .mcp_server import create_mcp
from .service import BotConfigUpdate, DownloadOptions, Service, SettingsUpdate


principal = contextvars.ContextVar('omni_agent_principal', default='agent')


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Analyze(Input):
    url: str = Field(min_length=1, max_length=8192)
    media_type: Literal['video', 'images'] = 'video'


class Download(Input):
    url: str = Field(min_length=1, max_length=8192)
    options: DownloadOptions = Field(default_factory=DownloadOptions)
    request_key: str | None = Field(None, max_length=128)
    approval_id: str | None = Field(None, max_length=64)


class Collection(Download):
    start: int = Field(1, ge=1, le=200)
    count: int = Field(1, ge=1, le=200)
    approval_id: str | None = Field(None, max_length=64)


class TaskID(Input):
    task_id: str = Field(min_length=1, max_length=64)


class FileID(Input):
    file_id: str = Field(min_length=1, max_length=64)


class DeliveryID(Input):
    delivery_id: str = Field(min_length=1, max_length=64)


class ListTasks(Input):
    state: Literal['QUEUED', 'DOWNLOADING', 'PROCESSING', 'READY', 'SENDING', 'SENT', 'FAILED', 'CLEANED', 'CANCELLED', 'EXPIRED'] | None = None
    limit: int = Field(50, ge=1, le=100)


class Deliver(TaskID):
    adapter: Literal['telegram', 'onebot']
    target: str = Field(min_length=1, max_length=128)
    request_key: str = Field(min_length=1, max_length=128)
    kind: Literal['auto', 'video', 'audio', 'photo', 'document'] = 'auto'


class UpdateSettingsArgs(Input):
    settings: SettingsUpdate


class UpdateBotConfigArgs(Input):
    config: BotConfigUpdate


class ProbeBotArgs(Input):
    adapter: Literal['telegram', 'onebot']


class ApprovalID(Input):
    approval_id: str = Field(min_length=1, max_length=64)


class PlatformName(Input):
    platform: str = Field(min_length=1, max_length=64)


class CookieImport(Input):
    platform: str
    text: str = Field(min_length=1, max_length=1024 * 1024)


class Retry(Input):
    reconciled_allow_retry: bool = False


SCHEMAS = {
    'analyze_media': Analyze,
    'download_media': Download,
    'download_collection': Collection,
    'get_task_status': TaskID,
    'cancel_task': TaskID,
    'list_tasks': ListTasks,
    'get_media_file': FileID,
    'get_service_capabilities': Input,
    'deliver_media': Deliver,
    'get_delivery_status': DeliveryID,
    'get_settings': Input,
    'update_settings': UpdateSettingsArgs,
    'get_bot_configs': Input,
    'update_bot_config': UpdateBotConfigArgs,
    'probe_bot': ProbeBotArgs,
    'list_approvals': Input,
    'approve_request': ApprovalID,
    'cleanup_cache': Input,
    'list_cookie_statuses': Input,
    'disable_platform_cookie': PlatformName,
    'remove_platform_cookie': PlatformName,
}


async def invoke(service, owner, name, arguments):
    try:
        owner = owner or 'agent'
        if name not in SCHEMAS:
            raise AgentError('UNKNOWN_TOOL', '工具不存在')
        args = SCHEMAS[name].model_validate(arguments)
        if name == 'analyze_media':
            result = await service.analyze(owner, args.url, args.media_type)
        elif name == 'download_media':
            result = await service.download(owner, args.url, args.options, args.request_key, approval_id=args.approval_id)
        elif name == 'download_collection':
            result = await service.download(owner, args.url, args.options, args.request_key,
                                            collection=True, start=args.start, count=args.count, approval_id=args.approval_id)
        elif name == 'get_task_status':
            result = service.store.public_job(args.task_id, owner)
        elif name == 'cancel_task':
            result = await service.cancel(owner, args.task_id)
        elif name == 'list_tasks':
            query = 'SELECT id FROM jobs WHERE owner=?' + (' AND state=?' if args.state else '') + ' ORDER BY created DESC LIMIT ?'
            rows = service.store.db.execute(query, (owner, args.state, args.limit) if args.state else (owner, args.limit)).fetchall()
            result = {'tasks': [service.store.public_job(r['id'], owner) for r in rows]}
        elif name == 'get_media_file':
            result = service.file_reference(owner, args.file_id)
        elif name == 'get_service_capabilities':
            result = service.capabilities()
        elif name == 'deliver_media':
            result = await service.deliver(owner, args.task_id, args.adapter, args.target, args.kind, args.request_key)
        elif name == 'get_delivery_status':
            row = service.store.delivery(args.delivery_id, owner)
            result = {k: row[k] for k in ('id', 'task_id', 'state', 'error')}
            result['receipts'] = [json.loads(r['receipt']) for r in service.store.db.execute('SELECT receipt FROM receipts WHERE delivery_id=?', (args.delivery_id,))]
        elif name == 'get_settings':
            result = service.get_settings()
        elif name == 'update_settings':
            result = service.update_settings(args.settings.model_dump(exclude_none=True))
        elif name == 'get_bot_configs':
            result = {'bots': service.get_bot_configs()}
        elif name == 'update_bot_config':
            result = service.update_bot_config(args.config.model_dump(exclude_none=True))
        elif name == 'probe_bot':
            result = await service.probe_bot(args.adapter)
        elif name == 'list_approvals':
            result = {'approvals': service.list_approvals()}
        elif name == 'approve_request':
            result = service.approve_request(args.approval_id)
        elif name == 'cleanup_cache':
            result = service.cleanup_cache()
        elif name == 'list_cookie_statuses':
            result = {'platforms': service.list_cookie_statuses()}
        elif name == 'disable_platform_cookie':
            result = service.disable_cookie(args.platform)
        elif name == 'remove_platform_cookie':
            result = service.remove_cookie(args.platform)
        return {'ok': True, 'result': result}
    except AgentError as error:
        return error.result()
    except ValidationError:
        return AgentError('INVALID_PARAMETER', '参数不符合工具 Schema').result()
    except Exception:
        return AgentError('INTERNAL_ERROR', '服务内部错误，请查看管理页最近错误').result()


class SecurityBoundary:
    def __init__(self, app, service):
        self.app, self.service = app, service

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get('headers', [])}

        async def error(code, message, status=403):
            await JSONResponse(AgentError(code, message).result(), status_code=status)(scope, receive, send)

        host = headers.get('host', '')
        allowed = {'127.0.0.1', 'localhost', '[::1]', 'testserver'} | set(filter(None, os.environ.get('OMNI_AGENT_ALLOWED_HOSTS', '').split(',')))
        hostname = urlsplit('http://' + host).hostname or ''
        if hostname not in allowed and ('[' + hostname + ']') not in allowed:
            return await error('HOST_DENIED', 'Host 不在许可名单', 403)
        origin = headers.get('origin')
        if origin:
            p = urlsplit(origin)
            valid_origins = {scope.get('scheme', 'http') + '://' + host, os.environ.get('OMNI_AGENT_PUBLIC_ORIGIN', '')}
            if origin not in valid_origins or p.scheme not in ('http', 'https'):
                return await error('ORIGIN_DENIED', '跨站请求被拒绝', 403)

        # 限制流式请求体最大 2MiB；已取消本机令牌授权校验，直接以默认身份执行
        data = bytearray()
        while True:
            event = await receive()
            if event['type'] == 'http.disconnect':
                return
            data.extend(event.get('body', b''))
            if len(data) > 2 * 1024**2:
                return await error('BODY_TOO_LARGE', '请求超过2MiB上限', 413)
            if not event.get('more_body'):
                break
        consumed = False

        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': bytes(data), 'more_body': False}
            return await receive()

        context = principal.set('agent')
        try:
            await self.app(scope, bounded_receive, send)
        finally:
            principal.reset(context)


def create_app(service=None):
    service = service or Service()

    async def call(name, arguments):
        return await invoke(service, principal.get(), name, arguments)

    mcp = create_mcp(call)
    hosts = ['127.0.0.1:*', 'localhost:*', '[::1]:*', 'testserver'] + list(filter(None, os.environ.get('OMNI_AGENT_ALLOWED_HOSTS', '').split(',')))
    mcp_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=2 * 1024**2,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=['http://127.0.0.1:*', 'http://localhost:*'] + ([os.environ['OMNI_AGENT_PUBLIC_ORIGIN']] if os.environ.get('OMNI_AGENT_PUBLIC_ORIGIN') else []),
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await service.close()

    app = FastAPI(title='OmniDownloader Agent Edition', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service, app.state.mcp = service, mcp
    app.add_middleware(SecurityBoundary, service=service)

    @app.exception_handler(AgentError)
    async def agent_error(request, exc):
        return JSONResponse(exc.result(), status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(AgentError('INVALID_PARAMETER', '参数无效；请检查字段、类型和范围').result(), status_code=422)

    @app.get('/health')
    async def health():
        healthy = service.dispatcher is not None and not service.dispatcher.done()
        return JSONResponse({'ok': healthy}, status_code=200 if healthy else 503)

    @app.post('/api/tools/{name}')
    async def tool(name: str, request: Request):
        try:
            arguments = await request.json()
        except ValueError:
            raise AgentError('INVALID_PARAMETER', '需要JSON参数')
        return await call(name, arguments)

    @app.get('/api/files/{file_id}')
    async def media_file(file_id: str):
        record, path = service.store.file(file_id, principal.get())
        return FileResponse(path, filename=path.name, media_type='application/octet-stream', headers={'Cache-Control': 'no-store'})

    @app.get('/api/admin/config')
    async def get_settings():
        return service.get_settings()

    @app.put('/api/admin/config')
    async def save_settings(settings: Settings):
        service.update_settings(settings.model_dump())
        return {'ok': True}

    @app.get('/api/admin/status')
    async def status():
        recent = service.store.db.execute("SELECT id,error,updated FROM jobs WHERE error!='' ORDER BY updated DESC LIMIT 10").fetchall()
        counts = {r['state']: r['count'] for r in service.store.db.execute('SELECT state,COUNT(*) count FROM jobs GROUP BY state')}
        deliveries = [dict(r) for r in service.store.db.execute('SELECT id,task_id,adapter,target,state,error FROM deliveries ORDER BY created DESC LIMIT 50')]
        cleanup = [dict(r) for r in service.store.db.execute('SELECT * FROM cleanup_log ORDER BY created DESC LIMIT 5')]
        return {
            'capabilities': service.capabilities(),
            'cache_bytes': service.store.usage(),
            'tasks': counts,
            'deliveries': deliveries,
            'errors': [dict(r) for r in recent],
            'cleanup': cleanup,
            'approvals': service.list_approvals(),
            'cleanup_error': service.last_cleanup_error,
            'mcp_path': '/mcp',
            'cache_path': str(service.config.cache),
            'data_path': str(service.config.directory),
        }

    @app.post('/api/admin/cleanup')
    async def cleanup():
        return service.cleanup_cache()

    @app.post('/api/admin/approvals/{identifier}')
    async def approve(identifier: str):
        service.approve_request(identifier)
        return {'ok': True}

    @app.post('/api/admin/deliveries/{identifier}/retry')
    async def retry(identifier: str, body: Retry):
        return service.retry_delivery(identifier, reconcile=body.reconciled_allow_retry)

    @app.get('/api/admin/sessions')
    async def sessions():
        return service.list_cookie_statuses()

    @app.post('/api/admin/sessions')
    async def import_cookie(body: CookieImport):
        return service.import_cookie(body.platform, body.text)

    @app.post('/api/admin/sessions/{platform}/enable')
    async def enable_cookie(platform: str):
        return service.set_cookie_enabled(platform, True)

    @app.post('/api/admin/sessions/{platform}/disable')
    async def disable_cookie(platform: str):
        return service.set_cookie_enabled(platform, False)

    @app.delete('/api/admin/sessions/{platform}')
    async def clear_cookie(platform: str):
        service.remove_cookie(platform)
        return {'ok': True}

    @app.post('/api/admin/sessions/{platform}/verify')
    async def verify_cookie(platform: str):
        return await service.verify_cookie(platform)

    @app.get('/api/admin/bots')
    async def bots():
        return service.get_bot_configs()

    @app.put('/api/admin/bots')
    async def save_bot(request: Request):
        try:
            data = await request.json()
        except ValueError:
            raise AgentError('INVALID_PARAMETER', '需要JSON参数')
        if not isinstance(data, dict):
            raise AgentError('INVALID_PARAMETER', '需要JSON对象')
        service.update_bot_config(data)
        return {'ok': True}

    @app.post('/api/admin/bots/{name}/probe')
    async def probe_bot(name: str):
        return await service.probe_bot(name)

    @app.get('/api/admin/mcp-config')
    async def mcp_config(request: Request):
        command = sys.executable
        if getattr(sys, 'frozen', False):
            command = str(Path(command).with_name('OmniDownloaderAgentMCP.exe'))
        args = ['--stdio'] if getattr(sys, 'frozen', False) else ['-m', 'omni_agent', '--stdio']
        return {
            'stdio': {
                'mcpServers': {
                    'omni-agent': {
                        'command': command,
                        'args': args,
                        'env': {
                            'OMNI_AGENT_DATA': str(service.config.directory),
                            'OMNI_AGENT_SERVER': str(request.base_url).rstrip('/'),
                            'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                        },
                    }
                }
            },
            'http': {
                'mcpServers': {
                    'omni-agent': {
                        'url': str(request.base_url).rstrip('/') + '/mcp',
                    }
                }
            },
        }

    static = Path(__file__).parent / 'static'

    @app.get('/')
    async def index():
        return FileResponse(
            static / 'index.html',
            headers={
                'Cache-Control': 'no-store',
                'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
                'Referrer-Policy': 'no-referrer',
            },
        )

    app.mount('/static', StaticFiles(directory=static), name='static')
    app.mount('/', mcp_app)
    return app
