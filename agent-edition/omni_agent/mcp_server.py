from typing import Literal, Any
import json

from mcp.server.mcpserver import MCPServer
from mcp import types
from pydantic import Field
from typing import Annotated

from . import __version__
from .service import DownloadOptions


class ToolBoundary:
    async def __call__(self,context,call_next):
        result=await call_next(context)
        if context.method=='tools/call' and isinstance(result,(types.CallToolResult,dict)):
            wire=result.model_dump(by_alias=True) if isinstance(result,types.CallToolResult) else result
            if wire.get('isError'):
                # SDK validation text may echo rejected caller values. Return a
                # structured, value-free error instead of credential-bearing input.
                body={'ok':False,'error':{'code':'INVALID_PARAMETER','message':'参数不符合工具 Schema，或工具不存在'}}
                return types.CallToolResult(content=[types.TextContent(type='text',text=json.dumps(body,ensure_ascii=True))],structured_content=body,is_error=True)
            if wire.get('structuredContent',{}).get('ok') is False:
                wire['isError']=True
                return wire
        return result


def create_mcp(invoke):
    server = MCPServer('OmniDownloader Agent Edition', version=__version__,middleware=[ToolBoundary()],
        instructions='Use get_service_capabilities before choosing media options. Administrator limits cannot be overridden. '
                     'Downloads are asynchronous. Poll get_task_status, then call deliver_media for automatic bot delivery. '
                     'Never fabricate credentials, send receipts or administrator approvals.')

    @server.tool(structured_output=True, description='Analyze a supported public share/post/collection link. Returns only observed metadata and real formats; no source URLs or credentials.')
    async def analyze_media(url: Annotated[str, Field(min_length=1, max_length=8192)], media_type: Literal['video', 'images'] = 'video') -> dict[str, Any]:
        return await invoke('analyze_media', {'url': url, 'media_type': media_type})

    @server.tool(structured_output=True, description='Create an asynchronous download and return a unique task ID. Omit options to use administrator defaults; set use_defaults=false for explicit options. request_key makes retries idempotent.')
    async def download_media(url: Annotated[str, Field(min_length=1, max_length=8192)], options: DownloadOptions | None = None,
                             request_key: Annotated[str | None, Field(max_length=128)] = None,
                             approval_id: str | None = None) -> dict[str, Any]:
        return await invoke('download_media', {'url': url, 'options': options.model_dump() if options else {}, 'request_key': request_key,'approval_id':approval_id})

    @server.tool(structured_output=True, description='Get the current state, progress, speed, file count and sanitized failure code of your download task.')
    async def get_task_status(task_id: str) -> dict[str, Any]:
        return await invoke('get_task_status', {'task_id': task_id})

    @server.tool(structured_output=True, description='List only tasks belonging to the authenticated identity, optionally filtered by state.')
    async def list_tasks(state: Literal['QUEUED','DOWNLOADING','PROCESSING','READY','SENDING','SENT','FAILED','CLEANED','CANCELLED','EXPIRED'] | None = None,
                         limit: Annotated[int, Field(ge=1, le=100)] = 50) -> dict[str, Any]:
        return await invoke('list_tasks', {'state': state, 'limit': limit})

    @server.tool(structured_output=True, description='Cancel your active request. The core process and network/FFmpeg children are terminated when no other request needs it. Shared downloads continue only for remaining owners.')
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return await invoke('cancel_task', {'task_id': task_id})

    @server.tool(structured_output=True, description='Download a bounded 1-based range of collection episodes/media. Large/profile requests require a one-time administrator-approved approval_id returned by CONFIRMATION_REQUIRED.')
    async def download_collection(url: str, start: Annotated[int, Field(ge=1, le=200)] = 1,
        count: Annotated[int, Field(ge=1, le=200)] = 1, options: DownloadOptions | None = None,
        request_key: Annotated[str | None, Field(max_length=128)] = None, approval_id: str | None = None) -> dict[str, Any]:
        return await invoke('download_collection', {'url': url, 'start': start, 'count': count,
            'options': options.model_dump() if options else {}, 'request_key': request_key, 'approval_id': approval_id})

    @server.tool(structured_output=True, description='Get an opaque controlled reference and SHA256 for a completed file owned by your identity. Accepts only a ledger file ID, never a server path.')
    async def get_media_file(file_id: str) -> dict[str, Any]:
        return await invoke('get_media_file', {'file_id': file_id})

    @server.tool(structured_output=True, description='Report observed runtime components, candidate platforms, enabled features, bot status and administrator limits. Platform availability is checked per request.')
    async def get_service_capabilities() -> dict[str, Any]:
        return await invoke('get_service_capabilities', {})

    @server.tool(structured_output=True, description='Queue delivery of all completed task files to an administrator-allowed Telegram or QQ target. Returns delivery ID immediately. Only trusted platform acknowledgements permit automatic cleanup.')
    async def deliver_media(task_id: str, adapter: Literal['telegram', 'onebot'], target: str,
                            request_key: Annotated[str, Field(min_length=1, max_length=128)],
                            kind: Literal['auto','video','audio','photo','document'] = 'auto') -> dict[str, Any]:
        return await invoke('deliver_media', {'task_id': task_id, 'adapter': adapter, 'target': target, 'request_key': request_key, 'kind': kind})

    @server.tool(structured_output=True, description='Query your delivery state and sanitized platform receipts. SENT means all files have platform acknowledgements; UNCERTAIN requires administrator reconciliation.')
    async def get_delivery_status(delivery_id: str) -> dict[str, Any]:
        return await invoke('get_delivery_status', {'delivery_id': delivery_id})

    return server
