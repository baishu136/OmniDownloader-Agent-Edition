import contextvars
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .cookie_login import MULTI_PLATFORM_KEY, find_local_browser, local_browser_available
from .desktop_tools import create_directory, list_directories, pick_directory, probe_proxy_connectivity
from .errors import AgentError
from .mcp_server import create_mcp
from .service import BotConfigUpdate, DownloadOptions, Service, SettingsUpdate
from .visualization import build_visualization


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
    notify_adapter: Literal['telegram', 'onebot'] | None = None
    notify_target: str | None = Field(None, max_length=128)


class Collection(Download):
    start: int = Field(1, ge=1, le=200)
    count: int = Field(1, ge=1, le=200)
    approval_id: str | None = Field(None, max_length=64)


class TaskID(Input):
    task_id: str = Field(min_length=1, max_length=64)
    after_seq: int | None = Field(None, ge=0)
    wait_seconds: float = Field(0.0, ge=0.0, le=30.0)


class FileID(Input):
    file_id: str = Field(min_length=1, max_length=64)


class DeliveryID(Input):
    delivery_id: str = Field(min_length=1, max_length=64)
    after_seq: int | None = Field(None, ge=0)
    wait_seconds: float = Field(0.0, ge=0.0, le=30.0)


class ListTasks(Input):
    state: Literal['QUEUED', 'DOWNLOADING', 'PROCESSING', 'READY', 'SENDING', 'SENT', 'FAILED', 'CLEANED', 'CANCELLED', 'EXPIRED'] | None = None
    limit: int = Field(50, ge=1, le=100)


class Deliver(Input):
    task_id: str = Field(min_length=1, max_length=64)
    adapter: Literal['telegram', 'onebot']
    target: str = Field(min_length=1, max_length=128)
    request_key: str = Field(min_length=1, max_length=128)
    kind: Literal['auto', 'video', 'audio', 'photo', 'document'] = 'auto'
    after_seq: int | None = Field(None, ge=0)


class UpdateSettingsArgs(Input):
    settings: SettingsUpdate


class ResetSettingsArgs(Input):
    reset_cache_dir: bool = False


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


class SyncBrowserProfileArgs(Input):
    include_manual: bool = False


class BrowserAccountCreateArgs(Input):
    label: str = Field(min_length=1, max_length=80)


class BrowserAccountOpenArgs(Input):
    platform: str = 'YouTube'


class BrowserAccountLogoutArgs(Input):
    account_ids: list[str] = Field(min_length=1, max_length=20)


class ManageGoogleArgs(Input):
    account_id: str = ''


class BrowserNavigateArgs(Input):
    target: Literal['google', 'platform', 'reload']


class BrowserPrefsUpdateArgs(Input):
    browser: Literal['auto', 'chrome', 'edge', 'brave', 'chromium'] | None = None
    mode: Literal['auto', 'clean', 'cdp'] | None = None


class VisualTestArgs(Input):
    adapter: str = ''
    target: str = ''


class AutoStartArgs(Input):
    enabled: bool


class Retry(Input):
    reconciled_allow_retry: bool = False


class PickDirArgs(Input):
    initial_dir: str = ''
    title: str = '选择文件夹'


class BrowseDirArgs(Input):
    path: str = ''


class CreateDirArgs(Input):
    parent: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=255)


class ProxyTestArgs(Input):
    proxy: str = ''
    target: str = ''


class BridgeRegisterArgs(Input):
    bridge_id: str = Field(min_length=1, max_length=128)
    kind: str = Field('http_api', min_length=1, max_length=40)
    name: str = Field('', max_length=80)
    transport: str = Field('http', max_length=40)
    client_name: str = Field('', max_length=80)
    client_version: str = Field('', max_length=40)
    pid: int | None = Field(None, ge=1)
    routes: list[str] | None = None


class BridgeUnregisterArgs(Input):
    bridge_id: str = Field(min_length=1, max_length=128)


SCHEMAS = {
    'analyze_media': Analyze,
    'download_media': Download,
    'download_collection': Collection,
    'get_task_status': TaskID,
    'cancel_task': TaskID,
    'list_tasks': ListTasks,
    'get_media_file': FileID,
    'get_service_capabilities': Input,
    'list_bridges': Input,
    'deliver_media': Deliver,
    'get_delivery_status': DeliveryID,
    'get_settings': Input,
    'update_settings': UpdateSettingsArgs,
    'reset_settings': ResetSettingsArgs,
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
            result = await service.download(
                owner, args.url, args.options, args.request_key,
                approval_id=args.approval_id,
                notify_adapter=args.notify_adapter,
                notify_target=args.notify_target,
            )
        elif name == 'download_collection':
            result = await service.download(
                owner, args.url, args.options, args.request_key,
                collection=True, start=args.start, count=args.count,
                approval_id=args.approval_id,
                notify_adapter=args.notify_adapter,
                notify_target=args.notify_target,
            )
        elif name == 'get_task_status':
            result = await service.wait_task_status(
                owner, args.task_id, after_seq=args.after_seq, wait_seconds=args.wait_seconds
            )
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
        elif name == 'list_bridges':
            result = {'bridges': service.bridges.list_bridges(), 'hints': service.bridges.local_hints, 'bots': service.get_bot_configs()}
        elif name == 'deliver_media':
            result = await service.deliver(
                owner, args.task_id, args.adapter, args.target, args.kind, args.request_key, after_seq=args.after_seq
            )
        elif name == 'get_delivery_status':
            result = await service.wait_delivery_status(
                owner, args.delivery_id, after_seq=args.after_seq, wait_seconds=args.wait_seconds
            )
        elif name == 'get_settings':
            result = service.get_settings()
        elif name == 'update_settings':
            result = service.update_settings(args.settings.model_dump(exclude_none=True))
        elif name == 'reset_settings':
            result = service.reset_settings(reset_cache_dir=args.reset_cache_dir)
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
        res = error.result()
        if name in ('analyze_media', 'download_media', 'download_collection', 'get_task_status', 'deliver_media', 'get_delivery_status', 'cancel_task'):
            res['visualization'] = build_visualization(
                service.config.settings, 'error', error_code=error.code, error_message=error.message
            )
        return res
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

        client_host = (scope.get('client') or ('', 0))[0]
        self.service.bridges.observe_http_request(scope.get('path', ''), headers, str(client_host))

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

    async def on_mcp_session(client_name, client_version):
        service.bridges.observe_mcp_session(client_name, client_version, transport='streamable-http')

    mcp = create_mcp(call, on_session=on_mcp_session)
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

    @app.patch('/api/admin/config')
    async def patch_settings(patch: SettingsUpdate):
        return {'ok': True, 'settings': service.update_settings(patch.model_dump(exclude_none=True))}

    @app.post('/api/admin/config/reset')
    async def reset_settings(body: ResetSettingsArgs | None = None):
        return {'ok': True, **service.reset_settings(reset_cache_dir=bool(body and body.reset_cache_dir))}

    @app.post('/api/admin/pick-directory')
    async def admin_pick_directory(body: PickDirArgs):
        return await pick_directory(body.initial_dir, body.title)

    @app.post('/api/admin/fs/list')
    async def admin_list_directories(body: BrowseDirArgs):
        return list_directories(body.path)

    @app.post('/api/admin/fs/mkdir')
    async def admin_create_directory(body: CreateDirArgs):
        return create_directory(body.parent, body.name)

    @app.post('/api/admin/proxy/test')
    async def admin_test_proxy(body: ProxyTestArgs):
        return await probe_proxy_connectivity(body.proxy, body.target)

    @app.post('/api/admin/visual/test')
    async def admin_test_visualization(body: VisualTestArgs | None = None):
        return await service.test_visualization(
            adapter=(body.adapter if body else ''),
            target=(body.target if body else ''),
        )

    @app.get('/api/admin/status')
    async def status():
        recent = service.store.db.execute("SELECT id,error,error_message,updated FROM jobs WHERE error!='' ORDER BY updated DESC LIMIT 10").fetchall()
        counts = {r['state']: r['count'] for r in service.store.db.execute('SELECT state,COUNT(*) count FROM jobs GROUP BY state')}
        deliveries = [dict(r) for r in service.store.db.execute('SELECT id,task_id,adapter,target,state,error,error_message FROM deliveries ORDER BY created DESC LIMIT 50')]
        cleanup = [dict(r) for r in service.store.db.execute('SELECT * FROM cleanup_log ORDER BY created DESC LIMIT 5')]
        s = service.config.settings
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
            'download_dir': s.download_dir,
            'image_dir': s.image_dir or s.download_dir,
            'bots': service.get_bot_configs(),
            'autostart': service.get_autostart_status(),
            'bridges': service.bridges.list_bridges(),
            'bridge_hints': service.bridges.local_hints,
            'local_browser': service.get_browser_preferences().get('resolved_name') or ((find_local_browser() or {}).get('name', '')),
        }

    @app.get('/api/admin/autostart')
    async def admin_autostart_get():
        return service.get_autostart_status()

    @app.put('/api/admin/autostart')
    async def admin_autostart_set(body: AutoStartArgs):
        return service.set_autostart(body.enabled)

    @app.get('/api/admin/bridges')
    async def admin_bridges():
        return await service.scan_bridges(probe_bots=False)

    @app.post('/api/admin/bridges/scan')
    async def admin_scan_bridges():
        return await service.scan_bridges(probe_bots=True)

    @app.post('/api/bridges/register')
    @app.post('/api/bridges/heartbeat')
    async def register_bridge(body: BridgeRegisterArgs, request: Request):
        entry = service.bridges.register(
            bridge_id=body.bridge_id,
            kind=body.kind,
            name=body.name,
            transport=body.transport,
            client_name=body.client_name,
            client_version=body.client_version,
            pid=body.pid,
            routes=body.routes,
            user_agent=request.headers.get('user-agent', ''),
        )
        return {'ok': True, 'bridge': entry}

    @app.post('/api/bridges/unregister')
    async def unregister_bridge(body: BridgeUnregisterArgs):
        service.bridges.unregister(body.bridge_id)
        return {'ok': True}

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
        return service.list_cookie_statuses(include_ui=True)

    @app.post('/api/admin/sessions')
    async def import_cookie(body: CookieImport):
        return service.import_cookie(body.platform, body.text)

    @app.post('/api/admin/sessions/bilibili/qr')
    async def bilibili_qr_start():
        return await service.start_bilibili_qr()

    @app.post('/api/admin/sessions/bilibili/qr/{qr_id}/poll')
    async def bilibili_qr_poll(qr_id: str):
        return await service.poll_bilibili_qr(qr_id)

    @app.post('/api/admin/sessions/browser-workspace/start')
    async def browser_workspace_start():
        return await service.start_multi_browser_login()

    @app.post('/api/admin/sessions/browser-workspace/capture')
    async def browser_workspace_capture():
        return service.browser_logins.request_manual_capture(MULTI_PLATFORM_KEY)

    @app.post('/api/admin/sessions/browser-workspace/cancel')
    async def browser_workspace_cancel():
        return service.browser_logins.request_cancel(MULTI_PLATFORM_KEY)

    @app.post('/api/admin/sessions/browser-profile/sync')
    async def browser_profile_sync(body: SyncBrowserProfileArgs | None = None):
        return await service.sync_browser_profile(include_manual=bool(body and body.include_manual))

    @app.post('/api/admin/sessions/browser-profile/reset')
    async def browser_profile_reset():
        return await service.reset_browser_profile()

    @app.get('/api/admin/sessions/accounts')
    async def browser_accounts_list():
        return service.list_browser_accounts()

    @app.post('/api/admin/sessions/accounts')
    async def browser_accounts_add(body: BrowserAccountCreateArgs):
        return service.add_browser_account(body.label)

    @app.post('/api/admin/sessions/accounts/logout')
    async def browser_accounts_logout(body: BrowserAccountLogoutArgs):
        return await service.logout_browser_accounts(body.account_ids)

    @app.post('/api/admin/sessions/accounts/{account_id}/activate')
    async def browser_accounts_activate(account_id: str):
        return service.activate_browser_account(account_id)

    @app.post('/api/admin/sessions/accounts/{account_id}/open')
    async def browser_accounts_open(account_id: str, body: BrowserAccountOpenArgs | None = None):
        return await service.open_browser_account(account_id, body.platform if body else 'YouTube')

    @app.post('/api/admin/sessions/accounts/{account_id}/sync')
    async def browser_accounts_sync(account_id: str, body: SyncBrowserProfileArgs | None = None):
        return await service.sync_browser_account(account_id, include_manual=bool(body and body.include_manual))

    @app.delete('/api/admin/sessions/accounts/{account_id}')
    async def browser_accounts_delete(account_id: str):
        return await service.remove_browser_account(account_id)

    @app.post('/api/admin/sessions/manage-google')
    async def browser_manage_google(body: ManageGoogleArgs | None = None):
        return await service.start_manage_google(body.account_id if body else '')

    @app.get('/api/admin/sessions/browser-prefs')
    async def browser_prefs_get():
        return service.get_browser_preferences()

    @app.post('/api/admin/sessions/browser-prefs')
    async def browser_prefs_update(body: BrowserPrefsUpdateArgs):
        return service.update_browser_preferences(browser=body.browser, mode=body.mode)

    @app.post('/api/admin/sessions/browser-login/{attempt_id}/navigate')
    async def browser_login_navigate(attempt_id: str, body: BrowserNavigateArgs):
        return await service.navigate_browser_login(attempt_id, body.target)

    @app.post('/api/admin/sessions/browser-login/{attempt_id}/relaunch-clean')
    async def browser_login_relaunch_clean(attempt_id: str):
        return await service.relaunch_browser_login_clean(attempt_id)

    @app.post('/api/admin/sessions/{platform}/browser-login')
    async def browser_login_start(platform: str):
        return await service.start_browser_login(platform)

    @app.get('/api/admin/sessions/browser-status/{attempt_id}')
    async def browser_login_status(attempt_id: str):
        return service.browser_logins.public_status(attempt_id)

    @app.post('/api/admin/sessions/{platform}/browser-capture')
    async def browser_login_manual_capture(platform: str):
        return service.browser_logins.request_manual_capture(platform)

    @app.post('/api/admin/sessions/{platform}/browser-cancel')
    async def browser_login_cancel(platform: str):
        return service.browser_logins.request_cancel(platform)

    @app.post('/api/admin/sessions/{platform}/open-login')
    async def browser_open_login_page(platform: str):
        return await service.open_login_page(platform)

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

    @app.post('/api/admin/sessions/{platform}/restore')
    async def restore_cookie(platform: str):
        return service.restore_cookie(platform)

    @app.get('/api/admin/diagnostics')
    async def diagnostics():
        return service.diagnose()

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

    @app.get('/favicon.ico')
    async def favicon():
        return FileResponse(static / 'favicon.svg', media_type='image/svg+xml')

    app.mount('/static', StaticFiles(directory=static), name='static')
    app.mount('/', mcp_app)
    return app
