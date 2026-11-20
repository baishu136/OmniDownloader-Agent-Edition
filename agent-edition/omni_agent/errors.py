"""Structured agent errors: stable code, pipeline stage, retry guidance and redaction.

The public shape stays backward compatible: existing clients read ``code`` and
``message``; new clients may additionally rely on ``stage``, ``retryable`` and
``suggested_action`` to decide whether to retry or ask the user to act.
"""
import re

STAGE_REQUEST = 'REQUEST'
STAGE_AUTH = 'AUTHENTICATING'
STAGE_PARSING = 'PARSING'
STAGE_DOWNLOADING = 'DOWNLOADING'
STAGE_STORAGE = 'STORAGE'
STAGE_DELIVERY = 'DELIVERY'
STAGE_INTERNAL = 'INTERNAL'

# Codes that describe a temporary condition. Only these are auto-retried.
_RETRYABLE = {
    'TIMEOUT',
    'WORKER_FAILED',
    'NETWORK_ERROR',
    'CONNECTION_ERROR',
    'CONNECTION_RESET',
    'DNS_ERROR',
    'PROXY_ERROR',
    'PROXY_UNAVAILABLE',
    'SERVER_ERROR',
    'HTTP_5XX',
    'RATE_LIMITED',
    'UPSTREAM_ERROR',
    'SERVICE_UNAVAILABLE',
    'BOT_OFFLINE',
    'BOT_UNREACHABLE',
    'BOT_RATE_LIMITED',
}

_STAGE_BY_CODE = {
    # authentication
    'AUTH_REQUIRED': STAGE_AUTH,
    'COOKIE_EXPIRED': STAGE_AUTH,
    'INVALID_COOKIE': STAGE_AUTH,
    'COOKIE_DISABLED': STAGE_AUTH,
    'CHALLENGE_REQUIRED': STAGE_AUTH,
    'LOGIN_REQUIRED': STAGE_AUTH,
    'NEEDS_AUTH': STAGE_AUTH,
    'BOT_AUTH_FAILED': STAGE_AUTH,
    # parsing
    'UNSUPPORTED_PLATFORM': STAGE_PARSING,
    'NO_MEDIA': STAGE_PARSING,
    'NO_COLLECTION': STAGE_PARSING,
    'COLLECTION_REQUIRED': STAGE_PARSING,
    'INVALID_URL': STAGE_PARSING,
    'INVALID_RANGE': STAGE_PARSING,
    'CAPABILITY_UNAVAILABLE': STAGE_PARSING,
    'MEDIA_UNCONFIRMED': STAGE_PARSING,
    'CONTENT_UNAVAILABLE': STAGE_PARSING,
    'ACCESS_DENIED': STAGE_PARSING,
    'QUALITY_UNAVAILABLE': STAGE_PARSING,
    'EXTRACTOR_CHANGED': STAGE_PARSING,
    'UPSTREAM_ERROR': STAGE_PARSING,
    # downloading / media processing
    'TIMEOUT': STAGE_DOWNLOADING,
    'WORKER_FAILED': STAGE_DOWNLOADING,
    'PROCESS_ISOLATION_FAILED': STAGE_DOWNLOADING,
    'NETWORK_ERROR': STAGE_DOWNLOADING,
    'CONNECTION_ERROR': STAGE_DOWNLOADING,
    'CONNECTION_RESET': STAGE_DOWNLOADING,
    'DNS_ERROR': STAGE_DOWNLOADING,
    'PROXY_ERROR': STAGE_DOWNLOADING,
    'PROXY_UNAVAILABLE': STAGE_DOWNLOADING,
    'SERVER_ERROR': STAGE_DOWNLOADING,
    'HTTP_5XX': STAGE_DOWNLOADING,
    'RATE_LIMITED': STAGE_DOWNLOADING,
    'ENGINE_ERROR': STAGE_DOWNLOADING,
    'DOWNLOAD_FAILED': STAGE_DOWNLOADING,
    'DISK_FULL': STAGE_DOWNLOADING,
    'ENGINE_LIMIT': STAGE_DOWNLOADING,
    # storage
    'PATH_DENIED': STAGE_STORAGE,
    'FILE_CHANGED': STAGE_STORAGE,
    'FILE_MISSING': STAGE_STORAGE,
    'FILE_INVALID': STAGE_STORAGE,
    'FILE_TOO_LARGE': STAGE_STORAGE,
    'CACHE_FULL': STAGE_STORAGE,
    'CACHE_NOT_EMPTY': STAGE_STORAGE,
    'LIMIT_EXCEEDED': STAGE_STORAGE,
    'PERMISSION_DENIED': STAGE_STORAGE,
    # delivery
    'DELIVERY_UNCERTAIN': STAGE_DELIVERY,
    'BOT_REJECTED': STAGE_DELIVERY,
    'BOT_FILE_TOO_LARGE': STAGE_DELIVERY,
    'BOT_DISABLED': STAGE_DELIVERY,
    'BOT_OFFLINE': STAGE_DELIVERY,
    'BOT_UNREACHABLE': STAGE_DELIVERY,
    'BOT_RATE_LIMITED': STAGE_DELIVERY,
    'TARGET_DENIED': STAGE_DELIVERY,
    'INVALID_TARGET': STAGE_DELIVERY,
    'RECEIPT_REQUIRED': STAGE_DELIVERY,
    'RECONCILIATION_REQUIRED': STAGE_DELIVERY,
    'NOT_READY': STAGE_DELIVERY,
    'SERVICE_REJECTED': STAGE_DELIVERY,
    # request / configuration
    'INVALID_PARAMETER': STAGE_REQUEST,
    'UNKNOWN_TOOL': STAGE_REQUEST,
    'FEATURE_DISABLED': STAGE_REQUEST,
    'POLICY_CHANGED': STAGE_REQUEST,
    'CONFIRMATION_REQUIRED': STAGE_REQUEST,
    'IDEMPOTENCY_CONFLICT': STAGE_REQUEST,
    'QUEUE_FULL': STAGE_REQUEST,
    'TASKS_ACTIVE': STAGE_REQUEST,
    'SERVICE_ALREADY_RUNNING': STAGE_REQUEST,
    'INVALID_STATE': STAGE_REQUEST,
    'INVALID_PROXY': STAGE_REQUEST,
    'SYSTEM_DENIED': STAGE_REQUEST,
    'PROCESS_DENIED': STAGE_REQUEST,
    'SSRF_BLOCKED': STAGE_REQUEST,
    # lifecycle
    'CANCELLED': STAGE_INTERNAL,
    'RESTART_INTERRUPTED': STAGE_INTERNAL,
    'INTERNAL_ERROR': STAGE_INTERNAL,
}

_ACTIONS = {
    STAGE_AUTH: '请重新登录该平台或更新 Cookie 后重试',
    STAGE_PARSING: '请确认链接有效、内容可公开访问且账号有权限；必要时更新解析组件',
    STAGE_DOWNLOADING: '请检查网络或代理后重试',
    STAGE_STORAGE: '请检查磁盘空间、目录权限或文件是否已被移动',
    STAGE_DELIVERY: '请检查机器人连接与目标会话；结果不确定时先核对平台消息记录',
    STAGE_REQUEST: '请按提示修正参数或配置后重试',
    STAGE_INTERNAL: '请查看服务日志并在必要时重试',
}

_RETRY_TEXT = re.compile(
    r'(?i)(timed?\s*out|timeout|connection\s*(reset|refused|aborted)|temporary|'
    r'try again|network|socket|dns|proxy|rate\s*limit|too many requests|service unavailable)'
)

# Absolute local paths are diagnostics, not user-facing detail. Strip them
# before any error text reaches a bot, an MCP client or a log.
_STOP = r'\s，；、。()\[\]{}<>|'
_WINDOWS_PATH = re.compile(r'(?<![\w:/])[A-Za-z]:\\(?:[^\\' + _STOP + r']+\\)*[^\\' + _STOP + r']*')
_UNC_PATH = re.compile(r'\\\\[^\\' + _STOP + r']+\\[^' + _STOP + r'\\]+')
_POSIX_PATH = re.compile(r'(?<![\w:])/(?:home|Users|tmp|var|opt|root|mnt|srv|app|data|private)(?:/[^/' + _STOP + r':]+)+')


def classify_error(code: str, message: str = '') -> dict:
    """Return the structured stage / retryability / next action for a code."""
    code = str(code or '').strip().upper()
    stage = _STAGE_BY_CODE.get(code, STAGE_INTERNAL)
    retryable = code in _RETRYABLE
    if not retryable and code not in _STAGE_BY_CODE and _RETRY_TEXT.search(str(message or '')):
        # Unknown code from the media engine, but the message names a
        # transient network condition: allow a bounded retry.
        retryable = True
        stage = STAGE_DOWNLOADING
    return {
        'code': code,
        'stage': stage,
        'retryable': bool(retryable),
        'suggested_action': _ACTIONS.get(stage, _ACTIONS[STAGE_INTERNAL]),
    }


class AgentError(Exception):
    def __init__(self, code, message, *, stage=None, retryable=None, suggested_action=None, **details):
        spec = classify_error(code, message)
        self.code, self.message = code, message
        self.stage = stage or spec['stage']
        self.retryable = spec['retryable'] if retryable is None else bool(retryable)
        self.suggested_action = suggested_action or spec['suggested_action']
        self.details = details
        super().__init__(message)

    def result(self):
        return {
            'ok': False,
            'error': {
                'code': self.code,
                'message': self.message,
                'stage': self.stage,
                'retryable': self.retryable,
                'suggested_action': self.suggested_action,
                **self.details,
            },
        }


def safe_text(value):
    # No URL (even with unfamiliar signed parameter names), header credential
    # or absolute local path from untrusted upstream errors/metadata is
    # returned to clients.
    text = re.sub(r'https?://\S+', '[URL]', str(value))
    text = re.sub(r'(?i)(cookie|authorization|token|secret|sessdata)\s*[:=][^\r\n]*', r'\1=[REDACTED]', text)
    text = _WINDOWS_PATH.sub('[PATH]', text)
    text = _UNC_PATH.sub('[PATH]', text)
    text = _POSIX_PATH.sub('[PATH]', text)
    return text[:500]
