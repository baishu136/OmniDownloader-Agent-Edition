"""Installed as yt_dlp_plugins/extractor/omni_x_guard.py."""
from omni_x_guard import install
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError
from yt_dlp.downloader.external import ExternalFD

install()


class OmniXGuardProbeIE(InfoExtractor):
    """Offline startup check: no account, DNS or HTTP request is performed."""
    _VALID_URL = r'omni-x-guard:probe$'

    def _real_extract(self, url):
        if not (getattr(self._downloader.urlopen, '_omni_x_guard', False)
                and getattr(ExternalFD.real_download, '_omni_x_guard', False)):
            raise ExtractorError('[ENGINE_UNAVAILABLE] X 请求保护未加载', expected=True)
        return {'id': 'x-guard', 'title': 'X request guard', 'ext': 'mp4',
                'url': 'data:video/mp4;base64,AA==', '_omni_x_guard_ready': True}


__all__ = ['OmniXGuardProbeIE']
