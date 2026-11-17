import asyncio
import hashlib
import json
import mimetypes
from pathlib import Path
import shutil
import time
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .config import Config, Settings
from .credentials import Vault
from .delivery import BotConfig, make_adapter
from .errors import AgentError, safe_text
from .runner import Runner
from .security import is_profile, parse_download_command, source_url
from .store import ACTIVE, Store


class DownloadOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    use_defaults: bool = True
    download_type: Literal['VIDEO_WITH_AUDIO', 'VIDEO_ONLY', 'AUDIO_ONLY', 'GIF', 'COVER', 'IMAGE', 'IMAGE_ZIP'] | None = None
    height: int | None = Field(None, ge=144, le=8640)
    container: Literal['mp4', 'mkv', 'auto'] | None = None
    audio_format: Literal['mp3', 'm4a', 'flac', 'opus'] | None = None
    pack_zip: bool | None = None
    all_items: bool | None = None
    preserve_original: bool = True


class SettingsUpdate(BaseModel):
    """允许用户与 Agent 部分更新或全量更新的服务设置模型。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    default_type: Literal['VIDEO_WITH_AUDIO', 'VIDEO_ONLY', 'AUDIO_ONLY', 'GIF', 'COVER', 'IMAGE', 'IMAGE_ZIP'] | None = None
    default_height: int | None = Field(None, ge=144, le=8640)
    max_height: int | None = Field(None, ge=144, le=8640)
    container: Literal['mp4', 'mkv', 'auto'] | None = None
    audio_format: Literal['mp3', 'm4a', 'flac', 'opus'] | None = None
    pack_zip: bool | None = None
    autonomous: bool | None = None
    max_file_bytes: int | None = Field(None, ge=1024, le=20 * 1024**3)
    max_cache_bytes: int | None = Field(None, ge=1024**2, le=1024**4)
    concurrency: int | None = Field(None, ge=1, le=8)
    max_queue: int | None = Field(None, ge=1, le=1000)
    task_timeout: int | None = Field(None, ge=10, le=7200)
    cache_ttl: int | None = Field(None, ge=60, le=2592000)
    download_dir: str | None = None
    image_dir: str | None = None
    cache_dir: str | None = None
    proxy: str | None = None
    video: bool | None = None
    audio: bool | None = None
    images: bool | None = None
    collections: bool | None = None
    profiles: bool | None = None
    covers: bool | None = None
    max_collection: int | None = Field(None, ge=1, le=200)
    max_profile: int | None = Field(None, ge=1, le=50)
    confirmation_threshold: int | None = Field(None, ge=1, le=200)
    auto_delete: bool | None = None
    reuse_cache: bool | None = None
    qq_video_mode: Literal['message', 'file'] | None = None
    qq_image_mode: Literal['message', 'file'] | None = None
    qq_forward_collection: bool | None = None
    theme: Literal['auto', 'light', 'dark'] | None = None


class BotConfigUpdate(BaseModel):
    """允许更新的机器人适配器参数模型。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    adapter: Literal['telegram', 'onebot']
    enabled: bool | None = None
    base_url: str | None = None
    token: str | None = None
    local_api: bool | None = None
    max_upload_bytes: int | None = Field(None, ge=1024, le=2000 * 1024**2)
    allowed_targets: list[str] | None = Field(None, max_length=200)
    shared_root: str | None = None
    remote_root: str | None = None


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
        # 同一状态目录同时只允许运行一个服务实例，避免损坏任务与文件账本
        self.lock_handle = open(self.config.directory / 'service.lock', 'a+b')
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                self.lock_handle.seek(0)
                if not self.lock_handle.read(1):
                    self.lock_handle.write(b'0')
                    self.lock_handle.flush()
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
        for task in list(self.work.values()):
            task.cancel()
        await asyncio.gather(*list(self.work.values()), return_exceptions=True)
        await self.runner.close()
        self.store.db.close()
        if self.lock_handle:
            if __import__('os').name == 'nt':
                import msvcrt
                self.lock_handle.seek(0)
                msvcrt.locking(self.lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.lock_handle.close()

    # -------------------- 设置与权限管理（用户与 Agent 共享） --------------------

    def get_settings(self):
        return self.config.settings.model_dump()

    def _validate_and_prepare_dirs(self, settings):
        for label, path_str in (('下载目录', settings.download_dir), ('图文目录', settings.image_dir)):
            if not path_str:
                continue
            target = Path(path_str).resolve()
            if target == self.config.directory.resolve() or target.is_relative_to(self.config.cache.resolve()):
                raise AgentError('INVALID_PARAMETER', f'{label}不能与服务内部状态或缓存目录重叠')
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise AgentError('INVALID_PARAMETER', f'无法创建或访问{label}') from None

    def update_settings(self, patch):
        merged = self.config.settings.model_dump() | patch
        try:
            settings = Settings.model_validate(merged)
        except ValidationError as exc:
            detail = exc.errors()[0].get('msg', '参数无效') if exc.errors() else '参数无效'
            raise AgentError('INVALID_PARAMETER', f'设置参数无效：{detail}') from None
        self._validate_and_prepare_dirs(settings)
        if settings.cache_dir != self.config.settings.cache_dir:
            active = self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('QUEUED','DOWNLOADING','PROCESSING','SENDING')").fetchone()[0]
            if active:
                raise AgentError('TASKS_ACTIVE', '活动任务结束前无法更换缓存目录')
            self.prune_workspaces()
        self.config.save(settings)
        return settings.model_dump()

    def reset_settings(self, *, reset_cache_dir=False):
        defaults = Settings()
        preserved_cache = False
        if not reset_cache_dir and self.config.settings.cache_dir:
            has_files = any((p.is_file() and p.name != '.owner') or p.is_symlink() or p.is_junction() for p in self.config.cache.rglob('*'))
            if has_files:
                defaults = defaults.model_copy(update={'cache_dir': self.config.settings.cache_dir})
                preserved_cache = True
        if defaults.cache_dir != self.config.settings.cache_dir:
            active = self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('QUEUED','DOWNLOADING','PROCESSING','SENDING')").fetchone()[0]
            if active:
                defaults = defaults.model_copy(update={'cache_dir': self.config.settings.cache_dir})
                preserved_cache = True
            else:
                self.prune_workspaces()
        self.config.save(defaults)
        return {'settings': defaults.model_dump(), 'cache_dir_preserved': preserved_cache}

    def get_bot_configs(self):
        return [
            self.bot_config(name).model_dump(exclude={'token'})
            | {
                'has_token': bool(self.bot_config(name).token),
                'status': self.bot_status.get(name, {'connected': None}),
            }
            for name in ('telegram', 'onebot')
        ]

    def update_bot_config(self, patch):
        adapter = patch.get('adapter')
        if adapter not in ('telegram', 'onebot'):
            raise AgentError('INVALID_PARAMETER', '不支持的机器人适配器')
        current = self.bot_config(adapter).model_dump()
        merged = dict(current)
        for key, value in patch.items():
            if value is not None:
                merged[key] = value
        # 未填写新令牌时保留已加密存储的旧令牌
        if not patch.get('token'):
            merged['token'] = current['token']
        if merged.get('enabled') and not merged.get('base_url'):
            if adapter == 'telegram' and not merged.get('local_api'):
                merged['base_url'] = 'https://api.telegram.org'
            elif adapter == 'onebot':
                merged['base_url'] = 'http://127.0.0.1:3000'
        try:
            body = BotConfig.model_validate(merged)
        except (ValidationError, ValueError, AttributeError):
            raise AgentError('INVALID_PARAMETER', '机器人参数无效；启用时需填写令牌、目标名单及共享目录') from None
        self.vault.put('bot-' + body.adapter, body.model_dump_json())
        self.bot_status.pop(body.adapter, None)
        return body.model_dump(exclude={'token'}) | {
            'has_token': bool(body.token),
            'status': self.bot_status.get(body.adapter, {'connected': None}),
        }

    async def probe_bot(self, name):
        try:
            result = await make_adapter(self.bot_config(name)).probe()
        except AgentError as error:
            self.bot_status[name] = {
                'connected': False,
                'error': error.code,
                'message': error.message,
                'checked_at': time.time(),
            }
            raise
        self.bot_status[name] = result
        return result

    def list_approvals(self):
        rows = self.store.db.execute('SELECT id,summary,expires,approved FROM approvals WHERE used=0 AND expires>?', (time.time(),)).fetchall()
        return [dict(r) for r in rows]

    def approve_request(self, identifier):
        cursor = self.store.db.execute('UPDATE approvals SET approved=1 WHERE id=? AND expires>? AND used=0', (identifier, time.time()))
        if not cursor.rowcount:
            raise AgentError('NOT_FOUND', '确认请求不存在或已过期')
        return {'ok': True, 'approval_id': identifier}

    def cleanup_cache(self):
        return self.store.cleanup()

    # -------------------- 平台 Cookie 状态与操作（严格隔离） --------------------

    def _validation_key(self, platform):
        return 'validation-' + str(list(self.domains).index(platform))

    def _session_info(self, platform):
        raw = self.vault.get(self._validation_key(platform))
        validation = json.loads(raw) if raw else {'state': 'UNVERIFIED'}
        status = self.sessions.status(platform)
        # 严禁向任何外部接口返回 Cookie 明文、键值对、文件路径或额外账号隐私信息
        clean_validation = {'state': validation.get('state', 'UNVERIFIED')}
        if 'checked_at' in validation:
            clean_validation['checked_at'] = validation['checked_at']
        return {
            'platform': status['platform'],
            'state': status['state'],
            'enabled': status['enabled'],
            'validation': clean_validation,
        }

    def _resolve_cookie_targets(self, platform):
        if platform == 'all':
            return list(self.domains)
        if platform not in self.domains:
            raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        return [platform]

    def list_cookie_statuses(self):
        return [self._session_info(site) for site in self.domains]

    def disable_cookie(self, platform):
        targets = self._resolve_cookie_targets(platform)
        for site in targets:
            self.sessions.set_enabled(site, False)
        return {'platforms': [self._session_info(site) for site in targets]}

    def remove_cookie(self, platform):
        targets = self._resolve_cookie_targets(platform)
        for site in targets:
            self.sessions.clear(site)
            self.vault.put(self._validation_key(site), '{"state":"MISSING"}')
        return {'platforms': [self._session_info(site) for site in targets]}

    def import_cookie(self, platform, text):
        """仅限用户管理页调用：加密导入或更新平台 Cookie 并默认启用。"""
        if platform not in self.domains:
            raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        text = text.strip()
        if not text.startswith(('# Netscape HTTP Cookie File', '# HTTP Cookie File')):
            rows = []
            for part in text.split(';'):
                name, sep, value = part.strip().partition('=')
                if not sep or not name or any(c in part for c in '\r\n\t'):
                    raise AgentError('INVALID_COOKIE', 'Cookie格式无效')
                rows.append('.' + self.domains[platform][0] + '\tTRUE\t/\tTRUE\t0\t' + name + '\t' + value)
            text = '# Netscape HTTP Cookie File\n' + '\n'.join(rows)
        try:
            self.sessions.import_text(platform, text)
        except (ValueError, OSError):
            raise AgentError('INVALID_COOKIE', 'Cookie文件无效或不包含此平台有效凭据') from None
        self.sessions.set_enabled(platform, True)
        self.vault.put(self._validation_key(platform), '{"state":"UNVERIFIED"}')
        return self._session_info(platform)

    def set_cookie_enabled(self, platform, enabled):
        """仅限用户管理页调用：手动启用或禁用平台 Cookie。"""
        if platform not in self.domains:
            raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        self.sessions.set_enabled(platform, enabled)
        return self._session_info(platform)

    async def verify_cookie(self, platform):
        """仅限用户管理页调用：在线验证平台 Cookie 有效性。"""
        if platform not in self.domains:
            raise AgentError('UNSUPPORTED_PLATFORM', '未知平台')
        if not self.sessions.is_enabled(platform):
            raise AgentError('COOKIE_DISABLED', '当前平台 Cookie 已禁用，请先启用后再检测')
        result = await self.runner.call({'action': 'verify_session', 'platform': platform})
        result['checked_at'] = time.time()
        self.vault.put(self._validation_key(platform), json.dumps(result))
        return result

    # -------------------- 媒体解析、下载与交付调度 --------------------

    def resolve_options(self, options, *, cmd_all=False, cmd_zip=False):
        s = self.config.settings
        overrides = options.model_dump(exclude_none=True, exclude={'use_defaults', 'preserve_original'})
        if options.use_defaults and overrides:
            raise AgentError('INVALID_PARAMETER', '指定自主参数时请设置 use_defaults=false')
        if not options.use_defaults and not s.autonomous:
            raise AgentError('FEATURE_DISABLED', '当前设置关闭了 Agent 自主参数选择')
        if not options.preserve_original:
            raise AgentError('CAPABILITY_UNAVAILABLE', '当前核心支持保留原编码及音频/GIF转换，不提供任意视频重编码')

        default_type = 'IMAGE' if s.default_type == 'IMAGE_ZIP' else s.default_type
        default_pack_zip = s.pack_zip or (s.default_type == 'IMAGE_ZIP')
        result = dict(
            download_type=default_type,
            height=s.default_height,
            container=s.container,
            audio_format=s.audio_format,
            pack_zip=default_pack_zip,
            all_items=False,
        )
        result.update(overrides)
        if result['download_type'] == 'IMAGE_ZIP':
            result['download_type'] = 'IMAGE'
            result['pack_zip'] = True
        if cmd_all:
            result['all_items'] = True
        if cmd_zip:
            result['pack_zip'] = True
        if result['height'] > s.max_height:
            raise AgentError('LIMIT_EXCEEDED', '请求画质超过最高允许画质')
        feature = {'VIDEO_WITH_AUDIO': 'video', 'VIDEO_ONLY': 'video', 'GIF': 'video', 'AUDIO_ONLY': 'audio',
                   'IMAGE': 'images', 'COVER': 'covers'}[result['download_type']]
        if not getattr(s, feature):
            raise AgentError('FEATURE_DISABLED', '当前设置关闭了所请求的下载能力')
        return result

    def check_url(self, raw):
        clean, cmd_all, cmd_zip = parse_download_command(raw)
        url = source_url(clean)
        if is_profile(url) and not self.config.settings.profiles:
            raise AgentError('FEATURE_DISABLED', '当前设置未允许用户主页解析与批量下载')
        return url, cmd_all, cmd_zip

    async def analyze(self, owner, raw_url, media_type='video'):
        url, _, _ = self.check_url(raw_url)
        s = self.config.settings
        if media_type == 'images' and not s.images:
            raise AgentError('FEATURE_DISABLED', '当前设置关闭了图文能力')
        if media_type == 'video' and not any((s.video, s.audio, s.covers)):
            raise AgentError('FEATURE_DISABLED', '当前设置关闭了视频相关能力')
        async with self.analysis_slots:
            result = await self.runner.call({'action': 'analyze', 'url': url, 'media_type': media_type, 'profile': is_profile(url)})
        result['options'] = [o for o in result.get('options', []) if getattr(s, {'VIDEO_WITH_AUDIO': 'video', 'VIDEO_ONLY': 'video',
            'GIF': 'video', 'AUDIO_ONLY': 'audio', 'COVER': 'covers', 'IMAGE': 'images', 'IMAGE_ZIP': 'images'}[o])]
        return result

    async def download(self, owner, raw_url, options, request_key=None, *, collection=False, start=1, count=1, approval_id=None):
        url, cmd_all, cmd_zip = self.check_url(raw_url)
        s = self.config.settings
        resolved = self.resolve_options(options, cmd_all=cmd_all, cmd_zip=cmd_zip)
        if (collection or resolved['all_items']) and not s.collections and resolved['download_type'] != 'IMAGE':
            raise AgentError('FEATURE_DISABLED', '当前设置关闭了合集下载')
        limit = s.max_profile if is_profile(url) else s.max_collection
        if start < 1 or count < 1 or count > limit or start + count - 1 > limit:
            raise AgentError('LIMIT_EXCEEDED', '下载范围超过当前数量上限')
        payload = dict(
            action='collection' if collection else 'download',
            url=url,
            options=resolved,
            start=start,
            count=count,
            all_items=resolved['all_items'],
            cmd_all=cmd_all,
            cmd_zip=cmd_zip,
            profile=is_profile(url),
        )
        generations = {p: self.sessions.status(p)['generation'] for p in self.domains}
        key = hashlib.sha256(json.dumps([payload, s.model_dump(), generations], sort_keys=True).encode()).hexdigest()
        request_key = request_key or uuid.uuid4().hex
        existing = self.store.db.execute('SELECT id,cache_key FROM jobs WHERE owner=? AND request_key=?', (owner, request_key)).fetchone()
        if existing:
            if existing['cache_key'] != key:
                raise AgentError('IDEMPOTENCY_CONFLICT', '请求键与现有任务参数不匹配')
            return self.store.public_job(existing['id'], owner)
        approval = None
        needs_confirm = (
            count > s.confirmation_threshold
            or is_profile(url)
            or (resolved['download_type'] == 'IMAGE' and resolved['pack_zip'])
            or (resolved['all_items'] and limit > s.confirmation_threshold)
            or resolved['height'] > 2160
            or s.max_file_bytes * count > 1024**3
        )
        if needs_confirm:
            approval = self.store.db.execute(
                'SELECT * FROM approvals WHERE id=? AND owner=? AND fingerprint=? AND approved=1 AND used=0 AND expires>?',
                (approval_id or '', owner, key, time.time()),
            ).fetchone()
            if not approval:
                existing_approval = self.store.db.execute(
                    'SELECT id FROM approvals WHERE owner=? AND fingerprint=? AND used=0 AND expires>?',
                    (owner, key, time.time()),
                ).fetchone()
                identifier = existing_approval['id'] if existing_approval else uuid.uuid4().hex
                if not existing_approval:
                    est = limit if (resolved['all_items'] or (resolved['download_type'] == 'IMAGE' and resolved['pack_zip'])) else count
                    self.store.db.execute(
                        'INSERT INTO approvals(id,owner,fingerprint,expires,summary) VALUES(?,?,?,?,?)',
                        (identifier, owner, key, time.time() + 600, f'{owner}: 最多{est} 个媒体，起始 {start}，类型 {resolved["download_type"]}'),
                    )
                raise AgentError('CONFIRMATION_REQUIRED', '此批量操作需要确认后重新调用（可在管理页或通过 approve_request 批准）', approval_id=identifier, expires_in=600)
        queued = self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('QUEUED','DOWNLOADING','PROCESSING')").fetchone()[0]
        if queued >= s.max_queue:
            raise AgentError('QUEUE_FULL', '下载队列已满')
        if self.store.usage() + s.max_file_bytes * count > s.max_cache_bytes or shutil.disk_usage(self.config.cache).free < s.max_file_bytes * count:
            raise AgentError('CACHE_FULL', '缓存配额或磁盘剩余空间不足，请先清理或调低文件上限')
        row, new = self.store.create_job(owner, payload, key, request_key)
        if approval:
            self.store.db.execute('UPDATE approvals SET used=1 WHERE id=?', (approval['id'],))
        return self.store.public_job(row['id'], owner)

    def _export_to_storage(self, payload, paths):
        """若用户配置了永久下载或图文存储目录，则安全导出成品副本，不受自动缓存清理影响。"""
        s = self.config.settings
        dtype = (payload.get('options') or {}).get('download_type')
        is_image_job = dtype in ('IMAGE', 'IMAGE_ZIP')
        target_str = (s.image_dir or s.download_dir) if is_image_job else s.download_dir
        if not target_str:
            return
        dest_dir = Path(target_str)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src_item in paths:
            src = Path(src_item)
            if not src.is_file():
                continue
            target = dest_dir / src.name
            index = 1
            while target.exists():
                target = dest_dir / f'{src.stem}_{index}{src.suffix}'
                index += 1
            partial = dest_dir / f'.{target.name}.{uuid.uuid4().hex[:8]}.part'
            try:
                shutil.copyfile(src, partial)
                if partial.stat().st_size != src.stat().st_size:
                    raise OSError('导出文件大小不一致')
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)

    async def dispatch(self):
        last_cleanup = 0
        while not self.stopping:
            s = self.config.settings
            for identifier, task in list(self.work.items()):
                if task.done():
                    try:
                        task.result()
                    except (Exception, asyncio.CancelledError):
                        pass
                    self.work.pop(identifier, None)
            slots = max(0, s.concurrency - len(self.work))
            if slots:
                jobs = self.store.db.execute("SELECT * FROM jobs WHERE state='QUEUED' AND source_id IS NULL ORDER BY created LIMIT ?", (slots,)).fetchall()
                for job in jobs:
                    if job['id'] not in self.work:
                        self.work[job['id']] = asyncio.create_task(self.run_job(dict(job)))
            sending = sum(identifier.startswith('send:') for identifier in self.work)
            if sending < 2:
                for delivery in self.store.db.execute("SELECT * FROM deliveries WHERE state='QUEUED' ORDER BY created LIMIT ?", (2 - sending,)).fetchall():
                    key = 'send:' + delivery['id']
                    if key not in self.work:
                        self.work[key] = asyncio.create_task(self.run_delivery(dict(delivery)))
            if time.monotonic() - last_cleanup > 60:
                try:
                    self.store.cleanup(expired=True)
                    self.prune_workspaces()
                except AgentError:
                    pass
                last_cleanup = time.monotonic()
            await asyncio.sleep(.2)

    async def run_job(self, row):
        identifier = row['id']
        try:
            payload = self.store.payload(row)
            saved_opts = payload['options']
            options = DownloadOptions(use_defaults=False, **saved_opts)
            if not self.config.settings.autonomous:
                options = DownloadOptions()
            current = self.resolve_options(
                options,
                cmd_all=bool(payload.get('cmd_all')),
                cmd_zip=bool(payload.get('cmd_zip')),
            )
            if current != saved_opts:
                raise AgentError('POLICY_CHANGED', '排队期间默认参数或权限已变化，请重新提交')
            self.check_url(payload['url'])
            if payload['action'] == 'collection' and not self.config.settings.collections:
                raise AgentError('FEATURE_DISABLED', '当前设置关闭了合集下载')
            self.store.update_job(identifier, 'DOWNLOADING')

            def progress(event):
                if self.store.job(identifier)['state'] == 'CANCELLED':
                    return
                self.store.update_job(identifier, 'PROCESSING' if event['state'] == 'PROCESSING' else 'DOWNLOADING',
                                      progress=event['progress'], speed=safe_text(event.get('speed', '')))
            result = await self.runner.call(payload, identifier, progress)
            self._export_to_storage(payload, result['paths'])
            self.store.finish(identifier, result['paths'])
        except asyncio.CancelledError:
            self.store.fail(identifier, 'CANCELLED', 'CANCELLED')
            raise
        except AgentError as error:
            self.store.fail(identifier, error.code)
        except Exception:
            self.store.fail(identifier, 'INTERNAL_ERROR')

    async def cancel(self, owner, identifier):
        job = self.store.job(identifier, owner)
        if job['state'] not in ACTIVE:
            raise AgentError('INVALID_STATE', '仅可取消排队或处理中的任务')
        followers = self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE source_id=? AND state='QUEUED'", (identifier,)).fetchone()[0]
        if followers:
            self.store.update_job(identifier, 'CANCELLED', error='REFERENCE_CANCELLED')
            return self.store.public_job(identifier, owner)
        task = self.work.get(identifier)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.store.fail(identifier, 'CANCELLED', 'CANCELLED')
        if job['source_id']:
            source = self.store.job(job['source_id'])
            remaining = self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE source_id=? AND state='QUEUED'", (source['id'],)).fetchone()[0]
            if source['state'] == 'CANCELLED' and not remaining and source['id'] in self.work:
                self.work[source['id']].cancel()
                await asyncio.gather(self.work[source['id']], return_exceptions=True)
        return self.store.public_job(identifier, owner)

    def bot_config(self, name):
        if name not in ('telegram', 'onebot'):
            raise AgentError('INVALID_PARAMETER', '不支持的机器人适配器')
        data = self.vault.get('bot-' + name)
        return BotConfig.model_validate_json(data) if data else BotConfig(adapter=name)

    async def deliver(self, owner, identifier, adapter, target, kind, request_key):
        config = self.bot_config(adapter)
        bot = make_adapter(config)
        bot.check_target(target)
        row, new = self.store.create_delivery(identifier, owner, adapter, target, kind, request_key)
        return {k: row[k] for k in ('id', 'task_id', 'state', 'error')}

    async def run_delivery(self, row):
        identifier = row['id']
        self.store.db.execute("UPDATE deliveries SET state='SENDING',updated=? WHERE id=? AND state='QUEUED'", (time.time(), identifier))
        try:
            adapter = make_adapter(self.bot_config(row['adapter']))
            s = self.config.settings
            pending = []
            for file in self.store.files(row['task_id']):
                if self.store.db.execute('SELECT 1 FROM receipts WHERE delivery_id=? AND file_id=?', (identifier, file['id'])).fetchone():
                    continue
                record, path = self.store.file(file['id'], row['owner'])
                pending.append((record, path))

            if row['adapter'] == 'onebot' and s.qq_forward_collection:
                forwardable = [(rec, p) for rec, p in pending if p.suffix.lower() != '.zip' and adapter.media_category(p) in ('image', 'video')]
                others = [(rec, p) for rec, p in pending if (rec, p) not in forwardable]
                if len(forwardable) >= 2:
                    receipt = await adapter.send_forward([p for _, p in forwardable], row['target'])
                    for rec, _ in forwardable:
                        self.store.receipt(identifier, rec['id'], receipt)
                    pending = others

            for record, path in pending:
                receipt = await adapter.send(
                    path,
                    row['target'],
                    row['kind'],
                    video_mode=s.qq_video_mode,
                    image_mode=s.qq_image_mode,
                )
                self.store.receipt(identifier, record['id'], receipt)

            self.store.sent(identifier)
            if s.auto_delete:
                try:
                    self.store.cleanup()
                except (AgentError, OSError):
                    self.last_cleanup_error = 'CLEANUP_DEFERRED'
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
            raise AgentError('RECONCILIATION_REQUIRED', '发送结果不确定，须核对平台后显式允许重试')
        if row['state'] not in ('FAILED', 'UNCERTAIN'):
            raise AgentError('INVALID_STATE', '此交付不需要重试')
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
        from .credentials import native_runtime
        ffmpeg = bool(native_runtime('ffmpeg'))
        try:
            import imageio_ffmpeg
            ffmpeg = ffmpeg or Path(imageio_ffmpeg.get_ffmpeg_exe()).is_file()
        except (ImportError, RuntimeError):
            pass
        s = self.config.settings
        return {
            'edition': 'OmniDownloader Agent Edition',
            'version': __version__,
            'mcp_sdk': version('mcp'),
            'transports': ['stdio_bridge', 'streamable-http'],
            'platforms': list(self.domains),
            'platform_availability': 'Actual extraction is verified per link and account; not every platform supports every media type.',
            'ffmpeg': ffmpeg,
            'deno': bool(native_runtime('deno')),
            'headless': True,
            'browser_assisted_login': False,
            'arbitrary_video_reencode': False,
            'drm_bypass': False,
            'auth_required': False,
            'download_commands': {'/us': '下载合集、分集视频或全部图片', '/zip': '将输出打包为 ZIP 压缩包发送'},
            'cookie_agent_permissions': ['read_status', 'disable', 'remove'],
            'login_validation': ['哔哩哔哩', 'Pixiv'],
            'features': {k: getattr(s, k) for k in ('video', 'audio', 'images', 'collections', 'profiles', 'covers', 'autonomous', 'reuse_cache', 'auto_delete', 'pack_zip', 'qq_forward_collection')},
            'qq_delivery': {'video_mode': s.qq_video_mode, 'image_mode': s.qq_image_mode, 'forward_collection': s.qq_forward_collection},
            'profile_adapters': {'YouTube': 'bounded video playlists', 'X (Twitter)': 'bounded first photo page'},
            'limits': {k: getattr(s, k) for k in ('max_height', 'max_file_bytes', 'max_cache_bytes', 'max_collection', 'max_profile', 'concurrency', 'task_timeout', 'confirmation_threshold')},
            'bots': {name: {'enabled': self.bot_config(name).enabled, 'status': self.bot_status.get(name, {'connected': None})} for name in ('telegram', 'onebot')},
        }

    def prune_workspaces(self):
        from .security import controlled
        for row in self.store.db.execute("SELECT id,state,updated FROM jobs WHERE state IN ('CLEANED','CANCELLED','EXPIRED','FAILED')").fetchall():
            if row['state'] == 'FAILED' and row['updated'] > time.time() - self.config.settings.cache_ttl:
                continue
            files = self.store.files(row['id'])
            if any(f['state'] != 'CLEANED' for f in files):
                continue
            path = controlled(self.config.cache / 'jobs', self.config.cache / 'jobs' / row['id'])
            if path.exists():
                for child in path.rglob('*'):
                    controlled(path, child)
                shutil.rmtree(path)
