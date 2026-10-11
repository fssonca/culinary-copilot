"""Start-up session-limit report (D0, owner demo 2026-10-09)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from culinary_copilot.api.app import log_session_limits
from culinary_copilot.config import DEMO_SESSION_LIMITS, Settings, session_limit_report

MAKEFILE = Path(__file__).resolve().parents[1] / "Makefile"
MAKE_VARS = {
    "session_max_steps": "DEMO_STEPS",
    "session_max_tool_calls": "DEMO_TOOL_CALLS",
    "agent_input_token_ceiling": "DEMO_INPUT_TOKENS",
    "agent_output_token_ceiling": "DEMO_OUTPUT_TOKENS",
    "agent_wall_clock_s": "DEMO_WALL_CLOCK_S",
}


def _demo_settings(**overrides: float) -> Settings:
    values = dict(DEMO_SESSION_LIMITS)
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_demo_limits_match_the_makefile() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    for name, var in MAKE_VARS.items():
        found = re.search(rf"^{var}\s*\?=\s*(\d+)\s*$", text, re.MULTILINE)
        assert found is not None, var
        assert float(found.group(1)) == DEMO_SESSION_LIMITS[name], var


def test_demo_limits_report_nothing_above() -> None:
    summary, above = session_limit_report(_demo_settings())
    assert above == []
    assert "session_max_steps=40" in summary


def test_unlimited_server_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    # The demo-day server: limits far above make demo.
    settings = _demo_settings(session_max_steps=1_000_000, agent_input_token_ceiling=100_000_000)
    with caplog.at_level(logging.INFO, logger="culinary_copilot.api.app"):
        above = log_session_limits(settings)
    assert above == [
        "session_max_steps=1000000 (make demo: 40)",
        "agent_input_token_ceiling=100000000 (make demo: 300000)",
    ]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "make demo" in warnings[0].getMessage()
