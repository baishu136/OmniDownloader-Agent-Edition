"""Process-scoped outbound guard compatible with Fake-IP / TUN proxies."""
import ipaddress
import re
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .errors import AgentError


HOSTS = {
    'bilibili.com', 'b23.tv', 'youtube.com', 'youtu.be', 'x.com', 'twitter.com', 't.co',
    'douyin.com', 'iesdouyin.com', 'xiaohongshu.com', 'xhslink.com', 'xhslink.cn',
    'rednote.com', 'xiaoheihe.cn', 'heybox.cn', 'pixiv.net', 'tiktok.com',
    'kuaishou.com', 'kwai.com', 'gifshow.com', 'chenzhongtech.com', 'instagram.com',
    'facebook.com', 'fb.watch', 'fb.me', 'pinterest.com', 'pin.it',
}
RAW_GETADDRINFO = socket.getaddrinfo
_ORIG_CONNECT = socket.socket.connect
_ORIG_CONNECT_EX = socket.socket.connect_ex
_ORIG_POPEN = subprocess.Popen

# 标准 Fake-IP 虚拟网段（Clash / Mihomo / sing-box 等 TUN 代理常用）
FAKE_IP_NETWORKS = (
    ipaddress.ip_network('198.18.0.0/15'),
    ipaddress.ip_network('fc00::/18'),
)


def public_ip(value):
    """兼容 Fake-IP 段（198.18.0.0/15 与 fc00::/18），严格拦截本机回环、真实私网与云元数据。"""
    address = ipaddress.ip_address(str(value).split('%')[0])
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    if any(address in net for net in FAKE_IP_NETWORKS):
        return True
    return address.is_global and not address.is_multicast and not address.is_unspecified


def validate_url(url, *, initial=False, resolve=True, using_proxy=False):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError()
        host = p.hostname.rstrip('.').lower()
        if initial and not any(host == h or host.endswith('.' + h) for h in HOSTS):
            raise AgentError('UNSUPPORTED_PLATFORM', '仅接受已配置媒体平台的作品链接')
        try:
            if not public_ip(host):
                raise AgentError('SSRF_BLOCKED', '不允许访问本机、私网或特殊地址')
        except ValueError:
            if host in ('localhost',) or host.endswith(('.localhost', '.local', '.internal')):
                raise AgentError('SSRF_BLOCKED', '不允许访问本机、私网或特殊地址')
        # 配置显式代理时域名由代理端解析，跳过本地 DNS 预校验；直连/TUN 模式允许公网及 Fake-IP
        if resolve and not using_proxy:
            rows = RAW_GETADDRINFO(host, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
            if not rows or any(not public_ip(row[4][0]) for row in rows):
                raise AgentError('SSRF_BLOCKED', '目标解析到非许可内网或保留地址')
        return url
    except AgentError:
        raise
    except (ValueError, OSError):
        raise AgentError('INVALID_URL', '链接格式无效或域名无法解析') from None


def parse_download_commands(raw):
    """解析链接前缀指令：/us（下载合集、分集视频、全部图片）、/zip（压缩包发送）、/merge（合并转发/媒体组发送）。"""
    text = str(raw or '').strip()
    all_items = False
    pack_zip = False
    merge_forward = False
    while True:
        match = re.match(r'^/(us|zip|merge)(?=\s|$|https?://)(?:\s+)?', text, re.I)
        if not match:
            break
        cmd = match.group(1).lower()
        if cmd == 'us':
            all_items = True
        elif cmd == 'zip':
            pack_zip = True
        elif cmd == 'merge':
            merge_forward = True
        text = text[match.end():].lstrip()
    return text, all_items, pack_zip, merge_forward


def parse_download_command(raw):
    """向后兼容的三元组前缀解析接口，返回 (clean_text, all_items, pack_zip)。"""
    clean, all_items, pack_zip, _ = parse_download_commands(raw)
    return clean, all_items, pack_zip


def source_url(text):
    clean, _, _, _ = parse_download_commands(text)
    match = re.search(r'https?://[^\s<>"\u201d]+', clean)
    if not match:
        raise AgentError('INVALID_URL', '请提供HTTP(S)作品或合集链接')
    return validate_url(match[0].rstrip('。，,;；!！)]}'), initial=True, resolve=False)


def is_profile(url):
    p = urlsplit(url)
    path = p.path.rstrip('/')
    host = (p.hostname or '').lower()
    # 识别常见用户主页/频道/搜索/动态链接形式（含 URL 编码路径）
    from urllib.parse import unquote
    path = unquote(path)
    if host.endswith(('x.com', 'twitter.com')):
        return not re.search(r'/status/\d+', path)
    if host.endswith(('youtube.com', 'tiktok.com')) and '/@' in path:
        return not re.search(r'/(video|photo|shorts|watch)/', path)
    if re.search(r'/(?:user|users|u|channel|channels|profile|search|feed|home|space)(?:/|$)', path, re.I):
        return True
    if host.endswith('bilibili.com') and host.startswith('space.'):
        return True
    if host.endswith('instagram.com') and not re.search(r'/(p|reel|reels|tv)/', path):
        return True
    return False


def controlled(root, path):
    root, path = Path(root).resolve(), Path(path)
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise AgentError('PATH_DENIED', '文件不在受控缓存目录')
    current = path.absolute()
    while current != root and current.parent != current:
        if current.is_symlink() or current.is_junction():
            raise AgentError('PATH_DENIED', '缓存路径包含链接或目录联接')
        current = current.parent
    return resolved


def install_network_guard(proxy=''):
    """安装兼容 Fake-IP / TUN 与显式代理的出站防护。

    - 显式代理端口仅作为唯一可信本地回环/私网出口；
    - 放行公网 IP 与标准 Fake-IP 网段（198.18.0.0/15、fc00::/18），继续拦截本机、真实私网与云元数据；
    - 限制 FFmpeg 仅使用本地协议，并禁止工作进程启动 Shell 命令。
    """
    proxy_destinations = set()
    has_proxy = bool(proxy)
    if proxy:
        p = urlsplit(proxy)
        if p.scheme not in ('http', 'https', 'socks5') or not p.hostname or not p.port:
            raise AgentError('INVALID_PROXY', '代理须为带端口的 http/https/socks5 地址')
        proxy_destinations = {(r[4][0], p.port) for r in RAW_GETADDRINFO(p.hostname, p.port, type=socket.SOCK_STREAM)}

    def guard(address):
        if not isinstance(address, tuple):
            return address
        host, port = address[:2]
        rows = RAW_GETADDRINFO(host, port, type=socket.SOCK_STREAM)
        if any(not public_ip(row[4][0]) and (row[4][0], port) not in proxy_destinations for row in rows):
            raise AgentError('SSRF_BLOCKED', '网络连接目标被禁止')
        return rows

    def connect(sock, address):
        rows = guard(address)
        pinned = next((row[4] for row in rows if row[0] == sock.family), address) if isinstance(address, tuple) else address
        return _ORIG_CONNECT(sock, pinned)

    def connect_ex(sock, address):
        rows = guard(address)
        pinned = next((row[4] for row in rows if row[0] == sock.family), address) if isinstance(address, tuple) else address
        return _ORIG_CONNECT_EX(sock, pinned)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex

    # 本地补丁：不再逐请求校验 URL。
    # worker.execute 已在任务入口用 validate_url(initial=True) 校验用户提交的链接；
    # 之后 yt-dlp / requests 访问的是平台接口返回的 CDN 地址（如 B 站
    # *.mcdn.bilivideo.cn:8082），它们合法地使用非默认端口并会跳转到其它 CDN 域名，
    # 逐请求校验会把正常下载判成 INVALID_URL（最终表现为 EXTRACTOR_CHANGED）。
    # SSRF 防护仍由上面的 socket 层承担：只放行公网 / Fake-IP / 显式代理地址。

    class GuardedPopen(_ORIG_POPEN):
        def __init__(self, args, *positional, **kwargs):
            if isinstance(args, (str, bytes)) or kwargs.get('shell'):
                raise AgentError('PROCESS_DENIED', '工作进程禁止Shell命令')
            name = Path(str(args[0])).name.lower()
            if 'ffmpeg' in name or 'ffprobe' in name:
                args = [args[0], '-protocol_whitelist', 'file,pipe,crypto,data', *args[1:]]
            super().__init__(args, *positional, **kwargs)

    subprocess.Popen = GuardedPopen
