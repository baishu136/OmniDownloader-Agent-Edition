"""自动识别、追踪并扫描已接入 OmniDownloader 的 MCP/Bot 客户端桥接。"""
import ctypes
import os
import socket
import threading
import time
from urllib.parse import unquote, urlsplit


def _pid_alive(pid: int | None) -> bool:
    """跨平台判断本机关联进程是否仍存活。"""
    if not pid or pid <= 0:
        return True
    if os.name == 'nt':
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _clean_routes(routes) -> list[str]:
    if not isinstance(routes, list):
        return []
    result = []
    for item in routes[:20]:
        text = str(item or '').strip()
        if text and len(text) <= 80:
            result.append(text)
    return result


class BridgeRegistry:
    """记录自动识别与心跳注册的接入桥接（MCP stdio / HTTP / AstrBot / 自定义插件）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self.local_hints: list[dict] = []

    def register(
        self,
        *,
        bridge_id: str,
        kind: str = 'http_api',
        name: str = '',
        transport: str = 'http',
        client_name: str = '',
        client_version: str = '',
        pid: int | None = None,
        routes: list[str] | None = None,
        user_agent: str = '',
        count_call: bool = False,
    ) -> dict:
        key = str(bridge_id or '').strip()[:128]
        if not key:
            raise ValueError('bridge_id 不能为空')
        now = time.time()
        with self._lock:
            existing = self._entries.get(key)
            if existing:
                entry = existing
                entry['last_seen'] = now
                entry['active'] = True
                if kind and (kind != 'http_api' or not entry.get('kind')):
                    entry['kind'] = kind
                if name and (not entry.get('name') or name not in ('HTTP 客户端', 'MCP 客户端')):
                    entry['name'] = name[:80]
                if transport:
                    entry['transport'] = transport[:40]
                if client_name:
                    entry['client_name'] = client_name[:80]
                if client_version:
                    entry['client_version'] = client_version[:40]
                if pid:
                    entry['pid'] = int(pid)
                if routes is not None:
                    entry['routes'] = _clean_routes(routes)
                if user_agent and not entry.get('user_agent'):
                    entry['user_agent'] = user_agent[:120]
                if count_call:
                    entry['call_count'] = entry.get('call_count', 0) + 1
            else:
                entry = {
                    'id': key,
                    'kind': kind[:40],
                    'name': (name or client_name or key)[:80],
                    'transport': transport[:40],
                    'client_name': client_name[:80],
                    'client_version': client_version[:40],
                    'pid': int(pid) if pid else None,
                    'routes': _clean_routes(routes),
                    'user_agent': user_agent[:120],
                    'first_seen': now,
                    'last_seen': now,
                    'call_count': 1 if count_call else 0,
                    'active': True,
                }
                self._entries[key] = entry
                if len(self._entries) > 64:
                    oldest = min(self._entries, key=lambda k: self._entries[k]['last_seen'])
                    self._entries.pop(oldest, None)
            return self._snapshot(entry, now)

    def unregister(self, bridge_id: str) -> None:
        key = str(bridge_id or '').strip()[:128]
        if not key:
            return
        with self._lock:
            entry = self._entries.get(key)
            if entry:
                entry['active'] = False
                entry['last_seen'] = time.time()

    def observe_http_request(self, path: str, headers: dict[str, str], client_host: str = '') -> None:
        """从 /mcp 与 /api/tools/* 请求自动识别接入桥接来源，不干扰管理页请求。"""
        if not (path == '/mcp' or path.startswith('/mcp/') or path.startswith('/api/tools/')):
            return
        bridge_id = headers.get('x-omni-bridge-id', '').strip()
        bridge_type = headers.get('x-omni-bridge-type', '').strip()
        bridge_name = unquote(headers.get('x-omni-bridge-name', '').strip())
        bridge_ver = unquote(headers.get('x-omni-bridge-version', '').strip())
        bridge_client = unquote(headers.get('x-omni-bridge-client', '').strip())
        bridge_pid_raw = headers.get('x-omni-bridge-pid', '').strip()
        bridge_pid = int(bridge_pid_raw) if bridge_pid_raw.isdigit() else None
        ua = headers.get('user-agent', '').strip()
        session_id = headers.get('mcp-session-id', '').strip()

        if path.startswith('/mcp'):
            kind = bridge_type or 'mcp_http'
            transport = 'streamable-http'
            key = bridge_id or (f'mcp-session:{session_id}' if session_id else f'mcp-http:{client_host or "local"}:{ua[:40] or "client"}')
            default_name = bridge_name or bridge_client or ('Streamable HTTP 客户端' if not ua else f'MCP ({ua.split("/", 1)[0][:32]})')
        else:
            kind = bridge_type or ('astrbot' if 'astrbot' in ua.lower() else 'http_api')
            transport = 'stdio' if kind == 'mcp_stdio' else 'http'
            key = bridge_id or f'{kind}:{client_host or "local"}:{(bridge_name or ua)[:40] or "tools"}'
            default_name = bridge_name or bridge_client or ('AstrBot 插件' if kind == 'astrbot' else f'HTTP API ({ua.split("/", 1)[0][:32] or "client"})')

        self.register(
            bridge_id=key,
            kind=kind,
            name=default_name,
            transport=transport,
            client_name=bridge_client,
            client_version=bridge_ver,
            pid=bridge_pid,
            user_agent=ua,
            count_call=True,
        )

    def observe_mcp_session(self, client_name: str, client_version: str = '', session_id: str = '', transport: str = 'streamable-http') -> None:
        """在 MCP 会话初始化或工具调用时记录 clientInfo。"""
        if not client_name:
            return
        key = f'mcp-client:{transport}:{client_name}:{client_version}'
        if session_id:
            key = f'mcp-session:{session_id}'
        self.register(
            bridge_id=key,
            kind='mcp_stdio' if transport == 'stdio' else 'mcp_http',
            name=client_name,
            transport=transport,
            client_name=client_name,
            client_version=client_version,
            count_call=False,
        )

    def _snapshot(self, entry: dict, now: float) -> dict:
        age = max(0.0, now - entry['last_seen'])
        alive = bool(entry.get('active')) and _pid_alive(entry.get('pid'))
        if not alive or age > 600:
            status = 'offline'
        elif age <= 90:
            status = 'online'
        else:
            status = 'idle'
        return dict(entry, status=status, idle_seconds=int(age))

    def list_bridges(self) -> list[dict]:
        now = time.time()
        with self._lock:
            items = [self._snapshot(entry, now) for entry in self._entries.values()]
        order = {'online': 0, 'idle': 1, 'offline': 2}
        items.sort(key=lambda item: (order.get(item['status'], 3), -item['last_seen']))
        return items

    def scan_local_endpoints(self, onebot_config, bot_status: dict) -> list[dict]:
        """在不写入配置或发送密钥的前提下，非侵入检查本机默认 OneBot 端口状态。"""
        hints = []
        if not onebot_config.enabled:
            target_url = onebot_config.base_url or 'http://127.0.0.1:3000'
            parsed = urlsplit(target_url)
            if parsed.hostname in ('127.0.0.1', 'localhost', '::1') and parsed.port:
                try:
                    with socket.create_connection((parsed.hostname, parsed.port), timeout=0.4):
                        hints.append({
                            'kind': 'onebot_candidate',
                            'name': '本机 OneBot / NapCat 端口活跃',
                            'address': f'{parsed.hostname}:{parsed.port}',
                            'status': 'detected_unconfigured',
                            'hint': f'检测到 {parsed.hostname}:{parsed.port} 正在监听，可在下方配置并启用 QQ / OneBot 连接。',
                        })
                except OSError:
                    pass
        elif bot_status.get('onebot', {}).get('connected') is True:
            st = bot_status['onebot']
            hints.append({
                'kind': 'onebot',
                'name': f"OneBot ({st.get('implementation', 'Online')})",
                'address': onebot_config.base_url,
                'status': 'online',
                'hint': f"已连接 {st.get('nickname', '')} ({st.get('bot_id', '-')})".strip(),
            })
        self.local_hints = hints
        return hints
