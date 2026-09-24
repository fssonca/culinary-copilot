"""Retrieval orchestration over authoritative clarification state (Phase 1).

Concurrency model mirrors the replan endpoint: snapshot the store (brief
lock, deep copies), release the lock, run blocking repository I/O in worker
threads via ``asyncio.to_thread``, then re-read the store and fail closed
with 409 when an intervening edit advanced either revision or a newer
group superseded the requested one. No store lock is held across I/O.

Readiness is recomputed from state on every call
(:func:`evaluate_readiness` reuses ``retrieval_ready`` plus conflict and
stable blocker-code checks). Caller-sent ready flags are never trusted.

Returned records are bounded evidence summaries of full documents, not
complete recipes: excerpts and ingredient-name lists are truncated (see
``evidence_limits``). Generation must later fetch complete documents using
the exact ``(dataset_id, source_id)`` identities.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from culinary_copilot.domain.clarification import (
    CookingRequestState,
    FieldStatus,
    is_blocked,
)
from culinary_copilot.domain.rule_planner import retrieval_ready
from culinary_copilot.recipes.durations import DurationStatus, classify_total
from culinary_copilot.retrieval.query import RetrievalQuery, map_request_to_query
from culinary_copilot.services.clarification_service import (
    READINESS_NOTE,
    SEARCH_UNENFORCED_TARGETS,
)

DEFAULT_LIMIT = 5
MAX_LIMIT = 10
MAX_EXCERPT_CHARS = 600
MAX_INGREDIENT_NAMES = 30

EVIDENCE_LIMITS = {
    "excerpt_chars": MAX_EXCERPT_CHARS,
    "ingredient_names": MAX_INGREDIENT_NAMES,
}

CONSTRAINTS_NOT_VERIFIED = (
    "dietary_compatibility",
    "allergy_safety",
    "nutrition",
    "quantities_units",
    "scaling",
    "completeness",
    "equipment_compatibility",
)

CEILING_EXPLANATION = (
    "Recipes without a usable reported total duration are excluded by this time limit."
)


class RetrievalNotFoundError(Exception):
    """Unknown clarification group."""


class RetrievalStaleError(Exception):
    """Expected revisions no longer match authoritative state."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def evaluate_readiness(state: CookingRequestState) -> tuple[bool, str, list[str]]:
    """Recompute retrieval readiness from authoritative state.

    Returns ``(ready, reason, blockers)``. ``reason`` is one of
    ``ready`` / ``conflicting`` / ``blocked`` / ``needs_clarification``.
    Blocked essentials are matched by stable blocker code, never by
    substring search in message text.
    """
    blockers: list[str] = []
    for field_name, status in state.field_status.items():
        if status == FieldStatus.CONFLICTING:
            blockers.append(
                f"conflicting_{field_name}: conflicting requirements need clarification"
            )
    for blocker in state.blockers:
        if blocker not in blockers:
            blockers.append(blocker)
    blockers = blockers[:50]

    if any(status == FieldStatus.CONFLICTING for status in state.field_status.values()):
        return False, "conflicting", blockers
    if is_blocked(blockers):
        return False, "blocked", blockers
    if retrieval_ready(state):
        return True, "ready", blockers
    return False, "needs_clarification", blockers


def _require_current_group(store: Any, request_id: str, group_id: str) -> None:
    """Enforce the approved contract: retrieval needs the current group.

    Older groups stay readable as history elsewhere; here a superseded
    group fails closed with a 409 directing the client to the current one.
    """
    current = store.current_group_id(request_id)
    if current is not None and current != group_id:
        raise RetrievalStaleError(
            f"superseded group: retrieval requires the current group {current}; "
            "refetch the group and retry with its revisions"
        )


def _excerpt_from_doc(doc: dict[str, Any]) -> str:
    description = ""
    raw_desc = doc.get("description")
    if isinstance(raw_desc, str) and raw_desc.strip():
        description = " ".join(raw_desc.split())
    steps: list[str] = []
    instructions = doc.get("instructions")
    if isinstance(instructions, list):
        for step in instructions[:2]:
            if isinstance(step, str) and step.strip():
                steps.append(" ".join(step.split()))
    parts = []
    if description:
        parts.append(description)
    parts.extend(steps)
    return " | ".join(parts)[:MAX_EXCERPT_CHARS]


def _nutrition_values_present(doc: dict[str, Any]) -> bool:
    """Presence of at least one nutrition value; never verification.

    Units and basis stay unverified (unknown) even when values exist.
    """
    nutrition = doc.get("nutrition")
    if not isinstance(nutrition, dict):
        return False
    return any(value is not None for value in nutrition.values())


def _usable_servings(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def build_evidence(
    *, dataset_id: str, source_id: str, title: str | None, doc: dict[str, Any] | None
) -> dict[str, Any]:
    """Build one bounded evidence summary from a full source document.

    Missing metadata stays explicit unknown; older documents that predate
    capability/quality fields report ``capabilities_unknown`` rather than
    assuming validation succeeded. A reported zero total duration is
    ``reported_zero_unverified`` (unknown for filtering and presentation);
    only ``reported_positive`` totals count as known.
    """
    if doc is None:
        return {
            "dataset_id": dataset_id,
            "source_id": source_id,
            "title": title,
            "evidence_type": "bounded_summary",
            "document_available": False,
            "unknowns": ["full_document"],
            "note": "Search matched but the full document was unavailable; no claims made.",
        }
    doc_title = doc.get("title") if isinstance(doc.get("title"), str) else title
    servings = _usable_servings(doc.get("servings"))
    servings_known = servings is not None
    durations = doc.get("durations_minutes")
    reported_raw = durations.get("TotalTime") if isinstance(durations, dict) else None
    duration_status, usable_total = classify_total(reported_raw)
    total_known = duration_status is DurationStatus.REPORTED_POSITIVE

    ingredients = doc.get("ingredients")
    ingredient_names: list[str] = []
    ingredient_count: int | None = None
    if isinstance(ingredients, list):
        ingredient_count = len(ingredients)
        for item in ingredients[:MAX_INGREDIENT_NAMES]:
            if isinstance(item, dict):
                name = item.get("canonical") or item.get("name") or item.get("original")
                if isinstance(name, str) and name.strip():
                    ingredient_names.append(" ".join(name.split())[:200])

    capabilities = doc.get("capabilities")
    capabilities_unknown = not isinstance(capabilities, dict)
    available_fields = doc.get("available_fields")
    available_unknown = not isinstance(available_fields, dict)
    flags = doc.get("flags")
    flags_list = list(flags) if isinstance(flags, list) else []
    quality_issues = doc.get("quality_issues")
    quality_list = list(quality_issues) if isinstance(quality_issues, list) else []
    provenance = doc.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {"dataset_id": dataset_id, "source_id": source_id}

    unknowns: list[str] = []
    if not total_known:
        unknowns.append("total_minutes")
    if not servings_known:
        unknowns.append("servings")
    if capabilities_unknown:
        unknowns.append("capabilities")
    if available_unknown:
        unknowns.append("available_fields")
    unknowns.extend(
        [
            "nutrition",
            "dietary_compatibility",
            "allergy_safety",
            "quantities_units",
            "scaling",
        ]
    )

    source_url = doc.get("source_url")
    return {
        "dataset_id": dataset_id,
        "source_id": source_id,
        "title": doc_title,
        "evidence_type": "bounded_summary",
        "document_available": True,
        "total_minutes": usable_total,
        "total_minutes_reported": reported_raw,
        "duration_status": duration_status.value,
        "total_minutes_known": total_known,
        "servings": servings,
        "servings_known": servings_known,
        "ingredient_count": ingredient_count,
        "ingredient_names": ingredient_names,
        "provenance": provenance,
        "flags": flags_list,
        "capabilities": capabilities if isinstance(capabilities, dict) else None,
        "capabilities_unknown": capabilities_unknown,
        "available_fields": available_fields if isinstance(available_fields, dict) else None,
        "available_fields_unknown": available_unknown,
        "quality_issues": quality_list,
        "nutrition_values_present": _nutrition_values_present(doc),
        "excerpt": _excerpt_from_doc(doc),
        "source_url": source_url,
        "source_note": (
            "No per-recipe source URL exists in the corpus; source_url is "
            "None when the source provides none (never invented)."
        ),
        "unknowns": unknowns,
    }


def _search_sync(engine: Any, query: RetrievalQuery, limit: int) -> list[dict[str, Any]]:
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


def _fetch_docs_sync(engine: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from culinary_copilot.recipes.repository import get_recipe

    out: list[dict[str, Any]] = []
    for row in rows:
        dataset_id = str(row.get("dataset_id", ""))
        source_id = str(row.get("source_id", ""))
        if not dataset_id or not source_id:
            continue
        try:
            doc = get_recipe(engine, source_id, dataset_id=dataset_id)
        except ValueError:
            continue
        evidence = build_evidence(
            dataset_id=dataset_id,
            source_id=source_id,
            title=row.get("title") if isinstance(row.get("title"), str) else None,
            doc=doc,
        )
        evidence["search_score"] = row.get("score")
        evidence["search_total_minutes"] = row.get("total_minutes")
        evidence["search_servings"] = row.get("servings")
        out.append(evidence)
    return out


async def retrieve_for_group(
    *,
    store: Any,
    engine: Any,
    group_id: str,
    expected_request_revision: int,
    expected_group_revision: int,
    limit: int = DEFAULT_LIMIT,
    dataset_id: str | None = None,
) -> dict[str, Any]:
    """Run retrieval for one clarification group; see module docstring."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if engine is None:
        raise ValueError("Recipe corpus unavailable for retrieval")

    snapshot = store.get_snapshot(group_id)
    if snapshot is None:
        raise RetrievalNotFoundError("clarification group not found")
    state, group = snapshot
    if expected_request_revision != state.revision or expected_group_revision != group.revision:
        raise RetrievalStaleError(
            f"stale revision: have request={state.revision} group={group.revision}; "
            f"got request={expected_request_revision} group={expected_group_revision}"
        )
    _require_current_group(store, state.request_id, group.group_id)

    ready, reason, blockers = evaluate_readiness(state)
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
        "evidence_note": (
            "Bounded evidence summaries of full documents. Generation must fetch "
            "complete documents using the exact (dataset_id, source_id) identities."
        ),
        "evidence_limits": dict(EVIDENCE_LIMITS),
    }
    if not ready:
        query = map_request_to_query(state, dataset_id=dataset_id)
        return {
            **base,
            "outcome": "not_ready",
            "not_ready_reason": reason,
            "blockers": blockers,
            "query": {
                "query_text": query.query_text,
                "match_mode": query.match_mode,
                "max_minutes": query.max_minutes,
                "dataset_id": query.dataset_id,
                "applied_filters": query.applied_filters,
                "unsupported_constraints": query.unsupported_constraints,
                "explanation": query.explanation,
            },
            "results": [],
            "result_count": 0,
            "explanation": (
                "Request is not ready for retrieval "
                f"(reason={reason}). No recipe search was run; "
                "resolve the documented blockers or provide dish/ingredients first."
            ),
        }

    query = map_request_to_query(state, dataset_id=dataset_id)
    if not query.query_text:
        return {
            **base,
            "outcome": "not_ready",
            "not_ready_reason": "needs_clarification",
            "blockers": blockers,
            "query": {
                "query_text": "",
                "match_mode": query.match_mode,
                "max_minutes": query.max_minutes,
                "dataset_id": query.dataset_id,
                "applied_filters": query.applied_filters,
                "unsupported_constraints": query.unsupported_constraints,
                "explanation": query.explanation,
            },
            "results": [],
            "result_count": 0,
            "explanation": "No dish or pantry terms available for a query.",
        }

    try:
        rows: list[dict[str, Any]] = await asyncio.to_thread(_search_sync, engine, query, limit)
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    except Exception as exc:
        raise ValueError(f"Recipe corpus unavailable: {exc!r}") from None
    try:
        results = await asyncio.to_thread(_fetch_docs_sync, engine, rows)
    except Exception as exc:
        raise ValueError(f"Recipe corpus unavailable: {exc!r}") from None

    # Detect intervening edits or superseding groups before publication
    # (no lock was held during I/O).
    fresh = store.get_snapshot(group_id)
    if fresh is None:
        raise RetrievalNotFoundError("clarification group not found")
    fresh_state, fresh_group = fresh
    if fresh_state.revision != state.revision or fresh_group.revision != group.revision:
        raise RetrievalStaleError(
            "stale revision: state changed while retrieving; "
            f"started at request={state.revision} group={group.revision}, "
            f"now request={fresh_state.revision} group={fresh_group.revision}; "
            "refetch the group and retry"
        )
    _require_current_group(store, state.request_id, group.group_id)

    datasets_searched = (
        [query.dataset_id]
        if query.dataset_id is not None
        else ["AkashPS11/recipes_data_food.com", "odunola/foodie"]
    )
    datasets_in_results = sorted(
        {str(r.get("dataset_id", "")) for r in results if r.get("dataset_id")}
    )
    ceiling_note = f" {CEILING_EXPLANATION}" if query.max_minutes is not None else ""
    if results:
        explanation = f"Found {len(results)} recipe(s) for query {query.query_text!r}"
        if query.max_minutes is not None:
            explanation += f" with max_minutes<={query.max_minutes:g}"
        explanation += (
            f". Searched: {datasets_searched}; represented in results: "
            f"{datasets_in_results}.{ceiling_note} No constraints were relaxed; "
            "unsupported constraints are preserved unchanged."
        )
    else:
        explanation = f"No recipes matched query {query.query_text!r}"
        if query.max_minutes is not None:
            explanation += f" with max_minutes<={query.max_minutes:g}"
        explanation += (
            f". Searched: {datasets_searched}.{ceiling_note} No constraints "
            "were dropped or relaxed to force a match; try a broader dish term, "
            "fewer time limits, or a different dataset slice."
        )
    return {
        **base,
        "outcome": "ready",
        "datasets_searched": datasets_searched,
        "datasets_in_results": datasets_in_results,
        "query": {
            "query_text": query.query_text,
            "match_mode": query.match_mode,
            "max_minutes": query.max_minutes,
            "dataset_id": query.dataset_id,
            "applied_filters": query.applied_filters,
            "required_ingredients": [],
            "pantry_hint_terms": query.pantry_hint_terms,
            "unsupported_constraints": query.unsupported_constraints,
            "transformations": query.transformations,
            "explanation": query.explanation,
        },
        "results": results,
        "result_count": len(results),
        "explanation": explanation,
    }
