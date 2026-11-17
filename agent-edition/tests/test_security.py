from pathlib import Path
import socket

import pytest

from omni_agent.errors import AgentError, safe_text
from omni_agent.security import controlled, is_profile, public_ip, source_url, validate_url
from omni_agent.service import DownloadOptions


@pytest.mark.parametrize('url',['http://127.0.0.1/a','http://169.254.169.254/latest/meta-data','http://[::1]/a','http://10.0.0.1/a','http://192.168.1.1/a','http://localhost/a','file:///etc/passwd','https://youtube.com@127.0.0.1/a','https://youtube.com:8443/a'])
def test_ssrf_invalid_urls_are_rejected(url):
    with pytest.raises(AgentError):validate_url(url,resolve=False)


def test_dns_private_result_and_redirect_url_denied(monkeypatch):
    monkeypatch.setattr('omni_agent.security.RAW_GETADDRINFO',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,'',('10.1.2.3',443))])
    with pytest.raises(AgentError) as error:validate_url('https://redirected.example/video')
    assert error.value.code=='SSRF_BLOCKED'


def test_public_ip_and_ipv4_mapped_ipv6():
    assert public_ip('8.8.8.8')
    assert not public_ip('::ffff:127.0.0.1')
    assert not public_ip('224.0.0.1')


@pytest.mark.parametrize('url',['https://x.com/example','https://space.bilibili.com/123','https://youtube.com/@example','https://tiktok.com/@example','https://instagram.com/example/'])
def test_homepage_detection(url):assert is_profile(url)


def test_share_text_and_forged_host():
    assert source_url('看看这个 https://www.bilibili.com/video/BV17x411w7KC。')=='https://www.bilibili.com/video/BV17x411w7KC'
    with pytest.raises(AgentError):source_url('https://youtube.com.evil.example/video')


def test_path_traversal_and_symlink(tmp_path):
    root=tmp_path/'cache'
    root.mkdir()
    with pytest.raises(AgentError):controlled(root,root/'..'/'outside')
    try:(root/'link').symlink_to(tmp_path,target_is_directory=True)
    except OSError:pytest.skip('Windows symlink privilege unavailable')
    with pytest.raises(AgentError):controlled(root,root/'link'/'outside')


def test_sanitization_drops_even_unknown_signed_parameters():
    value=safe_text('failed https://example.com/v?unusual=secret Cookie: private')
    assert 'secret' not in value and 'private' not in value


def test_limits_cannot_be_overridden_and_profile_off(service):
    with pytest.raises(AgentError):service.resolve_options(DownloadOptions(use_defaults=False,height=4320))
    service.config.settings.autonomous=False
    with pytest.raises(AgentError):service.resolve_options(DownloadOptions(use_defaults=False,height=720))
    with pytest.raises(AgentError):service.check_url('https://x.com/example')
