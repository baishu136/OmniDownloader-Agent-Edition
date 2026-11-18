from typing import Annotated, Any, Literal
import json

from mcp.server.mcpserver import MCPServer
from mcp import types
from pydantic import Field

from . import __version__
from .service import BotConfigUpdate, DownloadOptions, SettingsUpdate


class ToolBoundary:
    def __init__(self, on_session=None):
        self.on_session = on_session

    async def __call__(self, context, call_next):
        if self.on_session and context.method in ('initialize', 'tools/call'):
            session = getattr(context, 'session', None)
            params = getattr(session, 'client_params', None)
            info = getattr(params, 'clientInfo', None)
            if info and getattr(info, 'name', None):
                try:
                    await self.on_session(str(info.name), str(getattr(info, 'version', '') or ''))
                except Exception:
                    pass
        result = await call_next(context)
        if context.method == 'tools/call' and isinstance(result, (types.CallToolResult, dict)):
            wire = result.model_dump(by_alias=True) if isinstance(result, types.CallToolResult) else result
            if wire.get('isError'):
                # SDK 参数校验报错文本可能回显调用方传入的敏感值；统一转为结构化脱敏错误
                body = {'ok': False, 'error': {'code': 'INVALID_PARAMETER', 'message': '参数不符合工具 Schema，或工具不存在'}}
                return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(body, ensure_ascii=True))], structured_content=body, is_error=True)
            if wire.get('structuredContent', {}).get('ok') is False:
                wire['isError'] = True
                return wire
        return result


def create_mcp(invoke, on_session=None):
    server = MCPServer(
        'OmniDownloader Agent Edition',
        version=__version__,
        middleware=[ToolBoundary(on_session=on_session)],
        instructions='No authorization token is required for local access. You may read, update and reset service settings, manage bot configurations, inspect auto-detected bridges (list_bridges), cache cleanup and approvals. '
                     'URL prefixes supported by analyze_media/download_media: "/us" (download collection, multi-part videos or all images) and "/zip" (send as a ZIP archive). '
                     'Download visualization: task and delivery results include a "visualization" object reflecting user settings (visual_enabled, visual_on_receive, visual_on_start, visual_on_complete, visual_mode, custom templates). '
                     'When visualization.should_reply is true: if mode=="custom", use visualization.message directly; if mode=="agent", craft an informative update from stage and metadata. When should_reply is false, omit intermediate notices for disabled stages. '
                     'Errors: whenever any tool returns ok=false or task/delivery reaches FAILED/UNCERTAIN, ALWAYS output error_message / error.message directly to the user without hiding the failure cause. '
                     'For platform cookies, you can only inspect non-sensitive status (list_cookie_statuses), disable cookies (disable_platform_cookie) or remove cookies (remove_platform_cookie); '
                     'you cannot read cookie values, import/log in cookies, or re-enable disabled cookies. '
                     'Downloads are asynchronous: poll get_task_status, then call deliver_media for bot delivery.',
    )

    @server.tool(structured_output=True, description='Analyze a supported public share/post/collection link (supports /us and /zip prefixes). Returns only observed metadata and real formats.')
    async def analyze_media(url: Annotated[str, Field(min_length=1, max_length=8192)], media_type: Literal['video', 'images'] = 'video') -> dict[str, Any]:
        return await invoke('analyze_media', {'url': url, 'media_type': media_type})

    @server.tool(structured_output=True, description='Create an asynchronous download and return a task ID. Supports "/us" (collection/episodes/all images) and "/zip" (ZIP archive) URL prefixes. Omit options to use defaults; set use_defaults=false for explicit options.')
    async def download_media(url: Annotated[str, Field(min_length=1, max_length=8192)], options: DownloadOptions | None = None,
                             request_key: Annotated[str | None, Field(max_length=128)] = None,
                             approval_id: str | None = None) -> dict[str, Any]:
        return await invoke('download_media', {'url': url, 'options': options.model_dump() if options else {}, 'request_key': request_key, 'approval_id': approval_id})

    @server.tool(structured_output=True, description='Get the current state, progress, speed, file count and sanitized failure code of a download task.')
    async def get_task_status(task_id: str) -> dict[str, Any]:
        return await invoke('get_task_status', {'task_id': task_id})

    @server.tool(structured_output=True, description='List tasks, optionally filtered by state.')
    async def list_tasks(state: Literal['QUEUED', 'DOWNLOADING', 'PROCESSING', 'READY', 'SENDING', 'SENT', 'FAILED', 'CLEANED', 'CANCELLED', 'EXPIRED'] | None = None,
                         limit: Annotated[int, Field(ge=1, le=100)] = 50) -> dict[str, Any]:
        return await invoke('list_tasks', {'state': state, 'limit': limit})

    @server.tool(structured_output=True, description='Cancel an active request. The core process and network/FFmpeg children are terminated when no other request needs it.')
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return await invoke('cancel_task', {'task_id': task_id})

    @server.tool(structured_output=True, description='Download a bounded 1-based range of collection episodes/media. Large/profile requests require a one-time approval_id from CONFIRMATION_REQUIRED.')
    async def download_collection(url: str, start: Annotated[int, Field(ge=1, le=200)] = 1,
        count: Annotated[int, Field(ge=1, le=200)] = 1, options: DownloadOptions | None = None,
        request_key: Annotated[str | None, Field(max_length=128)] = None, approval_id: str | None = None) -> dict[str, Any]:
        return await invoke('download_collection', {'url': url, 'start': start, 'count': count,
            'options': options.model_dump() if options else {}, 'request_key': request_key, 'approval_id': approval_id})

    @server.tool(structured_output=True, description='Get an opaque controlled reference and SHA256 for a completed file. Accepts only a ledger file ID, never a server path.')
    async def get_media_file(file_id: str) -> dict[str, Any]:
        return await invoke('get_media_file', {'file_id': file_id})

    @server.tool(structured_output=True, description='Report observed runtime components, candidate platforms, enabled features, bot status, connected bridges and limits.')
    async def get_service_capabilities() -> dict[str, Any]:
        return await invoke('get_service_capabilities', {})

    @server.tool(structured_output=True, description='List auto-detected connected client bridges (MCP stdio, Streamable HTTP, AstrBot, bot clients) and configured bot adapter connection statuses without exposing secrets.')
    async def list_bridges() -> dict[str, Any]:
        return await invoke('list_bridges', {})

    @server.tool(structured_output=True, description='Queue delivery of all completed task files to an allowed Telegram or QQ target. Honors QQ video/image message/file and forward collection settings.')
    async def deliver_media(task_id: str, adapter: Literal['telegram', 'onebot'], target: str,
                            request_key: Annotated[str, Field(min_length=1, max_length=128)],
                            kind: Literal['auto', 'video', 'audio', 'photo', 'document'] = 'auto') -> dict[str, Any]:
        return await invoke('deliver_media', {'task_id': task_id, 'adapter': adapter, 'target': target, 'request_key': request_key, 'kind': kind})

    @server.tool(structured_output=True, description='Query delivery state and sanitized platform receipts.')
    async def get_delivery_status(delivery_id: str) -> dict[str, Any]:
        return await invoke('get_delivery_status', {'delivery_id': delivery_id})

    @server.tool(structured_output=True, description='Read all current download, ZIP packing, storage directories, QQ delivery mode, download visualization notification settings, feature, proxy, cache and theme settings.')
    async def get_settings() -> dict[str, Any]:
        return await invoke('get_settings', {})

    @server.tool(structured_output=True, description='Update one or more service settings (including download_dir, image_dir, cache_dir, qq_*, visual_enabled, visual_on_receive, visual_on_start, visual_on_complete, visual_mode, visual_*_template, proxy, theme). Does not touch cookies.')
    async def update_settings(settings: SettingsUpdate) -> dict[str, Any]:
        return await invoke('update_settings', {'settings': settings.model_dump(exclude_none=True)})

    @server.tool(structured_output=True, description='Reset service settings to defaults (preserves custom cache_dir if old cache still contains files unless reset_cache_dir=true).')
    async def reset_settings(reset_cache_dir: bool = False) -> dict[str, Any]:
        return await invoke('reset_settings', {'reset_cache_dir': reset_cache_dir})

    @server.tool(structured_output=True, description='Read Telegram and OneBot delivery adapter configurations and live connection info without exposing secret bot tokens.')
    async def get_bot_configs() -> dict[str, Any]:
        return await invoke('get_bot_configs', {})

    @server.tool(structured_output=True, description='Update Telegram or OneBot delivery adapter configuration.')
    async def update_bot_config(config: BotConfigUpdate) -> dict[str, Any]:
        return await invoke('update_bot_config', {'config': config.model_dump(exclude_none=True)})

    @server.tool(structured_output=True, description='Probe connectivity for the configured Telegram or OneBot adapter.')
    async def probe_bot(adapter: Literal['telegram', 'onebot']) -> dict[str, Any]:
        return await invoke('probe_bot', {'adapter': adapter})

    @server.tool(structured_output=True, description='List pending batch/profile confirmation requests.')
    async def list_approvals() -> dict[str, Any]:
        return await invoke('list_approvals', {})

    @server.tool(structured_output=True, description='Approve a pending batch/profile confirmation request by approval_id.')
    async def approve_request(approval_id: Annotated[str, Field(min_length=1, max_length=64)]) -> dict[str, Any]:
        return await invoke('approve_request', {'approval_id': approval_id})

    @server.tool(structured_output=True, description='Clean up delivered or expired media files in the controlled cache directory.')
    async def cleanup_cache() -> dict[str, Any]:
        return await invoke('cleanup_cache', {})

    @server.tool(structured_output=True, description='List non-sensitive cookie statuses (platform, state, enabled, validation state) without exposing any cookie values or credentials.')
    async def list_cookie_statuses() -> dict[str, Any]:
        return await invoke('list_cookie_statuses', {})

    @server.tool(structured_output=True, description='Disable stored cookie for a platform or "all" platforms without deleting it. Agents cannot re-enable, import or read cookies.')
    async def disable_platform_cookie(platform: Annotated[str, Field(min_length=1, max_length=64)]) -> dict[str, Any]:
        return await invoke('disable_platform_cookie', {'platform': platform})

    @server.tool(structured_output=True, description='Permanently remove stored cookie credentials for a platform or "all" platforms. Agents cannot import or read cookies.')
    async def remove_platform_cookie(platform: Annotated[str, Field(min_length=1, max_length=64)]) -> dict[str, Any]:
        return await invoke('remove_platform_cookie', {'platform': platform})

    return server
