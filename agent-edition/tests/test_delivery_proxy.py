"""Delivery proxy policy: local bots direct, remote bots through the configured proxy."""
from omni_agent.delivery import delivery_proxy

PROXY = 'http://127.0.0.1:7890'


def test_local_bot_is_not_proxied():
    assert delivery_proxy('http://127.0.0.1:3000', PROXY) is None
    assert delivery_proxy('http://localhost:5700', PROXY) is None
    assert delivery_proxy('http://192.168.1.20:3000', PROXY) is None


def test_remote_bot_uses_configured_proxy():
    assert delivery_proxy('https://api.telegram.org', PROXY) == PROXY
    assert delivery_proxy('http://8.8.8.8:5700', PROXY) == PROXY


def test_no_proxy_configured_stays_direct():
    assert delivery_proxy('https://api.telegram.org', '') is None


def test_invalid_proxy_url_still_passes_config_scope():
    # 格式校验由设置层负责；此处只决定是否绑定代理。
    assert delivery_proxy('https://api.telegram.org', 'socks5://127.0.0.1:7890') is not None
