"""Agent session state and cooking phases (Milestone 3, Phase 1).

Sessions live in Postgres (migration 005). The in-memory clarification store
stays for the existing clarification, retrieval and recommendation endpoints;
sessions link to clarification requests/groups by ID rather than copying
their state (see docs/sessions.md for the migration path).

Phases are data, not control flow: ``ALLOWED_TRANSITIONS`` lists the legal
moves. A same-phase update is always allowed (no transition). Any other move
not listed is rejected with the stable reason ``invalid_phase_transition``.

Direct recipe or technique requests may skip ``select``: ``recommend -> plan``
is an explicit allowed transition alongside the normal ``recommend -> select``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Stable reason codes for session failures. Every code below is mapped in
# domain/recommendations.py::_NEXT_ACTION_BY_REASON, and
# tests/test_next_action.py scans both domain/sessions.py and
# api/sessions.py so a new session reason without a mapping fails.
SESSION_NOT_FOUND_REASON = "unknown_session"
SESSION_STALE_REASON = "stale_revision"
SESSION_INVALID_TRANSITION_REASON = "invalid_phase_transition"

# Defaults mirror the Checkpoint 0 budgets (owner decision 2026-09-28):
# 12 tool calls per session; MAX_STEPS raised 8 -> 12 by the owner on
# 2026-10-04 (P3-L-12). Internet search is off by default
# and enforced in the backend (Phase 5 wires the tool; Phase 1 only stores
# the permission).
DEFAULT_TOOL_CALLS_REMAINING = 12
DEFAULT_STEPS_REMAINING = 12
DEFAULT_PHASE = "discover"


class SessionPhase(str, Enum):
    DISCOVER = "discover"
    CLARIFY = "clarify"
    RESEARCH = "research"
    RECOMMEND = "recommend"
    SELECT = "select"
    PLAN = "plan"
    COOK = "cook"
    PLATE = "plate"


# Allowed phase moves as data. Linear cooking flow
# discover -> clarify -> research -> recommend -> select -> plan -> cook -> plate
# plus the documented shortcuts and bounded back-edges for re-work.
# recommend -> clarify lets a hard-constraint rejection return to change
# constraints; recommend -> plan skips select for direct requests.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "discover": frozenset({"clarify", "research", "recommend"}),
    "clarify": frozenset({"research", "recommend", "discover"}),
    "research": frozenset({"recommend", "clarify"}),
    # Direct recipe or technique requests may skip select.
    "recommend": frozenset({"select", "plan", "clarify"}),
    "select": frozenset({"plan", "recommend"}),
    # A different dish asked for after a plan starts new options (owner
    # demo 2026-10-09: "an air fryer pizza" after an adobo plan failed).
    "plan": frozenset({"cook", "select", "recommend"}),
    "cook": frozenset({"plate", "plan"}),
    "plate": frozenset(),
}


def can_transition(from_phase: str, to_phase: str) -> bool:
    """True when ``from_phase -> to_phase`` is allowed (same phase always ok)."""
    if from_phase == to_phase:
        return True
    return to_phase in ALLOWED_TRANSITIONS.get(from_phase, frozenset())


def validate_transition(from_phase: str, to_phase: str) -> None:
    """Raise ValueError with the stable reason when the move is not allowed."""
    if not can_transition(from_phase, to_phase):
        raise ValueError(
            f"{SESSION_INVALID_TRANSITION_REASON}: "
            f"{from_phase!r} -> {to_phase!r} is not an allowed phase transition"
        )


class SessionState(BaseModel):
    """Persisted agent session (mirrors the ``sessions`` table)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    revision: int = Field(default=1, ge=1)
    current_phase: str = Field(default=DEFAULT_PHASE, min_length=1, max_length=50)
    clarification_request_id: str | None = Field(default=None, max_length=100)
    clarification_group_id: str | None = Field(default=None, max_length=100)
    constraints: dict[str, Any] = Field(default_factory=dict)
    confirmed_answers: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    unresolved_questions: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    epicure_outcome: str | None = Field(default=None, max_length=100)
    epicure_skip_reason: str | None = Field(default=None, max_length=500)
    suggestions: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    selected_dish: dict[str, Any] | None = None
    cooking_plan: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    internet_search_allowed: bool = False
    tool_calls_remaining: int = Field(default=DEFAULT_TOOL_CALLS_REMAINING, ge=0)
    steps_remaining: int = Field(default=DEFAULT_STEPS_REMAINING, ge=0)


class SessionEvent(BaseModel):
    """One append-only session event (mirrors ``session_events``)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=100)
    seq: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


def _answer_key(answer: dict[str, Any]) -> Any:
    """Stable identity for a confirmed answer (question id when present)."""
    for key in ("question_id", "id", "semantic_key"):
        value = answer.get(key)
        if isinstance(value, str) and value.strip():
            return (key, value.strip())
    return ("__full__", repr(sorted(answer.items(), key=lambda kv: kv[0])))


def merge_confirmed_answers(
    stored: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Union stored + incoming confirmed answers; stored answers are never lost.

    Append-only: every stored answer survives, keyed by question id when
    available. An incoming answer with the same key replaces the stored one
    (an explicit correction); otherwise incoming answers are appended.
    Never drops: when the union would exceed the 200-answer cap it raises
    ValueError (surfaced as 422 ``malformed``) instead of truncating.
    """
    merged: list[dict[str, Any]] = [dict(a) for a in stored]
    index: dict[Any, int] = {_answer_key(a): i for i, a in enumerate(merged)}
    for answer in incoming:
        key = _answer_key(answer)
        if key in index:
            merged[index[key]] = dict(answer)
        else:
            merged.append(dict(answer))
            index[key] = len(merged) - 1
    if len(merged) > 200:
        raise ValueError("malformed: confirmed answer cap (200) exceeded; nothing was dropped")
    return merged
