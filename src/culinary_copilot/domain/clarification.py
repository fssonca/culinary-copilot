"""Typed contracts for hybrid clarification and question planning.

Backend-only: no frontend work. A future UI presents one question group one
question at a time; the API returns the ordered group plus revisions.

Design notes (also documented in docs/clarification.md):

- ``CookingRequest`` (domain/requests.py) is preserved unchanged. Clarification
  state wraps it in :class:`CookingRequestState` so structured answers, history
  and per-field status travel together without mutating the original contract.
- Field status distinguishes unknown / provided / no_preference /
  skipped / conflicting. An empty list is NOT proof of "no restrictions":
  ``dietary_constraints=[]`` with status ``unknown`` means unasked, while
  status ``no_preference`` means the user explicitly reported no restrictions.
  Silence never implies a dietary restriction.
- "Ready for retrieval" means the request has enough search information for
  recipe discovery. It does NOT mean safe for generation, nutritionally
  verified, complete, or scalable.
- Dependencies are validated data (question id + required option ids), never
  executable expressions.
- User text and retrieved recipe content are untrusted data throughout.
"""

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

PLANNER_VERSION = "1"
CLARIFICATION_SCHEMA_VERSION = "1"

NonEmptyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
PromptText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
TopicText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
SemanticKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_\-]*$"
    ),
]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_\-]*$"
    ),
]

# Allowlisted question targets. Rule and LLM questions may only address these.
# "substitution_choice" covers contextual availability/preference choices
# (e.g. which cream alternative is available); "task_scope" covers explicit
# task changes (e.g. discovery -> scaling). Arbitrary state mutations are
# rejected by the hybrid validator.
ALLOWED_TARGETS = frozenset(
    {
        "dish",
        "ingredients",
        "portions",
        "time_minutes",
        "cuisine",
        "preferences",
        "dietary_constraints",
        "equipment",
        "substitution_choice",
        "task_scope",
    }
)

# Option ids that assert "none / no preference". Mutually exclusive with any
# other selection on the same question.
NONE_OPTION_IDS = frozenset({"none", "no_preference", "no_restriction"})

# Targets whose values are hard constraints: a model claim alone must never
# establish compliance. Unverified options keep explicit uncertainty.
HARD_CONSTRAINT_TARGETS = frozenset({"dietary_constraints"})


class FieldStatus(str, Enum):
    UNKNOWN = "unknown"
    PROVIDED = "provided"
    NO_PREFERENCE = "no_preference"
    SKIPPED = "skipped"
    CONFLICTING = "conflicting"


class PlanningOutcome(str, Enum):
    NEEDS_CLARIFICATION = "needs_clarification"
    READY_FOR_RETRIEVAL = "ready_for_retrieval"
    BLOCKED = "blocked"


# Stable machine codes for human-readable blocker messages. Readiness logic
# must match on these codes (via :func:`blocker_code`), never on substring
# searches inside the message text.
BLOCKER_DISH_DIRECTION_SKIPPED = "dish_direction_skipped"
BLOCKER_PORTIONS_SKIPPED = "portions_skipped"
BLOCKER_CONFLICT_PREFIX = "conflicting_"
BLOCKED_CODES = frozenset({BLOCKER_DISH_DIRECTION_SKIPPED, BLOCKER_PORTIONS_SKIPPED})


def blocker_code(blocker: str) -> str:
    """Stable code for a blocker message: the prefix before the first colon."""
    return blocker.split(":", 1)[0].strip()


def is_blocked(blockers: list[str]) -> bool:
    """True when any blocker carries a blocking skipped-essential code."""
    return any(blocker_code(blocker) in BLOCKED_CODES for blocker in blockers)


class QuestionInputType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"


class QuestionSource(str, Enum):
    RULE = "rule"
    LLM = "llm"


class PlanningMode(str, Enum):
    RULE_ONLY = "rule_only"
    LLM_ASSISTED = "llm_assisted"


class EvidenceStatus(str, Enum):
    """Provenance of substitution/recipe evidence attached to a question."""

    NOT_QUERIED = "not_queried"
    UNAVAILABLE = "unavailable"
    QUERIED_NO_RESULT = "queried_no_result"
    BACKED_BY_EVIDENCE = "backed_by_evidence"
    UNVERIFIED = "unverified"


class RecipeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=300)


class SubstitutionEvidence(BaseModel):
    """Safety/equivalence metadata for substitution options.

    A model claim alone never establishes allergy safety, dietary compliance,
    ingredient equivalence, or quantity conversion. When compatibility is
    unverified the option keeps ``verified=False`` and callers must not
    present it as satisfying a hard constraint.
    """

    model_config = ConfigDict(extra="forbid")

    status: EvidenceStatus = EvidenceStatus.UNVERIFIED
    verified: bool = False
    note: str = Field(default="", max_length=500)
    conflicts_with_constraints: list[str] = Field(default_factory=list, max_length=20)


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StableId
    label: str = Field(min_length=1, max_length=200)


class QuestionDependency(BaseModel):
    """Declarative dependency on another question's answer.

    ``depends_on`` names the parent question id; ``required_options`` lists
    parent option ids that activate this question (empty = any answer to the
    parent activates it). Validated data only — never executed.
    """

    model_config = ConfigDict(extra="forbid")

    depends_on: StableId
    required_options: list[StableId] = Field(default_factory=list, max_length=20)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StableId
    semantic_key: SemanticKey
    topic: TopicText
    prompt: PromptText
    input_type: QuestionInputType
    options: list[QuestionOption] = Field(default_factory=list, max_length=20)
    allow_custom_text: bool = False
    required: bool = False
    priority: int = Field(default=50, ge=1, le=100)
    depends_on: QuestionDependency | None = None
    source: QuestionSource = QuestionSource.RULE
    target: str = Field(min_length=1, max_length=100)
    recipe_refs: list[RecipeRef] = Field(default_factory=list, max_length=10)
    min_value: float | None = None
    max_value: float | None = None
    min_selections: int | None = None
    max_selections: int | None = None
    max_length: int | None = Field(default=None, ge=1, le=2000)
    substitution_evidence: SubstitutionEvidence | None = None
    evidence_backed: bool = False

    @model_validator(mode="after")
    def _validate_type_fields(self) -> "Question":
        if self.target not in ALLOWED_TARGETS:
            raise ValueError(f"Unsupported target {self.target!r}")
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("Option ids must be unique")
        labels = [o.label.strip().casefold() for o in self.options]
        if len(set(labels)) != len(labels):
            raise ValueError("Option labels must be unique")
        if self.input_type == QuestionInputType.TEXT:
            if self.options:
                raise ValueError("Text questions must not carry options")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("Text questions must not carry numeric bounds")
            if self.min_selections is not None or self.max_selections is not None:
                raise ValueError("Text questions must not carry selection counts")
        elif self.input_type == QuestionInputType.NUMBER:
            if self.options:
                raise ValueError("Number questions must not carry options")
            if self.allow_custom_text:
                raise ValueError("Number questions must not set allow_custom_text")
            if (
                self.min_value is not None
                and self.max_value is not None
                and self.min_value > self.max_value
            ):
                raise ValueError("min_value must not exceed max_value")
            if self.min_selections is not None or self.max_selections is not None:
                raise ValueError("Number questions must not carry selection counts")
        else:  # choice types
            if len(self.options) < 2:
                raise ValueError("Choice questions require at least two options")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("Choice questions must not carry numeric bounds")
            if self.input_type == QuestionInputType.SINGLE_CHOICE:
                if self.min_selections is not None or self.max_selections is not None:
                    raise ValueError("Single-choice questions select exactly one option")
            else:
                lo = self.min_selections if self.min_selections is not None else 1
                hi = self.max_selections if self.max_selections is not None else len(self.options)
                if lo < 1:
                    raise ValueError("min_selections must be >= 1")
                if hi > len(self.options):
                    raise ValueError("max_selections must not exceed option count")
                if lo > hi:
                    raise ValueError("min_selections must not exceed max_selections")
        return self


class QuestionGroup(BaseModel):
    """Ordered group for serial presentation (one question at a time)."""

    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    questions: list[Question] = Field(default_factory=list, max_length=12)
    created_by: PlanningMode = PlanningMode.RULE_ONLY
    planner_version: str = Field(default=PLANNER_VERSION, max_length=20)
    schema_version: str = Field(default=CLARIFICATION_SCHEMA_VERSION, max_length=20)


class AnswerPayload(BaseModel):
    """One answer submission for one question.

    Custom-text rule (documented, enforced consistently): ``custom_text``
    SUPPLEMENTS selected options — both are retained. A submission with only
    ``custom_text`` (no selections) records the custom text as the answer.
    ``no_preference`` and ``skip`` are mutually exclusive with every value.
    Reserved none-ids (``none``/``no_preference``/``no_restriction``) are
    mutually exclusive with any other selection.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: StableId
    text: str | None = Field(default=None, max_length=2000)
    number: float | None = None
    selected: list[StableId] | None = Field(default=None, max_length=20)
    custom_text: str | None = Field(default=None, max_length=500)
    no_preference: bool = False
    skip: bool = False

    @model_validator(mode="after")
    def _validate_mode(self) -> "AnswerPayload":
        if self.skip and (
            self.text is not None
            or self.number is not None
            or self.selected is not None
            or self.custom_text is not None
            or self.no_preference
        ):
            raise ValueError("skip is mutually exclusive with all answer values")
        if self.no_preference and (
            self.text is not None
            or self.number is not None
            or self.selected is not None
            or self.custom_text is not None
        ):
            raise ValueError("no_preference is mutually exclusive with all answer values")
        if self.text is not None and self.number is not None:
            raise ValueError("Provide text or number, not both")
        if self.selected is not None and (self.text is not None or self.number is not None):
            raise ValueError("Selections cannot be combined with text/number values")
        if self.selected is not None and len(set(self.selected)) != len(self.selected):
            raise ValueError("Duplicate selections are not allowed")
        if self.selected:
            lowered = {s.strip().casefold() for s in self.selected}
            if lowered & {n.casefold() for n in NONE_OPTION_IDS} and len(self.selected) > 1:
                raise ValueError("none/no-preference options are mutually exclusive")
        if self.text is not None and not self.text.strip():
            raise ValueError("text must not be blank")
        if self.custom_text is not None and not self.custom_text.strip():
            raise ValueError("custom_text must not be blank")
        return self


class AnswerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: StableId
    semantic_key: SemanticKey
    target: str = Field(min_length=1, max_length=100)
    status: Literal["answered", "skipped", "invalidated"] = "answered"
    text: str | None = None
    number: float | None = None
    selected: list[str] = Field(default_factory=list)
    custom_text: str | None = None
    no_preference: bool = False
    request_revision: int = Field(ge=1)


class ConfirmationStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"


class PendingConfirmation(BaseModel):
    """User confirmation gate for model-inferred corrections.

    A model-provided ``is_correction`` flag or confidence score is never
    sufficient evidence to replace an explicit value. The proposed value is
    recorded here and applied only when the user confirms through the typed
    answer endpoint. The previous value and history stay intact until then.
    Dietary relaxations are flagged and never applied silently.
    """

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, max_length=100)
    current_summary: str = Field(default="", max_length=500)
    proposed: str = Field(min_length=1, max_length=500)
    question_id: StableId
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    relaxes_constraint: bool = False
    quote_supported: bool = False

    @model_validator(mode="after")
    def _validate_target(self) -> "PendingConfirmation":
        if self.target not in ALLOWED_TARGETS:
            raise ValueError(f"Unsupported target {self.target!r}")
        return self


class CookingRequestState(BaseModel):
    """State wrapper around CookingRequest (which is preserved unchanged).

    ``field_status`` tracks per-target status; ``values`` mirrors structured
    values for the allowlisted targets (including ``dish`` and ``task_scope``,
    which have no home in CookingRequest). ``answers`` is append-only history:
    edits and invalidations append new records, never rewrite history.
    ``pending_confirmations`` gates model-inferred corrections: explicit values
    are replaced only by typed user answers, never by model proposals.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(default=1, ge=1)
    request: dict[str, Any] = Field(default_factory=dict)
    dish: str | None = Field(default=None, max_length=200)
    task_scope: str | None = Field(default=None, max_length=100)
    field_status: dict[str, FieldStatus] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    answers: list[AnswerRecord] = Field(default_factory=list, max_length=200)
    skipped_questions: list[str] = Field(default_factory=list, max_length=100)
    invalidated_questions: list[str] = Field(default_factory=list, max_length=100)
    blockers: list[str] = Field(default_factory=list, max_length=50)
    outcome: PlanningOutcome = PlanningOutcome.NEEDS_CLARIFICATION
    ready_for_retrieval: bool = False
    uncertain_notes: list[str] = Field(default_factory=list, max_length=50)
    replan_count: int = Field(default=0, ge=0)
    pending_confirmations: list[PendingConfirmation] = Field(default_factory=list, max_length=20)
