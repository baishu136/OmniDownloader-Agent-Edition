import hashlib
import json
from pathlib import Path
import time

import pytest

from conftest import ready_task
from omni_agent.errors import AgentError
from omni_agent.service import Service


def delivery(service,job,key='d1'):
    row,_=service.store.create_delivery(job['id'],job['owner'],'telegram','123','document',key)
    return row


def confirm(service,row):
    for file in service.store.files(row['task_id']):
        service.store.receipt(row['id'],file['id'],{'message_id':100,'platform':'telegram'})
    service.store.sent(row['id'])


def test_ready_is_not_sent_and_not_deleted(service):
    job=ready_task(service)
    path=Path(service.store.files(job['id'])[0]['path'])
    assert service.store.cleanup()['files_removed']==0
    assert path.exists()


def test_sent_cleanup_and_duplicate_receipt(service):
    job=ready_task(service)
    row=delivery(service,job)
    confirm(service,row)
    confirm(service,row)
    assert service.store.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]==1
    assert service.store.cleanup()['files_removed']==1
    assert service.store.cleanup()['files_removed']==0
    assert service.store.job(job['id'])['state']=='CLEANED'


def test_failed_send_keeps_file(service):
    job=ready_task(service)
    row=delivery(service,job)
    service.store.db.execute("UPDATE deliveries SET state='FAILED' WHERE id=?",(row['id'],))
    service.store.update_job(job['id'],'FAILED',error='BOT_REJECTED')
    assert service.store.cleanup()['files_removed']==0


def test_multiple_users_reuse_without_early_delete(service):
    first=ready_task(service,'alice','a')
    second=ready_task(service,'bob','b')
    assert second['source_id']==first['id']
    assert service.store.files(first['id'])[0]['id']==service.store.files(second['id'])[0]['id']
    confirm(service,delivery(service,first,'a'))
    assert service.store.cleanup()['files_removed']==0
    confirm(service,delivery(service,second,'b'))
    assert service.store.cleanup()['files_removed']==1


def test_multiple_deliveries_hold_file(service):
    job=ready_task(service)
    d1,d2=delivery(service,job,'d1'),delivery(service,job,'d2')
    confirm(service,d1)
    assert service.store.cleanup()['files_removed']==0
    confirm(service,d2)
    assert service.store.cleanup()['files_removed']==1


def test_no_fake_sent_without_receipts(service):
    row=delivery(service,ready_task(service))
    with pytest.raises(AgentError,match='回执不完整'): service.store.sent(row['id'])
    assert service.store.delivery(row['id'])['state']=='QUEUED'


def test_restart_keeps_uncertain_file(service):
    row=delivery(service,ready_task(service))
    service.store.db.execute("UPDATE deliveries SET state='SENDING' WHERE id=?",(row['id'],))
    service.store.recover()
    assert service.store.delivery(row['id'])['state']=='UNCERTAIN'
    assert service.store.cleanup(expired=True)['files_removed']==0
    with pytest.raises(AgentError) as error:service.retry_delivery(row['id'])
    assert error.value.code=='RECONCILIATION_REQUIRED'


def test_restart_disk_persistence(service):
    job=ready_task(service)
    row=delivery(service,job)
    confirm(service,row)
    reopened=Service(service.config.directory)
    assert reopened.store.job(job['id'])['state']=='SENT'
    assert reopened.store.delivery(row['id'])['state']=='SENT'
    assert reopened.store.cleanup()['files_removed']==1
    reopened.store.db.close()


def test_active_jobs_safely_end_on_restart(service):
    row,new=service.store.create_job('alice',{},'unfinished','u1')
    service.store.update_job(row['id'],'DOWNLOADING')
    service.store.recover()
    assert service.store.job(row['id'])['state']=='FAILED'


def test_deletion_cannot_escape_cache(service,tmp_path):
    job=ready_task(service)
    row=delivery(service,job)
    confirm(service,row)
    outside=tmp_path/'outside.txt'
    outside.write_text('unrelated')
    service.store.db.execute('UPDATE files SET path=?,digest=?',(str(outside),hashlib.sha256(outside.read_bytes()).hexdigest()))
    with pytest.raises(AgentError) as error:service.store.cleanup()
    assert error.value.code=='PATH_DENIED'
    assert outside.read_text()=='unrelated'


def test_changed_file_is_not_deleted(service):
    job=ready_task(service)
    confirm(service,delivery(service,job))
    Path(service.store.files(job['id'])[0]['path']).write_text('replaced')
    with pytest.raises(AgentError) as error:service.store.cleanup()
    assert error.value.code=='FILE_CHANGED'


def test_file_ownership(service):
    job=ready_task(service)
    file=service.store.files(job['id'])[0]
    with pytest.raises(AgentError):service.file_reference('bob',file['id'])
    with pytest.raises(AgentError):service.store.job(job['id'],'bob')
    reference=service.file_reference('alice',file['id'])
    assert 'path' not in reference
    assert reference['uri'].startswith('omni-file://')


def test_expiry_cleans_abandoned_ready_file(service):
    job=ready_task(service)
    service.store.db.execute('UPDATE jobs SET updated=?',(time.time()-90000,))
    assert service.store.cleanup(expired=True)['files_removed']==1


def test_idempotency_key_cannot_change_request(service):
    row,new=service.store.create_job('alice',{},'hash1','same')
    again,new=service.store.create_job('alice',{},'hash1','same')
    assert not new and again['id']==row['id']
    with pytest.raises(AgentError):service.store.create_job('alice',{},'hash2','same')


def test_followers_receive_completed_files(service):
    row,_=service.store.create_job('alice',{},'active','a')
    follower,new=service.store.create_job('bob',{},'active','b')
    assert not new and follower['state']=='QUEUED'
    output=service.config.cache/'jobs'/row['id']/'out.png'
    output.parent.mkdir(parents=True)
    output.write_bytes(b'validated-core-output')
    service.store.finish(row['id'],[output])
    assert service.store.job(follower['id'])['state']=='READY'
    assert len(service.store.files(follower['id']))==1
