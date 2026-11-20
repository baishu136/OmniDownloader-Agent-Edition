"""Durable service/delivery ledger; the legacy core still runs media tasks."""
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .errors import AgentError, classify_error, safe_text
from .security import controlled
from .visualization import build_visualization, stage_from_task_state, summarize_command


ACTIVE = ('QUEUED', 'DOWNLOADING', 'PROCESSING')


class Store:
    def __init__(self, config, vault):
        self.config, self.vault = config, vault
        # Mutations run without awaits on the single service event loop. ASGI
        # hosts may create the app on a different thread from their lifespan.
        self.db = sqlite3.connect(config.directory / 'ledger.sqlite3', isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                cache_key TEXT NOT NULL, source_id TEXT, payload BLOB NOT NULL,
                state TEXT NOT NULL, progress REAL DEFAULT 0, speed TEXT DEFAULT '',
                error TEXT DEFAULT '', error_message TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}', visual_stage TEXT DEFAULT '',
                created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(owner,request_key));
            CREATE INDEX IF NOT EXISTS jobs_cache ON jobs(cache_key,state);
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, bytes INTEGER NOT NULL,
                digest TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'READY', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS task_files (
                task_id TEXT REFERENCES jobs(id), file_id TEXT REFERENCES files(id),
                ord INTEGER DEFAULT 0,
                PRIMARY KEY(task_id,file_id));
            CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES jobs(id), owner TEXT NOT NULL,
                adapter TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
                state TEXT NOT NULL, request_key TEXT NOT NULL, error TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(owner,request_key));
            CREATE TABLE IF NOT EXISTS receipts (
                delivery_id TEXT REFERENCES deliveries(id), file_id TEXT REFERENCES files(id),
                receipt TEXT NOT NULL, PRIMARY KEY(delivery_id,file_id));
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, fingerprint TEXT NOT NULL,
                expires REAL NOT NULL, approved INTEGER DEFAULT 0, used INTEGER DEFAULT 0,
                summary TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cleanup_log (created REAL NOT NULL, files INTEGER NOT NULL, bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS visual_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                delivery_id TEXT DEFAULT '',
                owner TEXT NOT NULL,
                stage TEXT NOT NULL,
                state TEXT NOT NULL,
                should_reply INTEGER DEFAULT 1,
                server_notified INTEGER DEFAULT 0,
                message TEXT NOT NULL,
                created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS visual_events_task_seq ON visual_events(task_id, seq);
        ''')
        self._ensure_columns()

    def _ensure_columns(self):
        """兼容历史 SQLite 账本，自动补齐新增的非敏感详情与可视化字段。"""
        for table, column, definition in (
            ('jobs', 'error_message', "TEXT DEFAULT ''"),
            ('jobs', 'metadata', "TEXT DEFAULT '{}'"),
            ('jobs', 'visual_stage', "TEXT DEFAULT ''"),
            ('deliveries', 'error_message', "TEXT DEFAULT ''"),
            ('task_files', 'ord', 'INTEGER DEFAULT 0'),
        ):
            existing = {r['name'] for r in self.db.execute(f'PRAGMA table_info({table})').fetchall()}
            if column not in existing:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def recover(self):
        # Queued jobs were never started, so the dispatcher can safely run them
        # after restart. In-flight downloads cannot be resumed by the legacy
        # core; they end with an explicit reason instead of a false success.
        # Ambiguous uploads are never retried automatically.
        with self.transaction():
            self.db.execute("UPDATE jobs SET state='FAILED', error='RESTART_INTERRUPTED', error_message='服务重启导致下载任务中断，请重新发起下载' WHERE state IN ('DOWNLOADING','PROCESSING')")
            self.db.execute("UPDATE deliveries SET state='UNCERTAIN', error='RESTART_DURING_SEND', error_message='发送期间服务重启，结果不确定；请核对后重试' WHERE state='SENDING'")
            self.db.execute("UPDATE jobs SET state='FAILED', error='DELIVERY_UNCERTAIN', error_message='发送期间服务重启，请核对平台消息记录' WHERE state='SENDING'")
        self.reconcile_ready_files()

    def reconcile_ready_files(self):
        """READY jobs whose managed files vanished must not look deliverable."""
        for row in self.db.execute("SELECT * FROM jobs WHERE state='READY'").fetchall():
            files = self.files(row['id'])
            if not files:
                continue
            broken = False
            for record in files:
                if record['state'] == 'CLEANED':
                    continue
                try:
                    path = controlled(self.config.cache, record['path'])
                except AgentError:
                    broken = True
                    break
                if not path.is_file() or path.stat().st_size != record['bytes']:
                    broken = True
                    break
            if broken:
                self.fail(row['id'], 'FILE_MISSING', 'FAILED', error_message='缓存文件缺失或已变化，请重新下载')

    def job(self, job_id, owner=None):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or owner is not None and row['owner'] != owner:
            raise AgentError('NOT_FOUND', '任务不存在或不属于当前身份')
        return dict(row)

    def payload(self, row):
        return json.loads(self.vault.protect(row['payload'], True))

    def _parsed_meta(self, row) -> dict:
        try:
            meta = json.loads(row.get('metadata') or '{}')
            return meta if isinstance(meta, dict) else {}
        except ValueError:
            return {}

    def merge_job_metadata(self, job_id, metadata: dict):
        """合并并写入非敏感媒体元数据（平台、标题、作者、指令摘要），同步更新派生任务。"""
        if not isinstance(metadata, dict):
            return
        row = self.job(job_id)
        merged = self._parsed_meta(row)
        for key in ('platform', 'title', 'author', 'media_type', 'file_count', 'command_summary', 'adapter', 'target', 'notify_adapter', 'notify_target', 'notify_message_id'):
            val = metadata.get(key)
            if val not in (None, ''):
                merged[key] = safe_text(val) if isinstance(val, str) else val
        raw = json.dumps(merged, ensure_ascii=False)
        self.db.execute('UPDATE jobs SET metadata=?,updated=? WHERE id=? OR source_id=?', (raw, time.time(), job_id, job_id))

    def record_visual_event(
        self,
        job_id: str,
        stage: str,
        state: str,
        *,
        delivery_id: str = '',
        error_code: str = '',
        error_message: str = '',
        once_per_stage: bool = True,
        extra_context: dict | None = None,
    ) -> dict | None:
        """持久化记录关键可视化里程碑事件，确保极速下载或缓存复用时中间状态永不丢失。"""
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
        if once_per_stage and stage != 'error':
            dup = self.db.execute(
                'SELECT 1 FROM visual_events WHERE task_id=? AND delivery_id=? AND stage=? LIMIT 1',
                (job_id, delivery_id, stage),
            ).fetchone()
            if dup:
                return None

        files = self.files(job_id)
        meta = self._parsed_meta(row)
        if extra_context:
            meta.update({k: v for k, v in extra_context.items() if v not in (None, '')})
        s = self.config.settings
        default_target = s.download_dir or '服务本地输出'
        ctx = dict(
            meta,
            task_id=job_id,
            progress=row['progress'],
            speed=row['speed'],
            file_count=len(files) or meta.get('file_count') or 1,
            target=meta.get('target') or default_target,
        )
        vis = build_visualization(
            s,
            stage,
            context=ctx,
            error_code=error_code or row.get('error', ''),
            error_message=error_message or row.get('error_message', ''),
        )
        if stage == 'error' and once_per_stage:
            dup_err = self.db.execute(
                "SELECT 1 FROM visual_events WHERE task_id=? AND delivery_id=? AND stage='error' AND message=? LIMIT 1",
                (job_id, delivery_id, vis['message']),
            ).fetchone()
            if dup_err:
                return None

        now = time.time()
        cur = self.db.execute(
            '''INSERT INTO visual_events(task_id, delivery_id, owner, stage, state, should_reply, server_notified, message, created)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            (
                job_id,
                delivery_id,
                row['owner'],
                stage,
                state,
                1 if vis['should_reply'] else 0,
                0,
                vis['message'],
                now,
            ),
        )
        return {
            'seq': int(cur.lastrowid),
            'task_id': job_id,
            'delivery_id': delivery_id,
            'stage': stage,
            'state': state,
            'should_reply': bool(vis['should_reply']),
            'server_notified': False,
            'message': vis['message'],
            'created': now,
        }

    def mark_visual_event_notified(self, seq: int) -> None:
        if seq:
            self.db.execute('UPDATE visual_events SET server_notified=1 WHERE seq=?', (int(seq),))

    def visual_events_for(self, job_id: str, owner: str, *, after_seq: int | None = None) -> list[dict]:
        cursor = int(after_seq or 0)
        rows = self.db.execute(
            '''SELECT seq, task_id, delivery_id, stage, state, should_reply, server_notified, message, created
               FROM visual_events WHERE task_id=? AND owner=? AND seq>? ORDER BY seq ASC''',
            (job_id, owner, cursor),
        ).fetchall()
        return [
            {
                'seq': r['seq'],
                'task_id': r['task_id'],
                'delivery_id': r['delivery_id'],
                'stage': r['stage'],
                'state': r['state'],
                'should_reply': bool(r['should_reply']),
                'server_notified': bool(r['server_notified']),
                'message': r['message'],
                'created': r['created'],
            }
            for r in rows
        ]

    def latest_visual_seq(self, job_id: str, owner: str) -> int:
        row = self.db.execute(
            'SELECT COALESCE(MAX(seq), 0) m FROM visual_events WHERE task_id=? AND owner=?',
            (job_id, owner),
        ).fetchone()
        return int(row['m']) if row else 0

    def update_job(self, job_id, state, progress=None, error='', speed='', error_message=None, visual_stage=None):
        err_msg = error_message if error_message is not None else ('' if not error else None)
        self.db.execute(
            '''UPDATE jobs SET state=?,progress=COALESCE(?,progress),error=?,speed=?,
               error_message=COALESCE(?,error_message),visual_stage=COALESCE(?,visual_stage),updated=? WHERE id=?''',
            (state, progress, error, speed, err_msg, visual_stage, time.time(), job_id),
        )

    def files(self, job_id):
        return [dict(r) for r in self.db.execute(
            'SELECT f.* FROM files f JOIN task_files t ON t.file_id=f.id WHERE t.task_id=? ORDER BY t.ord ASC, f.created ASC, f.id ASC',
            (job_id,),
        )]

    def file(self, file_id, owner):
        row = self.db.execute('''SELECT f.* FROM files f JOIN task_files t ON t.file_id=f.id
            JOIN jobs j ON j.id=t.task_id WHERE f.id=? AND j.owner=? LIMIT 1''', (file_id, owner)).fetchone()
        if not row or row['state'] == 'CLEANED': raise AgentError('NOT_FOUND', '文件不存在或已清理')
        path = controlled(self.config.cache, row['path'])
        if not path.is_file() or path.stat().st_size != row['bytes']:
            raise AgentError('FILE_INVALID', '受控文件缺失或大小发生变化')
        return dict(row), path

    def create_job(self, owner, payload, cache_key, request_key, *, notify_meta: dict | None = None):
        existing = self.db.execute('SELECT * FROM jobs WHERE owner=? AND request_key=?', (owner, request_key)).fetchone()
        if existing:
            if existing['cache_key'] != cache_key: raise AgentError('IDEMPOTENCY_CONFLICT', '请求键已用于其他参数')
            return dict(existing), False
        source = None
        if self.config.settings.reuse_cache:
            candidates = self.db.execute("SELECT * FROM jobs WHERE cache_key=? AND state IN ('QUEUED','DOWNLOADING','PROCESSING','READY','SENDING','SENT') ORDER BY created DESC", (cache_key,)).fetchall()
            for candidate in candidates:
                if candidate['source_id']: continue
                if candidate['state'] in ACTIVE:
                    source = candidate
                    break
                files = self.files(candidate['id'])
                if files and all(f['state'] != 'CLEANED' and controlled(self.config.cache, f['path']).is_file() for f in files):
                    source = candidate
                    break
        job_id, now = uuid.uuid4().hex, time.time()
        state = ('QUEUED' if source['state'] in ACTIVE else 'READY') if source else 'QUEUED'
        opts = payload.get('options') or {}
        init_meta = self._parsed_meta(dict(source)) if source else {}
        init_meta.pop('notify_message_id', None)
        n_meta = dict(notify_meta or {})
        n_adapter = n_meta.get('notify_adapter') or payload.get('notify_adapter')
        n_target = n_meta.get('notify_target') or payload.get('notify_target')
        if n_adapter and n_target:
            init_meta['notify_adapter'] = n_adapter
            init_meta['notify_target'] = n_target
            init_meta['adapter'] = 'QQ / OneBot' if n_adapter == 'onebot' else 'Telegram'
            init_meta['target'] = n_target
        else:
            init_meta.pop('notify_adapter', None)
            init_meta.pop('notify_target', None)
        init_meta['command_summary'] = summarize_command(
            cmd_all=bool(payload.get('cmd_all')),
            cmd_zip=bool(payload.get('cmd_zip')),
            cmd_merge=bool(payload.get('cmd_merge')),
            pack_zip=bool(opts.get('pack_zip')),
            download_type=str(opts.get('download_type') or ''),
            image_format=str(opts.get('image_format') or ''),
        )
        visual_stage = 'complete' if state == 'READY' else 'receive'
        with self.transaction():
            self.db.execute(
                'INSERT INTO jobs(id,owner,request_key,cache_key,source_id,payload,state,metadata,visual_stage,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, owner, request_key, cache_key, source['id'] if source else None,
                 self.vault.protect(json.dumps(payload).encode()), state,
                 json.dumps(init_meta, ensure_ascii=False), visual_stage, now, now),
            )
            if source and state == 'READY':
                self.db.execute('INSERT INTO task_files(task_id,file_id,ord) SELECT ?,file_id,ord FROM task_files WHERE task_id=?', (job_id, source['id']))
            self.record_visual_event(job_id, 'receive', state)
            if state == 'READY':
                self.record_visual_event(job_id, 'complete', 'READY')
        return self.job(job_id), not bool(source)

    def finish(self, job_id, paths, metadata=None):
        records = []
        for name in paths:
            path = controlled(self.config.cache / 'jobs' / job_id, name)
            if not path.is_file() or not 0 < path.stat().st_size <= self.config.settings.max_file_bytes:
                raise AgentError('FILE_INVALID', '输出文件无效或超过单文件上限')
            with path.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            records.append((uuid.uuid4().hex, str(path), path.stat().st_size, digest, 'READY', time.time()))
        if not records: raise AgentError('FILE_INVALID', '核心未返回文件')
        with self.transaction():
            if metadata:
                self.merge_job_metadata(job_id, dict(metadata, file_count=len(records)))
            for idx, record in enumerate(records):
                self.db.execute('INSERT INTO files VALUES(?,?,?,?,?,?)', record)
                self.db.execute('INSERT INTO task_files(task_id,file_id,ord) VALUES(?,?,?)', (job_id, record[0], idx))
            if self.job(job_id)['state'] != 'CANCELLED':
                self.update_job(job_id, 'READY', 100, error='', error_message='', visual_stage='complete')
                self.record_visual_event(job_id, 'complete', 'READY')
            followers = self.db.execute("SELECT id FROM jobs WHERE source_id=? AND state='QUEUED'", (job_id,)).fetchall()
            for follower in followers:
                self.db.execute('INSERT INTO task_files(task_id,file_id,ord) SELECT ?,file_id,ord FROM task_files WHERE task_id=?', (follower['id'], job_id))
                self.update_job(follower['id'], 'READY', 100, error='', error_message='', visual_stage='complete')
                self.record_visual_event(follower['id'], 'complete', 'READY')

    def fail(self, job_id, error, state='FAILED', error_message=''):
        clean_msg = safe_text(error_message or error)
        with self.transaction():
            self.update_job(job_id, state, error=error, error_message=clean_msg, visual_stage='error')
            self.record_visual_event(job_id, 'error', state, error_code=error, error_message=clean_msg)
            followers = self.db.execute("SELECT id FROM jobs WHERE source_id=? AND state='QUEUED'", (job_id,)).fetchall()
            self.db.execute(
                "UPDATE jobs SET state=?,error=?,error_message=?,visual_stage='error',updated=? WHERE source_id=? AND state='QUEUED'",
                (state, error, clean_msg, time.time(), job_id),
            )
            for follower in followers:
                self.record_visual_event(follower['id'], 'error', state, error_code=error, error_message=clean_msg)

    def public_job(self, job_id, owner, *, after_seq: int | None = None):
        row = self.job(job_id, owner)
        files = self.files(job_id)
        meta = self._parsed_meta(row)
        s = self.config.settings
        default_target = s.download_dir or '服务本地输出'
        ctx = dict(
            meta,
            task_id=row['id'],
            progress=row['progress'],
            speed=row['speed'],
            file_count=len(files) or meta.get('file_count') or 1,
            target=meta.get('target') or default_target,
        )
        stage = stage_from_task_state(row['state'])
        pending = self.visual_events_for(row['id'], row['owner'], after_seq=after_seq)
        latest_seq = self.latest_visual_seq(row['id'], row['owner'])
        stage_notified = any(ev['stage'] == stage and ev['server_notified'] for ev in pending)
        vis = build_visualization(
            s,
            stage,
            context=ctx,
            error_code=row['error'],
            error_message=row.get('error_message', ''),
            pending_events=pending,
            latest_seq=latest_seq,
            server_notified=stage_notified,
        )
        error_spec = classify_error(row['error'], row.get('error_message', '')) if row['error'] else {}
        next_action = {
            'READY': 'deliver_or_save',
            'SENDING': 'wait',
            'SENT': 'none',
            'QUEUED': 'wait',
            'DOWNLOADING': 'wait',
            'PROCESSING': 'wait',
        }.get(row['state'], 'retry' if error_spec.get('retryable') else 'ask_user')
        return {k: row[k] for k in ('id', 'state', 'progress', 'speed', 'error', 'created', 'updated')} | {
            'error_message': row.get('error_message', '') or row['error'],
            'stage': error_spec.get('stage', ''),
            'retryable': error_spec.get('retryable', False),
            'suggested_action': error_spec.get('suggested_action', ''),
            'next_action': next_action,
            'metadata': meta,
            'file_count': len(files),
            'files': [{'file_id': f['id'], 'bytes': f['bytes'], 'state': f['state']} for f in files],
            'visualization': vis,
        }

    def create_delivery(self, job_id, owner, adapter, target, kind, request_key):
        job = self.job(job_id, owner)
        previous = self.db.execute('SELECT * FROM deliveries WHERE owner=? AND request_key=?', (owner, request_key)).fetchone()
        if previous:
            if (previous['task_id'], previous['adapter'], previous['target'], previous['kind']) != (job_id, adapter, target, kind):
                raise AgentError('IDEMPOTENCY_CONFLICT', '交付请求键已用于其他参数')
            return dict(previous), False
        if job['state'] not in ('READY', 'SENT', 'SENDING'): raise AgentError('NOT_READY', '下载任务尚未就绪')
        records = self.files(job_id)
        if not records or any(f['state'] == 'CLEANED' for f in records):
            raise AgentError('NOT_FOUND', '文件已经清理')
        # Preflight before a delivery row exists: a missing or truncated file
        # must fail here with a precise reason instead of during sending.
        for record in records:
            path = controlled(self.config.cache, record['path'])
            if not path.is_file() or path.stat().st_size != record['bytes']:
                raise AgentError('FILE_MISSING', '缓存文件缺失或大小已变化，请重新下载后再发送')
        identifier, now = uuid.uuid4().hex, time.time()
        with self.transaction():
            self.merge_job_metadata(job_id, {'adapter': adapter, 'target': target})
            self.db.execute('INSERT INTO deliveries(id,task_id,owner,adapter,target,kind,state,request_key,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (identifier, job_id, owner, adapter, target, kind, 'QUEUED', request_key, now, now))
            self.update_job(job_id, 'SENDING', visual_stage='complete')
            self.record_visual_event(
                job_id,
                'complete',
                'SENDING',
                delivery_id=identifier,
                extra_context={'adapter': 'QQ / OneBot' if adapter == 'onebot' else 'Telegram', 'target': target},
            )
        return self.delivery(identifier, owner), True

    def delivery(self, identifier, owner=None):
        row = self.db.execute('SELECT * FROM deliveries WHERE id=?', (identifier,)).fetchone()
        if not row or owner is not None and row['owner'] != owner: raise AgentError('NOT_FOUND', '交付任务不存在')
        return dict(row)

    def public_delivery(self, identifier, owner=None, *, include_receipts=True, after_seq: int | None = None):
        row = self.delivery(identifier, owner)
        job = self.job(row['task_id'])
        files = self.files(row['task_id'])
        meta = self._parsed_meta(job)
        ctx = dict(
            meta,
            task_id=row['task_id'],
            file_count=len(files) or meta.get('file_count') or 1,
            adapter='QQ / OneBot' if row['adapter'] == 'onebot' else 'Telegram',
            target=row['target'],
        )
        if row['state'] in ('FAILED', 'UNCERTAIN'):
            stage = 'error'
        elif row['state'] == 'SENT':
            stage = 'sent'
        else:
            stage = 'complete'
        pending = self.visual_events_for(row['task_id'], row['owner'], after_seq=after_seq)
        latest_seq = self.latest_visual_seq(row['task_id'], row['owner'])
        stage_notified = any(
            ev['stage'] == stage and ev['server_notified'] and (ev['delivery_id'] in ('', identifier))
            for ev in pending
        )
        vis = build_visualization(
            self.config.settings,
            stage,
            context=ctx,
            error_code=row['error'],
            error_message=row.get('error_message', ''),
            pending_events=pending,
            latest_seq=latest_seq,
            server_notified=stage_notified,
        )
        error_spec = classify_error(row['error'], row.get('error_message', '')) if row['error'] else {}
        sent_ids = {
            item['file_id']
            for item in self.db.execute('SELECT file_id FROM receipts WHERE delivery_id=?', (identifier,))
        }
        result = {
            'id': row['id'],
            'task_id': row['task_id'],
            'adapter': row['adapter'],
            'target': row['target'],
            'state': row['state'],
            'error': row['error'],
            'error_message': row.get('error_message', '') or row['error'],
            'stage': error_spec.get('stage', ''),
            'retryable': error_spec.get('retryable', False),
            'suggested_action': error_spec.get('suggested_action', ''),
            'files': [
                {'file_id': f['id'], 'bytes': f['bytes'], 'state': f['state'], 'sent': f['id'] in sent_ids}
                for f in files
            ],
            'visualization': vis,
        }
        if include_receipts:
            result['receipts'] = [
                json.loads(r['receipt'])
                for r in self.db.execute('SELECT receipt FROM receipts WHERE delivery_id=?', (identifier,))
            ]
        return result

    def receipt(self, delivery_id, file_id, receipt):
        # Called only after our authenticated adapter obtained a synchronous
        # platform acknowledgement; never accepts agent-supplied success flags.
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?)', (delivery_id, file_id, json.dumps(receipt)))

    def sent(self, identifier):
        row = self.delivery(identifier)
        with self.transaction():
            total = len(self.files(row['task_id']))
            count = self.db.execute('SELECT COUNT(*) FROM receipts WHERE delivery_id=?', (identifier,)).fetchone()[0]
            if not total or count != total: raise AgentError('RECEIPT_REQUIRED', '平台回执不完整')
            self.db.execute("UPDATE deliveries SET state='SENT',error='',updated=? WHERE id=?", (time.time(), identifier))
            pending = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE task_id=? AND state!='SENT'", (row['task_id'],)).fetchone()[0]
            if not pending:
                self.update_job(row['task_id'], 'SENT')
            self.record_visual_event(row['task_id'], 'sent', 'SENT', delivery_id=identifier)

    def cleanup_preview(self, *, expired=False):
        """列出下一次清理会删除的文件，不修改数据库或磁盘。"""
        now = time.time()
        expiring: set[str] = set()
        if expired:
            expiring = {
                row['id'] for row in self.db.execute(
                    """SELECT id FROM jobs WHERE updated<? AND state IN ('READY','FAILED','CANCELLED')
                       AND id NOT IN (SELECT task_id FROM deliveries WHERE state IN ('QUEUED','SENDING','UNCERTAIN'))""",
                    (now - self.config.settings.cache_ttl,),
                )
            }
        items = []
        for file in self.db.execute("SELECT * FROM files WHERE state!='CLEANED'").fetchall():
            tasks = [r[0] for r in self.db.execute('SELECT task_id FROM task_files WHERE file_id=?', (file['id'],))]
            placeholders = ','.join('?' for _ in tasks)
            hold = self.db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE id IN ({placeholders}) AND state NOT IN ('SENT','CLEANED','EXPIRED','CANCELLED')",
                tasks,
            ).fetchone()[0]
            if hold:
                continue
            pending = self.db.execute(
                f"SELECT COUNT(*) FROM deliveries WHERE task_id IN ({placeholders}) AND state!='SENT' AND NOT (state='FAILED' AND updated<?)",
                (*tasks, now - self.config.settings.cache_ttl if expired else 0),
            ).fetchone()[0]
            if pending:
                continue
            items.append({'file_id': file['id'], 'bytes': file['bytes'], 'state': file['state']})
        return {'files': len(items), 'bytes': sum(item['bytes'] for item in items), 'items': items[:200]}

    def cleanup(self, *, expired=False):
        removed, bytes_removed = 0, 0
        now = time.time()
        with self.transaction():
            if expired:
                self.db.execute("""UPDATE jobs SET state='EXPIRED' WHERE updated<? AND state IN ('READY','FAILED','CANCELLED')
                    AND id NOT IN (SELECT task_id FROM deliveries WHERE state IN ('QUEUED','SENDING','UNCERTAIN'))""",
                    (now - self.config.settings.cache_ttl,))
            for file in self.db.execute("SELECT * FROM files WHERE state!='CLEANED'").fetchall():
                tasks = [r[0] for r in self.db.execute('SELECT task_id FROM task_files WHERE file_id=?', (file['id'],))]
                placeholders = ','.join('?' for _ in tasks)
                hold = self.db.execute(f"SELECT COUNT(*) FROM jobs WHERE id IN ({placeholders}) AND state NOT IN ('SENT','CLEANED','EXPIRED','CANCELLED')", tasks).fetchone()[0]
                pending = self.db.execute(f"SELECT COUNT(*) FROM deliveries WHERE task_id IN ({placeholders}) AND state!='SENT' AND NOT (state='FAILED' AND updated<?)", (*tasks, now - self.config.settings.cache_ttl if expired else 0)).fetchone()[0]
                if hold or pending: continue
                path = controlled(self.config.cache, file['path'])
                if path.exists():
                    # A changed file is not our validated artifact; refuse to delete it.
                    with path.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    if digest != file['digest']: raise AgentError('FILE_CHANGED', '文件内容变化，已停止清理')
                    path.unlink()
                    bytes_removed += file['bytes']
                self.db.execute("UPDATE files SET state='CLEANED' WHERE id=?", (file['id'],))
                removed += 1
            self.db.execute("""UPDATE jobs SET state='CLEANED' WHERE state IN ('SENT','EXPIRED')
                AND id IN (SELECT task_id FROM task_files)
                AND id NOT IN (SELECT t.task_id FROM task_files t JOIN files f ON f.id=t.file_id WHERE f.state!='CLEANED')""")
            self.db.execute('INSERT INTO cleanup_log VALUES(?,?,?)', (now, removed, bytes_removed))
            self.db.execute('DELETE FROM cleanup_log WHERE rowid NOT IN (SELECT rowid FROM cleanup_log ORDER BY created DESC LIMIT 100)')
        return {'files_removed': removed, 'bytes_removed': bytes_removed}

    def usage(self):
        return sum(p.stat().st_size for p in self.config.cache.rglob('*') if p.is_file() and not p.is_symlink())
