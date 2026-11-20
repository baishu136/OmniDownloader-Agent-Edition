"""Download retry, restart recovery and delivery preflight contracts."""
import asyncio
from pathlib import Path

import pytest

from conftest import ready_task
from omni_agent import service as service_module
from omni_agent.errors import AgentError
from omni_agent.service import DownloadOptions


class FlakyRunner:
    """Fails a fixed number of times, then returns one real file."""

    instances = []

    def __init__(self, config, store, *, failures=0, code='TIMEOUT'):
        self.config, self.store = config, store
        self.failures, self.code = failures, code
        self.calls = 0
        FlakyRunner.instances.append(self)

    async def call(self, payload, identifier=None, progress=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise AgentError(self.code, '下载连接超时')
        from PIL import Image
        path = self.config.cache / 'jobs' / identifier / 'output' / 'out.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (4, 4), 'red').save(path)
        return {'paths': [str(path)], 'metadata': {'platform': 'YouTube', 'title': 'retry-test'}}

    async def close(self):
        pass


@pytest.fixture
def no_wait(monkeypatch):
    async def _pause(_seconds):
        return None
    monkeypatch.setattr(service_module, '_retry_pause', _pause)


def configure(service, *, retries=2):
    service.config.settings = service.config.settings.model_copy(
        update={'task_retries': retries, 'retry_base_seconds': 1, 'concurrency': 1})


async def queue_job(service):
    await service.download('alice', 'https://www.youtube.com/watch?v=retry', DownloadOptions())
    row = service.store.db.execute(
        "SELECT * FROM jobs WHERE owner='alice' ORDER BY created DESC LIMIT 1").fetchone()
    return dict(row)


async def test_retryable_error_is_retried_then_succeeds(service, no_wait):
    configure(service)
    service.runner = FlakyRunner(service.config, service.store, failures=1)
    row = await queue_job(service)
    await service.run_job(row)
    job = service.store.public_job(row['id'], 'alice')
    assert service.runner.calls == 2
    assert job['state'] == 'READY'
    assert job['file_count'] == 1
    assert job['retryable'] is False


async def test_non_retryable_error_is_not_retried(service, no_wait):
    configure(service)
    service.runner = FlakyRunner(service.config, service.store, failures=5, code='COOKIE_EXPIRED')
    row = await queue_job(service)
    await service.run_job(row)
    job = service.store.public_job(row['id'], 'alice')
    assert service.runner.calls == 1
    assert job['state'] == 'FAILED' and job['error'] == 'COOKIE_EXPIRED'
    assert job['retryable'] is False and job['stage'] == 'AUTHENTICATING'


async def test_retry_is_bounded_and_reports_structured_failure(service, no_wait):
    configure(service, retries=2)
    service.runner = FlakyRunner(service.config, service.store, failures=99)
    row = await queue_job(service)
    await service.run_job(row)
    job = service.store.public_job(row['id'], 'alice')
    assert service.runner.calls == 3
    assert job['state'] == 'FAILED' and job['error'] == 'TIMEOUT'
    assert job['retryable'] is True and job['stage'] == 'DOWNLOADING'
    assert job['suggested_action']


async def test_retry_does_not_duplicate_files(service, no_wait):
    configure(service, retries=2)
    service.runner = FlakyRunner(service.config, service.store, failures=2)
    row = await queue_job(service)
    await service.run_job(row)
    job = service.store.public_job(row['id'], 'alice')
    assert service.runner.calls == 3 and job['file_count'] == 1


async def test_restart_requeues_queued_jobs_but_fails_inflight(service):
    queued, _ = service.store.create_job('alice', {}, 'cache-queued', 'requeue-1')
    inflight, _ = service.store.create_job('alice', {}, 'cache-inflight', 'requeue-2')
    service.store.update_job(inflight['id'], 'DOWNLOADING')
    service.store.recover()
    assert service.store.job(queued['id'])['state'] == 'QUEUED'
    repaired = service.store.job(inflight['id'])
    assert repaired['state'] == 'FAILED' and repaired['error'] == 'RESTART_INTERRUPTED'


async def test_restart_marks_missing_ready_files(service):
    job = ready_task(service)
    Path(service.store.files(job['id'])[0]['path']).unlink()
    service.store.recover()
    repaired = service.store.job(job['id'])
    assert repaired['state'] == 'FAILED' and repaired['error'] == 'FILE_MISSING'
    assert service.store.public_job(job['id'], 'alice')['stage'] == 'STORAGE'


def test_delivery_reports_per_file_send_state(service):
    from PIL import Image
    job, _ = service.store.create_job('alice', {}, 'two-files', 'two-files-1')
    paths = []
    for index in range(2):
        path = service.config.cache / 'jobs' / job['id'] / 'output' / f'img{index}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (4, 4), 'blue').save(path)
        paths.append(str(path))
    service.store.finish(job['id'], paths)
    delivery, _ = service.store.create_delivery(job['id'], 'alice', 'telegram', '123', 'document', 'two-files-d1')
    files = service.store.files(job['id'])
    service.store.receipt(delivery['id'], files[0]['id'], {'message_id': 1, 'platform': 'telegram'})
    public = service.store.public_delivery(delivery['id'], 'alice')
    states = {item['file_id']: item['sent'] for item in public['files']}
    assert states[files[0]['id']] is True
    assert states[files[1]['id']] is False


def test_diagnostics_reports_environment_without_secrets(service):
    import json
    service.import_cookie('哔哩哔哩', 'SESSDATA=diag-secret; bili_jct=diag-jct')
    report = service.diagnose()
    ids = {check['id'] for check in report['checks']}
    assert {'data_dir', 'cache_dir', 'disk_space', 'database', 'vault', 'ffmpeg', 'proxy', 'credentials'} <= ids
    assert report['ok'] is True
    blob = json.dumps(report, ensure_ascii=False)
    assert 'diag-secret' not in blob and 'SESSDATA' not in blob


async def test_delivery_preflight_rejects_missing_file(service):
    job = ready_task(service)
    Path(service.store.files(job['id'])[0]['path']).unlink()
    with pytest.raises(AgentError) as error:
        service.store.create_delivery(job['id'], 'alice', 'telegram', '123', 'document', 'preflight-1')
    assert error.value.code == 'FILE_MISSING'
    assert service.store.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == 0
