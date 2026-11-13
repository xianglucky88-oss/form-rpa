"""结构化日志：所有输出经 SecretRedactor 脱敏，敏感值不进入普通日志。"""
from __future__ import annotations

import json
import logging
import sys

from .security import SecretRedactor


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": self.redactor.redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = self.redactor.redact(self.formatException(record.exc_info))
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = (self.redactor.redact_mapping(value)
                            if isinstance(value, dict) else self.redactor.redact(str(value)))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(redactor: SecretRedactor | None = None,
                      level: int = logging.INFO) -> SecretRedactor:
    redactor = redactor or SecretRedactor.from_env()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(redactor))
    root = logging.getLogger("form_rpa")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
    return redactor


def get_logger(name: str = "form_rpa") -> logging.Logger:
    return logging.getLogger(name if name.startswith("form_rpa") else f"form_rpa.{name}")
