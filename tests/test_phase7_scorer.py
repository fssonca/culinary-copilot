"""Scorer mutation tests (offline, synthetic run results, no DB).

Each test removes or tampers with one guard's evidence and asserts the
scorer fails the case. This proves the v2 checks bite instead of
passing on the stop reason alone.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "phase7_agent"))

from run import grade_case


def _result(
    *,
    stop: str,
    final: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
    error: bool = False,
) -> dict[str, Any]:
    last: dict[str, Any] = {"stop_reason": stop, "final": final}
    if error:
        last["error"] = True
    return {
        "runs": [last],
        "events": list(events or []),
        "epicure_outcome": None,
        "epicure_skip_reason": None,
    }


def _reject_event(errors: list[str]) -> dict[str, Any]:
    return {"type": "agent_validation_reject", "payload": {"errors": errors}}


def test_rejection_wrong_text_fails() -> None:
    case: dict[str, Any] = {
        "id": "synth-skip",
        "expected": {
            "stop_reason": "agent_validation_failed",
            "shape": "error",
            "expected_rejection": "needs Epicure consulted",
        },
        "required_tools": [],
        "allowed_tools": [],
        "forbidden_tools": [],
    }
    result = _result(
        stop="agent_validation_failed",
        final={},
        events=[_reject_event(["web ref 0: something else went wrong"])],
        error=True,
    )
    assert grade_case(case, result)["task_completion"] is False


def test_rejection_right_text_passes() -> None:
    case: dict[str, Any] = {
        "id": "synth-skip",
        "expected": {
            "stop_reason": "agent_validation_failed",
            "shape": "error",
            "expected_rejection": "needs Epicure consulted",
        },
        "required_tools": [],
        "allowed_tools": [],
        "forbidden_tools": [],
    }
    result = _result(
        stop="agent_validation_failed",
        final={},
        events=[_reject_event(["single option needs Epicure consulted in this session"])],
        error=True,
    )
    assert grade_case(case, result)["task_completion"] is True


def test_peanut_kept_fails() -> None:
    case: dict[str, Any] = {
        "id": "synth-allergen",
        "expected": {
            "stop_reason": "agent_sufficient_evidence",
            "shape": "options",
            "min_options": 1,
            "forbidden_option_ids": ["peanut-3"],
            "constraint_status": {"peanut-3": "violated"},
            "constraint_value": {"peanut-3": "peanut"},
            "dropped_with_reason": {
                "source_id": "peanut-3",
                "reason_contains": "peanut",
            },
        },
        "required_tools": [],
        "allowed_tools": [],
        "forbidden_tools": [],
    }
    final = {
        "options": [{"source_id": "peanut-3", "title": "Peanut Chicken"}],
        "constraint_check": [{"source_id": "peanut-3", "status": "violated", "value": "peanut"}],
        "dropped_options": [],
    }
    result = _result(stop="agent_sufficient_evidence", final=final)
    assert grade_case(case, result)["task_completion"] is False


def test_missing_constraint_check_fails() -> None:
    case: dict[str, Any] = {
        "id": "synth-resume",
        "expected": {
            "stop_reason": "agent_sufficient_evidence",
            "shape": "options",
            "min_options": 2,
            "allergen_checked": "peanut",
        },
        "required_tools": [],
        "allowed_tools": [],
        "forbidden_tools": [],
    }
    final = {
        "options": [{"source_id": "curry-1"}, {"source_id": "lentil-2"}],
        "constraint_check": [],
    }
    result = _result(stop="agent_sufficient_evidence", final=final)
    assert grade_case(case, result)["task_completion"] is False


def test_non_safety_ref_fails() -> None:
    case: dict[str, Any] = {
        "id": "synth-plan",
        "expected": {
            "stop_reason": "agent_sufficient_evidence",
            "shape": "plan",
            "needs_safety_ref": True,
        },
        "required_tools": [],
        "allowed_tools": [],
        "forbidden_tools": [],
    }
    final = {"plan": {"technique_refs": [{"doc_id": "tech-egg-boil-18", "chunk_id": 0}]}}
    result = _result(stop="agent_sufficient_evidence", final=final)
    assert grade_case(case, result)["task_completion"] is False


def test_safety_ref_passes() -> None:
    case: dict[str, Any] = {
        "id": "synth-plan",
        "expected": {
            "stop_reason": "agent_sufficient_evidence",
            "shape": "plan",
            "needs_safety_ref": True,
        },
        "required_tools": [],
        "allowed_tools": [],
        "forbidden_tools": [],
    }
    final = {"plan": {"technique_refs": [{"doc_id": "tech-fda-safe-32", "chunk_id": 0}]}}
    result = _result(stop="agent_sufficient_evidence", final=final)
    assert grade_case(case, result)["task_completion"] is True


def test_wrong_error_reason_fails() -> None:
    case: dict[str, Any] = {
        "id": "synth-timeout",
        "expected": {
            "stop_reason": "agent_sufficient_evidence",
            "shape": "options",
            "saw_error": "tool_timeout",
        },
        "required_tools": [],
        "allowed_tools": [],
        "forbidden_tools": [],
    }
    events = [
        {
            "type": "tool_call",
            "payload": {
                "tool": "search_recipes",
                "outcome": "error",
                "reason": "tool_invalid_arguments",
            },
        }
    ]
    final = {"options": [{"source_id": "curry-1"}]}
    result = _result(stop="agent_sufficient_evidence", final=final, events=events)
    assert grade_case(case, result)["task_completion"] is False
