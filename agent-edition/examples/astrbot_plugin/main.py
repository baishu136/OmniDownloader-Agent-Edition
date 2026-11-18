"""AstrBot 接入桥接插件：自动注册桥接心跳，支持阶段化下载可视化与报错直出。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register('omni_agent', 'OmniDownloader', 'Headless media delivery through Agent Edition', '0.2.0')
class OmniAgentPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
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

    async def process(self, event, url, download_type, approval_id=None, *, emit=None):
        """执行下载与交付全流程，严格按服务端可视化配置向对话推送阶段状态，出错时直接输出报错详情。"""
        async def _notify(text: str):
            if text and emit:
                await emit(text)

        route = self.route(event)
        message_id = str(event.message_obj.message_id)
        key = hashlib.sha256((event.unified_msg_origin + '|' + message_id + '|' + url + '|' + download_type).encode()).hexdigest()
        options = {} if download_type == 'default' else {'use_defaults': False, 'download_type': download_type}

        try:
            job = await self.call('download_media', url=url, options=options, request_key=key, approval_id=approval_id)
        except Exception as exc:
            err_msg = str(exc)
            await _notify(err_msg)
            return err_msg

        # 1. 发送/收到链接时状态
        if job.get('state') == 'QUEUED':
            await _notify(self._stage_text(job.get('visualization'), 'receive', '已收到链接，正在准备解析与下载...'))

        sent_start = False
        wait_meta_ticks = 0
        final_status = None
        for _ in range(7200):
            state = await self.call('get_task_status', task_id=job['id'])
            vis = state.get('visualization') or {}
            st = state['state']

            if st in ('DOWNLOADING', 'PROCESSING') and not sent_start:
                has_title = bool((state.get('metadata') or {}).get('title'))
                if has_title or wait_meta_ticks >= 2:
                    sent_start = True
                    await _notify(self._stage_text(vis, 'start', '开始下载媒体...'))
                else:
                    wait_meta_ticks += 1

            if st == 'READY':
                final_status = state
                break
            if st in ('FAILED', 'CANCELLED', 'EXPIRED'):
                err_msg = state.get('error_message') or vis.get('error_detail') or state.get('error') or '下载失败'
                await _notify(err_msg)
                return err_msg
            if st in ('SENT', 'CLEANED'):
                return '这条消息的媒体已经交付'
            await asyncio.sleep(1)
        else:
            timeout_msg = '等待超时，任务仍可通过管理页和 MCP 查询：' + job['id']
            await _notify(timeout_msg)
            return timeout_msg

        delivery = await self.call(
            'deliver_media',
            task_id=job['id'],
            adapter=route['adapter'],
            target=route['target'],
            request_key=key + '-send',
        )
        # 3. 下载完成时（正在发送）
        d_vis = delivery.get('visualization') or {}
        if d_vis.get('stages', {}).get('complete'):
            await _notify(self._stage_text(d_vis, 'complete', f"下载完成（共 {final_status.get('file_count', 1)} 个文件），正在发送..."))

        for _ in range(300):
            d_state = await self.call('get_delivery_status', delivery_id=delivery['id'])
            if d_state['state'] == 'SENT':
                return '媒体已发送完成（交付编号：' + delivery['id'] + '）。'
            if d_state['state'] in ('FAILED', 'UNCERTAIN'):
                err_msg = d_state.get('error_message') or d_state.get('error') or '媒体发送失败'
                await _notify(err_msg)
                return err_msg
            await asyncio.sleep(1)
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

    @filter.command('omni')
    async def download_command(self, event: AstrMessageEvent, url: str, approval_id: str = ''):
        async for result in self._run_command(event, url, approval_id):
            yield result

    @filter.command('us')
    async def download_all_command(self, event: AstrMessageEvent, url: str, approval_id: str = ''):
        """下载合集、分集视频或全部图片。"""
        prefixed = url if url.lstrip().lower().startswith('/us') else f'/us {url}'
        async for result in self._run_command(event, prefixed, approval_id):
            yield result

    @filter.command('zip')
    async def download_zip_command(self, event: AstrMessageEvent, url: str, approval_id: str = ''):
        """以 ZIP 压缩包形式下载并发送。"""
        prefixed = url if url.lstrip().lower().startswith('/zip') else f'/zip {url}'
        async for result in self._run_command(event, prefixed, approval_id):
            yield result

    @filter.llm_tool(name='omni_download_and_deliver')
    async def media_tool(self, event: AstrMessageEvent, url: str, download_type: str = 'default', approval_id: str = '') -> str:
        """下载媒体并由服务发送到当前聊天。支持在链接前加 /us（下载合集/分集/全部图片）和 /zip（打包压缩包发送）。

        Args:
            url(string): 作品或视频分享链接（可附带 /us 或 /zip 前缀指令）。
            download_type(string): default 或 VIDEO_WITH_AUDIO、VIDEO_ONLY、AUDIO_ONLY、GIF、COVER、IMAGE。
            approval_id(string): 高消耗请求批准后返回的确认ID，普通请求留空。
        """
        logs = []

        async def _emit(msg: str):
            logs.append(msg)
            if hasattr(event, 'send'):
                try:
                    await event.send(event.plain_result(msg))
                except Exception:
                    pass

        try:
            final = await self.process(event, url, download_type, approval_id or None, emit=_emit)
            return final if final in logs else '\n'.join([*logs, final]) if logs else final
        except (RuntimeError, OSError, ValueError, httpx.HTTPError) as error:
            return str(error) if isinstance(error, RuntimeError) else f'连接或配置失败：{error}'
