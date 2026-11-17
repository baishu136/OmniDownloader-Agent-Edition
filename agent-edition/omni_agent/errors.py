import re


class AgentError(Exception):
    def __init__(self, code, message, **details):
        self.code, self.message, self.details = code, message, details
        super().__init__(message)

    def result(self):
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.details}}


def safe_text(value):
    # No URL (even with unfamiliar signed parameter names) or header credential
    # from untrusted upstream errors/metadata is returned to clients.
    text = re.sub(r'https?://\S+', '[URL]', str(value))
    text = re.sub(r'(?i)(cookie|authorization|token|secret|sessdata)\s*[:=][^\r\n]*', r'\1=[REDACTED]', text)
    return text[:500]
