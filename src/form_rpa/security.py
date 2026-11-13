"""敏感值脱敏：保证账号、口令、令牌等不进入普通日志与事件记录。"""
from __future__ import annotations

import os
import re
from typing import Iterable

REDACTION = "***REDACTED***"

# 命中这些名字的字段/环境变量，其值一律脱敏
SENSITIVE_NAMES = {
    "password", "passwd", "pwd", "token", "secret", "credential",
    "credentials", "authorization", "cookie", "session", "api_key",
    "apikey", "access_key", "rpa_password", "rpa_token",
}

# 常见携带敏感值的文本模式（header、key=value、URL 内嵌口令等）
_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic)\s+[^\s,;]+"),
    re.compile(r"(?i)((?:password|passwd|pwd|token|secret|cookie|api[_-]?key)"
               r"\s*[:=]\s*)([^\s,;\"']+)"),
    re.compile(r"(?i)(https?://[^/\s:@]+:)([^@/\s]+)(@)"),
)


class SecretRedactor:
    """登记已知敏感值并对文本做替换；未登记时也能按通用模式兜底。"""

    def __init__(self, secrets: Iterable[str] | None = None) -> None:
        self._secrets: list[str] = []
        for value in secrets or ():
            self.register(value)

    @classmethod
    def from_env(cls, extra_names: Iterable[str] = ()) -> "SecretRedactor":
        names = SENSITIVE_NAMES | {n.lower() for n in extra_names}
        values = [v for k, v in os.environ.items() if k.lower() in names and v]
        return cls(values)

    def register(self, secret: str | None) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def redact(self, text: str | None) -> str:
        if not text:
            return text or ""
        out = str(text)
        for secret in self._secrets:
            if secret and secret in out:
                out = out.replace(secret, REDACTION)
        for pattern in _PATTERNS:
            if pattern.groups == 3:
                out = pattern.sub(lambda m: m.group(1) + REDACTION + (m.group(3) if m.lastindex == 3 else ""), out)
            else:
                out = pattern.sub(lambda m: m.group(1) + REDACTION, out)
        return out

    def redact_mapping(self, data: dict) -> dict:
        clean = {}
        for key, value in data.items():
            if str(key).lower() in SENSITIVE_NAMES:
                clean[key] = REDACTION
            elif isinstance(value, dict):
                clean[key] = self.redact_mapping(value)
            elif isinstance(value, str):
                clean[key] = self.redact(value)
            else:
                clean[key] = value
        return clean
