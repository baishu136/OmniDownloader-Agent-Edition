"""Cookie capture commit / readback / rollback contracts."""
import http.cookiejar

import pytest

from omni_agent.errors import AgentError
from omni_agent.service import Service


def cookies(service, platform):
    return {(c.domain, c.name, c.value) for c in service.sessions.jar(platform)}


def test_import_is_read_back_before_being_accepted(service):
    service.import_cookie('哔哩哔哩', 'SESSDATA=first-value; bili_jct=first-jct')
    values = {name: value for _, name, value in cookies(service, '哔哩哔哩')}
    assert values['SESSDATA'] == 'first-value'
    info = service._session_info('哔哩哔哩', include_ui=True)
    assert info['state'] == 'IMPORTED'
    assert info['validation']['state'] == 'UNVERIFIED'
    assert info['rollback_available'] is False


def test_invalid_import_does_not_overwrite_previous_cookie(service):
    service.import_cookie('哔哩哔哩', 'SESSDATA=keep-me; bili_jct=keep-jct')
    with pytest.raises(AgentError) as error:
        service.import_cookie('哔哩哔哩', 'not a cookie file')
    assert error.value.code == 'INVALID_COOKIE'
    values = {name: value for _, name, value in cookies(service, '哔哩哔哩')}
    assert values['SESSDATA'] == 'keep-me'


def test_failed_readback_restores_previous_cookie_and_enabled_state(service, monkeypatch):
    service.import_cookie('哔哩哔哩', 'SESSDATA=stable; bili_jct=stable-jct')
    service.set_cookie_enabled('哔哩哔哩', False)
    service.set_cookie_enabled('哔哩哔哩', True)
    real_jar = service.sessions.jar
    service.sessions.jar = lambda platform: http.cookiejar.MozillaCookieJar()
    try:
        with pytest.raises(AgentError) as error:
            service.import_cookie('哔哩哔哩', 'SESSDATA=broken; bili_jct=broken-jct')
    finally:
        service.sessions.jar = real_jar
    assert error.value.code == 'INVALID_COOKIE'
    values = {name: value for _, name, value in cookies(service, '哔哩哔哩')}
    assert values['SESSDATA'] == 'stable'


def test_second_import_keeps_previous_version_for_rollback(service):
    service.import_cookie('哔哩哔哩', 'SESSDATA=old; bili_jct=old-jct')
    service.import_cookie('哔哩哔哩', 'SESSDATA=new; bili_jct=new-jct')
    info = service._session_info('哔哩哔哩', include_ui=True)
    assert info['rollback_available'] is True
    restored = service.restore_cookie('哔哩哔哩')
    assert restored['state'] == 'IMPORTED'
    values = {name: value for _, name, value in cookies(service, '哔哩哔哩')}
    assert values['SESSDATA'] == 'old'


def test_restore_without_backup_is_refused(service):
    service.import_cookie('哔哩哔哩', 'SESSDATA=only; bili_jct=only-jct')
    with pytest.raises(AgentError) as error:
        service.restore_cookie('哔哩哔哩')
    assert error.value.code == 'NOT_FOUND'


async def test_rejected_capture_restores_previous_valid_cookie(service, monkeypatch):
    import json
    service.import_cookie('哔哩哔哩', 'SESSDATA=good; bili_jct=good')
    service.import_cookie('哔哩哔哩', 'SESSDATA=current; bili_jct=current')
    service.vault.put(service._validation_key('哔哩哔哩'),
                      json.dumps({'state': 'VALID', 'source': 'import'}))

    async def invalid(_platform):
        return {'state': 'INVALID'}

    monkeypatch.setattr(service, 'verify_cookie', invalid)
    saved = await service._save_captured_batch(
        [{'platform': '哔哩哔哩', 'netscape': 'SESSDATA=rejected; bili_jct=rejected'}], mode='auto')
    assert '哔哩哔哩' in saved
    values = {name: value for _, name, value in cookies(service, '哔哩哔哩')}
    assert values['SESSDATA'] == 'current'
    marker = json.loads(service.vault.get(service._validation_key('哔哩哔哩')))
    assert marker['rollback_reason'] == 'NEW_COOKIE_REJECTED'


def test_cookie_survives_service_restart(service):
    service.import_cookie('哔哩哔哩', 'SESSDATA=restart-me; bili_jct=restart-jct')
    reopened = Service(service.config.directory)
    try:
        values = {name: value for _, name, value in cookies(reopened, '哔哩哔哩')}
        assert values['SESSDATA'] == 'restart-me'
        assert reopened._session_info('哔哩哔哩', include_ui=True)['state'] == 'IMPORTED'
    finally:
        reopened.store.db.close()
