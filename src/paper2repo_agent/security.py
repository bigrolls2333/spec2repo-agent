from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    re.compile(r"(Authorization\s*:\s*Bearer\s+)[^\s]+", re.IGNORECASE),
]

SECRET_FIELD_HINTS = ("key", "token", "secret", "authorization", "password")


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        if "Authorization" in pattern.pattern:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if any(hint in key.lower() for hint in SECRET_FIELD_HINTS):
        return "[REDACTED]" if value else value
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, Mapping):
            safe[key] = redact_mapping(value)
        elif isinstance(value, list):
            safe[key] = [
                redact_mapping(item) if isinstance(item, Mapping) else redact_value(key, item)
                for item in value
            ]
        else:
            safe[key] = redact_value(key, value)
    return safe
