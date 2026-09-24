"""Fixed recommendation orchestration (Phase 3, Stage A).

Order: authoritative readiness → early Epicure consultation → retrieval →
complete source fetch → suggestion assessment/model selection →
deterministic validation → server-rendered response.

Concurrency mirrors retrieval: snapshot the store (brief lock, deep
copies), release the lock, run blocking DB/Epicure work in worker threads
(never on the event loop; no store lock held across I/O), then re-read the
store and fail closed with 409 when an intervening edit advanced either
revision or a newer group superseded the requested one.

Stage A uses the Epicure boundary with a fake adapter; Stage B connects
the real cached adapter. Retrieval ranking is reused unchanged
(``map_request_to_query`` + repository search); complete documents are
fetched by exact ``(dataset_id, source_id)`` identity, never legacy
fallback lookup.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from culinary_copilot.config import Settings
from culinary_copilot.domain.clarification import FieldStatus
from culinary_copilot.domain.recommendations import (
    INSUFFICIENT_BUDGET_EXCEEDED,
    INSUFFICIENT_HARD_CONSTRAINT,
    INSUFFICIENT_INCOMPLETE_ONLY,
    INSUFFICIENT_NO_CANDIDATES,
    INSUFFICIENT_SOURCE_FETCH,
    REASON_BAD_REQUEST,
    REASON_BUDGET_EXCEEDED,
    REASON_CONTENT_FILTER,
    REASON_EMPTY,
    REASON_INTERNAL_ERROR,
    REASON_INVALID_TOOL,
    REASON_NOT_FOUND,
    REASON_PROVIDER_REFUSAL,
    REASON_RATE_LIMITED,
    REASON_REQUEST_ERROR,
    REASON_SCHEMA_FAILURE,
    REASON_TRUNCATED,
    REASON_TURN_LIMIT,
    REASON_VALIDATION_REJECTED,
    REJECT_BAD_REFERENCE,
    REJECT_EVIDENCE_CHANGED,
    REJECT_HARD_CONSTRAINT,
    REJECT_INCOMPLETE_SOURCE,
    REJECT_INJECTION,
    REJECT_STEP_COVERAGE,
    REJECT_UNKNOWN_IDENTITY,
    EpicureOutcome,
    RecommendationOutcome,
    SelectionProposal,
)
from culinary_copilot.llm.client import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderCallError,
    ProviderContentFilterError,
    ProviderDisabledError,
    ProviderIncompleteError,
    ProviderInternalError,
    ProviderNotFoundError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderRequestError,
    ProviderSchemaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    request_size,
)
from culinary_copilot.recommendations.evidence import (
    build_candidate,
    evidence_fingerprint,
    render_recipe,
)
from culinary_copilot.recommendations.policy import (
    assess_candidate,
    hard_violation,
    unresolved_hard,
)
from culinary_copilot.recommendations.prompts import (
    SELECTION_SYSTEM_PROMPT,
    TOOL_FINAL_SELECTION_SYSTEM_PROMPT,
    TOOL_METADATA_SYSTEM_PROMPT,
    attach_labels,
    build_selection_payload,
    build_tool_final_system,
    build_tool_metadata_payload,
    candidate_label,
    fit_serialized,
    get_recipe_function,
    metadata_header,
    render_candidate_block,
    render_metadata_block,
    selection_header,
    selection_model_for,
)
from culinary_copilot.recommendations.propositions import (
    RequestFacts,
    evaluate_propositions,
    request_facts,
)
from culinary_copilot.retrieval.service import (
    CONSTRAINTS_NOT_VERIFIED,
    evaluate_readiness,
)
from culinary_copilot.services.clarification_service import (
    READINESS_NOTE,
    SEARCH_UNENFORCED_TARGETS,
)

_INJECTION_RE = re.compile(r"ignore\s+previous\s+instructions|^system\s*:", re.IGNORECASE | re.M)

# Explicit simple-technique skip policy (documented in
# docs/recommendations.md): single-ingredient preparations where pairing
# suggestions add no selection value. Skips carry this reason and never
# appear as successful consultations.
SIMPLE_TECHNIQUE_DISHES = frozenset({"toast", "boiled egg", "boiled eggs", "plain white rice"})
SIMPLE_TECHNIQUE_REASON = (
    "Simple-technique skip: single-ingredient preparation in the documented "
    "skip list; pairing suggestions add no selection value."
)

# Stage-reporting hook for streaming (Phase 4): one workflow, two
# transports. The non-streaming endpoint passes no sink; the SSE endpoint
# passes an async sink receiving (stage, detail). Stages carry counts and
# stable codes only — never prompts, recipe content, or constraint values.
# No provisional recipe fragments are emitted; there is no `provisional`
# event. Allowed stages: accepted, readiness, epicure, retrieval,
# evidence, provider_request, provider_turn, validation, revision_check.
StageSink = Callable[[str, dict[str, Any]], Awaitable[None]]

ALLOWED_STAGES = frozenset(
    {
        "accepted",
        "readiness",
        "epicure",
        "retrieval",
        "evidence",
        "provider_request",
        "provider_turn",
        "validation",
        "revision_check",
    }
)


class RecommendationNotFoundError(Exception):
    """Unknown clarification group."""


class RecommendationStaleError(Exception):
    """Expected revisions no longer match authoritative state."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RecommendationFailure(Exception):
    """Controlled failure with an HTTP mapping (no prompts/secrets/raw output)."""

    def __init__(
        self,
        *,
        http_status: int,
        reason: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.reason = reason
        self.message = message
        self.detail = detail or {}


def _require_current_group(store: Any, request_id: str, group_id: str) -> None:
    current = store.current_group_id(request_id)
    if current is not None and current != group_id:
        raise RecommendationStaleError(
            f"superseded group: recommendation requires the current group {current}; "
            "refetch the group and retry with its revisions"
        )


def _dietary(state: Any) -> list[str]:
    raw = state.values.get("dietary_constraints") or []
    return [str(v) for v in raw if str(v).strip()][:20]


def _pantry(state: Any) -> list[str]:
    raw = state.values.get("ingredients") or state.request.get("ingredients") or []
    out = [str(v).strip() for v in raw if isinstance(raw, list) and str(v).strip()]
    return out[:100]


def _simple_technique_skip(state: Any) -> str | None:
    dish = (state.dish or state.values.get("dish") or "").strip().lower()
    dish = " ".join(dish.split())
    pantry = _pantry(state)
    if dish in SIMPLE_TECHNIQUE_DISHES and not pantry:
        return SIMPLE_TECHNIQUE_REASON
    return None


def _search_sync(engine: Any, query: Any, limit: int) -> list[dict[str, Any]]:
    from culinary_copilot.recipes.repository import search_all, search_recipes

    if query.dataset_id is not None:
        return search_recipes(
            engine,
            query.query_text,
            ingredients=None,
            max_minutes=query.max_minutes,
            limit=limit,
            dataset_id=query.dataset_id,
            match_any_ingredients=query.match_any_ingredients,
            rank_pantry_terms=query.rank_pantry_terms,
        )
    return search_all(
        engine,
        query.query_text,
        ingredients=None,
        max_minutes=query.max_minutes,
        limit=limit,
        match_any_ingredients=query.match_any_ingredients,
        rank_pantry_terms=query.rank_pantry_terms,
    )


def _fetch_exact_sync(engine: Any, dataset_id: str, source_id: str) -> dict[str, Any] | None:
    from culinary_copilot.recipes.repository import get_recipe

    # Exact-pair lookup only: dataset_id is always supplied, so no legacy
    # fallback lookup can cross datasets here.
    return get_recipe(engine, source_id, dataset_id=dataset_id)


def _discovery_why(candidate: dict[str, Any]) -> str:
    """Why a discovery pointer is not a ready-to-cook recommendation.

    A recorded blocking source defect (for example a known inconsistency
    between the ingredient list and the steps) is reported as such, apart
    from structural gaps (missing sections, omitted entries) and from
    constraint gaps on otherwise admissible sources.
    """
    if candidate.get("defects_blocking"):
        return "blocking source defect"
    if not candidate.get("recommendable"):
        return "structurally incomplete source"
    return "constraint-unverified"


def _source_checks(candidate: dict[str, Any]) -> dict[str, Any]:
    """What admission checked for the selected source, and what it did not.

    Admission is structural: required sections present, no omitted
    entries, and no recorded error-severity defect. It is not semantic
    completeness. Consistency between the ingredient list and the steps is
    not detected automatically; only a defect already recorded on the
    stored source can block it.
    """
    return {
        "structural_admission": "passed",
        "recorded_blocking_defects": list(candidate.get("defects_blocking") or []),
        "recorded_quality_context": sorted(
            {str(c.get("code")) for c in candidate.get("quality_context", []) if c.get("code")}
        ),
        "source_flags": [str(f) for f in candidate.get("flags", [])],
        "ingredient_list_vs_steps": "not_checked",
        "note": (
            "Structural checks confirm that the ingredient and instruction "
            "sections exist and no entries were dropped. They do not "
            "establish that the ingredient list covers everything the steps "
            "use, or that the recipe is complete. No automatic consistency "
            "check exists; only a defect recorded on the stored source "
            "blocks a recommendation."
        ),
    }


def _validate_selection(
    proposal: SelectionProposal,
    candidates: list[dict[str, Any]],
    *,
    proposed_label: str | None = None,
) -> dict[str, Any]:
    """Deterministic validation of a model selection. Returns the candidate.

    ``proposed_label`` carries the server-issued label the model selected
    (label path); unknown identities persist the bounded proposed value
    plus the offered identities so the next run can be diagnosed.
    """
    by_identity = {(c["dataset_id"], c["source_id"]): c for c in candidates}
    key = (proposal.dataset_id, proposal.source_id)
    if key not in by_identity:
        detail: dict[str, Any] = {"validation_reason": REJECT_UNKNOWN_IDENTITY}
        detail["proposed"] = {
            "dataset_id": _identifier_echo(proposal.dataset_id),
            "source_id": _identifier_echo(proposal.source_id),
        }
        if proposed_label is not None:
            detail["proposed"]["candidate_label"] = _identifier_echo(proposed_label)
        detail["offered"] = [
            {
                "candidate_label": str(c.get("label") or "")[:20],
                "dataset_id": str(c.get("dataset_id") or "")[:200],
                "source_id": str(c.get("source_id") or "")[:200],
            }
            for c in candidates[:5]
        ]
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_VALIDATION_REJECTED,
            message="Proposal selection is not in the supplied evidence",
            detail=detail,
        )
    candidate = by_identity[key]
    if not candidate.get("recommendable"):
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_VALIDATION_REJECTED,
            message="Incomplete or defective sources cannot be promoted to complete recipes",
            detail={"validation_reason": REJECT_INCOMPLETE_SOURCE},
        )
    ingredient_refs = sorted(str(i.get("ref")) for i in candidate.get("ingredients", []))
    step_refs = [str(s.get("ref")) for s in candidate.get("instructions", [])]
    if sorted(proposal.ingredient_refs) != ingredient_refs or not ingredient_refs:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_VALIDATION_REJECTED,
            message="Ingredient references must exactly cover the selected recipe",
            detail={"validation_reason": REJECT_BAD_REFERENCE},
        )
    # Instructions must cover the original cooking steps in source order:
    # exact ordered match, no omissions or reordering. Index checks
    # establish traceability, not semantic correctness.
    if list(proposal.step_refs) != step_refs or not step_refs:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_VALIDATION_REJECTED,
            message="Step references must cover the original steps in source order",
            detail={"validation_reason": REJECT_STEP_COVERAGE},
        )
    # Defense in depth: typed propositions carry no free text (enum types,
    # pattern-bounded refs), but every string in the proposal is still
    # screened for instruction-override patterns.
    for text in _proposal_strings(proposal):
        if _INJECTION_RE.search(text or ""):
            raise RecommendationFailure(
                http_status=502,
                reason=REASON_VALIDATION_REJECTED,
                message="Proposal contains instruction-override patterns",
                detail={"validation_reason": REJECT_INJECTION},
            )
    assessments = candidate.get("_assessments", [])
    if hard_violation(assessments):
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_VALIDATION_REJECTED,
            message="A known hard-constraint violation cannot yield a recommendation",
            detail={"validation_reason": REJECT_HARD_CONSTRAINT},
        )
    if unresolved_hard(assessments):
        # Dietary evidence gaps cannot be promoted to supported claims.
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_VALIDATION_REJECTED,
            message="Unresolved hard constraint on the selected source",
            detail={"validation_reason": REJECT_HARD_CONSTRAINT},
        )
    return candidate


def _proposal_strings(proposal: SelectionProposal) -> list[str]:
    found: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(proposal.model_dump())
    return found


_SAFE_LOC_PART = re.compile(r"^[a-z_]{1,40}$")
_IDENTIFIER_SHAPE = re.compile(r"^[A-Za-z0-9._/:\-]{1,200}$")


def _identifier_echo(value: Any) -> str:
    """Echo a rejected identity only when it is identifier-shaped.

    Diagnosing identity mismatches (leading zeros, stripped ids) needs the
    proposed value, but rejected free text (sentences smuggled into an
    identity field) is never echoed: it becomes a redaction marker.
    """
    text = str(value or "")
    if _IDENTIFIER_SHAPE.match(text):
        return text
    return f"<redacted non-identifier text, {len(text)} chars>"


def _schema_error_summary(exc: ValidationError) -> list[dict[str, str]]:
    """Bounded schema-failure summary that never echoes model text.

    Only the error location (field names from the schema; unknown keys the
    model invented are redacted) and pydantic's error type travel; input
    values and messages (which quote the rejected input) are dropped.
    """
    summary: list[dict[str, str]] = []
    for error in exc.errors()[:20]:
        parts = []
        for part in error.get("loc", ()):
            if isinstance(part, int) or _SAFE_LOC_PART.match(str(part)):
                parts.append(str(part))
            else:
                parts.append("<redacted>")
        summary.append({"loc": ".".join(parts), "type": str(error.get("type", ""))[:60]})
    return summary


def _assess_epicure_suggestions(
    suggestions: list[dict[str, Any]], candidate: dict[str, Any] | None
) -> dict[str, Any]:
    """Deterministic used/rejected accounting for Epicure suggestions.

    A suggestion is 'used' only as a pairing-consideration note when its
    ingredient already appears in the selected source; otherwise it is
    rejected/deferred as a future adaptation. Suggestions never enter the
    rendered recipe and prove nothing about substitutions, dietary safety,
    nutrition, or chemistry.
    """
    names: set[str] = set()
    if candidate is not None:
        for item in candidate.get("ingredients", []):
            canon = str(item.get("canonical") or "").strip().lower()
            if canon:
                names.add(canon)
    used: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for suggestion in suggestions:
        ingredient = str(suggestion.get("ingredient") or "").strip().lower()
        record = dict(suggestion)
        if ingredient and ingredient in names:
            record["use"] = "pairing_note"
            record["reason"] = "Present in the selected source; pairing context only."
            used.append(record)
        else:
            record["use"] = "deferred"
            record["reason"] = (
                "Not in the selected source; deferred as a future adaptation, not a substitution."
            )
            rejected.append(record)
    return {"used": used, "rejected": rejected}


def _usage_totals(turns: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = sum(int(t.get("attempts") or 0) for t in turns)
    latency = sum(int(t.get("latency_ms") or 0) for t in turns)
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    reasoning_tokens: int | None = 0
    for turn in turns:
        if turn.get("input_tokens") is None:
            input_tokens = None
        elif input_tokens is not None:
            input_tokens += int(turn["input_tokens"])
        if turn.get("output_tokens") is None:
            output_tokens = None
        elif output_tokens is not None:
            output_tokens += int(turn["output_tokens"])
        if turn.get("reasoning_tokens") is None:
            reasoning_tokens = None
        elif reasoning_tokens is not None:
            reasoning_tokens += int(turn["reasoning_tokens"])
    # Unknown usage stays unknown (None), never zero-filled.
    return {
        "attempts": attempts,
        "latency_ms": latency,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_effort": turns[-1].get("reasoning_effort") if turns else None,
        "turns": turns,
    }


def _epicure_section(
    outcome: EpicureOutcome,
    suggestions: list[dict[str, Any]],
    note: str,
    *,
    degraded: bool,
    assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    section: dict[str, Any] = {
        "outcome": outcome.value,
        "suggestions": suggestions,
        "note": note,
        "degraded": degraded,
        "unverified_note": (
            "Epicure suggestions are unverified pairing notes. They do not "
            "establish substitutions, dietary safety, nutrition, or chemistry, "
            "and were never incorporated into the rendered recipe."
        ),
    }
    if assessment is not None:
        section["assessment"] = assessment
    if degraded:
        section["degradation_note"] = (
            "Recommendation proceeded without Epicure consultation; all "
            "grounding and constraint gates retained."
        )
    return section


async def _emit_stage(sink: StageSink | None, stage: str, detail: dict[str, Any]) -> None:
    if sink is None:
        return
    if stage not in ALLOWED_STAGES:
        return
    await sink(stage, dict(detail))


class _TurnLedger:
    """Provider proxy that records each provider turn as it happens.

    Emits ``provider_request`` before a turn is sent and ``provider_turn``
    when it completes, and keeps per-turn usage for telemetry: completed
    turns, the known usage of a failing turn, and turns completed before a
    cancellation. Delegates every call unchanged to the wrapped provider.
    """

    def __init__(
        self, provider: Any, stage: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        self._provider = provider
        self._stage = stage
        self.turns: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    async def complete_recommendation(self, **kwargs: Any) -> Any:
        return await self._call("complete_recommendation", _turn_record, kwargs)

    async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
        return await self._call("complete_native_tool_turn", _native_turn_record, kwargs)

    async def _call(
        self, method: str, to_record: Callable[[Any], dict[str, Any]], kwargs: dict[str, Any]
    ) -> Any:
        number = len(self.turns) + 1
        await self._stage("provider_request", {"turn": number})
        try:
            outcome = await getattr(self._provider, method)(**kwargs)
        except asyncio.CancelledError:
            # Whether the request reached the provider is unknown; so is usage.
            self.turns.append(_ledger_entry(number, status="cancelled", request_sent=None))
            raise
        except BaseException as exc:
            self.turns.append(
                _ledger_entry(
                    number,
                    status="failed",
                    request_sent=getattr(exc, "request_sent", None),
                    attempts=getattr(exc, "attempts", None),
                    input_tokens=getattr(exc, "input_tokens", None),
                    output_tokens=getattr(exc, "output_tokens", None),
                    reasoning_tokens=getattr(exc, "reasoning_tokens", None),
                    response_id=getattr(exc, "response_id", None),
                    failure=type(exc).__name__,
                )
            )
            raise
        record = to_record(outcome)
        entry = _ledger_entry(
            number,
            status="completed",
            request_sent=True,
            attempts=record.get("attempts"),
            latency_ms=record.get("latency_ms"),
            input_tokens=record.get("input_tokens"),
            output_tokens=record.get("output_tokens"),
            reasoning_tokens=record.get("reasoning_tokens"),
            response_id=record.get("response_id"),
            tool_calls=int(record.get("tool_calls") or 0),
        )
        self.turns.append(entry)
        await self._stage(
            "provider_turn",
            {"turn": number, "attempts": entry["attempts"], "tool_calls": entry["tool_calls"]},
        )
        return outcome


def _ledger_entry(
    number: int, *, status: str, request_sent: bool | None, **usage: Any
) -> dict[str, Any]:
    return {
        "turn": number,
        "status": status,
        "request_sent": request_sent,
        "attempts": usage.get("attempts"),
        "latency_ms": usage.get("latency_ms"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "response_id": usage.get("response_id"),
        "tool_calls": int(usage.get("tool_calls") or 0),
        "failure": usage.get("failure"),
    }


def _ledger_summary(turns: list[dict[str, Any]], model: str | None) -> dict[str, Any]:
    """Usage totals and cost from per-turn records.

    Turns known not to have been sent are excluded. Totals and the
    estimated cost are None unless every other turn's usage is known;
    ``known_cost_usd`` sums the turns whose usage is known (a lower bound
    when some usage is unknown). ``output_tokens`` already includes
    reasoning tokens, so reasoning is never added again.
    """
    from culinary_copilot.recommendations.pricing import estimate_cost_usd

    counted = [t for t in turns if t["request_sent"] is not False]
    known = [t for t in counted if t["input_tokens"] is not None and t["output_tokens"] is not None]
    complete = len(known) == len(counted)

    def _total(key: str) -> int | None:
        if not complete or any(t[key] is None for t in counted):
            return None
        return sum(int(t[key]) for t in counted)

    known_costs = [estimate_cost_usd(t["input_tokens"], t["output_tokens"], model) for t in known]
    return {
        "attempts": sum(int(t["attempts"] or 0) for t in turns),
        "provider_turns": len(counted),
        "tool_calls": sum(t["tool_calls"] for t in turns),
        "input_tokens": _total("input_tokens"),
        "output_tokens": _total("output_tokens"),
        "reasoning_tokens": _total("reasoning_tokens"),
        "response_ids": [str(t["response_id"]) for t in turns if t["response_id"]],
        "usage_complete": complete,
        "estimated_cost_usd": (
            estimate_cost_usd(_total("input_tokens"), _total("output_tokens"), model)
            if complete
            else None
        ),
        "known_cost_usd": (
            sum(c for c in known_costs if c is not None)
            if known and all(c is not None for c in known_costs)
            else None
        ),
    }


def _emit_rec_telemetry(
    *,
    ctx: dict[str, Any],
    settings: Settings,
    request_id: str,
    group_id: str,
    request_revision: int | None,
    group_revision: int | None,
    endpoint: str,
    transport: str,
    outcome: str,
    reason: str | None,
    stage_timings_ms: dict[str, int],
    total_latency_ms: int,
    cancelled: bool = False,
) -> None:
    """Emit the one telemetry event for this run (usage from the ledger)."""
    if ctx.get("emitted"):
        return
    ctx["emitted"] = True
    try:
        from culinary_copilot.obs.recommendations import emit_recommendation_event
        from culinary_copilot.recommendations.pricing import app_price_for

        model = settings.llm_rec_model
        ledger: _TurnLedger | None = ctx.get("ledger")
        turns = list(ledger.turns) if ledger is not None else []
        summary = _ledger_summary(turns, model)
        emit_recommendation_event(
            request_id=request_id,
            group_id=group_id,
            request_revision=request_revision,
            group_revision=group_revision,
            endpoint=endpoint,
            transport=transport,
            outcome=outcome,
            reason=reason,
            model=model,
            reasoning_effort=settings.llm_rec_reasoning_effort,
            pricing_version=app_price_for(model)[2],
            stage_timings_ms=dict(stage_timings_ms),
            total_latency_ms=total_latency_ms,
            attempts=summary["attempts"],
            provider_turns=summary["provider_turns"],
            tool_calls=summary["tool_calls"],
            input_tokens=summary["input_tokens"],
            output_tokens=summary["output_tokens"],
            reasoning_tokens=summary["reasoning_tokens"],
            response_ids=summary["response_ids"],
            estimated_cost_usd=summary["estimated_cost_usd"],
            known_cost_usd=summary["known_cost_usd"],
            usage_complete=summary["usage_complete"],
            turns=turns,
            cancelled=cancelled,
        )
    except Exception:
        pass


async def recommend_for_group(
    *,
    store: Any,
    engine: Any,
    settings: Settings,
    provider: Any,
    epicure: Any,
    group_id: str,
    expected_request_revision: int,
    expected_group_revision: int,
    limit: int = 3,
    dataset_id: str | None = None,
    tool_mode: bool = False,
    on_stage: StageSink | None = None,
    endpoint: str = "recommendations",
    transport: str = "json",
) -> dict[str, Any]:
    """Run the fixed recommendation workflow for one clarification group.

    ``on_stage`` is an optional async stage-reporting hook shared by the
    non-streaming and SSE transports: both run this same workflow. Stages
    carry counts and stable codes only.

    Exactly one telemetry event is emitted per run, including runs that are
    cancelled (a streaming client disconnect or a stream limit; the reason
    travels as the cancellation message) or that end in an unexpected
    error. Usage of provider turns completed before the end is kept.
    """
    ctx: dict[str, Any] = {
        "start": time.perf_counter(),
        "timings": {},
        "ids": (
            "unknown",
            group_id,
            expected_request_revision,
            expected_group_revision,
        ),
        "emitted": False,
    }

    def _final_telemetry(outcome: str, reason: str, *, cancelled: bool = False) -> None:
        request_id, grp, request_revision, group_revision = ctx["ids"]
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=request_id,
            group_id=grp,
            request_revision=request_revision,
            group_revision=group_revision,
            endpoint=endpoint,
            transport=transport,
            outcome=outcome,
            reason=reason,
            stage_timings_ms=dict(ctx["timings"]),
            total_latency_ms=int((time.perf_counter() - ctx["start"]) * 1000),
            cancelled=cancelled,
        )

    try:
        return await _recommend_impl(
            ctx=ctx,
            store=store,
            engine=engine,
            settings=settings,
            provider=provider,
            epicure=epicure,
            group_id=group_id,
            expected_request_revision=expected_request_revision,
            expected_group_revision=expected_group_revision,
            limit=limit,
            dataset_id=dataset_id,
            tool_mode=tool_mode,
            on_stage=on_stage,
            endpoint=endpoint,
            transport=transport,
        )
    except asyncio.CancelledError as exc:
        reason = str(exc.args[0]) if exc.args and exc.args[0] else "cancelled"
        _final_telemetry("cancelled", reason, cancelled=True)
        raise
    except RecommendationFailure as exc:
        _final_telemetry("error", exc.reason)
        raise
    except RecommendationNotFoundError:
        _final_telemetry("error", "unknown_group")
        raise
    except RecommendationStaleError:
        _final_telemetry("error", "stale_revision")
        raise
    except ValueError:
        _final_telemetry("error", "malformed")
        raise
    except Exception as exc:
        _final_telemetry("error", f"internal_error:{type(exc).__name__}")
        raise


async def _recommend_impl(
    *,
    ctx: dict[str, Any],
    store: Any,
    engine: Any,
    settings: Settings,
    provider: Any,
    epicure: Any,
    group_id: str,
    expected_request_revision: int,
    expected_group_revision: int,
    limit: int,
    dataset_id: str | None,
    tool_mode: bool,
    on_stage: StageSink | None,
    endpoint: str,
    transport: str,
) -> dict[str, Any]:
    from culinary_copilot.recipes.repository import SUPPORTED_DATASETS

    _start = ctx["start"]
    _stage_timings: dict[str, int] = ctx["timings"]
    _last = _start

    async def _stage(name: str, detail: dict[str, Any]) -> None:
        nonlocal _last
        now = time.perf_counter()
        # Provider stages repeat per turn: key them by turn so tool mode
        # keeps each turn's latency instead of overwriting it.
        key = f"{name}_{detail['turn']}" if "turn" in detail else name
        _stage_timings[key] = int((now - _last) * 1000)
        _last = now
        await _emit_stage(on_stage, name, detail)

    def _elapsed_ms() -> int:
        return int((time.perf_counter() - _start) * 1000)

    _ledger = _TurnLedger(provider, _stage)
    ctx["ledger"] = _ledger

    # Disabled generation fails before any network access (503).
    if not settings.llm_recommendation_enabled:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id="unknown",
            group_id=group_id,
            request_revision=expected_request_revision,
            group_revision=expected_group_revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="generation_disabled",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise RecommendationFailure(
            http_status=503,
            reason="generation_disabled",
            message="Recommendation generation is disabled (LLM_RECOMMENDATION_ENABLED=false)",
        )
    if not 1 <= limit <= settings.rec_candidate_max:
        raise ValueError(f"limit must be between 1 and {settings.rec_candidate_max}")
    if dataset_id is not None and (not dataset_id.strip() or dataset_id not in SUPPORTED_DATASETS):
        raise ValueError(f"Unsupported dataset_id; expected one of {sorted(SUPPORTED_DATASETS)}")
    if engine is None:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id="unknown",
            group_id=group_id,
            request_revision=expected_request_revision,
            group_revision=expected_group_revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="corpus_unavailable",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise RecommendationFailure(
            http_status=503, reason="corpus_unavailable", message="Recipe corpus unavailable"
        )

    snapshot = store.get_snapshot(group_id)
    if snapshot is None:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id="unknown",
            group_id=group_id,
            request_revision=expected_request_revision,
            group_revision=expected_group_revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="unknown_group",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise RecommendationNotFoundError("clarification group not found")
    state, group = snapshot
    ctx["ids"] = (state.request_id, group.group_id, state.revision, group.revision)
    if expected_request_revision != state.revision or expected_group_revision != group.revision:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="stale_revision",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise RecommendationStaleError(
            f"stale revision: have request={state.revision} group={group.revision}; "
            f"got request={expected_request_revision} group={expected_group_revision}"
        )
    try:
        _require_current_group(store, state.request_id, group.group_id)
    except RecommendationStaleError:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="stale_revision",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise
    await _stage(
        "accepted",
        {"request_revision": state.revision, "group_revision": group.revision},
    )

    ready, reason, blockers = evaluate_readiness(state)
    await _stage("readiness", {"ready": bool(ready), "reason": str(reason)})
    unenforced = [
        target
        for target in SEARCH_UNENFORCED_TARGETS
        if state.field_status.get(target) == FieldStatus.PROVIDED
    ]
    base = {
        "request_id": state.request_id,
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
        "readiness_note": READINESS_NOTE,
        "unenforced_constraints": unenforced,
        "constraints_not_verified": list(CONSTRAINTS_NOT_VERIFIED),
    }
    if not ready:
        # Conflicting cooking requirements produce clarification (200),
        # never a concurrency 409.
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="clarification",
            reason=str(reason),
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        return {
            **base,
            "outcome": RecommendationOutcome.CLARIFICATION.value,
            "clarification_reason": reason,
            "blockers": blockers,
            "recipe": None,
        }

    # Early Epicure consultation (synchronous adapter work off the loop).
    skip_reason = _simple_technique_skip(state)
    if skip_reason is not None:
        epicure_outcome = EpicureOutcome.SIMPLE_TECHNIQUE_SKIP
        epicure_suggestions: list[dict[str, Any]] = []
        epicure_note = skip_reason
    elif epicure is None:
        epicure_outcome = EpicureOutcome.DISABLED
        epicure_suggestions = []
        epicure_note = "Epicure adapter not configured"
    else:
        pantry_for_epicure = _pantry(state)[: max(1, settings.rec_epicure_max_ingredients)]
        try:
            epicure_outcome, epicure_suggestions, epicure_note = await asyncio.to_thread(
                epicure.consult,
                pantry_for_epicure,
                suggestion_count=settings.rec_epicure_suggestion_count,
            )
        except Exception as exc:
            epicure_outcome = EpicureOutcome.UNAVAILABLE
            epicure_suggestions = []
            epicure_note = f"Epicure consultation failed: {type(exc).__name__}"
    epicure_degraded = epicure_outcome in (EpicureOutcome.DISABLED, EpicureOutcome.UNAVAILABLE)
    await _stage("epicure", {"outcome": epicure_outcome.value, "degraded": epicure_degraded})

    # Retrieval (ranking reused unchanged) + complete source fetch by exact
    # identity. No store lock is held across this I/O.
    from culinary_copilot.retrieval.query import map_request_to_query

    query = map_request_to_query(state, dataset_id=dataset_id)
    effective_limit = max(1, min(limit, settings.rec_candidate_count, settings.rec_candidate_max))
    if not query.query_text:
        await _stage("retrieval", {"retrieved": 0, "fetched": 0})
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="insufficient_evidence",
            reason=INSUFFICIENT_NO_CANDIDATES,
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        return {
            **base,
            "outcome": RecommendationOutcome.INSUFFICIENT_EVIDENCE.value,
            "insufficient_reason": INSUFFICIENT_NO_CANDIDATES,
            "detail": "No dish or pantry terms available for a query.",
            "discovery": [],
            "epicure": _epicure_section(
                epicure_outcome, epicure_suggestions, epicure_note, degraded=epicure_degraded
            ),
            "recipe": None,
        }
    try:
        rows: list[dict[str, Any]] = await asyncio.to_thread(
            _search_sync, engine, query, effective_limit
        )
    except ValueError as exc:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="corpus_unavailable",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise RecommendationFailure(
            http_status=503, reason="corpus_unavailable", message="Recipe corpus unavailable"
        ) from exc
    except Exception as exc:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="corpus_unavailable",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise RecommendationFailure(
            http_status=503, reason="corpus_unavailable", message="Recipe corpus unavailable"
        ) from exc

    fetch_failed = 0
    raw_candidates: list[dict[str, Any]] = []
    for row in rows:
        ds = str(row.get("dataset_id", ""))
        sid = str(row.get("source_id", ""))
        if not ds or not sid:
            continue
        try:
            doc = await asyncio.to_thread(_fetch_exact_sync, engine, ds, sid)
        except ValueError:
            fetch_failed += 1
            continue
        except Exception:
            fetch_failed += 1
            continue
        if doc is None:
            fetch_failed += 1
            continue
        title = row.get("title") if isinstance(row.get("title"), str) else None
        raw_candidates.append(build_candidate(dataset_id=ds, source_id=sid, title=title, doc=doc))

    await _stage(
        "retrieval",
        {"retrieved": len(rows), "fetched": len(raw_candidates), "fetch_failed": fetch_failed},
    )
    if not raw_candidates:
        if fetch_failed:
            fail_reason = INSUFFICIENT_SOURCE_FETCH
            detail = "Search matched but no complete source document could be fetched."
        else:
            fail_reason = INSUFFICIENT_NO_CANDIDATES
            detail = (
                f"No recipes matched query {query.query_text!r}. No constraints "
                "were dropped or relaxed to force a match."
            )
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="insufficient_evidence",
            reason=fail_reason,
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        return {
            **base,
            "outcome": RecommendationOutcome.INSUFFICIENT_EVIDENCE.value,
            "insufficient_reason": fail_reason,
            "detail": detail,
            "discovery": [],
            "epicure": _epicure_section(
                epicure_outcome, epicure_suggestions, epicure_note, degraded=epicure_degraded
            ),
            "recipe": None,
        }

    # Deterministic constraint assessment; violated candidates can never
    # yield a recommendation and are excluded from generation evidence.
    dietary = _dietary(state)
    ceiling = query.max_minutes
    eligible: list[dict[str, Any]] = []
    hard_excluded = 0
    for candidate in raw_candidates:
        assessments = assess_candidate(candidate, dietary_constraints=dietary, time_ceiling=ceiling)
        candidate["_assessments"] = assessments
        if hard_violation(assessments) or unresolved_hard(assessments):
            hard_excluded += 1
            continue
        eligible.append(candidate)

    discovery = [
        {
            "dataset_id": c["dataset_id"],
            "source_id": c["source_id"],
            "title": c["title"],
            "provenance": c["provenance"],
            "unverified": True,
            "ready_to_cook": False,
            "why": _discovery_why(c),
            "source_defects": list(c.get("defects_blocking") or []),
            "unverified_note": (
                "Discovery pointer only: explicitly unverified, not a validated recipe."
            ),
        }
        for c in raw_candidates
        if not hard_violation(c.get("_assessments", []))
    ]

    if not eligible:
        await _stage("evidence", {"kept": 0, "dropped": 0, "hard_excluded": hard_excluded})
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="insufficient_evidence",
            reason=INSUFFICIENT_HARD_CONSTRAINT,
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        return {
            **base,
            "outcome": RecommendationOutcome.INSUFFICIENT_EVIDENCE.value,
            "insufficient_reason": INSUFFICIENT_HARD_CONSTRAINT,
            "detail": (
                "All retrieved sources carry a violated or unverified hard "
                "constraint; no successful recommendation can be made."
            ),
            "constraints": [
                a.model_dump() for c in raw_candidates for a in c.get("_assessments", [])
            ],
            "discovery": discovery,
            "epicure": _epicure_section(
                epicure_outcome, epicure_suggestions, epicure_note, degraded=epicure_degraded
            ),
            "recipe": None,
        }

    recommendable_only = [c for c in eligible if c.get("recommendable")]
    max_input = settings.llm_rec_max_input_chars
    dish = state.dish or (
        state.values.get("dish") if isinstance(state.values.get("dish"), str) else None
    )
    pantry = _pantry(state)
    facts = request_facts(state, dish=dish, pantry=pantry, time_ceiling=ceiling)
    epicure_context = (
        f"{epicure_outcome.value}: {epicure_note}. "
        f"Suggestions: {[s.get('ingredient') for s in epicure_suggestions][:10]}"
    )
    assessments_note_full = "; ".join(
        f"{c['dataset_id']}/{c['source_id']}: "
        + ", ".join(f"{a.constraint}={a.verdict.value}" for a in c.get("_assessments", []))
        for c in recommendable_only
    )

    def _selection_header_for(kept_subset: list[dict[str, Any]]) -> str:
        return selection_header(
            dish=dish,
            pantry=pantry,
            time_ceiling=ceiling,
            dietary=dietary,
            epicure_note=epicure_context,
            assessments_note=assessments_note_full,
            portions=facts.portions,
            not_provided=facts.missing_fields(),
        )

    # The response schema travels with every request, so the budget
    # measures the complete serialized request (messages + schema). The
    # schema is sized for every recommendable candidate's label (an upper
    # bound for any kept subset).
    reserve_model = selection_model_for(
        [candidate_label(i) for i in range(max(1, len(recommendable_only)))]
    )

    def _selection_request_chars(system_text: str, user_text: str) -> int:
        return request_size(
            input_items=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            response_model=reserve_model,
        )["chars"]

    if not recommendable_only:
        await _stage("evidence", {"kept": 0, "dropped": 0, "hard_excluded": hard_excluded})
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="insufficient_evidence",
            reason=INSUFFICIENT_INCOMPLETE_ONLY,
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        return {
            **base,
            "outcome": RecommendationOutcome.INSUFFICIENT_EVIDENCE.value,
            "insufficient_reason": INSUFFICIENT_INCOMPLETE_ONLY,
            "detail": (
                "No recommendable source available: required sections missing, "
                "entries omitted, or explicit source defects recorded. "
                "Nothing is promoted to a recipe."
            ),
            "discovery": discovery,
            "epicure": _epicure_section(
                epicure_outcome, epicure_suggestions, epicure_note, degraded=epicure_degraded
            ),
            "recipe": None,
        }

    # Budget the ACTUAL serialized payload (blocks + header + system):
    # whole candidates only, never truncated. A smaller later candidate
    # can still fit after an oversized one is skipped.
    kept, dropped = fit_serialized(
        recommendable_only,
        block_fn=render_candidate_block,
        header_fn=_selection_header_for,
        system=SELECTION_SYSTEM_PROMPT,
        evidence_max_chars=settings.rec_evidence_max_chars,
        total_max_chars=max_input,
        total_fn=_selection_request_chars if not tool_mode else None,
    )
    if not kept:
        await _stage("evidence", {"kept": 0, "dropped": dropped, "hard_excluded": hard_excluded})
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="insufficient_evidence",
            reason=INSUFFICIENT_BUDGET_EXCEEDED,
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        return {
            **base,
            "outcome": RecommendationOutcome.INSUFFICIENT_EVIDENCE.value,
            "insufficient_reason": INSUFFICIENT_BUDGET_EXCEEDED,
            "detail": (
                "Recommendable sources exist but the complete serialized "
                "payload cannot fit the evidence/input budgets; candidate "
                "count reduced to zero rather than truncating."
            ),
            "discovery": discovery,
            "epicure": _epicure_section(
                epicure_outcome, epicure_suggestions, epicure_note, degraded=epicure_degraded
            ),
            "recipe": None,
        }

    assessments_note = "; ".join(
        f"{c['dataset_id']}/{c['source_id']}: "
        + ", ".join(f"{a.constraint}={a.verdict.value}" for a in c.get("_assessments", []))
        for c in kept
    )

    # Server-issued candidate labels (shared by both provider paths):
    # the model selects a label; the server maps it to the exact identity.
    labels = attach_labels(kept)

    # Evidence snapshots: what the model sees, what is validated, and what
    # is rendered must be the same content (checked again before render).
    snapshot_fingerprints = {
        (c["dataset_id"], c["source_id"]): evidence_fingerprint(c) for c in kept
    }
    # Diagnostics: the exact offered set (server-issued labels, identities,
    # titles, snapshot fingerprints) travels with every outcome after this
    # point, so selection errors can be told apart from retrieval gaps.
    offered_candidates = [
        {
            "candidate_label": str(c.get("label") or ""),
            "dataset_id": c["dataset_id"],
            "source_id": c["source_id"],
            "title": c.get("title"),
            "evidence_fingerprint": snapshot_fingerprints[(c["dataset_id"], c["source_id"])],
        }
        for c in kept
    ]
    tool_continuation: dict[str, Any] = {}

    max_turns = max(1, settings.rec_max_provider_turns)

    await _stage(
        "evidence",
        {"kept": len(kept), "dropped": dropped, "hard_excluded": hard_excluded},
    )
    try:
        candidate, turns, proposal, payload_sizes = await _select(
            tool_mode=tool_mode,
            kept=kept,
            labels=labels,
            engine=engine,
            settings=settings,
            provider=_ledger,
            dish=dish,
            pantry=pantry,
            ceiling=ceiling,
            dietary=dietary,
            facts=facts,
            epicure_context=epicure_context,
            assessments_note=assessments_note,
            max_input=max_input,
            max_turns=max_turns,
            snapshot_fingerprints=snapshot_fingerprints,
            tool_continuation=tool_continuation,
        )
        key = (candidate["dataset_id"], candidate["source_id"])
        if evidence_fingerprint(candidate) != snapshot_fingerprints.get(key):
            failure = RecommendationFailure(
                http_status=502,
                reason=REASON_VALIDATION_REJECTED,
                message="Validated evidence differs from the evidence supplied to the model",
                detail={"validation_reason": REJECT_EVIDENCE_CHANGED},
            )
            _attach_turns(failure, turns, pending_attempts=0)
            raise failure
    except RecommendationFailure as exc:
        exc.detail.setdefault("offered_candidates", offered_candidates)
        if tool_continuation:
            exc.detail.setdefault("tool_continuation", tool_continuation)
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason=exc.reason,
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise

    # Typed propositions: prerequisites checked against authoritative
    # request state and the selected source; failing optional ones are
    # omitted and recorded, never invalidating the selection.
    propositions = evaluate_propositions(proposal, candidate, facts)
    await _stage("validation", {"rejected_propositions": len(propositions["rejected"])})

    # Suggestion assessment after evidence is available (deterministic).
    assessment = _assess_epicure_suggestions(epicure_suggestions, candidate)
    rendered = render_recipe(candidate)

    # Recheck state/group revisions before final publication (no lock was
    # held during I/O): intervening edits fail closed with 409.
    fresh = store.get_snapshot(group_id)
    if fresh is None:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=state.revision,
            group_revision=group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="unknown_group",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise RecommendationNotFoundError("clarification group not found")
    fresh_state, fresh_group = fresh
    if fresh_state.revision != state.revision or fresh_group.revision != group.revision:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=fresh_state.revision,
            group_revision=fresh_group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="stale_revision",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise RecommendationStaleError(
            "stale revision: state changed while generating; "
            f"started at request={state.revision} group={group.revision}, "
            f"now request={fresh_state.revision} group={fresh_group.revision}; "
            "refetch the group and retry"
        )
    try:
        _require_current_group(store, state.request_id, group.group_id)
    except RecommendationStaleError:
        _emit_rec_telemetry(
            ctx=ctx,
            settings=settings,
            request_id=state.request_id,
            group_id=group.group_id,
            request_revision=fresh_state.revision,
            group_revision=fresh_group.revision,
            endpoint=endpoint,
            transport=transport,
            outcome="error",
            reason="stale_revision",
            stage_timings_ms=dict(_stage_timings),
            total_latency_ms=_elapsed_ms(),
        )
        raise
    await _stage("revision_check", {"current": True})

    candidate_assessments = candidate.get("_assessments", [])
    _emit_rec_telemetry(
        ctx=ctx,
        settings=settings,
        request_id=state.request_id,
        group_id=group.group_id,
        request_revision=state.revision,
        group_revision=group.revision,
        endpoint=endpoint,
        transport=transport,
        outcome="recommendation",
        reason=None,
        stage_timings_ms=dict(_stage_timings),
        total_latency_ms=_elapsed_ms(),
    )
    return {
        **base,
        "outcome": RecommendationOutcome.RECOMMENDATION.value,
        "selection": {
            "dataset_id": candidate["dataset_id"],
            "source_id": candidate["source_id"],
            "title": candidate["title"],
        },
        "recipe": rendered,
        "ingredient_refs": [str(i.get("ref")) for i in candidate.get("ingredients", [])],
        "step_refs": [str(s.get("ref")) for s in candidate.get("instructions", [])],
        "selection_reasons": [r["text"] for r in propositions["reasons"]],
        "selection_reasons_note": (
            "Server-worded from allowlisted proposition types whose "
            "prerequisites were checked against your request and the "
            "selected source; the model supplied no text. Each sentence "
            "states only what it says: no dietary, allergy, nutrition, "
            "scaling, or practical-time claims. All recipe content was "
            "rendered by the server from the stored source."
        ),
        "needs": [q["text"] for q in propositions["questions"]],
        "propositions": {
            "reasons": propositions["reasons"],
            "questions": propositions["questions"],
        },
        "rejected_propositions": propositions["rejected"],
        "source_checks": _source_checks(candidate),
        "constraints": [a.model_dump() for a in candidate_assessments],
        "epicure": _epicure_section(
            epicure_outcome,
            epicure_suggestions,
            epicure_note,
            degraded=epicure_degraded,
            assessment=assessment,
        ),
        "usage": {
            **_usage_totals(turns),
            "model": turns[-1].get("model") if turns else None,
            "cost_note": "Unknown usage/cost stays unknown; no pricing applied offline.",
        },
        "evidence": {
            "candidate_count": len(kept),
            "retrieved_count": len(rows),
            "hard_excluded": hard_excluded,
            "dropped_for_budget": dropped,
            "evidence_budget_chars": settings.rec_evidence_max_chars,
            "input_budget_chars": max_input,
            "request_chars_per_turn": [p["chars"] for p in payload_sizes],
            "request_utf8_bytes_per_turn": [p["utf8_bytes"] for p in payload_sizes],
            "budget_unit_note": (
                "Budgets are characters of the serialized request (messages, "
                "continuation items, tools, schemas), not tokens; cost "
                "reservations use UTF-8 bytes as a token upper bound."
            ),
            "evidence_fingerprint": snapshot_fingerprints[key],
            "offered_candidates": offered_candidates,
            **({"tool_continuation": tool_continuation} if tool_continuation else {}),
        },
    }


async def _select(
    *,
    tool_mode: bool,
    kept: list[dict[str, Any]],
    labels: list[str],
    engine: Any,
    settings: Settings,
    provider: Any,
    dish: str | None,
    pantry: list[str],
    ceiling: float | None,
    dietary: list[str],
    facts: RequestFacts,
    epicure_context: str,
    assessments_note: str,
    max_input: int,
    max_turns: int,
    snapshot_fingerprints: dict[tuple[str, str], str],
    tool_continuation: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], SelectionProposal, list[dict[str, int]]]:
    """Provider selection on the default or native tool path."""
    if tool_mode:
        return await _tool_mode_selection(
            kept=kept,
            engine=engine,
            settings=settings,
            provider=provider,
            dish=dish,
            pantry=pantry,
            max_input=max_input,
            max_turns=max_turns,
            facts=facts,
            dietary=dietary,
            epicure_note=epicure_context,
            snapshot_fingerprints=snapshot_fingerprints,
            continuation=tool_continuation,
        )
    # Exact payload with the kept-only header; fitting above guarantees it
    # fits, and it is never truncated.
    system, user = build_selection_payload(
        dish=dish,
        pantry=pantry,
        time_ceiling=ceiling,
        dietary=dietary,
        candidates=kept,
        epicure_note=epicure_context,
        assessments_note=assessments_note,
        portions=facts.portions,
        not_provided=facts.missing_fields(),
    )
    response_model = selection_model_for(labels)
    size = request_size(
        input_items=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_model=response_model,
    )
    if size["chars"] > max_input:  # defensive; fit guarantees this
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_BUDGET_EXCEEDED,
            message="Serialized request exceeds the input budget",
            detail=_budget_detail(size, max_input, turn=1, provider_reached=False),
        )
    outcome = await _provider_selection_call(provider, system, user, response_model)
    turns = [_turn_record(outcome)]
    proposal, candidate = _proposal_from_parsed(outcome.parsed, kept, turns)
    return candidate, turns, proposal, [size]


def _turn_record(outcome: Any) -> dict[str, Any]:
    return {
        "model": outcome.model,
        "attempts": outcome.attempts,
        "latency_ms": outcome.latency_ms,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "reasoning_tokens": getattr(outcome, "reasoning_tokens", None),
        "reasoning_effort": getattr(outcome, "reasoning_effort", None),
        "response_id": getattr(outcome, "response_id", None),
        "attempt_errors": list(getattr(outcome, "attempt_details", None) or []),
    }


def _redact_error_message(message: str) -> str:
    """Bounded, secret-scrubbed exception message for persisted diagnostics.

    At most 300 chars; API keys, bearer tokens, and key assignments are
    redacted (the type is recorded separately). Failed bodies never carry
    prompts or raw model output — this is operator metadata only.
    """
    scrubbed = re.sub(r"(sk-[A-Za-z0-9-_]{4})[A-Za-z0-9-_]+", r"\1…redacted", message)
    scrubbed = re.sub(
        r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]+)",
        r"\1…redacted",
        scrubbed,
    )
    scrubbed = re.sub(
        r"(?i)(api[_-]?key\s*[:=]\s*)(['\"]?)([^\s'\"]+)",
        r"\1\2…redacted",
        scrubbed,
    )
    return scrubbed[:300]


def _provider_4xx_detail(exc: Any) -> dict[str, Any]:
    """Attempt metadata plus the bounded redacted provider error
    message/code/param for 4xx failures (same treatment as internal)."""
    detail = _provider_error_detail(exc)
    message = getattr(exc, "error_message", None)
    if message:
        detail["error_message"] = _redact_error_message(str(message))[:500]
    code = getattr(exc, "error_code", None)
    if code:
        detail["error_code"] = str(code)[:200]
    param = getattr(exc, "error_param", None)
    if param:
        detail["error_param"] = str(param)[:200]
    return detail


def _provider_error_detail(exc: Any) -> dict[str, Any]:
    """Safe operator metadata for provider failures.

    Attempt counts, SDK exception type, HTTP status, provider request id,
    token counts, and incomplete reason only — never prompts, secrets, or
    raw model output.
    """
    detail: dict[str, Any] = {"attempts": int(getattr(exc, "attempts", 0) or 0)}
    detail["provider_reached"] = bool(getattr(exc, "request_sent", False))
    attempt_errors = list(getattr(exc, "attempt_details", None) or [])
    if attempt_errors:
        detail["attempt_errors"] = attempt_errors
    for key in ("input_tokens", "output_tokens", "reasoning_tokens"):
        value = getattr(exc, key, None)
        if value is not None:
            detail[key] = value
    incomplete_reason = getattr(exc, "incomplete_reason", None)
    if incomplete_reason:
        detail["incomplete_reason"] = incomplete_reason
    response_id = getattr(exc, "response_id", None)
    if response_id:
        detail["response_id"] = response_id
    return detail


def _proposal_from_parsed(
    parsed: Any, kept: list[dict[str, Any]], turns: list[dict[str, Any]]
) -> tuple[SelectionProposal, dict[str, Any]]:
    """Resolve parsed provider output to a validated (proposal, candidate).

    Label form (production wire): ``candidate_label`` is resolved through
    the server-issued offered set to the exact identity — leading-zero
    IDs and rewrites cannot occur. Legacy id form (fakes and older
    scripts): exact-matched as before. Both forms share ref/notes schema
    validation and deterministic evidence validation.
    """
    data = dict(parsed or {})
    label = data.pop("candidate_label", None)
    if label is not None or "dataset_id" not in data:
        try:
            resolved = resolve_candidate_label(label, kept)
        except RecommendationFailure as exc:
            exc.detail.update(_failure_turn_meta(turns))
            raise
        data["dataset_id"] = resolved["dataset_id"]
        data["source_id"] = resolved["source_id"]
        proposed_label: str | None = str(label or "")[:200]
    else:
        proposed_label = None
    try:
        proposal = SelectionProposal.model_validate(data)
    except ValidationError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_SCHEMA_FAILURE,
            message="Provider output failed schema validation",
            detail={
                "validation_errors": _schema_error_summary(exc),
                **_failure_turn_meta(turns),
            },
        ) from None
    try:
        candidate = _validate_selection(proposal, kept, proposed_label=proposed_label)
    except RecommendationFailure as exc:
        exc.detail.update(_failure_turn_meta(turns))
        raise
    return proposal, candidate


def _failure_turn_meta(turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Attempt metadata for post-provider validation failures.

    The provider was reached (a billable response was received); only
    safe counts travel — never prompts or raw model output.
    """
    return {
        "attempts": sum(int(t.get("attempts") or 0) for t in turns),
        "provider_reached": True,
    }


def _attach_turns(
    exc: RecommendationFailure, completed: list[dict[str, Any]], *, pending_attempts: int
) -> None:
    """Keep completed turns' usage on a later failure (per-turn billing).

    ``completed`` turns reached the provider and returned usage;
    ``pending_attempts`` counts attempts of the failing turn (0 when it
    was never sent, e.g. a continuation that could not fit).
    """
    if not completed:
        return
    exc.detail["prior_turns"] = _usage_totals(completed)
    exc.detail["prior_response_ids"] = [
        t.get("response_id") for t in completed if t.get("response_id")
    ]
    exc.detail["attempts"] = pending_attempts
    exc.detail["provider_reached"] = True


def _budget_detail(
    size: dict[str, int], limit: int, *, turn: int, provider_reached: bool
) -> dict[str, Any]:
    return {
        "turn": turn,
        "required_chars": size["chars"],
        "limit_chars": limit,
        "required_utf8_bytes": size["utf8_bytes"],
        "unit_note": "characters of the serialized request, not tokens",
        "attempts": 0,
        "provider_reached": provider_reached,
    }


async def _provider_selection_call(
    provider: Any, system: str, user: str, response_model: Any
) -> Any:
    """One bounded provider call with explicit failure mapping.

    Refusal is a controlled failed outcome (502, reason provider_refusal),
    never insufficient evidence. Timeout maps to 504; unavailable/auth/
    rate-limit/not-found to 503 with distinct reasons; bad-request and
    other 4xx to 502 with distinct reasons (never collapsed into
    provider_unavailable); incomplete to 502 (truncated, with usage and
    incomplete reason); content-filter trips and schema problems to 502.
    Rate limits previously escaped as uncaught 500s; they are now
    controlled 503s that bill nothing and never stop the run alone.
    """
    try:
        return await provider.complete_recommendation(
            system=system, user=user, response_model=response_model
        )
    except ProviderDisabledError as exc:
        raise RecommendationFailure(
            http_status=503, reason="generation_disabled", message=str(exc)[:200]
        ) from None
    except ProviderTimeoutError as exc:
        raise RecommendationFailure(
            http_status=504,
            reason="provider_timeout",
            message="Provider request timed out",
            detail=_provider_error_detail(exc),
        ) from exc
    except ProviderAuthError as exc:
        raise RecommendationFailure(
            http_status=503,
            reason="provider_auth",
            message="Provider authentication failed",
            detail=_provider_error_detail(exc),
        ) from exc
    except ProviderNotFoundError as exc:
        raise RecommendationFailure(
            http_status=503,
            reason=REASON_NOT_FOUND,
            message="Provider resource not found",
            detail=_provider_4xx_detail(exc),
        ) from exc
    except ProviderRateLimitError as exc:
        raise RecommendationFailure(
            http_status=503,
            reason=REASON_RATE_LIMITED,
            message="Provider rate limited the request",
            detail=_provider_error_detail(exc),
        ) from exc
    except (ProviderUnavailableError, ConnectionError) as exc:
        detail = _provider_error_detail(exc) if isinstance(exc, ProviderCallError) else {}
        raise RecommendationFailure(
            http_status=503,
            reason="provider_unavailable",
            message="Provider unavailable",
            detail=detail,
        ) from exc
    except ProviderInternalError as exc:
        detail = _provider_error_detail(exc)
        detail["error_message"] = _redact_error_message(str(exc))
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INTERNAL_ERROR,
            message="Provider path failed before a billable call completed",
            detail=detail,
        ) from exc
    except ProviderBadRequestError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_BAD_REQUEST,
            message="Provider rejected the request",
            detail=_provider_4xx_detail(exc),
        ) from exc
    except ProviderRequestError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_REQUEST_ERROR,
            message="Provider rejected the request",
            detail=_provider_4xx_detail(exc),
        ) from exc
    except ProviderRefusalError as exc:
        # Controlled failed outcome: 502 with reason provider_refusal.
        # Documented mapping; never mislabeled insufficient_evidence.
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_PROVIDER_REFUSAL,
            message="Provider refused the selection request",
        ) from exc
    except ProviderContentFilterError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_CONTENT_FILTER,
            message="Provider content filter ended the response",
            detail=_provider_error_detail(exc),
        ) from exc
    except ProviderIncompleteError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_TRUNCATED,
            message="Provider response was incomplete",
            detail=_provider_error_detail(exc),
        ) from exc
    except ProviderSchemaError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_SCHEMA_FAILURE,
            message="Provider returned no structured output",
        ) from exc


async def _native_turn_call(
    provider: Any,
    *,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: dict[str, Any] | None,
    response_model: Any,
) -> Any:
    """One native tool turn with the same failure mapping as structured calls."""
    try:
        return await provider.complete_native_tool_turn(
            input_items=input_items,
            tools=tools,
            tool_choice=tool_choice,
            response_model=response_model,
        )
    except ProviderDisabledError as exc:
        raise RecommendationFailure(
            http_status=503, reason="generation_disabled", message=str(exc)[:200]
        ) from None
    except ProviderTimeoutError as exc:
        raise RecommendationFailure(
            http_status=504,
            reason="provider_timeout",
            message="Provider request timed out",
            detail=_provider_error_detail(exc),
        ) from exc
    except ProviderAuthError as exc:
        raise RecommendationFailure(
            http_status=503,
            reason="provider_auth",
            message="Provider authentication failed",
            detail=_provider_error_detail(exc),
        ) from exc
    except ProviderNotFoundError as exc:
        raise RecommendationFailure(
            http_status=503,
            reason=REASON_NOT_FOUND,
            message="Provider resource not found",
            detail=_provider_4xx_detail(exc),
        ) from exc
    except ProviderRateLimitError as exc:
        raise RecommendationFailure(
            http_status=503,
            reason=REASON_RATE_LIMITED,
            message="Provider rate limited the request",
            detail=_provider_error_detail(exc),
        ) from exc
    except (ProviderUnavailableError, ConnectionError) as exc:
        detail = _provider_error_detail(exc) if isinstance(exc, ProviderCallError) else {}
        raise RecommendationFailure(
            http_status=503,
            reason="provider_unavailable",
            message="Provider unavailable",
            detail=detail,
        ) from exc
    except ProviderInternalError as exc:
        detail = _provider_error_detail(exc)
        detail["error_message"] = _redact_error_message(str(exc))
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INTERNAL_ERROR,
            message="Provider path failed before a billable call completed",
            detail=detail,
        ) from exc
    except ProviderBadRequestError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_BAD_REQUEST,
            message="Provider rejected the request",
            detail=_provider_4xx_detail(exc),
        ) from exc
    except ProviderRequestError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_REQUEST_ERROR,
            message="Provider rejected the request",
            detail=_provider_4xx_detail(exc),
        ) from exc
    except ProviderRefusalError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_PROVIDER_REFUSAL,
            message="Provider refused the tool request",
        ) from exc
    except ProviderContentFilterError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_CONTENT_FILTER,
            message="Provider content filter ended the response",
            detail=_provider_error_detail(exc),
        ) from exc
    except ProviderIncompleteError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_TRUNCATED,
            message="Provider response was incomplete",
            detail=_provider_error_detail(exc),
        ) from exc
    except ProviderSchemaError as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_SCHEMA_FAILURE,
            message="Provider returned no output",
        ) from exc


def _native_turn_record(turn: Any) -> dict[str, Any]:
    return {
        "model": turn.model,
        "attempts": turn.attempts,
        "latency_ms": turn.latency_ms,
        "input_tokens": turn.input_tokens,
        "output_tokens": turn.output_tokens,
        "reasoning_tokens": getattr(turn, "reasoning_tokens", None),
        "reasoning_effort": getattr(turn, "reasoning_effort", None),
        "response_id": getattr(turn, "response_id", None),
        "attempt_errors": list(getattr(turn, "attempt_details", None) or []),
        "tool_calls": len(turn.tool_calls),
        # Diagnosable tool-call envelope: call ids, names, and parsed
        # arguments (identifier-shaped values only).
        "tool_call_envelope": [
            {
                "call_id": _identifier_echo(call.call_id),
                "name": _identifier_echo(call.name),
                "arguments": _safe_arguments(call.arguments),
            }
            for call in turn.tool_calls
        ],
        # Continuation items by type and id ONLY: reasoning content,
        # summaries, and encrypted content are never recorded.
        "continuation_items": [
            {
                "type": _identifier_echo(item.get("type")),
                "id": _identifier_echo(item.get("id")) if item.get("id") else None,
                **(
                    {"call_id": _identifier_echo(item.get("call_id"))}
                    if item.get("call_id")
                    else {}
                ),
            }
            for item in (getattr(turn, "chain_items", None) or [])
            if isinstance(item, dict)
        ],
    }


def _safe_arguments(raw: str) -> Any:
    """Parsed tool arguments with identifier-shaped values only."""
    try:
        parsed = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return "<unparseable>"
    if not isinstance(parsed, dict):
        return "<not an object>"
    return {
        (key if _SAFE_LOC_PART.match(str(key)) else "<redacted>"): (
            _identifier_echo(value) if isinstance(value, str) else type(value).__name__
        )
        for key, value in list(parsed.items())[:10]
    }


def _parse_tool_arguments(raw: str) -> dict[str, Any]:
    """Strict get_recipe argument contract.

    Label form (production): exactly ``{candidate_label}`` with a string
    value; the server maps it to the exact identity. Legacy form (fakes
    and older scripts): exactly ``{dataset_id, source_id}`` strings,
    still exact-matched. No other shape is accepted.
    """
    try:
        args = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INVALID_TOOL,
            message="Tool arguments are not valid JSON",
            detail={"arguments_error": "malformed_json"},
        ) from exc
    if not isinstance(args, dict):
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INVALID_TOOL,
            message="Tool arguments must be a JSON object",
            detail={"arguments_error": "not_an_object"},
        )
    if set(args) == {"candidate_label"} and isinstance(args.get("candidate_label"), str):
        if not args["candidate_label"]:
            raise RecommendationFailure(
                http_status=502,
                reason=REASON_INVALID_TOOL,
                message="Tool candidate_label must be a non-empty string",
                detail={"arguments_error": "malformed_arguments"},
            )
        return {"candidate_label": args["candidate_label"]}
    if (
        set(args) != {"dataset_id", "source_id"}
        or not args.get("dataset_id")
        or not args.get("source_id")
        or not isinstance(args.get("dataset_id"), str)
    ):
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INVALID_TOOL,
            message="Tool arguments must be exactly {candidate_label} or {dataset_id, source_id}",
            detail={"arguments_error": "malformed_arguments"},
        )
    if not isinstance(args.get("source_id"), str):
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INVALID_TOOL,
            message="Tool arguments must be exactly {dataset_id, source_id} strings",
            detail={"arguments_error": "malformed_arguments"},
        )
    return {"dataset_id": args["dataset_id"], "source_id": args["source_id"]}


def resolve_candidate_label(label: Any, offered: list[dict[str, Any]]) -> dict[str, Any]:
    """Map a server-issued label back to the exact offered candidate.

    Labels are compared exactly (no fuzzy or normalized matching).
    Unknown labels fail closed with the bounded proposed value plus the
    offered labels so the next run can be diagnosed.
    """
    text = str(label or "")[:200]
    if text:
        for candidate in offered:
            if str(candidate.get("label") or "") == text:
                return candidate
    raise RecommendationFailure(
        http_status=502,
        reason=REASON_VALIDATION_REJECTED,
        message="Selected candidate label was not offered",
        detail={
            "validation_reason": REJECT_UNKNOWN_IDENTITY,
            "proposed": {"candidate_label": _identifier_echo(text)},
            "offered": [
                {
                    "candidate_label": str(c.get("label") or "")[:20],
                    "dataset_id": str(c.get("dataset_id") or "")[:200],
                    "source_id": str(c.get("source_id") or "")[:200],
                }
                for c in offered[:5]
            ],
        },
    )


async def _tool_mode_selection(
    *,
    kept: list[dict[str, Any]],
    engine: Any,
    settings: Settings,
    provider: Any,
    dish: str | None,
    pantry: list[str],
    max_input: int,
    max_turns: int,
    facts: RequestFacts,
    dietary: list[str],
    epicure_note: str,
    snapshot_fingerprints: dict[tuple[str, str], str],
    continuation: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], SelectionProposal, list[dict[str, int]]]:
    """Opt-in native get_recipe tool mode: metadata → one allowlisted fetch → final.

    Native provider function-call lifecycle (Responses API):

    - Turn 1 exposes the single strict ``get_recipe`` function with forced
      tool choice over bounded candidate metadata (never full documents).
      The returned native ``function_call`` (name, JSON arguments,
      ``call_id``) is validated against the allowlist and dispatched as one
      read-only exact-pair fetch.
    - The fetched document must match the request's evidence snapshot
      (content fingerprint, not counts); a changed source fails closed. The
      snapshot is what the model sees, what is validated, and what is
      rendered.
    - Turn 2 sends final-selection instructions plus server request context
      (replacing the turn-1 tool-only system item), the retained turn-1
      user message, every continuation item (reasoning items, then the
      ``function_call`` with its ``call_id``), and the exact
      ``function_call_output``. No tools are offered.
    - Each turn's complete serialized request (items, tools, schemas) is
      budgeted before it is sent; nothing is truncated. A continuation that
      cannot fit stops before turn 2 with turn-1 usage preserved.

    Bounds are enforced separately: tool calls (``REC_MAX_TOOL_CALLS``),
    provider turns (``REC_MAX_PROVIDER_TURNS``), and transport attempts
    (recorded per turn, bounded by ``LLM_REC_MAX_RETRIES`` in the
    provider module).
    """
    if max_turns < 2:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_TURN_LIMIT,
            message="Turn ceiling reached before tool dispatch",
        )
    if settings.rec_max_tool_calls < 1:
        raise RecommendationFailure(
            http_status=502, reason=REASON_TURN_LIMIT, message="Tool calls are not permitted"
        )
    turns: list[dict[str, Any]] = []
    sizes: list[dict[str, int]] = []
    # Diagnostic record of the continuation (ids, types, sizes,
    # fingerprints; never reasoning content), filled as the flow advances.
    trace = continuation if continuation is not None else {}
    tool_choice = {"type": "function", "name": "get_recipe"}
    reserve_tools = [get_recipe_function([candidate_label(i) for i in range(len(kept))])]

    def _turn1_chars(system_text: str, user_text: str) -> int:
        return request_size(
            input_items=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            tools=reserve_tools,
            tool_choice=tool_choice,
        )["chars"]

    meta_kept, _meta_dropped = fit_serialized(
        kept,
        block_fn=render_metadata_block,
        header_fn=lambda ks: metadata_header(dish=dish, pantry=pantry),
        system=TOOL_METADATA_SYSTEM_PROMPT,
        evidence_max_chars=max_input,
        total_max_chars=max_input,
        total_fn=_turn1_chars,
    )
    if not meta_kept:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_BUDGET_EXCEEDED,
            message="Candidate metadata cannot fit the input budget",
            detail={"turn": 1, "limit_chars": max_input, "attempts": 0},
        )
    allowed = {(c["dataset_id"], c["source_id"]) for c in meta_kept}
    meta_labels = [str(c.get("label") or "") for c in meta_kept]
    system, user = build_tool_metadata_payload(dish=dish, pantry=pantry, candidates=meta_kept)
    base_items = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tools = [get_recipe_function(meta_labels)]
    trace["turn1_offered_labels"] = list(meta_labels)
    size1 = request_size(input_items=base_items, tools=tools, tool_choice=tool_choice)
    if size1["chars"] > max_input:  # defensive; fit guarantees this
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_BUDGET_EXCEEDED,
            message="Turn-1 request exceeds the input budget",
            detail=_budget_detail(size1, max_input, turn=1, provider_reached=False),
        )
    sizes.append(size1)
    first = await _native_turn_call(
        provider,
        input_items=base_items,
        tools=tools,
        tool_choice=tool_choice,
        response_model=None,
    )
    turns.append(_native_turn_record(first))

    # Everything between the turns fails closed with turn-1 usage kept.
    try:
        call, snapshot = await _dispatch_tool_call(
            first=first,
            meta_kept=meta_kept,
            allowed=allowed,
            engine=engine,
            settings=settings,
            snapshot_fingerprints=snapshot_fingerprints,
        )
        resolved_label = str(snapshot.get("label") or "")
        tool_block = render_candidate_block(snapshot)
        trace["call_id"] = _identifier_echo(call.call_id)
        trace["fetched"] = {
            "candidate_label": resolved_label,
            "dataset_id": snapshot["dataset_id"],
            "source_id": snapshot["source_id"],
            "snapshot_fingerprint": snapshot_fingerprints.get(
                (snapshot["dataset_id"], snapshot["source_id"])
            ),
            "fetched_matches_snapshot": True,
        }
        final_system = build_tool_final_system(
            time_ceiling=facts.time_ceiling,
            dietary=dietary,
            portions=facts.portions,
            not_provided=facts.missing_fields(),
            assessments_note=", ".join(
                f"{a.constraint}={a.verdict.value}" for a in snapshot.get("_assessments", [])
            ),
            epicure_note=epicure_note,
        )
        chained = (
            [{"role": "system", "content": final_system}, base_items[1]]
            + list(first.chain_items or [])
            + [{"type": "function_call_output", "call_id": call.call_id, "output": tool_block}]
        )
        final_model = selection_model_for([resolved_label])
        size2 = request_size(input_items=chained, response_model=final_model)
        trace["turn2_input_items"] = [
            {
                "kind": _identifier_echo(item.get("role") or item.get("type")),
                **({"id": _identifier_echo(item["id"])} if item.get("id") else {}),
                **({"call_id": _identifier_echo(item["call_id"])} if item.get("call_id") else {}),
            }
            for item in chained
        ]
        trace["turn2_system_is_final_selection_prompt"] = final_system.startswith(
            TOOL_FINAL_SELECTION_SYSTEM_PROMPT
        )
        trace["turn2_tools_offered"] = False
        trace["tool_result_chars"] = len(tool_block)
        trace["turn2_request_chars"] = size2["chars"]
        trace["turn2_request_utf8_bytes"] = size2["utf8_bytes"]
        if len(tool_block) > settings.rec_evidence_max_chars or size2["chars"] > max_input:
            # Budget the actual continuation (instructions, retained
            # context, continuation items, tool result, schema). Evidence
            # is never truncated to fit: stop before sending turn 2.
            detail = _budget_detail(size2, max_input, turn=2, provider_reached=True)
            detail["tool_result_chars"] = len(tool_block)
            detail["evidence_limit_chars"] = settings.rec_evidence_max_chars
            raise RecommendationFailure(
                http_status=502,
                reason=REASON_BUDGET_EXCEEDED,
                message="Turn-2 continuation cannot fit the input budgets",
                detail=detail,
            )
    except RecommendationFailure as exc:
        _attach_turns(exc, turns, pending_attempts=0)
        raise
    sizes.append(size2)
    try:
        second = await _native_turn_call(
            provider,
            input_items=chained,
            tools=None,
            tool_choice=None,
            response_model=final_model,
        )
    except RecommendationFailure as exc:
        # Keep turn-1 usage and response ids for per-turn billing; the
        # failing turn's own attempt count stays in ``attempts``.
        _attach_turns(exc, turns, pending_attempts=int(exc.detail.get("attempts") or 0))
        raise
    turns.append(_native_turn_record(second))
    try:
        if second.tool_calls:
            # A repeated tool request where the final selection belongs is
            # a turn-limit violation, not a new dispatch: no agent loop.
            raise RecommendationFailure(
                http_status=502,
                reason=REASON_TURN_LIMIT,
                message="Repeated tool request after the single allowed call",
            )
        if second.parsed is None:
            raise RecommendationFailure(
                http_status=502, reason=REASON_EMPTY, message="Empty final selection response"
            )
        # Validation set is exactly the snapshot supplied to the model.
        final_proposal, candidate = _proposal_from_parsed(second.parsed, [snapshot], turns)
    except RecommendationFailure as exc:
        _attach_turns(exc, turns, pending_attempts=0)
        raise
    return candidate, turns, final_proposal, sizes


async def _dispatch_tool_call(
    *,
    first: Any,
    meta_kept: list[dict[str, Any]],
    allowed: set[tuple[str, str]],
    engine: Any,
    settings: Settings,
    snapshot_fingerprints: dict[tuple[str, str], str],
) -> tuple[Any, dict[str, Any]]:
    """Validate the single turn-1 call, fetch once, return (call, snapshot).

    The returned snapshot is the request's own evidence candidate (with
    its label and server assessments), confirmed unchanged by comparing
    the refetched document's content fingerprint.
    """
    if not first.tool_calls:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INVALID_TOOL,
            message="First turn returned no function call",
            detail={"arguments_error": "absent_call"},
        )
    if len(first.tool_calls) > 1:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INVALID_TOOL,
            message="First turn returned multiple function calls; only one is allowed",
            detail={"arguments_error": "multiple_calls"},
        )
    call = first.tool_calls[0]
    if call.name != "get_recipe":
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INVALID_TOOL,
            message=f"Unknown tool {call.name!r}; only get_recipe is allowlisted",
        )
    if not call.call_id:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INVALID_TOOL,
            message="Function call is missing its call identifier",
            detail={"arguments_error": "missing_call_id"},
        )
    # The continuation must carry the function_call item answered by the
    # function_call_output (same call_id), or the provider cannot pair them.
    if not any(
        isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == call.call_id
        for item in first.chain_items or []
    ):
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_INVALID_TOOL,
            message="Continuation items lack the function_call for this call_id",
            detail={"arguments_error": "missing_continuation_item"},
        )
    args = _parse_tool_arguments(call.arguments)
    if "candidate_label" in args:
        # Label form (production wire): map through the OFFERED metadata
        # set, never the full kept list, so cross-request labels fail.
        snapshot = resolve_candidate_label(args["candidate_label"], meta_kept)
    elif (args["dataset_id"], args["source_id"]) not in allowed:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_VALIDATION_REJECTED,
            message="Tool call identity is not in the candidate set",
            detail={"validation_reason": REJECT_UNKNOWN_IDENTITY},
        )
    else:
        snapshot = next(
            c
            for c in meta_kept
            if (c["dataset_id"], c["source_id"]) == (args["dataset_id"], args["source_id"])
        )
    if settings.rec_max_tool_calls < 1:
        raise RecommendationFailure(
            http_status=502, reason=REASON_TURN_LIMIT, message="Tool-call ceiling exceeded"
        )
    key = (snapshot["dataset_id"], snapshot["source_id"])
    try:
        doc = await asyncio.to_thread(_fetch_exact_sync, engine, key[0], key[1])
    except Exception as exc:
        raise RecommendationFailure(
            http_status=503, reason="corpus_unavailable", message="Recipe corpus unavailable"
        ) from exc
    if doc is None:
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_VALIDATION_REJECTED,
            message="Tool call identity could not be fetched",
            detail={"validation_reason": REJECT_UNKNOWN_IDENTITY},
        )
    fetched = build_candidate(
        dataset_id=key[0], source_id=key[1], title=snapshot.get("title"), doc=doc
    )
    fetched_fingerprint = evidence_fingerprint(fetched)
    if fetched_fingerprint != snapshot_fingerprints.get(key):
        raise RecommendationFailure(
            http_status=502,
            reason=REASON_VALIDATION_REJECTED,
            message="Fetched source differs from the request's evidence snapshot",
            detail={
                "validation_reason": REJECT_EVIDENCE_CHANGED,
                "snapshot_fingerprint": snapshot_fingerprints.get(key),
                "fetched_fingerprint": fetched_fingerprint,
            },
        )
    return call, snapshot
