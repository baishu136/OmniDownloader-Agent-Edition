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

    async def download_and_deliver(self, url, adapter, target, download_type=None, approval_id=None, *, on_status=None):
        key = uuid.uuid4().hex
        options = {'use_defaults': False, 'download_type': download_type} if download_type else {}
        job = await self.tool('download_media', url=url, options=options, request_key=key, approval_id=approval_id)
        self._emit_stage(job.get('visualization'), 'receive', on_status)

        sent_start = False
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            status = await self.tool('get_task_status', task_id=job['id'])
            if status['state'] in ('DOWNLOADING', 'PROCESSING') and not sent_start:
                sent_start = True
                self._emit_stage(status.get('visualization'), 'start', on_status)
            if status['state'] == 'READY':
                break
            if status['state'] in ('FAILED', 'CANCELLED', 'EXPIRED', 'CLEANED'):
                raise RuntimeError(status.get('error_message') or status['error'] or status['state'])
            await asyncio.sleep(1)
        else:
            raise RuntimeError('WAIT_TIMEOUT: download continues; query task ' + job['id'])

        delivery = await self.tool('deliver_media', task_id=job['id'], adapter=adapter, target=target, request_key=key + '-delivery')
        self._emit_stage(delivery.get('visualization'), 'complete', on_status)
        while time.monotonic() < deadline:
            result = await self.tool('get_delivery_status', delivery_id=delivery['id'])
            if result['state'] == 'SENT':
                return result
            if result['state'] in ('FAILED', 'UNCERTAIN'):
                raise RuntimeError((result.get('error_message') or result['error']) + '; delivery_id=' + delivery['id'])
            await asyncio.sleep(1)
        raise RuntimeError('WAIT_TIMEOUT: check delivery ' + delivery['id'])


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
