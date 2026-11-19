"""Runnable framework-neutral end-to-end download -> send -> receipt client."""
import argparse
import asyncio
import os
import time
import uuid

from urllib.parse import quote

import httpx


class OmniBotClient:
    def __init__(self, server, *, bridge_name='Bot CLI Client'):
        self.server = server.rstrip('/')
        self.bridge_id = f'bot-cli-{os.getpid()}'
        self.bridge_name = bridge_name

    def _headers(self):
        return {
            'X-Omni-Bridge-Id': self.bridge_id,
            'X-Omni-Bridge-Type': 'bot_client',
            'X-Omni-Bridge-Name': quote(self.bridge_name, safe=' -_.'),
            'X-Omni-Bridge-Pid': str(os.getpid()),
        }

    async def tool(self, name, **arguments):
        async with httpx.AsyncClient(trust_env=False, timeout=1900) as client:
            response = await client.post(self.server + '/api/tools/' + name, json=arguments, headers=self._headers())
            response.raise_for_status()
            data = response.json()
            if not data['ok']:
                error = data['error']
                raise RuntimeError(error['code'] + ': ' + error['message'] + (' approval_id=' + error['approval_id'] if error.get('approval_id') else ''))
            return data['result']

    @staticmethod
    def _emit_stage(vis, stage, on_status):
        if not isinstance(vis, dict) or not on_status:
            return
        if vis.get('should_reply') and vis.get('current_stage') == stage and vis.get('message'):
            on_status(stage, vis['message'])

    @staticmethod
    def _consume_events(vis, cursor, emitted, on_status, *, defer_complete=False):
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
            if ev.get('should_reply') and ev.get('message') and on_status:
                on_status(stage, str(ev['message']))
                if stage and stage != 'error':
                    emitted.add(stage)
        latest = vis.get('latest_seq')
        if isinstance(latest, int) and (cursor is None or latest > cursor):
            cursor = latest
        return cursor, server_notified, True

    async def download_and_deliver(
        self,
        url,
        adapter,
        target,
        download_type=None,
        approval_id=None,
        *,
        on_status=None,
        notify_via_server=True,
    ):
        key = uuid.uuid4().hex
        options = {'use_defaults': False, 'download_type': download_type} if download_type else {}
        dl_args = {'url': url, 'options': options, 'request_key': key, 'approval_id': approval_id}
        if notify_via_server and adapter and target:
            dl_args['notify_adapter'] = adapter
            dl_args['notify_target'] = target
        try:
            job = await self.tool('download_media', **dl_args)
        except RuntimeError as exc:
            if on_status:
                on_status('error', str(exc))
            raise

        cursor = None
        emitted_stages = set()
        job_vis = job.get('visualization') or {}
        cursor, _, used_events = self._consume_events(job_vis, cursor, emitted_stages, on_status, defer_complete=True)
        if not used_events and job.get('state') in ('QUEUED', 'READY') and 'receive' not in emitted_stages:
            self._emit_stage(job_vis, 'receive', on_status)
            emitted_stages.add('receive')

        deadline = time.monotonic() + 7200
        if job['state'] != 'READY':
            prev_state = job['state']
            while time.monotonic() < deadline:
                t0 = time.monotonic()
                q_args = {'task_id': job['id'], 'wait_seconds': 5.0}
                if cursor is not None:
                    q_args['after_seq'] = cursor
                status = await self.tool('get_task_status', **q_args)
                vis = status.get('visualization') or {}
                prev_cursor = cursor
                cursor, err_server_notified, used_events = self._consume_events(
                    vis, cursor, emitted_stages, on_status, defer_complete=True
                )
                if not used_events and status['state'] in ('DOWNLOADING', 'PROCESSING') and 'start' not in emitted_stages:
                    emitted_stages.add('start')
                    self._emit_stage(vis, 'start', on_status)
                if status['state'] == 'READY':
                    break
                if status['state'] in ('FAILED', 'CANCELLED', 'EXPIRED', 'CLEANED'):
                    err_msg = status.get('error_message') or vis.get('error_detail') or status.get('error') or status['state']
                    if on_status and not err_server_notified and (not used_events or cursor == prev_cursor):
                        on_status('error', err_msg)
                    raise RuntimeError(err_msg)
                if time.monotonic() - t0 < 0.2 and status['state'] == prev_state and cursor == prev_cursor:
                    await asyncio.sleep(0.2)
                prev_state = status['state']
            else:
                timeout_msg = 'WAIT_TIMEOUT: download continues; query task ' + job['id']
                if on_status:
                    on_status('error', timeout_msg)
                raise RuntimeError(timeout_msg)

        d_args = {'task_id': job['id'], 'adapter': adapter, 'target': target, 'request_key': key + '-delivery'}
        if cursor is not None:
            d_args['after_seq'] = cursor
        try:
            delivery = await self.tool('deliver_media', **d_args)
        except RuntimeError as exc:
            if on_status:
                on_status('error', str(exc))
            raise
        d_vis = delivery.get('visualization') or {}
        cursor, _, d_used_events = self._consume_events(d_vis, cursor, emitted_stages, on_status, defer_complete=False)
        if not d_used_events and 'complete' not in emitted_stages:
            self._emit_stage(d_vis, 'complete', on_status)
            emitted_stages.add('complete')

        prev_d_state = delivery['state']
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            s_args = {'delivery_id': delivery['id'], 'wait_seconds': 5.0}
            if cursor is not None:
                s_args['after_seq'] = cursor
            result = await self.tool('get_delivery_status', **s_args)
            r_vis = result.get('visualization') or {}
            prev_cursor = cursor
            cursor, err_server_notified, r_used_events = self._consume_events(
                r_vis, cursor, emitted_stages, on_status, defer_complete=False
            )
            if result['state'] == 'SENT':
                return result
            if result['state'] in ('FAILED', 'UNCERTAIN'):
                err_msg = result.get('error_message') or r_vis.get('error_detail') or result.get('error') or result['state']
                if on_status and not err_server_notified and (not r_used_events or cursor == prev_cursor):
                    on_status('error', err_msg)
                raise RuntimeError(err_msg + '; delivery_id=' + delivery['id'])
            if time.monotonic() - t0 < 0.2 and result['state'] == prev_d_state and cursor == prev_cursor:
                await asyncio.sleep(0.2)
            prev_d_state = result['state']
        timeout_msg = 'WAIT_TIMEOUT: check delivery ' + delivery['id']
        if on_status:
            on_status('error', timeout_msg)
        raise RuntimeError(timeout_msg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', default=os.environ.get('OMNI_AGENT_SERVER', 'http://127.0.0.1:58080'))
    parser.add_argument('--adapter', choices=['telegram', 'onebot'], required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--type', dest='download_type')
    parser.add_argument('--approval-id', help='Approved identifier returned by CONFIRMATION_REQUIRED')
    args = parser.parse_args()
    result = asyncio.run(OmniBotClient(args.server).download_and_deliver(
        args.url, args.adapter, args.target, args.download_type, args.approval_id,
        on_status=lambda stage, text: print(f'[{stage}] {text}'),
    ))
    print(result)


if __name__ == '__main__':
    main()
