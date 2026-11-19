import socket
import subprocess

import pytest

from omni_agent.errors import AgentError, safe_text
from omni_agent.security import controlled, install_network_guard, is_profile, public_ip, source_url, validate_url
from omni_agent.service import DownloadOptions


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/a',
    'http://169.254.169.254/latest/meta-data',
    'http://[::1]/a',
    'http://10.0.0.1/a',
    'http://192.168.1.1/a',
    'http://localhost/a',
    'file:///etc/passwd',
    'https://youtube.com@127.0.0.1/a',
    'https://youtube.com:8443/a',
])
def test_ssrf_invalid_urls_are_rejected(url):
    with pytest.raises(AgentError):
        validate_url(url, resolve=False)


def test_dns_private_result_and_redirect_url_denied(monkeypatch):
    monkeypatch.setattr('omni_agent.security.RAW_GETADDRINFO', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('10.1.2.3', 443))])
    with pytest.raises(AgentError) as error:
        validate_url('https://redirected.example/video')
    assert error.value.code == 'SSRF_BLOCKED'


def test_fake_ip_and_public_ip_allowed_while_blocking_private_and_loopback(monkeypatch):
    # 公网 IP 放行
    assert public_ip('8.8.8.8')
    # 标准 IPv4 / IPv6 Fake-IP 网段放行
    assert public_ip('198.18.0.2')
    assert public_ip('198.19.255.254')
    assert public_ip('fc00::1')
    assert public_ip('::ffff:198.18.0.5')
    # 本机回环、云元数据、真实私网、多播与未指定地址严格阻断
    assert not public_ip('127.0.0.1')
    assert not public_ip('::1')
    assert not public_ip('::ffff:127.0.0.1')
    assert not public_ip('0.0.0.0')
    assert not public_ip('169.254.169.254')
    assert not public_ip('fe80::1')
    assert not public_ip('10.0.0.1')
    assert not public_ip('172.16.0.1')
    assert not public_ip('192.168.1.1')
    assert not public_ip('fd12:3456:789a::1')
    assert not public_ip('224.0.0.1')

    # DNS 解析返回 Fake-IP 时，正常媒体平台 URL 放行（兼容 Clash/Mihomo/sing-box TUN）
    monkeypatch.setattr('omni_agent.security.RAW_GETADDRINFO', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('198.18.0.25', 443))])
    assert validate_url('https://www.bilibili.com/video/BV17x411w7KC', initial=True) == 'https://www.bilibili.com/video/BV17x411w7KC'


def test_explicit_proxy_skips_local_dns_precheck_but_blocks_loopback_literal(monkeypatch):
    def fail_dns(*a, **k):
        raise AssertionError('using_proxy=True 时不应强行依赖本地 DNS 解析')
    monkeypatch.setattr('omni_agent.security.RAW_GETADDRINFO', fail_dns)
    assert validate_url('https://www.bilibili.com/video/BV17x411w7KC', initial=True, using_proxy=True) == 'https://www.bilibili.com/video/BV17x411w7KC'
    with pytest.raises(AgentError) as error:
        validate_url('http://127.0.0.1/api/admin/config', initial=False, using_proxy=True)
    assert error.value.code == 'SSRF_BLOCKED'


def test_network_guard_allows_proxy_and_fake_ip_while_blocking_other_loopback(monkeypatch):
    from omni_agent import security
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_popen = subprocess.Popen
    connected = []

    def fake_connect(sock, address):
        connected.append(address)

    def fake_getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (host, port))]

    monkeypatch.setattr(security, '_ORIG_CONNECT', fake_connect)
    monkeypatch.setattr(security, 'RAW_GETADDRINFO', fake_getaddrinfo)
    try:
        install_network_guard('http://127.0.0.1:7890')
        s = socket.socket()
        try:
            # 1. 允许连配置的本地代理端口
            s.connect(('127.0.0.1', 7890))
            # 2. 允许连 Fake-IP 虚拟段
            s.connect(('198.18.0.12', 443))
            assert connected == [('127.0.0.1', 7890), ('198.18.0.12', 443)]
            # 3. 禁止连本地其他非代理端口（如自身管理服务 58080）与云元数据
            with pytest.raises(AgentError) as err1:
                s.connect(('127.0.0.1', 58080))
            assert err1.value.code == 'SSRF_BLOCKED'
            with pytest.raises(AgentError) as err2:
                s.connect(('169.254.169.254', 80))
            assert err2.value.code == 'SSRF_BLOCKED'
            # 4. 禁止 Shell 命令，且 subprocess.Popen 仍是类（兼容 yt_dlp 继承）
            assert isinstance(subprocess.Popen, type)
            with pytest.raises(AgentError) as err3:
                subprocess.Popen('echo hi', shell=True)
            assert err3.value.code == 'PROCESS_DENIED'
        finally:
            s.close()
    finally:
        socket.socket.connect = orig_connect
        socket.socket.connect_ex = orig_connect_ex
        subprocess.Popen = orig_popen


@pytest.mark.parametrize('url', ['https://x.com/example', 'https://space.bilibili.com/123', 'https://youtube.com/@example', 'https://tiktok.com/@example', 'https://instagram.com/example/'])
def test_homepage_detection(url):
    assert is_profile(url)


@pytest.mark.parametrize('url', [
    'https://www.tiktok.com/@example/photo/7123456789',
    'https://www.tiktok.com/@example/video/7123456789',
    'https://x.com/u/status/12345',
])
def test_single_post_urls_not_flagged_as_profile(url):
    assert not is_profile(url)


def test_share_text_and_forged_host():
    assert source_url('看看这个 https://www.bilibili.com/video/BV17x411w7KC。') == 'https://www.bilibili.com/video/BV17x411w7KC'
    with pytest.raises(AgentError):
        source_url('https://youtube.com.evil.example/video')


def test_path_traversal_and_symlink(tmp_path):
    root = tmp_path / 'cache'
    root.mkdir()
    with pytest.raises(AgentError):
        controlled(root, root / '..' / 'outside')
    try:
        (root / 'link').symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('Windows symlink privilege unavailable')
    with pytest.raises(AgentError):
        controlled(root, root / 'link' / 'outside')


def test_sanitization_drops_even_unknown_signed_parameters():
    value = safe_text('failed https://example.com/v?unusual=secret Cookie: private')
    assert 'secret' not in value and 'private' not in value


def test_limits_cannot_be_overridden_and_profile_off(service):
    with pytest.raises(AgentError):
        service.resolve_options(DownloadOptions(use_defaults=False, height=4320))
    service.config.settings.autonomous = False
    with pytest.raises(AgentError):
        service.resolve_options(DownloadOptions(use_defaults=False, height=720))
    with pytest.raises(AgentError):
        service.check_url('https://x.com/example')
