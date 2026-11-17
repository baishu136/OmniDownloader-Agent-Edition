import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock

from omni_agent.credentials import core_root
from omni_agent.worker import execute


async def test_real_task_manager_is_used_for_media_job(tmp_path,monkeypatch):
    """Mock ONLY platform I/O; actually run the original scheduler and file checks."""
    sys.path.insert(0,str(core_root()))
    monkeypatch.setenv('OMNI_STATE_DIR',str(tmp_path/'core-state'))
    monkeypatch.setattr('omni_agent.worker.install_network_guard',lambda *args:None)
    monkeypatch.setattr('omni_agent.worker.validate_url',lambda url,**kw:url)
    from core.config import config
    from core.models import FormatOption, VideoMetadata
    from core.task_manager import task_manager
    from engine.bilibili_extractor import BilibiliDirectExtractor
    from engine.url_sniffer import UrlSniffer
    url='https://www.bilibili.com/video/BV17x411w7KC'
    metadata=VideoMetadata(url=url,title='fixture',siteName='哔哩哔哩',availableVideoFormats=[FormatOption(formatId='f1',resolutionLabel='720p',width=1280,height=720)])
    monkeypatch.setattr(UrlSniffer,'sanitize_and_resolve_url',AsyncMock(return_value=url))
    monkeypatch.setattr(BilibiliDirectExtractor,'extract',AsyncMock(return_value=metadata))
    async def download(task,progress,cancel):
        from PIL import Image
        path=Path(config.download_dir)/'cover.png'
        Image.new('RGB',(16,16),'red').save(path)
        return str(path)
    monkeypatch.setattr(BilibiliDirectExtractor,'download',download)
    events=[]
    await execute({'action':'download','url':url,'state':str(tmp_path/'work'),'data':str(tmp_path/'data'),
        'max_file_bytes':1024**2,'options':{'download_type':'COVER','height':1080,'container':'mp4','audio_format':'mp3'}},events.append)
    result=events[-1]
    assert result['kind']=='result' and len(result['result']['paths'])==1
    assert any(task.status.value=='COMPLETED' for task in task_manager.tasks.values())
    assert Path(result['result']['paths'][0]).exists()


async def test_real_core_analyze_preserves_truthful_formats(tmp_path,monkeypatch):
    monkeypatch.setattr('omni_agent.worker.install_network_guard',lambda *args:None)
    monkeypatch.setattr('omni_agent.worker.validate_url',lambda url,**kw:url)
    from core.models import FormatOption,VideoMetadata
    from engine.bilibili_extractor import BilibiliDirectExtractor
    from engine.url_sniffer import UrlSniffer
    url='https://www.bilibili.com/video/BV17x411w7KC'
    monkeypatch.setattr(UrlSniffer,'sanitize_and_resolve_url',AsyncMock(return_value=url))
    monkeypatch.setattr(BilibiliDirectExtractor,'extract',AsyncMock(return_value=VideoMetadata(url=url,title='fixture',siteName='哔哩哔哩',
        availableVideoFormats=[FormatOption(formatId='f1',resolutionLabel='720p',width=1280,height=720,vcodec='avc1',dynamicRange='SDR')])))
    events=[]
    await execute({'action':'analyze','url':url,'state':str(tmp_path/'work'),'data':str(tmp_path/'data')},events.append)
    result=events[-1]['result']
    assert result['formats'][0]['height']==720 and result['formats'][0]['hdr']=='SDR'
    assert 'url' not in result


async def test_collection_of_images_creates_one_core_task_per_selected_image(tmp_path,monkeypatch):
    monkeypatch.setattr('omni_agent.worker.install_network_guard',lambda *args:None)
    monkeypatch.setattr('omni_agent.worker.validate_url',lambda url,**kw:url)
    from core.config import config
    from engine.images import ImageEngine
    from engine.url_sniffer import UrlSniffer
    url='https://www.xiaohongshu.com/explore/fixture'
    post={'url':url,'platform':'小红书','title':'fixture','resources':[{'id':'i1'},{'id':'i2'},{'id':'i3'}]}
    monkeypatch.setattr(UrlSniffer,'sanitize_and_resolve_url',AsyncMock(return_value=url))
    monkeypatch.setattr(ImageEngine,'extract',AsyncMock(return_value=post))
    selected=[]
    async def download(task,progress,cancel):
        from PIL import Image
        selected.extend(task.selected_image_ids)
        path=Path(config.download_dir)/(task.id+'.png')
        Image.new('RGB',(8,8),'green').save(path)
        return str(path)
    monkeypatch.setattr(ImageEngine,'download',download)
    events=[]
    await execute({'action':'collection','url':url,'state':str(tmp_path/'work'),'data':str(tmp_path/'data'),
        'start':2,'count':2,'max_file_bytes':1024**2,
        'options':{'download_type':'IMAGE','height':1080,'container':'mp4','audio_format':'mp3'}},events.append)
    assert selected==['i2','i3']
    assert len(events[-1]['result']['paths'])==2
