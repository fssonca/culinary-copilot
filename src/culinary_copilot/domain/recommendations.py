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
