"""Chat text link extraction with command prefixes and safety flags."""
import pytest

from omni_agent.errors import AgentError
from omni_agent.security import extract_chat_links


def test_extracts_multiple_links_with_command_prefixes():
    text = ('看这个 https://www.bilibili.com/video/BV1xx?spm_id_from=333.999 和 '
            '/us /merge https://www.youtube.com/shorts/abc。')
    links = extract_chat_links(text)
    assert [item['url'] for item in links] == [
        'https://www.bilibili.com/video/BV1xx',
        'https://www.youtube.com/shorts/abc',
    ]
    assert links[0]['all_items'] is False and links[0]['merge_forward'] is False
    assert links[1]['all_items'] is True and links[1]['merge_forward'] is True


def test_deduplicates_and_flags_unsafe_links():
    text = ('https://www.bilibili.com/video/BV1 '
            'https://www.bilibili.com/video/BV1 '
            'http://127.0.0.1/admin '
            'https://example.invalid/video')
    links = extract_chat_links(text)
    assert len(links) == 3
    assert links[0]['valid'] is True
    assert links[1]['valid'] is False and links[1]['error_code'] == 'SSRF_BLOCKED'
    assert links[2]['valid'] is False and links[2]['error_code'] == 'UNSUPPORTED_PLATFORM'


def test_strips_trailing_punctuation_and_tracking_params():
    links = extract_chat_links('复制打开 https://b23.tv/abcd?share_source=copy&utm_medium=x，')
    assert links[0]['url'] == 'https://b23.tv/abcd'
    assert links[0]['valid'] is True


def test_ignores_text_without_links():
    assert extract_chat_links('这段文字没有链接') == []


def test_service_rejects_oversized_text(service):
    with pytest.raises(AgentError) as error:
        service.extract_links('x' * 100_001)
    assert error.value.code == 'INVALID_PARAMETER'
