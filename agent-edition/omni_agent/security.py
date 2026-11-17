"""Process-scoped outbound guard. Never installed in the legacy GUI process."""
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


def public_ip(value):
    address = ipaddress.ip_address(str(value).split('%')[0])
    if address.version == 6 and address.ipv4_mapped: address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_unspecified


def validate_url(url, *, initial=False, resolve=True):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError()
        host = p.hostname.rstrip('.').lower()
        if initial and not any(host == h or host.endswith('.' + h) for h in HOSTS):
            raise AgentError('UNSUPPORTED_PLATFORM', '仅接受已配置媒体平台的作品链接')
        try:
            if not public_ip(host): raise AgentError('SSRF_BLOCKED', '不允许访问本机、私网或特殊地址')
        except ValueError:
            if host in ('localhost',) or host.endswith(('.localhost', '.local', '.internal')):
                raise AgentError('SSRF_BLOCKED', '不允许访问本机、私网或特殊地址')
        if resolve:
            rows = RAW_GETADDRINFO(host, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
            if not rows or any(not public_ip(row[4][0]) for row in rows):
                raise AgentError('SSRF_BLOCKED', '目标解析到非公共地址')
        return url
    except AgentError: raise
    except (ValueError, OSError): raise AgentError('INVALID_URL', '链接格式无效或域名无法解析') from None


def parse_download_command(raw):
    """解析链接前缀指令：/us（下载合集、分集视频、全部图片）、/zip（以压缩包形式发送）。"""
    text = str(raw or '').strip()
    all_items = False
    pack_zip = False
    while True:
        match = re.match(r'^/(us|zip)(?=\s|$|https?://)(?:\s+)?', text, re.I)
        if not match:
            break
        cmd = match.group(1).lower()
        if cmd == 'us':
            all_items = True
        elif cmd == 'zip':
            pack_zip = True
        text = text[match.end():].lstrip()
    return text, all_items, pack_zip


def source_url(text):
    clean, _, _ = parse_download_command(text)
    match = re.search(r'https?://[^\s<>"\u201d]+', clean)
    if not match: raise AgentError('INVALID_URL', '请提供HTTP(S)作品或合集链接')
    return validate_url(match[0].rstrip('。，,;；!！)]}'), initial=True, resolve=False)


def is_profile(url):
    p = urlsplit(url)
    path = p.path.rstrip('/')
    host = (p.hostname or '').lower()
    # Fail closed for known home/profile/search/feed forms, including encoded paths.
    from urllib.parse import unquote
    path = unquote(path)
    if re.search(r'/(?:user|users|u|channel|channels|profile|search|feed|home|space)(?:/|$)', path, re.I): return True
    if host.endswith('bilibili.com') and host.startswith('space.'): return True
    if host.endswith(('x.com', 'twitter.com')) and not re.search(r'/status/\d+', path): return True
    if host.endswith(('youtube.com', 'tiktok.com')) and '/@' in path and not re.search(r'/(video|shorts|watch)/', path): return True
    if host.endswith('instagram.com') and not re.search(r'/(p|reel|reels|tv)/', path): return True
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
    """Validate each DNS resolution AND actual socket destination, including redirects.

    Administrator-configured proxy is the only private socket exception. URL
    validation remains enabled; SOCKS remote-DNS is disabled so IP checks persist.
    FFmpeg is restricted to local protocols; browser capture is disabled.
    """
    proxy_destinations = set()
    if proxy:
        p = urlsplit(proxy)
        if p.scheme not in ('http', 'https', 'socks5') or not p.hostname or not p.port:
            raise AgentError('INVALID_PROXY', '代理须为带端口的 http/https/socks5 地址')
        proxy_destinations = {(r[4][0], p.port) for r in RAW_GETADDRINFO(p.hostname, p.port, type=socket.SOCK_STREAM)}

    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guard(address):
        if not isinstance(address, tuple): return address
        host, port = address[:2]
        rows = RAW_GETADDRINFO(host, port, type=socket.SOCK_STREAM)
        if any(not public_ip(row[4][0]) and (row[4][0], port) not in proxy_destinations for row in rows):
            raise AgentError('SSRF_BLOCKED', '网络连接目标被禁止')
        return rows

    def connect(sock, address):
        rows = guard(address)
        pinned = next((row[4] for row in rows if row[0] == sock.family), address) if isinstance(address,tuple) else address
        return original_connect(sock, pinned)

    def connect_ex(sock, address):
        rows = guard(address)
        pinned = next((row[4] for row in rows if row[0] == sock.family), address) if isinstance(address,tuple) else address
        return original_connect_ex(sock, pinned)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    import requests
    original_send = requests.Session.send

    def send(session, request, **kwargs):
        validate_url(request.url)
        return original_send(session, request, **kwargs)

    requests.Session.send = send
    import yt_dlp
    original_open = yt_dlp.YoutubeDL.urlopen

    def urlopen(engine, request):
        validate_url(request if isinstance(request, str) else request.url)
        return original_open(engine, request)

    yt_dlp.YoutubeDL.urlopen = urlopen
    original_popen = subprocess.Popen

    def popen(args, *positional, **kwargs):
        if isinstance(args, (str, bytes)) or kwargs.get('shell'):
            raise AgentError('PROCESS_DENIED', '工作进程禁止Shell命令')
        name = Path(str(args[0])).name.lower()
        if 'ffmpeg' in name or 'ffprobe' in name:
            args = [args[0], '-protocol_whitelist', 'file,pipe,crypto,data', *args[1:]]
        return original_popen(args, *positional, **kwargs)

    subprocess.Popen = popen
