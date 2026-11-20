"""Actual output metadata returned with finished jobs."""
from PIL import Image

from omni_agent.worker import describe_outputs


def test_describe_outputs_reports_actual_image_dimensions(tmp_path):
    path = tmp_path / 'cover.png'
    Image.new('RGB', (7, 5), 'green').save(path)
    described = describe_outputs([str(path)])
    assert described[0]['type'] == 'image'
    assert described[0]['width'] == 7 and described[0]['height'] == 5
    assert described[0]['ext'] == 'png' and described[0]['bytes'] > 0
    assert 'path' not in described[0]


def test_job_metadata_persists_output_descriptions(service):
    job, _ = service.store.create_job('alice', {}, 'outputs-cache', 'outputs-1')
    service.store.merge_job_metadata(job['id'], {'outputs': [{'type': 'image', 'width': 7, 'height': 5, 'bytes': 10, 'ext': 'png'}]})
    meta = service.store.public_job(job['id'], 'alice')['metadata']
    assert meta['outputs'][0]['width'] == 7
