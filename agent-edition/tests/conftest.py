import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def service(tmp_path, monkeypatch):
    from omni_agent.service import Service
    monkeypatch.delenv('OMNI_AGENT_ADMIN_TOKEN',raising=False)
    monkeypatch.delenv('OMNI_AGENT_TOKEN',raising=False)
    return Service(tmp_path)


def ready_task(service, owner='alice', request_key=None):
    import uuid
    from PIL import Image
    row,new = service.store.create_job(owner, {'url':'https://www.youtube.com/watch?v=test'}, 'key', request_key or uuid.uuid4().hex)
    if new:
        path = service.config.cache / 'jobs' / row['id'] / 'output' / 'test.png'
        path.parent.mkdir(parents=True)
        Image.new('RGB',(4,4),'blue').save(path)
        service.store.finish(row['id'],[path])
    return service.store.job(row['id'])
