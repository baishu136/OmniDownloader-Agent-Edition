import asyncio
import hashlib
import json
import mimetypes
from pathlib import Path
import secrets
import shutil
import time
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import Config
from .credentials import Vault
from .delivery import BotConfig, make_adapter
from .errors import AgentError, safe_text
from .runner import Runner
from .security import is_profile, source_url
from .store import ACTIVE, Store


class DownloadOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    use_defaults: bool = True
    download_type: Literal['VIDEO_WITH_AUDIO', 'VIDEO_ONLY', 'AUDIO_ONLY', 'GIF', 'COVER', 'IMAGE', 'IMAGE_ZIP'] | None = None
    height: int | None = Field(None, ge=144, le=8640)
    container: Literal['mp4', 'mkv', 'auto'] | None = None
    audio_format: Literal['mp3', 'm4a', 'flac', 'opus'] | None = None
    preserve_original: bool = True


class Service:
    def __init__(self, directory=None, runner_factory=Runner):
        self.config = Config(directory)
        self.vault = Vault(self.config.directory)
        self.sessions, self.domains = self.vault.bind_core()
        self.store = Store(self.config, self.vault)
        self.runner = runner_factory(self.config, self.store)
        self.work = {}
        self.dispatcher = None
        self.stopping = False
        self.bot_status = {}
        self.last_cleanup_error = ''
        self.lock_handle = None
        self.analysis_slots = asyncio.Semaphore(2)

    async def start(self):
        # A second server against the same ledger must not interrupt live jobs.
        self.lock_handle = open(self.config.directory / 'service.lock', 'a+b')
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                self.lock_handle.seek(0)
                if not self.lock_handle.read(1): self.lock_handle.write(b'0'); self.lock_handle.flush()
                self.lock_handle.seek(0)
                msvcrt.locking(self.lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_handle.close()
            self.lock_handle = None
            raise AgentError('SERVICE_ALREADY_RUNNING', '同一状态目录已经有服务运行') from None
        self.store.recover()
        self.dispatcher = asyncio.create_task(self.dispatch())

    async def close(self):
        self.stopping = True
        if self.dispatcher:
            self.dispatcher.cancel()
            await asyncio.gather(self.dispatcher, return_exceptions=True)
        for task in list(self.work.values()): task.cancel()
        await asyncio.gather(*list(self.work.values()), return_exceptions=True)
        await self.runner.close()
        self.store.db.close()
        if self.lock_handle:
            if __import__('os').name == 'nt':
                import msvcrt
                self.lock_handle.seek(0)
                msvcrt.locking(self.lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.lock_handle.close()

    def resolve_options(self, options):
        s = self.config.settings
        overrides = options.model_dump(exclude_none=True, exclude={'use_defaults', 'preserve_original'})
        if options.use_defaults and overrides:
            raise AgentError('INVALID_PARAMETER', '指定自主参数时请设置 use_defaults=false')
        if not options.use_defaults and not s.autonomous:
            raise AgentError('FEATURE_DISABLED', '管理员关闭了 Agent 自主参数选择')
        if not options.preserve_original:
            raise AgentError('CAPABILITY_UNAVAILABLE', '当前核心支持保留原编码及音频/GIF转换，不提供任意视频重编码')
        result = dict(download_type=s.default_type, height=s.default_height, container=s.container, audio_format=s.audio_format)
        result.update(overrides)
        if result['height'] > s.max_height: raise AgentError('LIMIT_EXCEEDED', '请求画质超过管理员最高画质')
        feature = {'VIDEO_WITH_AUDIO': 'video', 'VIDEO_ONLY': 'video', 'GIF': 'video', 'AUDIO_ONLY': 'audio',
                   'IMAGE': 'images', 'IMAGE_ZIP': 'images', 'COVER': 'covers'}[result['download_type']]
        if not getattr(s, feature): raise AgentError('FEATURE_DISABLED', '管理员关闭了所请求的下载能力')
        return result

    def check_url(self, raw):
        url = source_url(raw)
        if is_profile(url) and not self.config.settings.profiles:
            raise AgentError('FEATURE_DISABLED', '管理员未允许用户主页解析与批量下载')
        return url

    async def analyze(self, owner, url, media_type='video'):
        url = self.check_url(url)
        s = self.config.settings
        if media_type == 'images' and not s.images: raise AgentError('FEATURE_DISABLED', '管理员关闭了图文能力')
        if media_type == 'video' and not any((s.video, s.audio, s.covers)):
            raise AgentError('FEATURE_DISABLED', '管理员关闭了视频相关能力')
        async with self.analysis_slots:
            result = await self.runner.call({'action': 'analyze', 'url': url, 'media_type': media_type, 'profile': is_profile(url)})
        result['options'] = [o for o in result.get('options', []) if getattr(s, {'VIDEO_WITH_AUDIO': 'video', 'VIDEO_ONLY': 'video',
            'GIF': 'video', 'AUDIO_ONLY': 'audio', 'COVER': 'covers', 'IMAGE': 'images', 'IMAGE_ZIP': 'images'}[o])]
        return result

    async def download(self, owner, url, options, request_key=None, *, collection=False, start=1, count=1, approval_id=None):
        url = self.check_url(url)
        s = self.config.settings
        resolved = self.resolve_options(options)
        if collection and not s.collections: raise AgentError('FEATURE_DISABLED', '管理员关闭了合集下载')
        limit = s.max_profile if is_profile(url) else s.max_collection
        if start < 1 or count < 1 or count > limit or start + count - 1 > limit:
            raise AgentError('LIMIT_EXCEEDED', '下载范围超过管理员数量上限')
        payload = dict(action='collection' if collection else 'download', url=url, options=resolved,
                       start=start, count=count, profile=is_profile(url))
        generations = {p: self.sessions.status(p)['generation'] for p in self.domains}
        key = hashlib.sha256(json.dumps([payload, s.model_dump(), generations], sort_keys=True).encode()).hexdigest()
        request_key = request_key or uuid.uuid4().hex
        existing = self.store.db.execute('SELECT id,cache_key FROM jobs WHERE owner=? AND request_key=?', (owner, request_key)).fetchone()
        if existing:
            if existing['cache_key'] != key: raise AgentError('IDEMPOTENCY_CONFLICT', '请求键与现有任务参数不匹配')
            return self.store.public_job(existing['id'], owner)
        approval = None
        if (count > s.confirmation_threshold or is_profile(url) or resolved['download_type']=='IMAGE_ZIP'
                or resolved['height'] > 2160 or s.max_file_bytes * count > 1024**3):
            approval = self.store.db.execute('SELECT * FROM approvals WHERE id=? AND owner=? AND fingerprint=? AND approved=1 AND used=0 AND expires>?',
                (approval_id or '', owner, key, time.time())).fetchone()
            if not approval:
                existing_approval=self.store.db.execute('SELECT id FROM approvals WHERE owner=? AND fingerprint=? AND used=0 AND expires>?',
                    (owner,key,time.time())).fetchone()
                identifier = existing_approval['id'] if existing_approval else uuid.uuid4().hex
                if not existing_approval:
                    self.store.db.execute('INSERT INTO approvals(id,owner,fingerprint,expires,summary) VALUES(?,?,?,?,?)',
                        (identifier, owner, key, time.time() + 600, f'{owner}: 最多{limit if resolved["download_type"]=="IMAGE_ZIP" else count} 个媒体，起始 {start}，类型 {resolved["download_type"]}'))
                raise AgentError('CONFIRMATION_REQUIRED', '此批量操作需要管理员在管理页确认后重新调用', approval_id=identifier, expires_in=600)
        queued = self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('QUEUED','DOWNLOADING','PROCESSING')").fetchone()[0]
        if queued >= s.max_queue: raise AgentError('QUEUE_FULL', '下载队列已满')
        if self.store.usage() + s.max_file_bytes * count > s.max_cache_bytes or shutil.disk_usage(self.config.cache).free < s.max_file_bytes * count:
            raise AgentError('CACHE_FULL', '缓存配额或磁盘剩余空间不足，请先清理或调低文件上限')
        row, new = self.store.create_job(owner, payload, key, request_key)
        if approval: self.store.db.execute('UPDATE approvals SET used=1 WHERE id=?', (approval['id'],))
        return self.store.public_job(row['id'], owner)

    async def dispatch(self):
        last_cleanup = 0
        while not self.stopping:
            s = self.config.settings
            for identifier, task in list(self.work.items()):
                if task.done():
                    # Observe exceptions so background errors never disappear.
                    try: task.result()
                    except (Exception, asyncio.CancelledError): pass
                    self.work.pop(identifier, None)
            slots = max(0, s.concurrency - len(self.work))
            if slots:
                jobs = self.store.db.execute("SELECT * FROM jobs WHERE state='QUEUED' AND source_id IS NULL ORDER BY created LIMIT ?", (slots,)).fetchall()
                for job in jobs:
                    if job['id'] not in self.work:
                        self.work[job['id']] = asyncio.create_task(self.run_job(dict(job)))
            # Delivery has its own bounded slots; no unbounded upload coroutine creation.
            sending = sum(identifier.startswith('send:') for identifier in self.work)
            if sending < 2:
                for delivery in self.store.db.execute("SELECT * FROM deliveries WHERE state='QUEUED' ORDER BY created LIMIT ?", (2 - sending,)).fetchall():
                    key = 'send:' + delivery['id']
                    if key not in self.work: self.work[key] = asyncio.create_task(self.run_delivery(dict(delivery)))
            if time.monotonic() - last_cleanup > 60:
                try:
                    self.store.cleanup(expired=True)
                    self.prune_workspaces()
                except AgentError: pass
                last_cleanup = time.monotonic()
            await asyncio.sleep(.2)

    async def run_job(self, row):
        identifier = row['id']
        try:
            payload = self.store.payload(row)
            # Recheck current administrator policy when queued work actually starts.
            options = DownloadOptions(use_defaults=False, **payload['options'])
            if not self.config.settings.autonomous: options = DownloadOptions()
            current = self.resolve_options(options)
            if current != payload['options']: raise AgentError('POLICY_CHANGED', '排队期间默认参数或权限已变化，请重新提交')
            self.check_url(payload['url'])
            if payload['action'] == 'collection' and not self.config.settings.collections:
                raise AgentError('FEATURE_DISABLED', '管理员关闭了合集下载')
            self.store.update_job(identifier, 'DOWNLOADING')
            def progress(event):
                if self.store.job(identifier)['state'] == 'CANCELLED': return
                self.store.update_job(identifier, 'PROCESSING' if event['state'] == 'PROCESSING' else 'DOWNLOADING',
                                      progress=event['progress'], speed=safe_text(event.get('speed', '')))
            result = await self.runner.call(payload, identifier, progress)
            self.store.finish(identifier, result['paths'])
        except asyncio.CancelledError:
            self.store.fail(identifier, 'CANCELLED', 'CANCELLED')
            raise
        except AgentError as error: self.store.fail(identifier, error.code)
        except Exception: self.store.fail(identifier, 'INTERNAL_ERROR')

    async def cancel(self, owner, identifier):
        job = self.store.job(identifier, owner)
        if job['state'] not in ACTIVE: raise AgentError('INVALID_STATE', '仅可取消排队或处理中的任务')
        followers = self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE source_id=? AND state='QUEUED'", (identifier,)).fetchone()[0]
        if followers:
            # Release this request while the common core job continues for its
            # other owners. Never let one user kill another user's download.
            self.store.update_job(identifier,'CANCELLED',error='REFERENCE_CANCELLED')
            return self.store.public_job(identifier,owner)
        task = self.work.get(identifier)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.store.fail(identifier, 'CANCELLED', 'CANCELLED')
        if job['source_id']:
            source = self.store.job(job['source_id'])
            remaining = self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE source_id=? AND state='QUEUED'",(source['id'],)).fetchone()[0]
            if source['state']=='CANCELLED' and not remaining and source['id'] in self.work:
                self.work[source['id']].cancel()
                await asyncio.gather(self.work[source['id']],return_exceptions=True)
        return self.store.public_job(identifier, owner)

    def bot_config(self, name):
        if name not in ('telegram', 'onebot'): raise AgentError('INVALID_PARAMETER', '不支持的机器人适配器')
        data = self.vault.get('bot-' + name)
        return BotConfig.model_validate_json(data) if data else BotConfig(adapter=name)

    async def deliver(self, owner, identifier, adapter, target, kind, request_key):
        config = self.bot_config(adapter)
        bot = make_adapter(config)
        bot.check_target(target)
        identities = json.loads(self.vault.get('identities') or '{}')
        if owner in identities and target not in identities[owner]['targets']:
            raise AgentError('TARGET_DENIED', '当前身份未获授权向此会话发送文件')
        row, new = self.store.create_delivery(identifier, owner, adapter, target, kind, request_key)
        return {k: row[k] for k in ('id', 'task_id', 'state', 'error')}

    async def run_delivery(self, row):
        identifier = row['id']
        self.store.db.execute("UPDATE deliveries SET state='SENDING',updated=? WHERE id=? AND state='QUEUED'", (time.time(), identifier))
        try:
            adapter = make_adapter(self.bot_config(row['adapter']))
            for file in self.store.files(row['task_id']):
                if self.store.db.execute('SELECT 1 FROM receipts WHERE delivery_id=? AND file_id=?', (identifier, file['id'])).fetchone(): continue
                record, path = self.store.file(file['id'], row['owner'])
                receipt = await adapter.send(path, row['target'], row['kind'])
                self.store.receipt(identifier, file['id'], receipt)
            self.store.sent(identifier)
            if self.config.settings.auto_delete:
                try: self.store.cleanup()
                except (AgentError,OSError):
                    # A cleanup error cannot undo a real platform acknowledgement.
                    self.last_cleanup_error='CLEANUP_DEFERRED'
        except asyncio.CancelledError:
            self.store.db.execute("UPDATE deliveries SET state='UNCERTAIN',error='SEND_INTERRUPTED' WHERE id=?", (identifier,))
            self.store.update_job(row['task_id'], 'FAILED', error='DELIVERY_UNCERTAIN')
            raise
        except AgentError as error:
            state = 'UNCERTAIN' if error.code == 'DELIVERY_UNCERTAIN' else 'FAILED'
            self.store.db.execute('UPDATE deliveries SET state=?,error=?,updated=? WHERE id=?', (state, error.code, time.time(), identifier))
            self.store.update_job(row['task_id'], 'FAILED', error=error.code)
        except Exception:
            self.store.db.execute("UPDATE deliveries SET state='UNCERTAIN',error='SEND_ERROR' WHERE id=?", (identifier,))
            self.store.update_job(row['task_id'], 'FAILED', error='DELIVERY_UNCERTAIN')

    def retry_delivery(self, identifier, *, reconcile=False):
        row = self.store.delivery(identifier)
        if row['state'] == 'UNCERTAIN' and not reconcile:
            raise AgentError('RECONCILIATION_REQUIRED', '发送结果不确定，管理员须核对平台后显式允许重试')
        if row['state'] not in ('FAILED', 'UNCERTAIN'): raise AgentError('INVALID_STATE', '此交付不需要重试')
        self.store.db.execute("UPDATE deliveries SET state='QUEUED',error='' WHERE id=?", (identifier,))
        self.store.update_job(row['task_id'], 'SENDING')
        return {'id': identifier, 'state': 'QUEUED'}

    def file_reference(self, owner, identifier):
        record, path = self.store.file(identifier, owner)
        return {'file_id': identifier, 'uri': 'omni-file://' + identifier, 'bytes': record['bytes'],
                'sha256': record['digest'], 'mime_type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                'name': path.name, 'delivery': 'Use deliver_media with the owning task_id; this URI is not a filesystem path.'}

    def capabilities(self):
        from importlib.metadata import version
        from .credentials import core_root,native_runtime
        import shutil
        ffmpeg = bool(native_runtime('ffmpeg'))
        try:
            import imageio_ffmpeg
            ffmpeg = ffmpeg or Path(imageio_ffmpeg.get_ffmpeg_exe()).is_file()
        except (ImportError, RuntimeError): pass
        s = self.config.settings
        return {'edition': 'OmniDownloader Agent Edition', 'version': __version__, 'mcp_sdk': version('mcp'),
            'transports': ['stdio_bridge', 'streamable-http'], 'platforms': list(self.domains),
            'platform_availability': 'Actual extraction is verified per link and account; not every platform supports every media type.',
            'ffmpeg': ffmpeg, 'deno':bool(native_runtime('deno')), 'headless': True, 'browser_assisted_login': False,
            'arbitrary_video_reencode': False, 'drm_bypass': False,
            'login_validation': ['哔哩哔哩', 'Pixiv'], 'features': {k: getattr(s, k) for k in ('video','audio','images','collections','profiles','covers','autonomous','reuse_cache','auto_delete')},
            'profile_adapters': {'YouTube':'bounded video playlists','X (Twitter)':'bounded first photo page'},
            'limits': {k: getattr(s, k) for k in ('max_height','max_file_bytes','max_cache_bytes','max_collection','max_profile','concurrency','task_timeout','confirmation_threshold')},
            'bots': {name: {'enabled': self.bot_config(name).enabled, 'status': self.bot_status.get(name, {'connected': None})} for name in ('telegram', 'onebot')}}

    def prune_workspaces(self):
        # Only recorded UUID workspaces in terminal states, and only after all
        # validated artifacts have been cleaned. No user-selected directory rmtree.
        from .security import controlled
        for row in self.store.db.execute("SELECT id,state,updated FROM jobs WHERE state IN ('CLEANED','CANCELLED','EXPIRED','FAILED')").fetchall():
            if row['state'] == 'FAILED' and row['updated'] > time.time() - self.config.settings.cache_ttl: continue
            files = self.store.files(row['id'])
            if any(f['state'] != 'CLEANED' for f in files): continue
            path = controlled(self.config.cache / 'jobs', self.config.cache / 'jobs' / row['id'])
            if path.exists():
                for child in path.rglob('*'): controlled(path, child)
                shutil.rmtree(path)

    def authenticate(self, token, admin=False):
        if secrets.compare_digest(token, self.config.tokens['admin']):
            if admin: return 'admin'
            raise AgentError('AUTH_REQUIRED', '媒体接口请使用独立 Agent 令牌')
        if not admin:
            if secrets.compare_digest(token, self.config.tokens['agent']): return 'agent'
            identities = json.loads(self.vault.get('identities') or '{}')
            for name, row in identities.items():
                if secrets.compare_digest(token, row['token']): return name
        raise AgentError('AUTH_REQUIRED', '需要有效的管理员或 Agent 访问令牌')
