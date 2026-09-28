"""Typed tool errors (Milestone 3, Phase 2).

Tool failures are data, never exceptions into the caller. Every error
carries a stable ``reason`` mapped in
``domain/recommendations.py::_NEXT_ACTION_BY_REASON`` so clients get a
``next_action`` without parsing the message. Reason constants live in
``domain/recommendations.py`` (``REASON_TOOL_*``, ``REASON_SCALE_*``,
``REASON_CONVERT_*``); this module re-exports the error-type literals
used in tool result schemas.
"""

from __future__ import annotations

from typing import Literal

ToolErrorType = Literal["timeout", "invalid_arguments", "unavailable", "permission_denied"]

TIMEOUT: ToolErrorType = "timeout"
INVALID_ARGUMENTS: ToolErrorType = "invalid_arguments"
UNAVAILABLE: ToolErrorType = "unavailable"
PERMISSION_DENIED: ToolErrorType = "permission_denied"

__all__ = [
    "ToolErrorType",
    "TIMEOUT",
    "INVALID_ARGUMENTS",
    "UNAVAILABLE",
    "PERMISSION_DENIED",
]
