"""Deterministic redaction for untrusted text sent to external model providers."""
import re


_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization|cookie|set-cookie|private[_-]?key)(?:$|[_-])",
    re.I,
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.I | re.S,
)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization|cookie|session[_-]?cookie)\s*([:=])\s*"
    r"([^\s,;，；]+)"
)
_URL_SECRET = re.compile(
    r"(?i)([?&](?:token|key|password|secret|access_token|refresh_token)=)"
    r"[^&#\s]+"
)


def redact_text(value):
    """Return text safe for an external model while preserving useful context."""
    if not isinstance(value, str) or not value:
        return value
    text = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", value)
    text = _BEARER.sub(lambda m: f"{m.group(1)} [REDACTED]", text)
    text = _JWT.sub("[REDACTED_JWT]", text)
    text = _URL_SECRET.sub(r"\1[REDACTED]", text)
    text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    return text


def redact_payload(value, *, key=None):
    """Recursively redact mappings/lists used as model request payloads."""
    if key is not None and _SENSITIVE_KEY.search(str(key)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {name: redact_payload(item, key=name) for name, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    return redact_text(value)


def redaction_changed(value):
    return redact_payload(value) != value

