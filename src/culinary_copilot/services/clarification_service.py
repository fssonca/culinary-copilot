"""Orchestration for clarification groups: rule planning + bounded hybrid LLM."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from culinary_copilot.config import Settings
from culinary_copilot.domain.clarification import (
    BLOCKER_CONFLICT_PREFIX,
    CLARIFICATION_SCHEMA_VERSION,
    ConfirmationStatus,
    CookingRequestState,
    EvidenceStatus,
    FieldStatus,
    PlanningMode,
    PlanningOutcome,
    Question,
    QuestionGroup,
    blocker_code,
    is_blocked,
)
from culinary_copilot.domain.requests import CookingRequest
from culinary_copilot.domain.rule_planner import (
    DEFAULT_MAX_QUESTIONS,
    MAX_GROUP_QUESTIONS,
    plan_rule_questions,
    retrieval_ready,
)
from culinary_copilot.llm.client import (
    ApplicationLlmProvider,
    ProviderDisabledError,
)
from culinary_copilot.services.answers import init_state
from culinary_copilot.services.hybrid_planner import (
    EvidenceItem,
    LlmPlanProposal,
    apply_llm_proposal,
    build_planning_prompt,
)
from culinary_copilot.services.store import new_id

PLANNER_VERSION = "1"

# Targets the current search implementation does not enforce. Dish direction
# feeds the query text, ingredients filter candidates, and time_minutes caps
# duration; everything else must be checked downstream. Surfaced explicitly so
# readiness is never mistaken for compatibility or safety.
SEARCH_UNENFORCED_TARGETS = (
    "cuisine",
    "preferences",
    "dietary_constraints",
    "equipment",
    "substitution_choice",
)

READINESS_NOTE = (
    "Ready for retrieval means sufficient search information only. It does not "
    "establish dietary compatibility, allergy safety, validated quantities, "
    "nutritional verification, or permission to scale. Constraints listed in "
    "unenforced_constraints are preserved in state but are not enforced by the "
    "current search implementation and must be checked downstream."
)


def _clamp_max(max_questions: int | None) -> int:
    if max_questions is None:
        return DEFAULT_MAX_QUESTIONS
    if not 1 <= max_questions <= MAX_GROUP_QUESTIONS:
        raise ValueError(f"max_questions must be between 1 and {MAX_GROUP_QUESTIONS}")
    return max_questions


def build_initial_state(
    *,
    request_id: str,
    request: CookingRequest | dict[str, Any] | None,
    dish: str | None,
    task_scope: str | None,
    no_preference_targets: list[str] | None = None,
) -> CookingRequestState:
    if isinstance(request, CookingRequest):
        req_dict = request.model_dump()
    else:
        req_dict = dict(request or {})
    return init_state(
        request_id=request_id,
        request=req_dict,
        dish=dish,
        task_scope=task_scope,
        no_preference_targets=no_preference_targets,
    )


def _state_summary(state: CookingRequestState) -> dict[str, Any]:
    return {
        "dish": state.dish,
        "task_scope": state.task_scope,
        "field_status": {k: v.value for k, v in state.field_status.items()},
        "values": state.values,
        "blockers": state.blockers,
    }


async def _retrieve_evidence(
    *,
    engine: Any,
    message: str,
    state: CookingRequestState,
    limit: int = 3,
) -> tuple[list[EvidenceItem], EvidenceStatus]:
    """Bounded server-retrieved canonical recipe evidence.

    Distinguishes NOT_QUERIED (no query attempted) from QUERIED_NO_RESULT
    (queried, nothing useful) from UNAVAILABLE (retrieval failed/disabled).
    Runs blocking repository work in a worker thread, never on the loop.
    """
    query = (message or state.dish or "").strip()
    if not query and not state.values.get("ingredients"):
        return [], EvidenceStatus.NOT_QUERIED
    if engine is None:
        return [], EvidenceStatus.UNAVAILABLE
    ingredients = [str(v) for v in (state.values.get("ingredients") or []) if str(v).strip()][:5]
    try:
        rows: list[dict[str, Any]] = await asyncio.to_thread(
            _search_sync, engine, query or "recipe", ingredients, limit
        )
    except Exception:
        return [], EvidenceStatus.UNAVAILABLE
    items = [
        EvidenceItem(
            dataset_id=str(r.get("dataset_id", "")),
            source_id=str(r.get("source_id", "")),
            title=str(r.get("title", ""))[:300],
            snippet="",
        )
        for r in rows
        if r.get("dataset_id") and r.get("source_id")
    ]
    if not items:
        return [], EvidenceStatus.QUERIED_NO_RESULT
    return items[:limit], EvidenceStatus.BACKED_BY_EVIDENCE


def _search_sync(
    engine: Any, query: str, ingredients: list[str], limit: int
) -> list[dict[str, Any]]:
    from culinary_copilot.recipes.repository import search_all

    return search_all(engine, query, ingredients=ingredients or None, limit=limit)


async def _maybe_epicure_hint(*, epicure: Any, ingredient: str) -> tuple[str, EvidenceStatus]:
    """Best-effort Epicure availability hint (never a safety proof)."""
    if epicure is None:
        return "epicure_not_queried", EvidenceStatus.NOT_QUERIED
    try:
        enabled = bool(getattr(getattr(epicure, "settings", None), "epicure_enabled", False))
    except Exception:
        enabled = False
    if not enabled:
        return "epicure_disabled", EvidenceStatus.UNAVAILABLE
    if not ingredient.strip():
        return "epicure_not_queried", EvidenceStatus.NOT_QUERIED
    try:
        pairs = await asyncio.to_thread(epicure.find_balanced_pairings, ingredient.strip(), 5)
        names = [p.ingredient for p in pairs][:5]
        if not names:
            return "epicure_queried_no_result", EvidenceStatus.QUERIED_NO_RESULT
        return f"epicure_neighbors:{','.join(names)}", EvidenceStatus.BACKED_BY_EVIDENCE
    except Exception:
        return "epicure_unavailable", EvidenceStatus.UNAVAILABLE


async def plan_group(
    *,
    state: CookingRequestState,
    message: str,
    settings: Settings,
    provider: ApplicationLlmProvider | None,
    engine: Any = None,
    epicure: Any = None,
    max_questions: int = DEFAULT_MAX_QUESTIONS,
    use_llm: bool = True,
) -> tuple[list[Question], dict[str, Any]]:
    """Plan one bounded group: rule questions + at most one LLM call."""
    max_q = _clamp_max(max_questions)
    rule_questions, rule_blockers, rule_outcome = plan_rule_questions(state, max_questions=max_q)
    if rule_blockers and not state.blockers:
        state.blockers = list(rule_blockers)
    meta: dict[str, Any] = {
        "planning_mode": PlanningMode.RULE_ONLY.value,
        "rule_count": len(rule_questions),
        "llm_count": 0,
        "provider_error": None,
        "provider_model": None,
        "latency_ms": None,
        "attempts": 0,
        "input_tokens": None,
        "output_tokens": None,
        "llm_report": {},
    }
    if not use_llm or not settings.llm_enabled or provider is None:
        if settings.llm_enabled and use_llm and provider is None:
            meta["provider_error"] = "provider_unavailable"
        final = list(rule_questions)
        _apply_outcome(state, final, rule_outcome, rule_blockers)
        return final[:max_q], meta

    # Bounded evidence for recipe-specific follow-ups.
    evidence, evidence_status = await _retrieve_evidence(
        engine=engine, message=message, state=state
    )
    prior = [
        {
            "question_id": a.question_id,
            "target": a.target,
            "selected": a.selected,
            "text": (a.text or "")[:200],
            "no_preference": a.no_preference,
            "status": a.status,
        }
        for a in state.answers[-10:]
    ]
    system, user = build_planning_prompt(
        message=message,
        state_summary=_state_summary(state),
        prior_answers=prior,
        rule_questions=rule_questions,
        evidence=evidence,
        max_input_chars=settings.llm_app_max_input_chars,
    )
    started = time.perf_counter()
    try:
        outcome = await provider.complete_planning(
            system=system, user=user, response_model=LlmPlanProposal
        )
    except ProviderDisabledError:
        meta["provider_error"] = "disabled"
        _apply_outcome(state, rule_questions, rule_outcome, rule_blockers)
        return list(rule_questions)[:max_q], meta
    except Exception as exc:
        meta["provider_error"] = _provider_code(exc)
        _apply_outcome(state, rule_questions, rule_outcome, rule_blockers)
        return list(rule_questions)[:max_q], meta
    latency_ms = int((time.perf_counter() - started) * 1000)
    meta["latency_ms"] = outcome.latency_ms or latency_ms
    meta["attempts"] = outcome.attempts
    meta["provider_model"] = outcome.model
    meta["input_tokens"] = outcome.input_tokens
    meta["output_tokens"] = outcome.output_tokens
    if not outcome.ok or not outcome.parsed:
        meta["provider_error"] = outcome.error_code or "provider_failure"
        _apply_outcome(state, rule_questions, rule_outcome, rule_blockers)
        return list(rule_questions)[:max_q], meta
    try:
        proposal = LlmPlanProposal.model_validate(outcome.parsed)
    except Exception:
        meta["provider_error"] = "schema_failure"
        _apply_outcome(state, rule_questions, rule_outcome, rule_blockers)
        return list(rule_questions)[:max_q], meta

    # Free-text interpretation guard: without an enabled provider path the
    # caller never reaches here. Explicit values are never replaced by model
    # proposals; inferred corrections return as confirmation questions and
    # unknown-field updates apply only when grounded and shape-valid.
    llm_questions, report = apply_llm_proposal(
        state=state,
        proposal=proposal,
        rule_questions=rule_questions,
        evidence=evidence,
        evidence_status=evidence_status,
        message=message,
    )
    meta["llm_report"] = {
        k: v
        for k, v in report.items()
        if k in ("accepted", "rejected", "rejected_overwrites", "pending_confirmations")
    }
    for note in state.uncertain_notes[-5:]:
        _ = note
    # Merge: required rule questions first, then accepted LLM questions,
    # then remaining rule questions to fill the bound. LLM context wins over
    # low-value questionnaire tail so one planning call always fits.
    required_rule = [q for q in rule_questions if q.required]
    rest_rule = [q for q in rule_questions if not q.required]
    merged: list[Question] = (required_rule + llm_questions + rest_rule)[:max_q]
    meta["llm_count"] = len(llm_questions)
    meta["rule_count"] = len(rule_questions)
    if llm_questions:
        meta["planning_mode"] = PlanningMode.LLM_ASSISTED.value
    final_outcome = rule_outcome
    if state.ready_for_retrieval or retrieval_ready(state):
        final_outcome = PlanningOutcome.READY_FOR_RETRIEVAL
    _apply_outcome(state, merged[:max_q], final_outcome, rule_blockers)
    # Epicure hint is best-effort observability only; never blocks planning.
    _ = await _maybe_epicure_hint(epicure=epicure, ingredient="")
    # Telemetry is emitted by the API layer with the real group id (not
    # "pending") for every path, including rule-only and error exits; see
    # api/clarification.py. plan_group only returns meta.
    return merged[:max_q], meta


def _apply_outcome(
    state: CookingRequestState,
    questions: list[Question],
    outcome: PlanningOutcome,
    blockers: list[str],
) -> None:
    from culinary_copilot.domain.rule_planner import retrieval_ready as _ready

    # Conflict blockers derive from state so they survive planning round-trips.
    conflict_blockers = [
        f"{BLOCKER_CONFLICT_PREFIX}{field}: conflicting requirements need clarification"
        for field, status in state.field_status.items()
        if status == FieldStatus.CONFLICTING
    ]
    merged_blockers = list(blockers)
    for blocker in conflict_blockers:
        if not any(blocker_code(b) == blocker_code(blocker) for b in merged_blockers):
            merged_blockers.append(blocker)
    merged_blockers = merged_blockers[:50]

    if any(v == FieldStatus.CONFLICTING for v in state.field_status.values()):
        state.outcome = PlanningOutcome.NEEDS_CLARIFICATION
        state.ready_for_retrieval = False
        if merged_blockers:
            state.blockers = list(merged_blockers)
        return
    if merged_blockers and is_blocked(merged_blockers):
        state.outcome = PlanningOutcome.BLOCKED
        state.ready_for_retrieval = False
        state.blockers = list(merged_blockers)
        return
    if _ready(state):
        state.outcome = PlanningOutcome.READY_FOR_RETRIEVAL
        state.ready_for_retrieval = True
        return
    state.outcome = outcome
    state.ready_for_retrieval = False
    if merged_blockers:
        state.blockers = list(merged_blockers)


def _provider_code(exc: Exception) -> str:
    name = type(exc).__name__
    mapping = {
        "ProviderAuthError": "auth_failure",
        "ProviderRateLimitError": "rate_limited",
        "ProviderTimeoutError": "timeout",
        "ProviderUnavailableError": "provider_unavailable",
        "ProviderRefusalError": "refusal",
        "ProviderIncompleteError": "incomplete",
        "ProviderSchemaError": "schema_failure",
    }
    return mapping.get(name, "provider_failure")


def make_group(
    *, state: CookingRequestState, questions: list[Question], created_by: str
) -> QuestionGroup:
    from culinary_copilot.domain.clarification import PlanningMode as _Mode

    mode = _Mode.LLM_ASSISTED if created_by == "llm_assisted" else _Mode.RULE_ONLY
    return QuestionGroup(
        group_id=new_id("grp"),
        request_id=state.request_id,
        revision=1,
        questions=list(questions),
        created_by=mode,
        planner_version=PLANNER_VERSION,
        schema_version=CLARIFICATION_SCHEMA_VERSION,
    )


def group_response(
    *,
    state: CookingRequestState,
    group: QuestionGroup,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = meta or {}
    answered = [a.question_id for a in state.answers if a.status == "answered"]
    # Serial presentation order: required first, then priority.
    ordered = sorted(group.questions, key=lambda q: (not q.required, -q.priority, q.id))
    unenforced = [
        target
        for target in SEARCH_UNENFORCED_TARGETS
        if state.field_status.get(target) == FieldStatus.PROVIDED
    ]
    return {
        "request_id": state.request_id,
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
        "outcome": state.outcome.value,
        "ready_for_retrieval": state.ready_for_retrieval,
        "readiness_note": READINESS_NOTE,
        "unenforced_constraints": unenforced,
        "planning_mode": group.created_by.value,
        "questions": [q.model_dump() for q in ordered],
        "answered": answered,
        "skipped": list(state.skipped_questions),
        "invalidated": list(state.invalidated_questions),
        "blockers": list(state.blockers),
        "pending_confirmations": [
            c.model_dump()
            for c in state.pending_confirmations
            if c.status == ConfirmationStatus.PENDING
        ],
        "state": {
            "field_status": {k: v.value for k, v in state.field_status.items()},
            "values": state.values,
            "request": state.request,
            "dish": state.dish,
            "task_scope": state.task_scope,
            "uncertain_notes": list(state.uncertain_notes),
            "replan_count": state.replan_count,
        },
        "versions": {
            "planner": group.planner_version,
            "schema": group.schema_version,
        },
        "counts": {
            "rule": meta.get("rule_count", sum(1 for q in group.questions if q.source == "rule")),
            "llm": meta.get("llm_count", sum(1 for q in group.questions if q.source == "llm")),
        },
        "provider": {
            "model": meta.get("provider_model"),
            "error": meta.get("provider_error"),
            "latency_ms": meta.get("latency_ms"),
            "attempts": meta.get("attempts", 0),
            "input_tokens": meta.get("input_tokens"),
            "output_tokens": meta.get("output_tokens"),
        },
    }
