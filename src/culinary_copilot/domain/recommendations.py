"""Typed contracts for source-grounded recommendations (Phase 3).

Selection, not recipe rewriting: the model selects one stored source recipe
and references its ingredients/steps with stable source-local references.
Python assembles all factual recipe content from the stored source document.
The model must not supply replacement quantities or rewritten cooking
instructions: the proposal schemas below forbid them (``extra="forbid"``),
so any smuggled quantities/rewrites fail schema validation (502) instead of
reaching the rendered response.

Business outcomes (HTTP 200):
- ``recommendation``: one validated, server-rendered source recipe.
- ``clarification``: conflicting/blocked/not-ready state, or a hard
  constraint the user can resolve; no recipe content.
- ``insufficient_evidence``: no complete, constraint-compatible source
  available; may carry explicitly unverified source-linked discovery
  candidates (never presented as verified recipes).

Errors (no recommendation content):
- 422 malformed input; 404 unknown group; 409 stale/superseded revisions
  (including edits that land mid-execution); 503 disabled generation or
  unavailable service/provider; 504 provider timeout; 502 invalid/incomplete
  provider output or rejected proposal, with distinct stable reason codes.
- Provider refusal is a controlled failed outcome with reason
  ``provider_refusal`` mapped to HTTP 502. It is never mislabeled as
  insufficient evidence (which is a successful 200 outcome describing the
  corpus, not the provider).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RecommendationOutcome(str, Enum):
    RECOMMENDATION = "recommendation"
    CLARIFICATION = "clarification"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ConstraintVerdict(str, Enum):
    SUPPORTED = "supported"
    VIOLATED = "violated"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not_applicable"


class EpicureOutcome(str, Enum):
    CONSULTED = "consulted"
    SIMPLE_TECHNIQUE_SKIP = "simple_technique_skip"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    UNMAPPED = "unmapped"
    INSUFFICIENT_INGREDIENT_CONTEXT = "insufficient_ingredient_context"


# Stable reason codes for insufficient_evidence outcomes (200).
INSUFFICIENT_NO_CANDIDATES = "no_candidates"
INSUFFICIENT_BUDGET_EXCEEDED = "evidence_budget_exceeded"
INSUFFICIENT_INCOMPLETE_ONLY = "incomplete_source_only"
INSUFFICIENT_HARD_CONSTRAINT = "hard_constraint_unresolved"
INSUFFICIENT_SOURCE_FETCH = "source_fetch_unavailable"

# Stable reason codes for failed 502 responses (error.detail.reason).
REASON_SCHEMA_FAILURE = "schema_failure"
REASON_VALIDATION_REJECTED = "validation_rejected"
REASON_TRUNCATED = "truncated_incomplete_response"
REASON_EMPTY = "empty_response"
REASON_PROVIDER_REFUSAL = "provider_refusal"
REASON_TURN_LIMIT = "turn_limit_exceeded"
REASON_INVALID_TOOL = "invalid_tool_call"
REASON_BAD_REQUEST = "provider_bad_request"
REASON_REQUEST_ERROR = "provider_request_error"
REASON_CONTENT_FILTER = "provider_content_filter"
REASON_INTERNAL_ERROR = "provider_internal_error"
# Stable reason codes for failed 503 responses (error.detail.reason).
REASON_UNAVAILABLE = "provider_unavailable"
REASON_RATE_LIMITED = "provider_rate_limited"
REASON_NOT_FOUND = "provider_not_found"

# Stable validation rejection sub-codes (error.detail.validation_reason).
REJECT_UNKNOWN_IDENTITY = "unknown_identity"
REJECT_BAD_REFERENCE = "bad_reference"
REJECT_STEP_COVERAGE = "step_coverage"
REJECT_INCOMPLETE_SOURCE = "incomplete_source"
REJECT_HARD_CONSTRAINT = "hard_constraint_violation"
REJECT_INJECTION = "prompt_injection_detected"
# Tool mode: the refetched source differs from the snapshot the request
# validated and would render (fail closed, never validate one version and
# render another).
REJECT_EVIDENCE_CHANGED = "evidence_changed"

# Failed 502: the complete serialized request for a turn cannot fit the
# input budget. Raised before that turn is sent (earlier turns' usage kept).
REASON_BUDGET_EXCEEDED = "input_budget_exceeded"

# Stable reason codes for agent sessions (Milestone 3, Phase 1). Session
# errors reuse the same ``next_action`` contract as recommendations so a
# client knows what to do without parsing the message.
REASON_UNKNOWN_SESSION = "unknown_session"
REASON_INVALID_PHASE_TRANSITION = "invalid_phase_transition"
REASON_SESSION_UNAVAILABLE = "session_unavailable"
REASON_SESSION_NOT_MIGRATED = "session_store_not_migrated"

# Stable reason codes for the typed tool layer (Milestone 3, Phase 2,
# reviewed: "unavailable" split into permanent vs transient).
# Tool failures are typed error results, never exceptions into the caller.
# Every code below is mapped in ``_NEXT_ACTION_BY_REASON``, and
# tests/test_next_action.py scans domain/recommendations.py plus
# src/culinary_copilot/tools/*.py so a new tool reason without a mapping fails.
# ``error_type`` stays "unavailable" for both not-configured and transient
# unavailable (schema); the ``reason`` carries the distinction.
REASON_TOOL_TIMEOUT = "tool_timeout"
REASON_TOOL_INVALID_ARGUMENTS = "tool_invalid_arguments"
REASON_TOOL_NOT_CONFIGURED = "tool_not_configured"
REASON_TOOL_UNAVAILABLE = "tool_unavailable"
REASON_TOOL_INTERNAL_ERROR = "tool_internal_error"
REASON_TOOL_PERMISSION_DENIED = "tool_permission_denied"
REASON_SCALE_MISSING_SERVINGS = "scale_missing_servings"
REASON_CONVERT_UNSUPPORTED_UNIT = "convert_unsupported_unit"
# Permission-gated web search (Milestone 3, Phase 5, part 2, owner items
# 2–3): per-session slot exhaustion and provider-did-not-search. The
# scanner in tests/test_next_action.py requires a mapping for each.
REASON_SEARCH_BUDGET_EXHAUSTED = "search_budget_exhausted"
REASON_SEARCH_NOT_PERFORMED = "search_not_performed"

# Stable stop reasons for the bounded agent loop (Milestone 3, Phase 3,
# reviewed). Step, tool-call and token budgets are per session and never
# reset, so exhausting one is ``change_request`` (start a new session);
# the wall clock is per run, so it stays ``retry``. Finals
# (sufficient_evidence, needs_user_input) are terminal success codes,
# not errors: they are intentionally NOT in ``_NEXT_ACTION_BY_REASON``
# (see the scanner's documented exclusion set), and only error
# terminals emit ``next_action`` to clients.
REASON_AGENT_MAX_STEPS = "agent_max_steps"
REASON_AGENT_TOOL_BUDGET = "agent_tool_budget_exhausted"
REASON_AGENT_TOKEN_BUDGET = "agent_token_budget_exhausted"
REASON_AGENT_WALL_CLOCK = "agent_wall_clock_exceeded"
REASON_AGENT_SUFFICIENT = "agent_sufficient_evidence"
REASON_AGENT_NEEDS_INPUT = "agent_needs_user_input"
REASON_AGENT_NO_PROGRESS = "agent_no_progress"
REASON_AGENT_VALIDATION_FAILED = "agent_validation_failed"
REASON_UNKNOWN_QUESTION = "unknown_question"
REASON_UNKNOWN_OPTION = "unknown_option"
REASON_HISTORY_PAIRING = "history_pairing_error"

# What a client should do after a failure (P4-REV-01). Messages state the
# failure; ``next_action`` states the remedy, so a retry is never suggested
# where it cannot succeed.
NEXT_RETRY = "retry"  # transient or model-dependent; costs a new model call
NEXT_REFETCH_AND_RETRY = "refetch_and_retry"  # request changed; reload first
NEXT_CHANGE_REQUEST = "change_request"  # the same request fails the same way
NEXT_CONTACT_OPERATOR = "contact_operator"  # server configuration must change
NEXT_ACTIONS = (NEXT_RETRY, NEXT_REFETCH_AND_RETRY, NEXT_CHANGE_REQUEST, NEXT_CONTACT_OPERATOR)

_NEXT_ACTION_BY_REASON: dict[str, str] = {
    # Model output problems: another attempt can succeed.
    REASON_SCHEMA_FAILURE: NEXT_RETRY,
    REASON_VALIDATION_REJECTED: NEXT_RETRY,  # except hard constraints, below
    REASON_TRUNCATED: NEXT_RETRY,
    REASON_EMPTY: NEXT_RETRY,
    REASON_TURN_LIMIT: NEXT_RETRY,
    REASON_INVALID_TOOL: NEXT_RETRY,
    # Transient provider or infrastructure failures.
    REASON_UNAVAILABLE: NEXT_RETRY,
    REASON_RATE_LIMITED: NEXT_RETRY,
    "provider_timeout": NEXT_RETRY,
    "corpus_unavailable": NEXT_RETRY,
    "stream_duration_exceeded": NEXT_RETRY,
    # The request itself has to change.
    REASON_PROVIDER_REFUSAL: NEXT_CHANGE_REQUEST,
    REASON_CONTENT_FILTER: NEXT_CHANGE_REQUEST,
    REASON_BUDGET_EXCEEDED: NEXT_CHANGE_REQUEST,
    "unknown_group": NEXT_CHANGE_REQUEST,
    REASON_UNKNOWN_SESSION: NEXT_CHANGE_REQUEST,
    REASON_INVALID_PHASE_TRANSITION: NEXT_CHANGE_REQUEST,
    REASON_SESSION_UNAVAILABLE: NEXT_RETRY,
    REASON_SESSION_NOT_MIGRATED: NEXT_CONTACT_OPERATOR,
    REASON_TOOL_TIMEOUT: NEXT_RETRY,
    REASON_TOOL_INVALID_ARGUMENTS: NEXT_CHANGE_REQUEST,
    REASON_TOOL_NOT_CONFIGURED: NEXT_CONTACT_OPERATOR,
    REASON_TOOL_UNAVAILABLE: NEXT_RETRY,
    REASON_TOOL_INTERNAL_ERROR: NEXT_CONTACT_OPERATOR,
    REASON_TOOL_PERMISSION_DENIED: NEXT_CHANGE_REQUEST,
    REASON_SCALE_MISSING_SERVINGS: NEXT_CHANGE_REQUEST,
    REASON_CONVERT_UNSUPPORTED_UNIT: NEXT_CHANGE_REQUEST,
    REASON_SEARCH_BUDGET_EXHAUSTED: NEXT_CHANGE_REQUEST,
    REASON_SEARCH_NOT_PERFORMED: NEXT_RETRY,
    REASON_AGENT_MAX_STEPS: NEXT_CHANGE_REQUEST,
    REASON_AGENT_TOOL_BUDGET: NEXT_CHANGE_REQUEST,
    REASON_AGENT_TOKEN_BUDGET: NEXT_CHANGE_REQUEST,
    REASON_AGENT_WALL_CLOCK: NEXT_RETRY,
    REASON_AGENT_NO_PROGRESS: NEXT_CHANGE_REQUEST,
    REASON_AGENT_VALIDATION_FAILED: NEXT_CHANGE_REQUEST,
    REASON_UNKNOWN_QUESTION: NEXT_CHANGE_REQUEST,
    REASON_UNKNOWN_OPTION: NEXT_CHANGE_REQUEST,
    REASON_HISTORY_PAIRING: NEXT_RETRY,
    "malformed": NEXT_CHANGE_REQUEST,
    # State moved on while running.
    "stale_revision": NEXT_REFETCH_AND_RETRY,
    # Server configuration or code: retrying cannot help. Provider 4xx
    # other than rate limits are never retried by the service either.
    REASON_BAD_REQUEST: NEXT_CONTACT_OPERATOR,
    REASON_REQUEST_ERROR: NEXT_CONTACT_OPERATOR,
    REASON_INTERNAL_ERROR: NEXT_CONTACT_OPERATOR,  # local failure, not provider
    REASON_NOT_FOUND: NEXT_CONTACT_OPERATOR,
    "provider_auth": NEXT_CONTACT_OPERATOR,
    "generation_disabled": NEXT_CONTACT_OPERATOR,
    "stream_event_limit_exceeded": NEXT_CONTACT_OPERATOR,
    "internal_error": NEXT_CONTACT_OPERATOR,
}


def next_action_for(reason: str, detail: dict[str, Any] | None = None) -> str:
    """Stable client remedy for a failure ``reason`` (see ``NEXT_ACTIONS``).

    A hard-constraint rejection is ``change_request``: the constraint's
    evidence is missing from the sources, so repeating the request fails
    again. Unmapped reasons fall back to ``contact_operator``.
    """
    if (
        reason == REASON_VALIDATION_REJECTED
        and (detail or {}).get("validation_reason") == REJECT_HARD_CONSTRAINT
    ):
        return NEXT_CHANGE_REQUEST
    return _NEXT_ACTION_BY_REASON.get(reason, NEXT_CONTACT_OPERATOR)


# Typed proposition allowlist (replaces free-text selection_reasons/needs).
# The model proposes only a type (plus bounded ingredient refs where the
# type needs them); the server checks each type's prerequisites against
# authoritative request state and the selected source and renders the
# wording itself. See recommendations/propositions.py and
# docs/recommendations.md for prerequisites and exact wording.
ReasonType = Literal[
    "dish_named_in_title",
    "uses_listed_ingredients",
    "reported_time_within_limit",
    "stated_yield_matches_portions",
]
QuestionType = Literal[
    "desired_portions",
    "time_available",
    "dietary_restrictions",
    "available_ingredients",
]
MAX_PROPOSITIONS = 6
MAX_PROPOSITION_REFS = 6
# Source-local ingredient reference only; any other text fails the schema.
IngredientRef = Annotated[str, Field(pattern=r"^ing-[0-9]{1,4}$")]


class ReasonProposal(BaseModel):
    """One typed selection reason; the server renders its wording."""

    model_config = ConfigDict(extra="forbid")

    type: ReasonType
    ingredient_refs: list[IngredientRef] = Field(
        default_factory=list, max_length=MAX_PROPOSITION_REFS
    )


class QuestionProposal(BaseModel):
    """One typed follow-up question for the user; the server renders it."""

    model_config = ConfigDict(extra="forbid")

    type: QuestionType


class SelectionProposal(BaseModel):
    """Model selection output: identity + source-local refs + typed propositions.

    No recipe-content fields and no free-text fields exist here by design.
    Extra fields (quantities, rewritten steps, constraint verdicts, free-text
    reasons) are forbidden and fail validation. ``reasons`` and
    ``questions`` carry allowlisted types only; the server validates each
    proposition's prerequisites and renders the public wording, omitting
    (and recording) propositions whose prerequisites fail.
    """

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    ingredient_refs: list[str] = Field(default_factory=list, max_length=100)
    step_refs: list[str] = Field(default_factory=list, max_length=200)
    reasons: list[ReasonProposal] = Field(default_factory=list, max_length=MAX_PROPOSITIONS)
    questions: list[QuestionProposal] = Field(default_factory=list, max_length=MAX_PROPOSITIONS)


class RecommendationRequest(BaseModel):
    """Revision-pinned recommendation over one clarification group."""

    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1, max_length=100)
    request_revision: int = Field(ge=1)
    group_revision: int = Field(ge=1)
    limit: int = Field(default=3, ge=1, le=5)
    dataset_id: str | None = Field(default=None, max_length=200)
    tool_mode: bool = False


class ConstraintAssessment(BaseModel):
    """Server-computed constraint verdict (never model-asserted)."""

    model_config = ConfigDict(extra="forbid")

    constraint: str = Field(min_length=1, max_length=100)
    verdict: ConstraintVerdict = ConstraintVerdict.UNRESOLVED
    detail: str = Field(default="", max_length=500)


def error_body(
    *, code: str, reason: str, message: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Stable failed-response envelope (no prompts, secrets, or raw model output)."""
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "reason": reason,
            "message": message,
        }
    }
    if extra:
        body["error"]["detail"] = extra
    return body


def outcome_for_http_status(outcome: RecommendationOutcome) -> Literal[200]:
    assert outcome in (
        RecommendationOutcome.RECOMMENDATION,
        RecommendationOutcome.CLARIFICATION,
        RecommendationOutcome.INSUFFICIENT_EVIDENCE,
    )
    return 200
