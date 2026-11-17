import contextvars
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
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
from .delivery import BotConfig, make_adapter
from .errors import AgentError
from .mcp_server import create_mcp
from .service import DownloadOptions, Service


principal = contextvars.ContextVar('omni_agent_principal', default=None)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Analyze(Input):
    url: str = Field(min_length=1, max_length=8192)
    media_type: Literal['video','images'] = 'video'


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
    state: Literal['QUEUED','DOWNLOADING','PROCESSING','READY','SENDING','SENT','FAILED','CLEANED','CANCELLED','EXPIRED'] | None = None
    limit: int = Field(50, ge=1, le=100)


class Deliver(TaskID):
    adapter: Literal['telegram','onebot']
    target: str = Field(min_length=1, max_length=128)
    request_key: str = Field(min_length=1, max_length=128)
    kind: Literal['auto','video','audio','photo','document'] = 'auto'


class CookieImport(Input):
    platform: str
    text: str = Field(min_length=1, max_length=1024 * 1024)


class Retry(Input):
    reconciled_allow_retry: bool = False


class Identity(Input):
    name: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    targets: list[str] = Field(max_length=200)


SCHEMAS = {'analyze_media': Analyze, 'download_media': Download, 'download_collection': Collection,
    'get_task_status': TaskID, 'cancel_task': TaskID, 'list_tasks': ListTasks, 'get_media_file': FileID,
    'get_service_capabilities': Input, 'deliver_media': Deliver, 'get_delivery_status': DeliveryID}


async def invoke(service, owner, name, arguments):
    try:
        if not owner: raise AgentError('AUTH_REQUIRED', '缺少可信身份上下文')
        if name not in SCHEMAS: raise AgentError('UNKNOWN_TOOL', '工具不存在')
        args = SCHEMAS[name].model_validate(arguments)
        if name == 'analyze_media': result = await service.analyze(owner, args.url, args.media_type)
        elif name == 'download_media': result = await service.download(owner, args.url, args.options, args.request_key,approval_id=args.approval_id)
        elif name == 'download_collection': result = await service.download(owner, args.url, args.options, args.request_key,
            collection=True, start=args.start, count=args.count, approval_id=args.approval_id)
        elif name == 'get_task_status': result = service.store.public_job(args.task_id, owner)
        elif name == 'cancel_task': result = await service.cancel(owner, args.task_id)
        elif name == 'list_tasks':
            query = 'SELECT id FROM jobs WHERE owner=?' + (' AND state=?' if args.state else '') + ' ORDER BY created DESC LIMIT ?'
            rows = service.store.db.execute(query, (owner, args.state, args.limit) if args.state else (owner, args.limit)).fetchall()
            result = {'tasks': [service.store.public_job(r['id'], owner) for r in rows]}
        elif name == 'get_media_file': result = service.file_reference(owner, args.file_id)
        elif name == 'get_service_capabilities': result = service.capabilities()
        elif name == 'deliver_media': result = await service.deliver(owner, args.task_id, args.adapter, args.target, args.kind, args.request_key)
        elif name == 'get_delivery_status':
            row = service.store.delivery(args.delivery_id, owner)
            result = {k: row[k] for k in ('id','task_id','state','error')}
            result['receipts'] = [json.loads(r['receipt']) for r in service.store.db.execute('SELECT receipt FROM receipts WHERE delivery_id=?', (args.delivery_id,))]
        return {'ok': True, 'result': result}
    except AgentError as error: return error.result()
    except ValidationError: return AgentError('INVALID_PARAMETER', '参数不符合工具 Schema').result()
    except Exception: return AgentError('INTERNAL_ERROR', '服务内部错误，请查看管理页最近错误').result()


class SecurityBoundary:
    def __init__(self, app, service): self.app, self.service = app, service

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k,v in scope.get('headers', [])}
        path = scope['path']
        async def error(code, message, status=401):
            await JSONResponse(AgentError(code, message).result(), status_code=status)(scope, receive, send)
        host = headers.get('host', '')
        allowed = {'127.0.0.1', 'localhost', '[::1]', 'testserver'} | set(filter(None, os.environ.get('OMNI_AGENT_ALLOWED_HOSTS', '').split(',')))
        hostname = urlsplit('http://' + host).hostname or ''
        if hostname not in allowed and ('[' + hostname + ']') not in allowed:
            return await error('HOST_DENIED', 'Host 不在管理员许可名单', 403)
        origin = headers.get('origin')
        if origin:
            p = urlsplit(origin)
            valid_origins = {scope.get('scheme', 'http') + '://' + host, os.environ.get('OMNI_AGENT_PUBLIC_ORIGIN', '')}
            if origin not in valid_origins or p.scheme not in ('http','https'):
                return await error('ORIGIN_DENIED', '跨站请求被拒绝', 403)
        owner = None
        if path.startswith(('/api/', '/mcp')):
            auth = headers.get('authorization', '')
            token = auth[7:] if auth.startswith('Bearer ') else ''
            try: owner = self.service.authenticate(token, admin=path.startswith('/api/admin/'))
            except AgentError as exc: return await error(exc.code, exc.message)
        # Bound streamed bodies as well as Content-Length. Never buffer media uploads
        # here: bots upload outbound, and all inbound requests are small JSON.
        data = bytearray()
        while True:
            event = await receive()
            if event['type'] == 'http.disconnect': return
            data.extend(event.get('body', b''))
            if len(data) > 2 * 1024**2: return await error('BODY_TOO_LARGE', '请求超过2MiB上限', 413)
            if not event.get('more_body'): break
        consumed = False
        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type':'http.request','body':bytes(data),'more_body':False}
            return await receive()
        context = principal.set(owner)
        try: await self.app(scope, bounded_receive, send)
        finally: principal.reset(context)


def create_app(service=None):
    service = service or Service()
    async def call(name, arguments): return await invoke(service, principal.get(), name, arguments)
    mcp = create_mcp(call)
    hosts = ['127.0.0.1:*','localhost:*','[::1]:*','testserver'] + list(filter(None, os.environ.get('OMNI_AGENT_ALLOWED_HOSTS', '').split(',')))
    mcp_app = mcp.streamable_http_app(json_response=True, stateless_http=True, max_request_body_size=2 * 1024**2,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
            allowed_origins=['http://127.0.0.1:*','http://localhost:*'] + ([os.environ['OMNI_AGENT_PUBLIC_ORIGIN']] if os.environ.get('OMNI_AGENT_PUBLIC_ORIGIN') else [])))

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            async with mcp.session_manager.run(): yield
        finally: await service.close()

    app = FastAPI(title='OmniDownloader Agent Edition', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service, app.state.mcp = service, mcp
    app.add_middleware(SecurityBoundary, service=service)

    @app.exception_handler(AgentError)
    async def agent_error(request, exc): return JSONResponse(exc.result(), status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc): return JSONResponse(AgentError('INVALID_PARAMETER', '参数无效；请检查字段、类型和范围').result(), status_code=422)

    @app.get('/health')
    async def health():
        healthy = service.dispatcher is not None and not service.dispatcher.done()
        return JSONResponse({'ok':healthy},status_code=200 if healthy else 503)

    @app.post('/api/tools/{name}')
    async def tool(name: str, request: Request):
        try: arguments = await request.json()
        except ValueError: raise AgentError('INVALID_PARAMETER', '需要JSON参数')
        return await call(name, arguments)

    @app.get('/api/files/{file_id}')
    async def media_file(file_id: str):
        record, path = service.store.file(file_id, principal.get())
        return FileResponse(path, filename=path.name, media_type='application/octet-stream', headers={'Cache-Control':'no-store'})

    @app.get('/api/admin/config')
    async def get_settings(): return service.config.settings.model_dump()

    @app.put('/api/admin/config')
    async def save_settings(settings: Settings):
        if settings.cache_dir != service.config.settings.cache_dir:
            active = service.store.db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('QUEUED','DOWNLOADING','PROCESSING','SENDING')").fetchone()[0]
            if active: raise AgentError('TASKS_ACTIVE','活动任务结束前无法更换缓存目录')
            service.prune_workspaces()
        service.config.save(settings)
        return {'ok': True}

    @app.get('/api/admin/status')
    async def status():
        recent = service.store.db.execute("SELECT id,error,updated FROM jobs WHERE error!='' ORDER BY updated DESC LIMIT 10").fetchall()
        counts = {r['state']: r['count'] for r in service.store.db.execute('SELECT state,COUNT(*) count FROM jobs GROUP BY state')}
        deliveries = [dict(r) for r in service.store.db.execute('SELECT id,task_id,adapter,target,state,error FROM deliveries ORDER BY created DESC LIMIT 50')]
        cleanup = [dict(r) for r in service.store.db.execute('SELECT * FROM cleanup_log ORDER BY created DESC LIMIT 5')]
        approvals = [dict(r) for r in service.store.db.execute('SELECT id,summary,expires,approved FROM approvals WHERE used=0 AND expires>?', (time.time(),))]
        return {'capabilities': service.capabilities(), 'cache_bytes': service.store.usage(), 'tasks': counts,
                'deliveries': deliveries, 'errors': [dict(r) for r in recent], 'cleanup': cleanup, 'approvals': approvals,
                'cleanup_error': service.last_cleanup_error, 'mcp_path': '/mcp', 'cache_path': str(service.config.cache)}

    @app.post('/api/admin/cleanup')
    async def cleanup(): return service.store.cleanup()

    @app.post('/api/admin/approvals/{identifier}')
    async def approve(identifier: str):
        cursor = service.store.db.execute('UPDATE approvals SET approved=1 WHERE id=? AND expires>? AND used=0', (identifier,time.time()))
        if not cursor.rowcount: raise AgentError('NOT_FOUND', '确认请求不存在或已过期')
        return {'ok':True}

    @app.post('/api/admin/deliveries/{identifier}/retry')
    async def retry(identifier: str, body: Retry): return service.retry_delivery(identifier, reconcile=body.reconciled_allow_retry)

    @app.get('/api/admin/sessions')
    async def sessions():
        return [service.sessions.status(site) | {'validation': json.loads(service.vault.get('validation-' + str(list(service.domains).index(site))) or '{"state":"UNVERIFIED"}')} for site in service.domains]

    @app.post('/api/admin/sessions')
    async def import_cookie(body: CookieImport):
        if body.platform not in service.domains: raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        text = body.text.strip()
        if not text.startswith(('# Netscape HTTP Cookie File','# HTTP Cookie File')):
            rows = []
            for part in text.split(';'):
                name, sep, value = part.strip().partition('=')
                if not sep or not name or any(c in part for c in '\r\n\t'): raise AgentError('INVALID_COOKIE', 'Cookie格式无效')
                rows.append('.' + service.domains[body.platform][0] + '\tTRUE\t/\tTRUE\t0\t' + name + '\t' + value)
            text = '# Netscape HTTP Cookie File\n' + '\n'.join(rows)
        try: result = service.sessions.import_text(body.platform, text)
        except (ValueError, OSError): raise AgentError('INVALID_COOKIE', 'Cookie文件无效或不包含此平台有效凭据') from None
        service.vault.put('validation-' + str(list(service.domains).index(body.platform)), '{"state":"UNVERIFIED"}')
        return result

    @app.delete('/api/admin/sessions/{platform}')
    async def clear_cookie(platform: str):
        if platform not in service.domains: raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        service.sessions.clear(platform)
        service.vault.put('validation-' + str(list(service.domains).index(platform)), '{"state":"MISSING"}')
        return {'ok':True}

    @app.post('/api/admin/sessions/{platform}/verify')
    async def verify_cookie(platform: str):
        if platform not in service.domains: raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        result = await service.runner.call({'action':'verify_session','platform':platform})
        result['checked_at'] = time.time()
        service.vault.put('validation-' + str(list(service.domains).index(platform)), json.dumps(result))
        return result

    @app.get('/api/admin/bots')
    async def bots():
        return [service.bot_config(name).model_dump(exclude={'token'}) | {'has_token': bool(service.bot_config(name).token)} for name in ('telegram','onebot')]

    @app.put('/api/admin/bots')
    async def save_bot(request: Request):
        try:
            data = await request.json()
            if not data.get('token'): data['token'] = service.bot_config(data.get('adapter')).token
            body = BotConfig.model_validate(data)
        except (ValidationError, ValueError, AttributeError):
            raise AgentError('INVALID_PARAMETER','机器人参数无效；启用时需填写令牌、目标名单及共享目录') from None
        service.vault.put('bot-' + body.adapter, body.model_dump_json())
        service.bot_status.pop(body.adapter, None)
        return {'ok':True}

    @app.post('/api/admin/bots/{name}/probe')
    async def probe_bot(name: str):
        try: result = await make_adapter(service.bot_config(name)).probe()
        except AgentError as error:
            service.bot_status[name] = {'connected':False,'error':error.code}
            raise
        service.bot_status[name] = result
        return result

    @app.get('/api/admin/mcp-config')
    async def mcp_config(request: Request):
        # Tokens are copied from local auth.json by the administrator; never echo
        # raw keys through ordinary settings or MCP responses.
        command = sys.executable
        if getattr(sys,'frozen',False): command = str(Path(command).with_name('OmniDownloaderAgentMCP.exe'))
        args = ['--stdio'] if getattr(sys,'frozen',False) else ['-m','omni_agent','--stdio']
        return {'stdio': {'mcpServers': {'omni-agent': {'command':command,'args':args,
                    'env': {'OMNI_AGENT_DATA': str(service.config.directory), 'OMNI_AGENT_SERVER': str(request.base_url).rstrip('/'),
                            'PYTHONPATH': str(Path(__file__).resolve().parents[1])}}}},
                'http': {'mcpServers': {'omni-agent': {'url': str(request.base_url).rstrip('/') + '/mcp',
                           'headers':{'Authorization':'Bearer <auth.json 中的 agent 令牌或独立身份令牌>'}}}}}

    @app.post('/api/admin/identities')
    async def add_identity(body: Identity):
        if body.name in ('admin','agent'): raise AgentError('INVALID_PARAMETER','保留身份名')
        data = json.loads(service.vault.get('identities') or '{}')
        if body.name in data: raise AgentError('IDENTITY_EXISTS','身份已存在')
        token = secrets.token_urlsafe(32)
        data[body.name] = {'token':token,'targets':body.targets}
        service.vault.put('identities', json.dumps(data))
        return {'name':body.name,'token':token,'notice':'仅此创建响应显示一次；为每个聊天会话创建独立身份'}

    @app.get('/api/admin/identities')
    async def list_identities():
        data = json.loads(service.vault.get('identities') or '{}')
        return [{'name':k,'targets':v['targets']} for k,v in data.items()]

    @app.delete('/api/admin/identities/{name}')
    async def delete_identity(name: str):
        data = json.loads(service.vault.get('identities') or '{}')
        data.pop(name,None)
        service.vault.put('identities',json.dumps(data))
        return {'ok':True}

    static = Path(__file__).parent / 'static'
    @app.get('/')
    async def index(): return FileResponse(static / 'index.html', headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",'Referrer-Policy':'no-referrer'})
    app.mount('/static', StaticFiles(directory=static), name='static')
    app.mount('/', mcp_app)
    return app
