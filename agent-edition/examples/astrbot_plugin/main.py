"""AstrBot 接入桥接插件：自动注册桥接心跳，支持阶段化下载可视化与报错直出。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import time
import httpx
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register('omni_agent', 'OmniDownloader', 'Headless media delivery through Agent Edition', '0.2.0')
class OmniAgentPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        if not hasattr(self, 'context') or self.context is None:
            self.context = context
        self.server = os.environ.get('OMNI_AGENT_SERVER', 'http://127.0.0.1:58080').rstrip('/')
        self.identities = Path(os.environ.get('OMNI_AGENT_CHAT_IDENTITIES', 'data/omni-agent-chats.json'))
        self.bridge_id = f'astrbot-{os.getpid()}'
        self._heartbeat_task = None

    def _bridge_headers(self) -> dict[str, str]:
        return {
            'X-Omni-Bridge-Id': self.bridge_id,
            'X-Omni-Bridge-Type': 'astrbot',
            'X-Omni-Bridge-Name': 'AstrBot Plugin',
            'X-Omni-Bridge-Version': '0.2.0',
            'X-Omni-Bridge-Pid': str(os.getpid()),
        }

    def _all_routes(self) -> list[str]:
        try:
            if not self.identities.is_file():
                return []
            rows = json.loads(self.identities.read_text('utf-8'))
            return [f"{v['adapter']}:{v['target']}" for v in rows.values() if isinstance(v, dict) and v.get('adapter') and v.get('target')][:20]
        except (OSError, ValueError):
            return []

    async def _send_heartbeat(self, endpoint: str = 'heartbeat'):
        try:
            payload = {
                'bridge_id': self.bridge_id,
                'kind': 'astrbot',
                'name': 'AstrBot 机器人插件',
                'transport': 'http',
                'client_name': 'AstrBot',
                'client_version': '0.2.0',
                'pid': os.getpid(),
                'routes': self._all_routes(),
            }
            async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
                await client.post(f'{self.server}/api/bridges/{endpoint}', json=payload, headers=self._bridge_headers())
        except Exception:
            pass

    def _ensure_heartbeat(self):
        if self._heartbeat_task is None or self._heartbeat_task.done():
            async def _loop():
                await self._send_heartbeat('register')
                while True:
                    await asyncio.sleep(30)
                    await self._send_heartbeat('heartbeat')

            try:
                self._heartbeat_task = asyncio.create_task(_loop())
            except RuntimeError:
                pass

    def route(self, event):
        rows = json.loads(self.identities.read_text('utf-8'))
        route = rows.get(event.unified_msg_origin)
        if not route:
            raise RuntimeError('当前会话尚未配置 OmniDownloader 交付路由')
        return route

    async def call(self, name, **arguments):
        self._ensure_heartbeat()
        async with httpx.AsyncClient(timeout=1900, trust_env=False, follow_redirects=False) as client:
            response = await client.post(self.server + '/api/tools/' + name, json=arguments, headers=self._bridge_headers())
            if response.status_code != 200:
                raise RuntimeError('OmniDownloader 服务连接或接口调用失败')
            data = response.json()
            if not data['ok']:
                error = data['error']
                detail = error['message'] + (' approval_id=' + error['approval_id'] if error.get('approval_id') else '')
                raise RuntimeError(f"[{error['code']}] {detail}")
            return data['result']

    @staticmethod
    def _stage_text(vis: dict | None, stage: str, fallback: str = '') -> str:
        if not isinstance(vis, dict):
            return ''
        stages = vis.get('stages') or {}
        if stage != 'error' and not stages.get(stage):
            return ''
        text = str(vis.get('message') or '').strip()
        return text or fallback

    @staticmethod
    async def _consume_events(vis: dict | None, cursor: int | None, emitted: set[str], notify, *, defer_complete: bool = False) -> tuple[int | None, bool, bool]:
        """消费服务端 pending_events 里程碑，返回更新后的游标、是否检测到服务端已通知、以及是否使用了事件流。"""
        if not isinstance(vis, dict) or 'pending_events' not in vis:
            return cursor, False, False
        server_notified = bool(vis.get('server_notified'))
        for ev in vis.get('pending_events') or []:
            stage = ev.get('stage') or ''
            if stage == 'complete' and defer_complete:
                continue
            if stage == 'sent':
                continue
            if ev.get('server_notified'):
                server_notified = True
                if stage and stage != 'error':
                    emitted.add(stage)
                continue
            if stage != 'error' and stage in emitted:
                continue
            if ev.get('should_reply') and ev.get('message'):
                await notify(str(ev['message']))
                if stage and stage != 'error':
                    emitted.add(stage)
        latest = vis.get('latest_seq')
        if isinstance(latest, int) and (cursor is None or latest > cursor):
            cursor = latest
        return cursor, server_notified, True

    async def process(self, event, url, download_type, approval_id=None, *, emit=None):
        """执行下载与交付全流程：优先依赖事件流游标与长轮询，防止极速下载与缓存复用丢阶段，出错时直接输出报错详情。"""
        async def _notify(text: str):
            if text and emit:
                await emit(text)

        route = self.route(event)
        message_id = str(event.message_obj.message_id)
        key = hashlib.sha256((event.unified_msg_origin + '|' + message_id + '|' + url + '|' + download_type).encode()).hexdigest()
        options = {} if download_type == 'default' else {'use_defaults': False, 'download_type': download_type}

        try:
            job = await self.call(
                'download_media',
                url=url,
                options=options,
                request_key=key,
                approval_id=approval_id,
                notify_adapter=route['adapter'],
                notify_target=route['target'],
            )
        except Exception as exc:
            err_msg = str(exc)
            await _notify(err_msg)
            return err_msg

        cursor: int | None = None
        emitted_stages: set[str] = set()
        job_vis = job.get('visualization') or {}

        # 1. 发送/收到链接时状态（无论排队或缓存秒命中均不丢失 receive）
        cursor, _, used_events = await self._consume_events(job_vis, cursor, emitted_stages, _notify, defer_complete=True)
        if not used_events and job.get('state') in ('QUEUED', 'READY') and 'receive' not in emitted_stages:
            await _notify(self._stage_text(job_vis, 'receive', '已收到链接，正在准备解析与下载...'))
            emitted_stages.add('receive')

        final_status = job if job.get('state') == 'READY' else None
        if final_status is None:
            prev_st = job.get('state')
            for _ in range(1500):
                t0 = time.monotonic()
                call_args = {'task_id': job['id'], 'wait_seconds': 5.0}
                if cursor is not None:
                    call_args['after_seq'] = cursor
                state = await self.call('get_task_status', **call_args)
                vis = state.get('visualization') or {}
                st = state['state']

                prev_cursor = cursor
                cursor, err_server_notified, used_events = await self._consume_events(
                    vis, cursor, emitted_stages, _notify, defer_complete=True
                )
                if not used_events and st in ('DOWNLOADING', 'PROCESSING') and 'start' not in emitted_stages:
                    emitted_stages.add('start')
                    await _notify(self._stage_text(vis, 'start', '开始下载媒体...'))

                if st == 'READY':
                    final_status = state
                    break
                if st in ('FAILED', 'CANCELLED', 'EXPIRED'):
                    err_msg = state.get('error_message') or vis.get('error_detail') or state.get('error') or '下载失败'
                    if not err_server_notified and (not used_events or cursor == prev_cursor):
                        await _notify(err_msg)
                    return err_msg
                if st in ('SENT', 'CLEANED'):
                    return '这条消息的媒体已经交付'

                if time.monotonic() - t0 < 0.2 and st == prev_st and cursor == prev_cursor:
                    await asyncio.sleep(0.2)
                prev_st = st
            else:
                timeout_msg = '等待超时，任务仍可通过管理页和 MCP 查询：' + job['id']
                await _notify(timeout_msg)
                return timeout_msg

        deliver_args = {
            'task_id': job['id'],
            'adapter': route['adapter'],
            'target': route['target'],
            'request_key': key + '-send',
        }
        if cursor is not None:
            deliver_args['after_seq'] = cursor
        delivery = await self.call('deliver_media', **deliver_args)

        # 3. 下载完成时（正在发送）
        d_vis = delivery.get('visualization') or {}
        cursor, _, used_events = await self._consume_events(d_vis, cursor, emitted_stages, _notify, defer_complete=False)
        if not used_events and 'complete' not in emitted_stages and d_vis.get('stages', {}).get('complete'):
            emitted_stages.add('complete')
            await _notify(self._stage_text(d_vis, 'complete', f"下载完成（共 {final_status.get('file_count', 1)} 个文件），正在发送..."))

        prev_d_st = delivery.get('state')
        for _ in range(120):
            t0 = time.monotonic()
            d_args = {'delivery_id': delivery['id'], 'wait_seconds': 5.0}
            if cursor is not None:
                d_args['after_seq'] = cursor
            d_state = await self.call('get_delivery_status', **d_args)
            d_s_vis = d_state.get('visualization') or {}
            prev_cursor = cursor
            cursor, err_server_notified, d_used_events = await self._consume_events(
                d_s_vis, cursor, emitted_stages, _notify, defer_complete=False
            )
            dst = d_state['state']
            if dst == 'SENT':
                return '媒体已发送完成（交付编号：' + delivery['id'] + '）。'
            if dst in ('FAILED', 'UNCERTAIN'):
                err_msg = d_state.get('error_message') or d_s_vis.get('error_detail') or d_state.get('error') or '媒体发送失败'
                if not err_server_notified and (not d_used_events or cursor == prev_cursor):
                    await _notify(err_msg)
                return err_msg
            if time.monotonic() - t0 < 0.2 and dst == prev_d_st and cursor == prev_cursor:
                await asyncio.sleep(0.2)
            prev_d_st = dst
        return '媒体已进入发送队列（交付编号：' + delivery['id'] + '），请留意聊天消息。'

    async def _run_command(self, event: AstrMessageEvent, url: str, approval_id: str):
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def _emit(msg: str):
            await queue.put(msg)

        async def _worker():
            try:
                await self.process(event, url, 'default', approval_id or None, emit=_emit)
            except (RuntimeError, OSError, ValueError, httpx.HTTPError) as error:
                await queue.put(str(error) if isinstance(error, RuntimeError) else f'连接或配置失败：{error}')
            finally:
                await queue.put(None)

        task = asyncio.create_task(_worker())
        last_sent = ''
        while True:
            item = await queue.get()
            if item is None:
                break
            if item != last_sent:
                last_sent = item
                yield event.plain_result(item)
        await task

    @staticmethod
    def _extract_command_input(event: AstrMessageEvent, cmd: str, url: str = '', approval_id: str = '') -> tuple[str, str]:
        """从完整原始消息中提取链接与可选审批 ID，兼容 /us /zip /merge 组合前缀与带空格分享文案。"""
        raw_msg = str(getattr(event, 'message_str', '') or '').strip()
        body = ''
        if raw_msg:
            stripped = re.sub(rf'^(?:@\S+\s+)*[!/／]?{re.escape(cmd)}(?=\s|$|https?://|/)', '', raw_msg, count=1, flags=re.I).strip()
            if stripped != raw_msg:
                body = stripped
                approval_id = ''
        if not body:
            u = str(url or '').strip()
            a = str(approval_id or '').strip()
            if a and ('http://' in a or 'https://' in a or a.startswith('/') or not re.fullmatch(r'[0-9a-fA-F]{16,64}', a)):
                body = f'{u} {a}'.strip()
                approval_id = ''
            else:
                body = u
                approval_id = a

        # 支持从末尾提取显式 approval_id=xxx 或独立 32 位十六进制审批码（避免吞掉 URL）
        found_approval = str(approval_id or '').strip()
        if not found_approval and body:
            m_kv = re.search(r'(?:\s+|^)approval_id=([A-Za-z0-9_-]{8,64})\s*$', body)
            if m_kv:
                found_approval = m_kv.group(1)
                body = body[:m_kv.start()].strip()
            else:
                m_hex = re.search(r'\s+([0-9a-fA-F]{32})\s*$', body)
                if m_hex and ('http://' in body[:m_hex.start()] or 'https://' in body[:m_hex.start()]):
                    found_approval = m_hex.group(1)
                    body = body[:m_hex.start()].strip()

        if cmd in ('us', 'zip', 'merge'):
            token = f'/{cmd}'
            if not re.search(rf'(?:^|\s)/{cmd}(?=\s|$|https?://)', body, re.I):
                body = f'{token} {body}'.strip()
        return body, found_approval

    @filter.command('omni')
    async def download_command(self, event: AstrMessageEvent, url: str = '', approval_id: str = ''):
        payload_url, parsed_approval = self._extract_command_input(event, 'omni', url, approval_id)
        async for result in self._run_command(event, payload_url, parsed_approval):
            yield result

    @filter.command('us')
    async def download_all_command(self, event: AstrMessageEvent, url: str = '', approval_id: str = ''):
        """下载合集、分集视频或全部图片（支持与 /zip、/merge 组合使用）。"""
        payload_url, parsed_approval = self._extract_command_input(event, 'us', url, approval_id)
        async for result in self._run_command(event, payload_url, parsed_approval):
            yield result

    @filter.command('zip')
    async def download_zip_command(self, event: AstrMessageEvent, url: str = '', approval_id: str = ''):
        """以 ZIP 压缩包形式下载并发送（支持与 /us 组合使用）。"""
        payload_url, parsed_approval = self._extract_command_input(event, 'zip', url, approval_id)
        async for result in self._run_command(event, payload_url, parsed_approval):
            yield result

    @filter.command('merge')
    async def download_merge_command(self, event: AstrMessageEvent, url: str = '', approval_id: str = ''):
        """以合并转发聊天记录（QQ）或媒体组相册（Telegram）发送（支持与 /us 组合使用）。"""
        payload_url, parsed_approval = self._extract_command_input(event, 'merge', url, approval_id)
        async for result in self._run_command(event, payload_url, parsed_approval):
            yield result

    @filter.llm_tool(name='omni_download_and_deliver')
    async def media_tool(self, event: AstrMessageEvent, url: str, download_type: str = 'default', approval_id: str = '') -> str:
        """下载媒体并由服务发送到当前聊天。支持在链接前加 /us（下载合集/分集/全部图片；不加 /us 时图文仅下载首图）、/zip（打包压缩包发送）和 /merge（以合并转发/媒体组发送）。

        Args:
            url(string): 作品或视频分享链接（可附带 /us、/zip 或 /merge 前缀指令）。
            download_type(string): default 或 VIDEO_WITH_AUDIO、VIDEO_ONLY、AUDIO_ONLY、GIF、COVER、IMAGE。
            approval_id(string): 高消耗请求批准后返回的确认ID，普通请求留空。
        """
        logs = []

        async def _emit(msg: str):
            logs.append(msg)
            chain = event.plain_result(msg)
            ctx = getattr(self, 'context', None)
            if ctx is not None and hasattr(ctx, 'send_message'):
                try:
                    sent = await ctx.send_message(event.unified_msg_origin, chain)
                    if sent is not False:
                        return
                except Exception:
                    pass
            if hasattr(event, 'send'):
                try:
                    await event.send(chain)
                except Exception:
                    pass

        try:
            final = await self.process(event, url, download_type, approval_id or None, emit=_emit)
            return final if final in logs else '\n'.join([*logs, final]) if logs else final
        except (RuntimeError, OSError, ValueError, httpx.HTTPError) as error:
            return str(error) if isinstance(error, RuntimeError) else f'连接或配置失败：{error}'
