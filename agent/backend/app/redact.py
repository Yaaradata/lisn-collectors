"""Credential redaction for tool outputs (DSN passwords, tokens, API keys).

Copied into the agent package so tools never import collector/. Apply to every
string that could carry an exception message — especially last_error.
"""

from __future__ import annotations

import os
import re
from typing import Any, overload

_URI_PASSWORD = re.compile(
    r"((?:postgresql(?:\+\w+)?|postgres)://[^:/?#\s]+:)([^@/\s]+)(@)",
    re.IGNORECASE,
)
_KV_PASSWORD = re.compile(r"(password\s*=\s*)(\S+)", re.IGNORECASE)
_BEARER = re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE)
_API_KEY = re.compile(
    r"((?:api[_-]?key|access[_-]?token)\s*[=:]\s*)\S+",
    re.IGNORECASE,
)
_SIGNOZ_HEADER = re.compile(
    r"(signoz-ingestion-key\s*[=:]\s*)\S+",
    re.IGNORECASE,
)


@overload
def redact_secrets(text: None) -> None: ...


@overload
def redact_secrets(text: str) -> str: ...


def redact_secrets(text: str | None) -> str | None:
    if text is None:
        return None
    out = _URI_PASSWORD.sub(r"\1***\3", text)
    out = _KV_PASSWORD.sub(r"\1***", out)
    out = _BEARER.sub(r"\1***", out)
    out = _API_KEY.sub(r"\1***", out)
    out = _SIGNOZ_HEADER.sub(r"\1***", out)
    key = os.environ.get("SIGNOZ_INGESTION_KEY", "").strip()
    if key and key in out:
        out = out.replace(key, "***")
    api = os.environ.get("SIGNOZ_API_KEY", "").strip()
    if api and api in out:
        out = out.replace(api, "***")
    return out


def redact_tree(value: Any) -> Any:
    """Walk lists/dicts and scrub every string leaf."""
    if value is None:
        return None
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [redact_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_tree(v) for k, v in value.items()}
    return value
