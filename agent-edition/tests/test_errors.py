"""Structured error taxonomy and redaction contracts."""
import pytest

from omni_agent.errors import AgentError, classify_error, safe_text


def test_transient_network_error_is_retryable():
    spec = classify_error('TIMEOUT', '下载连接超时')
    assert spec['retryable'] is True
    assert spec['stage'] == 'DOWNLOADING'
    assert spec['suggested_action']


def test_authentication_error_is_not_retryable():
    spec = classify_error('COOKIE_EXPIRED', '登录已失效请重登')
    assert spec['retryable'] is False
    assert spec['stage'] == 'AUTHENTICATING'


def test_unknown_code_with_transient_message_is_retryable():
    spec = classify_error('ENGINE_UNKNOWN', 'connection reset by peer')
    assert spec['retryable'] is True
    assert spec['stage'] == 'DOWNLOADING'


def test_core_media_codes_are_classified():
    assert classify_error('ACCESS_DENIED')['stage'] == 'PARSING'
    assert classify_error('CONTENT_UNAVAILABLE')['retryable'] is False
    assert classify_error('QUALITY_UNAVAILABLE')['suggested_action']
    assert classify_error('RATE_LIMITED')['retryable'] is True
    assert classify_error('PROXY_ERROR')['stage'] == 'DOWNLOADING'


def test_unknown_code_with_permanent_message_is_not_retryable():
    spec = classify_error('ENGINE_UNKNOWN', '该视频已被删除')
    assert spec['retryable'] is False


def test_agent_error_carries_structured_result_and_details():
    error = AgentError('FILE_TOO_LARGE', '文件超过上限', limit=100)
    payload = error.result()['error']
    assert payload['code'] == 'FILE_TOO_LARGE'
    assert payload['stage'] == 'STORAGE'
    assert payload['retryable'] is False
    assert payload['suggested_action']
    assert payload['limit'] == 100


def test_safe_text_redacts_urls_credentials_and_local_paths():
    raw = (r'打开 C:\Users\alice\Videos\demo.mp4 失败；'
           r'日志 /home/alice/omni/log.txt；'
           'https://cdn.example.com/a?token=1；SESSDATA=secret-value')
    text = safe_text(raw)
    assert 'alice' not in text
    assert 'cdn.example.com' not in text
    assert 'secret-value' not in text
    assert '[PATH]' in text
    assert '[URL]' in text


def test_classification_does_not_leak_the_original_code_case():
    spec = classify_error('timeout')
    assert spec['code'] == 'TIMEOUT'
