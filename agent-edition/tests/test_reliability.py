"""Download retry, restart recovery and delivery preflight contracts."""
import asyncio
from pathlib import Path

import pytest

from conftest import ready_task
from omni_agent import service as service_module
from omni_agent.errors import AgentError
from omni_agent.service import DownloadOptions, Service


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


def test_cleanup_preview_matches_actual_cleanup(service):
    job = ready_task(service)
    delivery, _ = service.store.create_delivery(job['id'], 'alice', 'telegram', '123', 'document', 'cleanup-1')
    for file in service.store.files(job['id']):
        service.store.receipt(delivery['id'], file['id'], {'message_id': 1, 'platform': 'telegram'})
    service.store.sent(delivery['id'])
    preview = service.store.cleanup_preview()
    assert preview['files'] == 1 and preview['bytes'] > 0
    removed = service.store.cleanup()
    assert removed['files_removed'] == 1
    assert service.store.cleanup_preview()['files'] == 0


async def test_disk_space_preflight_blocks_before_download(service, no_wait, monkeypatch):
    import collections
    configure(service)
    service.runner = FlakyRunner(service.config, service.store, failures=0)
    row = await queue_job(service)
    usage = collections.namedtuple('usage', 'total used free')
    monkeypatch.setattr(service_module.shutil, 'disk_usage', lambda _path: usage(10 ** 12, 10 ** 12, 1024))
    await service.run_job(row)
    job = service.store.public_job(row['id'], 'alice')
    assert service.runner.calls == 0
    assert job['state'] == 'FAILED' and job['error'] == 'DISK_FULL'


def test_settings_export_import_and_backup(service):
    service.update_settings({'max_height': 2161})
    exported = service.export_settings()
    assert exported['settings']['max_height'] == 2161
    payload = dict(exported['settings'], max_height=1440, default_height=1080)
    service.import_settings({'settings': payload})
    assert service.get_settings()['max_height'] == 1440
    assert service.config.path.with_suffix('.json.bak').is_file()
    before = service.get_settings()
    with pytest.raises(AgentError) as error:
        service.import_settings({'settings': dict(payload, unknown_field=1)})
    assert error.value.code == 'INVALID_PARAMETER'
    assert service.get_settings() == before


async def test_per_owner_active_limit_is_enforced(service):
    service.update_settings({'max_active_per_owner': 1})
    await service.download('alice', 'https://www.youtube.com/watch?v=quota-a', DownloadOptions())
    with pytest.raises(AgentError) as error:
        await service.download('alice', 'https://www.youtube.com/watch?v=quota-b', DownloadOptions())
    assert error.value.code == 'LIMIT_EXCEEDED'
    await service.download('bob', 'https://www.youtube.com/watch?v=quota-c', DownloadOptions())


def test_public_job_exposes_next_action(service):
    ready = ready_task(service)
    assert service.store.public_job(ready['id'], 'alice')['next_action'] == 'deliver_or_save'
    failed, _ = service.store.create_job('alice', {}, 'next-action', 'next-action-1')
    service.store.fail(failed['id'], 'TIMEOUT', error_message='超时')
    assert service.store.public_job(failed['id'], 'alice')['next_action'] == 'retry'


def test_onboarding_status_and_completion(service):
    assert service.onboarding_status()['first_run'] is True
    assert service.onboarding_status()['completed'] is False
    assert service.complete_onboarding()['completed'] is True
    reopened = Service(service.config.directory)
    try:
        status = reopened.onboarding_status()
        assert status['first_run'] is False and status['completed'] is True
        assert status['suggested_steps'] == []
    finally:
        reopened.store.db.close()


def test_admin_task_list_and_retry_guard(service):
    retryable, _ = service.store.create_job('alice', {}, 'admin-a', 'admin-a-1')
    service.store.fail(retryable['id'], 'TIMEOUT', error_message='超时')
    permanent, _ = service.store.create_job('alice', {}, 'admin-b', 'admin-b-1')
    service.store.fail(permanent['id'], 'COOKIE_EXPIRED', error_message='登录失效')
    tasks = service.list_admin_tasks()['tasks']
    assert {task['id'] for task in tasks} == {retryable['id'], permanent['id']}
    assert all('next_action' in task for task in tasks)
    retried = service.retry_job(retryable['id'])
    assert retried['state'] == 'QUEUED' and retried['error'] == ''
    with pytest.raises(AgentError) as error:
        service.retry_job(permanent['id'])
    assert error.value.code == 'INVALID_STATE'


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
