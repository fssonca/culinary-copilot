"""Query/log minimization (Phase 5, part 2, owner decision 8).

Narrow heuristics, not a complete privacy control: email, phone, street
address, and "my <Name>" patterns are replaced before provider
submission AND before storage. Runs on: the query, tool args (including
the record_tool_args trajectory path), errors, URLs (query strings
stripped by default), summaries, exports, and live-runner raw files.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+\d[\d\s().-]{7,}\d|\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b|\(\d{3}\)\s*\d{3}[-.\s]?\d{4})"
)
_STREET_SUFFIX = (
    r"street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|"
    r"circle|cir|place|pl|terrace|ter|way|plaza|parkway|pkwy"
)
_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.'-]{2,}(?:\s+[A-Za-z0-9.'-]{2,}){0,3}\s+"
    rf"(?:{_STREET_SUFFIX})\b\.?",
    re.IGNORECASE,
)
_MY_NAME_RE = re.compile(r"\bmy\s+([A-Z][a-zA-Z'-]{1,40})(?:\s+([A-Z][a-zA-Z'-]{1,40}))?")


def _scrub_text(text: str) -> str:
    out = _EMAIL_RE.sub("[redacted-email]", text)
    out = _PHONE_RE.sub("[redacted-phone]", out)
    out = _ADDRESS_RE.sub("[redacted-address]", out)

    def _name(match: re.Match[str]) -> str:
        second = match.group(2)
        return "my [redacted-name]" if second else "my [redacted-name]"

    return _MY_NAME_RE.sub(_name, out)


def minimize_query(query: str) -> str:
    """Minimized query: scrubbed, bounded to 500 chars (model contract)."""
    return _scrub_text(str(query or ""))[:500]


def minimize_summary(text: str, limit: int = 1000) -> str:
    """Minimized summary text (bounded, scrubbed)."""
    return _scrub_text(str(text or ""))[:limit]


def minimize_error(text: str, limit: int = 500) -> str:
    """Minimized error text (bounded, scrubbed, no stack traces)."""
    first_line = str(text or "").splitlines()[0] if str(text or "") else ""
    return _scrub_text(first_line)[:limit]


def minimize_url(url: str) -> str:
    """Minimized URL: scrubbed, query string and fragment stripped by default."""
    raw = _scrub_text(str(url or ""))
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return raw[:500]
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "", "", ""))[:500]
    except ValueError:
        return raw[:500]


def minimize_tool_args(args: dict[str, Any], limit: int = 2000) -> str:
    """Minimized JSON for the record_tool_args trajectory path (valid JSON)."""
    import json as _json

    scrubbed: dict[str, Any] = {}
    for key in sorted(args):
        value = args[key]
        if isinstance(value, str):
            scrubbed[key] = _scrub_text(value)[:1000]
        elif isinstance(value, list):
            scrubbed[key] = [
                (_scrub_text(v)[:500] if isinstance(v, str) else v) for v in value[:20]
            ]
        else:
            scrubbed[key] = value
    try:
        text = _json.dumps(scrubbed, default=str)
    except (TypeError, ValueError):
        return '{"_unserializable": true}'
    if len(text) <= limit:
        return text
    return _json.dumps({"_truncated": True})


__all__ = [
    "minimize_error",
    "minimize_query",
    "minimize_summary",
    "minimize_tool_args",
    "minimize_url",
]
