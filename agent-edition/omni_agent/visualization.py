"""下载可视化阶段事件、自定义模板渲染与直接报错输出构造器。"""
import re

from .errors import safe_text

_PLACEHOLDER_RE = re.compile(r'\{([a-zA-Z0-9_]+)\}')

_DEFAULT_TEMPLATES = {
    'receive': '已收到链接（{command_summary}），正在准备解析与下载...',
    'start': '开始下载：{platform} · {title}',
    'complete': '下载完成（共 {file_count} 个文件），正在发送至 {target}...',
    'sent': '媒体发送已完成（共 {file_count} 个文件）。',
    'error': '下载或发送失败：{error}',
}


def render_template(template: str, variables: dict) -> str:
    """使用正则安全替换模板占位符，避免 format 注入或缺失字段报错。"""
    source = str(template or '')

    def _replace(match):
        key = match.group(1)
        value = variables.get(key)
        return '' if value is None else str(value)

    return _PLACEHOLDER_RE.sub(_replace, source).strip()


def summarize_command(*, cmd_all: bool = False, cmd_zip: bool = False, pack_zip: bool = False, download_type: str = '') -> str:
    """生成可读的链接指令与打包模式摘要。"""
    parts = []
    if cmd_all:
        parts.append('合集全量 /us')
    if cmd_zip or pack_zip:
        parts.append('压缩包 /zip' if cmd_zip else '压缩包导出')
    if download_type and download_type != 'VIDEO_WITH_AUDIO':
        labels = {
            'VIDEO_ONLY': '仅视频',
            'AUDIO_ONLY': '仅音频',
            'GIF': 'GIF',
            'COVER': '封面',
            'IMAGE': '图文原图',
            'IMAGE_ZIP': '图文压缩包',
        }
        parts.append(labels.get(download_type, download_type))
    return ' + '.join(parts) if parts else '默认下载参数'


def format_error_detail(error_code: str = '', error_message: str = '') -> str:
    """拼接脱敏后的完整报错详情，确保报错原因可直接输出给用户。"""
    code = str(error_code or '').strip()
    message = safe_text(error_message).strip() if error_message else ''
    if message and code and f'[{code}]' not in message and message != code:
        return f'[{code}] {message}'
    return message or code or '未知错误'


def stage_from_task_state(state: str) -> str:
    """将后台任务状态映射为可视化通知阶段名。"""
    if state == 'QUEUED':
        return 'receive'
    if state in ('DOWNLOADING', 'PROCESSING'):
        return 'start'
    if state in ('READY', 'SENDING'):
        return 'complete'
    if state in ('SENT', 'CLEANED'):
        return 'sent'
    if state in ('FAILED', 'UNCERTAIN', 'CANCELLED', 'EXPIRED'):
        return 'error'
    return ''


def build_visualization(
    settings,
    stage: str,
    *,
    context: dict | None = None,
    error_code: str = '',
    error_message: str = '',
) -> dict:
    """根据当前配置构造统一的可视化策略与阶段消息载荷。"""
    ctx = dict(context or {})
    error_detail = format_error_detail(error_code, error_message) if (error_code or error_message) else ''
    variables = {
        'task_id': str(ctx.get('task_id') or '')[:8],
        'full_task_id': str(ctx.get('task_id') or ''),
        'platform': str(ctx.get('platform') or '媒体平台'),
        'title': str(ctx.get('title') or '未命名媒体'),
        'author': str(ctx.get('author') or ''),
        'file_count': ctx.get('file_count') or 1,
        'progress': int(round(float(ctx.get('progress') or 0))),
        'speed': str(ctx.get('speed') or ''),
        'adapter': str(ctx.get('adapter') or '默认目标'),
        'target': str(ctx.get('target') or '本地保存目录'),
        'command_summary': str(ctx.get('command_summary') or '默认下载参数'),
        'error': error_detail,
        'error_code': str(error_code or ''),
    }
    stages = {
        'receive': bool(settings.visual_enabled and settings.visual_on_receive),
        'start': bool(settings.visual_enabled and settings.visual_on_start),
        'complete': bool(settings.visual_enabled and settings.visual_on_complete),
        'error': True,
    }

    if stage == 'error' or error_detail:
        current_stage = 'error'
        should_reply = True
        template = settings.visual_error_template or _DEFAULT_TEMPLATES['error']
        rendered = render_template(template, variables)
        if error_detail and error_detail not in rendered:
            rendered = f'{rendered}：{error_detail}' if rendered else error_detail
        guidance = 'direct_error: An error occurred. Directly output error_detail / message to the user without hiding the cause.'
    elif stage in ('receive', 'start', 'complete'):
        current_stage = stage
        should_reply = stages[stage]
        template_map = {
            'receive': settings.visual_receive_template,
            'start': settings.visual_start_template,
            'complete': settings.visual_complete_template,
        }
        template = template_map[stage] or _DEFAULT_TEMPLATES[stage]
        rendered = render_template(template, variables) if should_reply else ''
        if not should_reply:
            guidance = f'silent: User disabled "{stage}" notifications. Do not send an intermediate status message for this stage.'
        elif settings.visual_mode == 'custom':
            guidance = f'exact_custom: User chose custom content for "{stage}". Reply with visualization.message directly.'
        else:
            guidance = f'agent_decides: Stage "{stage}" is enabled with visual_mode="agent". Compose a clear status reply from metadata/variables or use visualization.message.'
    elif stage == 'sent':
        current_stage = 'sent'
        should_reply = False
        rendered = render_template(_DEFAULT_TEMPLATES['sent'], variables)
        guidance = 'completed: Delivery succeeded.'
    else:
        current_stage = stage or ''
        should_reply = False
        rendered = ''
        guidance = 'silent: No active visualization stage.'

    return {
        'enabled': bool(settings.visual_enabled),
        'mode': settings.visual_mode,
        'stages': stages,
        'current_stage': current_stage,
        'should_reply': should_reply,
        'message': rendered,
        'agent_instruction': guidance,
        'error_detail': error_detail,
        'variables': variables,
    }
