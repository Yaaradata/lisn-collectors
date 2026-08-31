"""Redact credentials from exception and log text before they are stored or returned.

Write-time only: secrets must never land in collector_job.last_error (or any
persisted field). Read-time scrubbing would leave them in the database.
"""

from __future__ import annotations

import re
from typing import overload

# postgresql://user:secret@host  (also postgres:// and postgresql+driver://)
_URI_PASSWORD = re.compile(
    r"((?:postgresql(?:\+\w+)?|postgres)://[^:/?#\s]+:)([^@/\s]+)(@)",
    re.IGNORECASE,
)

# libpq key/value: password=secret  (stops at whitespace)
_KV_PASSWORD = re.compile(
    r"(password\s*=\s*)(\S+)",
    re.IGNORECASE,
)

_BEARER = re.compile(
    r"(Authorization:\s*Bearer\s+)\S+",
    re.IGNORECASE,
)

# api_key=... / apikey: ... / access_token=...
_API_KEY = re.compile(
    r"((?:api[_-]?key|access[_-]?token)\s*[=:]\s*)\S+",
    re.IGNORECASE,
)


@overload
def redact_secrets(text: None) -> None: ...


@overload
def redact_secrets(text: str) -> str: ...


def redact_secrets(text: str | None) -> str | None:
    """Return *text* with DSN passwords, bearer tokens, and API keys scrubbed.

    Safe on ``None`` and on text with no match — returns the input unchanged
    (same object for ``None`` / no-match ``str``).
    """
    if text is None:
        return None
    out = _URI_PASSWORD.sub(r"\1***\3", text)
    out = _KV_PASSWORD.sub(r"\1***", out)
    out = _BEARER.sub(r"\1***", out)
    out = _API_KEY.sub(r"\1***", out)
    return out
