"""Small AstrBot bridge. Trusted session routing is never supplied by the LLM."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register('omni_agent','OmniDownloader','Headless media delivery through Agent Edition','0.1.0')
class OmniAgentPlugin(Star):
    def __init__(self,context:Context):
        super().__init__(context)
        self.server=os.environ.get('OMNI_AGENT_SERVER','http://127.0.0.1:58080').rstrip('/')
        self.identities=Path(os.environ.get('OMNI_AGENT_CHAT_IDENTITIES','data/omni-agent-chats.json'))

    def route(self,event):
        # Exact unified_msg_origin keys are copied from AstrBot's session panel.
        # Each gets its own administrator-created token and fixed adapter/target.
        rows=json.loads(self.identities.read_text('utf-8'))
        route=rows.get(event.unified_msg_origin)
        if not route:raise RuntimeError('当前会话尚未配置 OmniDownloader 独立身份')
        return route

    async def call(self,route,name,**arguments):
        async with httpx.AsyncClient(timeout=1900,trust_env=False,follow_redirects=False) as client:
            response=await client.post(self.server+'/api/tools/'+name,headers={'Authorization':'Bearer '+route['token']},json=arguments)
            if response.status_code!=200:raise RuntimeError('OmniDownloader 服务连接或身份认证失败')
            data=response.json()
            if not data['ok']:
                error=data['error']
                raise RuntimeError(error['code']+': '+error['message']+(' approval_id='+error['approval_id'] if error.get('approval_id') else ''))
            return data['result']

    async def process(self,event,url,download_type,approval_id=None):
        route=self.route(event)
        # A stable request key from the trusted message ID prevents event replay
        # from downloading/sending the same media twice.
        message_id=str(event.message_obj.message_id)
        key=hashlib.sha256((event.unified_msg_origin+'|'+message_id+'|'+url+'|'+download_type).encode()).hexdigest()
        options={} if download_type=='default' else {'use_defaults':False,'download_type':download_type}
        job=await self.call(route,'download_media',url=url,options=options,request_key=key,approval_id=approval_id)
        for _ in range(7200):
            state=await self.call(route,'get_task_status',task_id=job['id'])
            if state['state']=='READY':break
            if state['state'] in ('FAILED','CANCELLED','EXPIRED'):return '下载未完成：'+state['error']
            if state['state'] in ('SENT','CLEANED'):return '这条消息的媒体已经交付'
            await asyncio.sleep(1)
        else:return '等待超时，任务仍可通过管理页和 MCP 查询：'+job['id']
        delivery=await self.call(route,'deliver_media',task_id=job['id'],adapter=route['adapter'],target=route['target'],request_key=key+'-send')
        return '文件已就绪并进入交付队列：'+delivery['id']+'。服务会在平台确认成功后按设置清理。'

    @filter.command('omni')
    async def download_command(self,event:AstrMessageEvent,url:str,approval_id:str=''):
        try:result=await self.process(event,url,'default',approval_id or None)
        except (RuntimeError,OSError,ValueError,httpx.HTTPError) as error:
            result=str(error) if isinstance(error,RuntimeError) else '连接或配置失败，请检查服务与会话身份配置'
        yield event.plain_result(result)

    @filter.llm_tool(name='omni_download_and_deliver')
    async def media_tool(self,event:AstrMessageEvent,url:str,download_type:str='default',approval_id:str='')->str:
        """下载媒体并由服务发送到当前聊天。管理员限制始终优先。

        Args:
            url(string): 作品或视频分享链接。
            download_type(string): default 或 VIDEO_WITH_AUDIO、VIDEO_ONLY、AUDIO_ONLY、GIF、COVER、IMAGE、IMAGE_ZIP。
            approval_id(string): 高消耗请求经管理员批准后返回的确认ID，普通请求留空。
        """
        try:return await self.process(event,url,download_type,approval_id or None)
        except (RuntimeError,OSError,ValueError,httpx.HTTPError) as error:
            return str(error) if isinstance(error,RuntimeError) else '连接或配置失败，请检查服务与会话身份配置'
