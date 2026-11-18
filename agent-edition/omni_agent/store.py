"""Durable service/delivery ledger; the legacy core still runs media tasks."""
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .errors import AgentError, safe_text
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
        ''')
        self._ensure_columns()

    def _ensure_columns(self):
        """兼容历史 SQLite 账本，自动补齐新增的非敏感详情与可视化字段。"""
        for table, column, definition in (
            ('jobs', 'error_message', "TEXT DEFAULT ''"),
            ('jobs', 'metadata', "TEXT DEFAULT '{}'"),
            ('jobs', 'visual_stage', "TEXT DEFAULT ''"),
            ('deliveries', 'error_message', "TEXT DEFAULT ''"),
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
        # Never retry an ambiguous upload: upstream may have accepted it before
        # the process died. Keep both media and receipt ledger for reconciliation.
        with self.transaction():
            self.db.execute("UPDATE jobs SET state='FAILED', error='RESTART_INTERRUPTED', error_message='服务重启导致下载任务中断，请重新发起下载' WHERE state IN ('QUEUED','DOWNLOADING','PROCESSING')")
            self.db.execute("UPDATE deliveries SET state='UNCERTAIN', error='RESTART_DURING_SEND', error_message='发送期间服务重启，结果不确定；请核对后重试' WHERE state='SENDING'")
            self.db.execute("UPDATE jobs SET state='FAILED', error='DELIVERY_UNCERTAIN', error_message='发送期间服务重启，请核对平台消息记录' WHERE state='SENDING'")

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
        for key in ('platform', 'title', 'author', 'media_type', 'file_count', 'command_summary', 'adapter', 'target'):
            val = metadata.get(key)
            if val not in (None, ''):
                merged[key] = safe_text(val) if isinstance(val, str) else val
        raw = json.dumps(merged, ensure_ascii=False)
        self.db.execute('UPDATE jobs SET metadata=?,updated=? WHERE id=? OR source_id=?', (raw, time.time(), job_id, job_id))

    def update_job(self, job_id, state, progress=None, error='', speed='', error_message=None, visual_stage=None):
        err_msg = error_message if error_message is not None else ('' if not error else None)
        self.db.execute(
            '''UPDATE jobs SET state=?,progress=COALESCE(?,progress),error=?,speed=?,
               error_message=COALESCE(?,error_message),visual_stage=COALESCE(?,visual_stage),updated=? WHERE id=?''',
            (state, progress, error, speed, err_msg, visual_stage, time.time(), job_id),
        )

    def files(self, job_id):
        return [dict(r) for r in self.db.execute('SELECT f.* FROM files f JOIN task_files t ON t.file_id=f.id WHERE t.task_id=? ORDER BY f.id', (job_id,))]

    def file(self, file_id, owner):
        row = self.db.execute('''SELECT f.* FROM files f JOIN task_files t ON t.file_id=f.id
            JOIN jobs j ON j.id=t.task_id WHERE f.id=? AND j.owner=? LIMIT 1''', (file_id, owner)).fetchone()
        if not row or row['state'] == 'CLEANED': raise AgentError('NOT_FOUND', '文件不存在或已清理')
        path = controlled(self.config.cache, row['path'])
        if not path.is_file() or path.stat().st_size != row['bytes']:
            raise AgentError('FILE_INVALID', '受控文件缺失或大小发生变化')
        return dict(row), path

    def create_job(self, owner, payload, cache_key, request_key):
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
        init_meta['command_summary'] = summarize_command(
            cmd_all=bool(payload.get('cmd_all')),
            cmd_zip=bool(payload.get('cmd_zip')),
            pack_zip=bool(opts.get('pack_zip')),
            download_type=str(opts.get('download_type') or ''),
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
                self.db.execute('INSERT INTO task_files SELECT ?,file_id FROM task_files WHERE task_id=?', (job_id, source['id']))
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
            for record in records:
                self.db.execute('INSERT INTO files VALUES(?,?,?,?,?,?)', record)
                self.db.execute('INSERT INTO task_files VALUES(?,?)', (job_id, record[0]))
            if self.job(job_id)['state'] != 'CANCELLED':
                self.update_job(job_id, 'READY', 100, error='', error_message='', visual_stage='complete')
            followers = self.db.execute("SELECT id FROM jobs WHERE source_id=? AND state='QUEUED'", (job_id,)).fetchall()
            for follower in followers:
                self.db.execute('INSERT INTO task_files SELECT ?,file_id FROM task_files WHERE task_id=?', (follower['id'], job_id))
                self.update_job(follower['id'], 'READY', 100, error='', error_message='', visual_stage='complete')

    def fail(self, job_id, error, state='FAILED', error_message=''):
        clean_msg = safe_text(error_message or error)
        with self.transaction():
            self.update_job(job_id, state, error=error, error_message=clean_msg, visual_stage='error')
            self.db.execute(
                "UPDATE jobs SET state=?,error=?,error_message=?,visual_stage='error',updated=? WHERE source_id=? AND state='QUEUED'",
                (state, error, clean_msg, time.time(), job_id),
            )

    def public_job(self, job_id, owner):
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
        vis = build_visualization(
            s,
            stage_from_task_state(row['state']),
            context=ctx,
            error_code=row['error'],
            error_message=row.get('error_message', ''),
        )
        return {k: row[k] for k in ('id', 'state', 'progress', 'speed', 'error', 'created', 'updated')} | {
            'error_message': row.get('error_message', '') or row['error'],
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
        if not self.files(job_id) or any(f['state'] == 'CLEANED' for f in self.files(job_id)):
            raise AgentError('NOT_FOUND', '文件已经清理')
        identifier, now = uuid.uuid4().hex, time.time()
        with self.transaction():
            self.merge_job_metadata(job_id, {'adapter': adapter, 'target': target})
            self.db.execute('INSERT INTO deliveries(id,task_id,owner,adapter,target,kind,state,request_key,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (identifier, job_id, owner, adapter, target, kind, 'QUEUED', request_key, now, now))
            self.update_job(job_id, 'SENDING', visual_stage='complete')
        return self.delivery(identifier, owner), True

    def delivery(self, identifier, owner=None):
        row = self.db.execute('SELECT * FROM deliveries WHERE id=?', (identifier,)).fetchone()
        if not row or owner is not None and row['owner'] != owner: raise AgentError('NOT_FOUND', '交付任务不存在')
        return dict(row)

    def public_delivery(self, identifier, owner=None, *, include_receipts=True):
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
        vis = build_visualization(
            self.config.settings,
            stage,
            context=ctx,
            error_code=row['error'],
            error_message=row.get('error_message', ''),
        )
        result = {
            'id': row['id'],
            'task_id': row['task_id'],
            'adapter': row['adapter'],
            'target': row['target'],
            'state': row['state'],
            'error': row['error'],
            'error_message': row.get('error_message', '') or row['error'],
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
            if not pending: self.update_job(row['task_id'], 'SENT')

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
