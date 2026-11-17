import os
import sys
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    default_type: Literal['VIDEO_WITH_AUDIO', 'VIDEO_ONLY', 'AUDIO_ONLY', 'GIF', 'COVER', 'IMAGE', 'IMAGE_ZIP'] = 'VIDEO_WITH_AUDIO'
    default_height: int = Field(1080, ge=144, le=8640)
    max_height: int = Field(2160, ge=144, le=8640)
    container: Literal['mp4', 'mkv', 'auto'] = 'mp4'
    audio_format: Literal['mp3', 'm4a', 'flac', 'opus'] = 'mp3'
    autonomous: bool = True
    max_file_bytes: int = Field(500 * 1024**2, ge=1024, le=20 * 1024**3)
    max_cache_bytes: int = Field(4 * 1024**3, ge=1024**2, le=1024**4)
    concurrency: int = Field(2, ge=1, le=8)
    max_queue: int = Field(100, ge=1, le=1000)
    task_timeout: int = Field(1800, ge=10, le=7200)
    cache_ttl: int = Field(86400, ge=60, le=2592000)
    cache_dir: str = ''
    proxy: str = ''
    video: bool = True
    audio: bool = True
    images: bool = True
    collections: bool = True
    profiles: bool = False
    covers: bool = True
    max_collection: int = Field(20, ge=1, le=200)
    max_profile: int = Field(10, ge=1, le=50)
    confirmation_threshold: int = Field(5, ge=1, le=200)
    auto_delete: bool = True
    reuse_cache: bool = True
    theme: Literal['auto', 'light', 'dark'] = 'auto'

    @model_validator(mode='after')
    def defaults_obey_limits(self):
        if self.default_height > self.max_height:
            raise ValueError('默认画质不得高于最高允许画质')
        if self.max_file_bytes > self.max_cache_bytes:
            raise ValueError('单文件上限不得大于缓存总上限')
        if self.proxy:
            from urllib.parse import urlsplit
            p = urlsplit(self.proxy)
            if p.scheme not in ('http', 'https', 'socks5') or not p.hostname or not p.port or p.username or p.password or p.query or p.fragment or p.path not in ('', '/'):
                raise ValueError('代理需为带端口的 http/https/socks5 地址，不接受嵌入凭据')
        return self


def default_data_dir():
    """解析独立默认数据目录，不再写入系统 %LOCALAPPDATA% 全局目录。"""
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).resolve().parent
        # 若 EXE 位于独立源码工程内部（如 dist/OmniDownloaderAgent），统一共用工作区根目录的 data/
        for candidate in (exe_dir, *exe_dir.parents[:3]):
            if (candidate / 'agent-edition' / 'omni_agent').is_dir() and (candidate / 'windows').is_dir():
                return candidate / 'data'
        return exe_dir / 'data'
    return Path(__file__).resolve().parents[2] / 'data'


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        path.chmod(0o700)
    return path


def atomic_write(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Config:
    def __init__(self, directory=None):
        data_dir = directory or os.environ.get('OMNI_AGENT_DATA') or default_data_dir()
        self.directory = private_dir(data_dir)
        self.path = self.directory / 'settings.json'
        self.settings = Settings.model_validate_json(self.path.read_text('utf-8')) if self.path.exists() else Settings(
            cache_dir=os.environ.get('OMNI_AGENT_CACHE', ''), proxy=os.environ.get('OMNI_AGENT_PROXY', ''))
        self.cache = private_dir(Path(self.settings.cache_dir or self.directory / 'cache') / 'omni-agent-managed')
        self.bind_cache()

    def save(self, settings):
        if settings.cache_dir != self.settings.cache_dir:
            from .errors import AgentError
            if any((p.is_file() and p.name != '.owner') or p.is_symlink() or p.is_junction() for p in self.cache.rglob('*')):
                raise AgentError('CACHE_NOT_EMPTY', '更换缓存目录前请完成交付并清理旧缓存')
            destination = private_dir(Path(settings.cache_dir or self.directory / 'cache') / 'omni-agent-managed')
            self.bind_cache(destination)
        else:
            destination = self.cache
        atomic_write(self.path, settings.model_dump_json(indent=2).encode())
        self.cache = destination
        self.settings = settings

    def bind_cache(self, directory=None):
        marker = (directory or self.cache) / '.owner'
        owner = hashlib.sha256(str(self.directory.resolve()).encode()).hexdigest()
        if marker.exists() and marker.read_text() != owner:
            raise ValueError('缓存目录已属于另一个 Agent Edition 状态目录')
        if not marker.exists():
            atomic_write(marker, owner.encode())
