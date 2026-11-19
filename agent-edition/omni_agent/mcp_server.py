from typing import Annotated, Any, Literal
import json

from mcp.server.mcpserver import Context, MCPServer
from mcp import types
from pydantic import Field

from . import __version__
from .service import BotConfigUpdate, DownloadOptions, SettingsUpdate


async def _emit_mcp_progress(ctx: Context | None, res: Any) -> None:
    """将工具返回结果或错误中的可视化阶段事件通过 MCP 进度通知实时发出。"""
    if ctx is None or not isinstance(res, dict):
        return
    payload = res.get('result') if res.get('ok') and isinstance(res.get('result'), dict) else res
    vis = payload.get('visualization') if isinstance(payload, dict) else None
    if not isinstance(vis, dict):
        return
    try:
        events = vis.get('pending_events') or []
        raw_prog = payload.get('progress')
        if raw_prog is None:
            raw_prog = (vis.get('variables') or {}).get('progress', 0)
        prog_val = float(raw_prog or 0)
        if events:
            for ev in events:
                if ev.get('should_reply') and ev.get('message'):
                    ev_prog = 100.0 if ev.get('stage') in ('complete', 'sent') else prog_val
                    await ctx.report_progress(ev_prog, 100.0, str(ev['message']))
        elif vis.get('should_reply') and vis.get('message'):
            stage_prog = 100.0 if vis.get('current_stage') in ('complete', 'sent') else prog_val
            await ctx.report_progress(stage_prog, 100.0, str(vis['message']))
    except Exception:
        pass


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
                     'URL prefixes supported by analyze_media/download_media: "/us" (download collection, multi-part videos or all images; without /us image posts download only the first image), "/zip" (send as a ZIP archive), and "/merge" (force merged forward message on QQ or media group on Telegram). '
                     'Download visualization & status delivery: task and delivery results include "visualization" with ordered "pending_events" (each having seq, stage, should_reply, server_notified, message), "latest_seq", and "server_notified". '
                     'When triggering downloads for QQ / Telegram users, pass notify_adapter and notify_target (if known and permitted) so the server can push status messages immediately via the configured bot without waiting for LLM turn boundaries. '
                     'When polling get_task_status / get_delivery_status / deliver_media, pass after_seq=visualization.latest_seq and wait_seconds=5 (up to 30) so milestone transitions return immediately without missing fast stages or cache hits. '
                     'For every event in pending_events where should_reply is true and server_notified is false (or fallback visualization.should_reply): if mode=="custom", send event.message verbatim; if mode=="agent", craft a concise timely update. Skip stages with server_notified=true to avoid duplicates. '
                     'Errors: whenever any tool returns ok=false or task/delivery reaches FAILED/UNCERTAIN, ALWAYS output error_detail / error_message / error.message directly to the user without hiding the failure cause. '
                     'For platform cookies, you can only inspect non-sensitive status (list_cookie_statuses), disable cookies (disable_platform_cookie) or remove cookies (remove_platform_cookie); '
                     'you cannot read cookie values, import/log in cookies, or re-enable disabled cookies. '
                     'Downloads are asynchronous: use get_task_status with wait_seconds/after_seq, then call deliver_media for bot delivery.',
    )

    @server.tool(structured_output=True, description='Analyze a supported public share/post/collection link (supports /us, /zip and /merge prefixes; image posts auto-detect). Returns only observed metadata and real formats.')
    async def analyze_media(url: Annotated[str, Field(min_length=1, max_length=8192)], media_type: Literal['video', 'images'] = 'video') -> dict[str, Any]:
        return await invoke('analyze_media', {'url': url, 'media_type': media_type})

    @server.tool(structured_output=True, description='Create an asynchronous download and return a task ID. Supports "/us" (collection/episodes/all images; without /us image posts fetch only the first image), "/zip" (ZIP archive) and "/merge" (force merged forward/media group delivery) prefixes. Auto-detects image posts when images feature is enabled. Optionally supply notify_adapter/notify_target.')
    async def download_media(url: Annotated[str, Field(min_length=1, max_length=8192)], options: DownloadOptions | None = None,
                             request_key: Annotated[str | None, Field(max_length=128)] = None,
                             approval_id: str | None = None,
                             notify_adapter: Literal['telegram', 'onebot'] | None = None,
                             notify_target: Annotated[str | None, Field(max_length=128)] = None,
                             ctx: Context | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {'url': url, 'options': options.model_dump() if options else {}, 'request_key': request_key, 'approval_id': approval_id}
        if notify_adapter is not None:
            payload['notify_adapter'] = notify_adapter
        if notify_target is not None:
            payload['notify_target'] = notify_target
        res = await invoke('download_media', payload)
        await _emit_mcp_progress(ctx, res)
        return res

    @server.tool(structured_output=True, description='Get task state, progress, speed, visualization pending_events and sanitized error details. Pass after_seq (from visualization.latest_seq) and wait_seconds (0-30) to block until the next milestone without missing fast transitions.')
    async def get_task_status(task_id: str,
                              after_seq: Annotated[int | None, Field(ge=0)] = None,
                              wait_seconds: Annotated[float, Field(ge=0.0, le=30.0)] = 0.0,
                              ctx: Context | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {'task_id': task_id}
        if after_seq is not None:
            payload['after_seq'] = after_seq
        if wait_seconds:
            payload['wait_seconds'] = float(wait_seconds)
        res = await invoke('get_task_status', payload)
        await _emit_mcp_progress(ctx, res)
        return res

    @server.tool(structured_output=True, description='List tasks, optionally filtered by state.')
    async def list_tasks(state: Literal['QUEUED', 'DOWNLOADING', 'PROCESSING', 'READY', 'SENDING', 'SENT', 'FAILED', 'CLEANED', 'CANCELLED', 'EXPIRED'] | None = None,
                         limit: Annotated[int, Field(ge=1, le=100)] = 50) -> dict[str, Any]:
        return await invoke('list_tasks', {'state': state, 'limit': limit})

    @server.tool(structured_output=True, description='Cancel an active request. The core process and network/FFmpeg children are terminated when no other request needs it.')
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return await invoke('cancel_task', {'task_id': task_id})

    @server.tool(structured_output=True, description='Download a bounded 1-based range of collection episodes/media. Large/profile requests require a one-time approval_id from CONFIRMATION_REQUIRED. Optionally pass notify_adapter/notify_target for direct bot notifications.')
    async def download_collection(url: str, start: Annotated[int, Field(ge=1, le=200)] = 1,
        count: Annotated[int, Field(ge=1, le=200)] = 1, options: DownloadOptions | None = None,
        request_key: Annotated[str | None, Field(max_length=128)] = None, approval_id: str | None = None,
        notify_adapter: Literal['telegram', 'onebot'] | None = None,
        notify_target: Annotated[str | None, Field(max_length=128)] = None,
        ctx: Context | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {'url': url, 'start': start, 'count': count,
            'options': options.model_dump() if options else {}, 'request_key': request_key, 'approval_id': approval_id}
        if notify_adapter is not None:
            payload['notify_adapter'] = notify_adapter
        if notify_target is not None:
            payload['notify_target'] = notify_target
        res = await invoke('download_collection', payload)
        await _emit_mcp_progress(ctx, res)
        return res

    @server.tool(structured_output=True, description='Get an opaque controlled reference and SHA256 for a completed file. Accepts only a ledger file ID, never a server path.')
    async def get_media_file(file_id: str) -> dict[str, Any]:
        return await invoke('get_media_file', {'file_id': file_id})

    @server.tool(structured_output=True, description='Report observed runtime components, candidate platforms, enabled features, bot status, connected bridges and limits.')
    async def get_service_capabilities() -> dict[str, Any]:
        return await invoke('get_service_capabilities', {})

    @server.tool(structured_output=True, description='List auto-detected connected client bridges (MCP stdio, Streamable HTTP, AstrBot, bot clients) and configured bot adapter connection statuses without exposing secrets.')
    async def list_bridges() -> dict[str, Any]:
        return await invoke('list_bridges', {})

    @server.tool(structured_output=True, description='Queue delivery of all completed task files in original order to an allowed Telegram or QQ target. Honors /merge, QQ video/image/forward settings (including qq_forward_max_items batch limit) and Telegram sendMediaGroup. Pass after_seq to avoid repeating earlier milestones.')
    async def deliver_media(task_id: str, adapter: Literal['telegram', 'onebot'], target: str,
                            request_key: Annotated[str, Field(min_length=1, max_length=128)],
                            kind: Literal['auto', 'video', 'audio', 'photo', 'document'] = 'auto',
                            after_seq: Annotated[int | None, Field(ge=0)] = None,
                            ctx: Context | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {'task_id': task_id, 'adapter': adapter, 'target': target, 'request_key': request_key, 'kind': kind}
        if after_seq is not None:
            payload['after_seq'] = after_seq
        res = await invoke('deliver_media', payload)
        await _emit_mcp_progress(ctx, res)
        return res

    @server.tool(structured_output=True, description='Query delivery state, sanitized platform receipts, and unconsumed visualization events. Supports after_seq and wait_seconds (0-30) for timely updates.')
    async def get_delivery_status(delivery_id: str,
                                  after_seq: Annotated[int | None, Field(ge=0)] = None,
                                  wait_seconds: Annotated[float, Field(ge=0.0, le=30.0)] = 0.0,
                                  ctx: Context | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {'delivery_id': delivery_id}
        if after_seq is not None:
            payload['after_seq'] = after_seq
        if wait_seconds:
            payload['wait_seconds'] = float(wait_seconds)
        res = await invoke('get_delivery_status', payload)
        await _emit_mcp_progress(ctx, res)
        return res

    @server.tool(structured_output=True, description='Read all current download, ZIP packing, image format (image_format), storage directories, QQ delivery mode, download visualization notification settings, feature, proxy, cache and theme settings.')
    async def get_settings() -> dict[str, Any]:
        return await invoke('get_settings', {})

    @server.tool(structured_output=True, description='Update one or more service settings (including default_type, default_height, container, audio_format, image_format, pack_zip, download_dir, image_dir, cache_dir, qq_*, visual_enabled, visual_on_receive, visual_on_start, visual_on_complete, visual_mode, visual_notify_channel, visual_notify_target, visual_reuse_message, visual_*_template, proxy, theme). Does not touch cookies.')
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
