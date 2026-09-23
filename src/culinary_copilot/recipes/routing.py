"""Deterministic routing to LLM extraction (hybrid ingestion).

Flow per record::

    raw source
        ↓ deterministic parsing (adapter v2)
        ↓ quality and ambiguity checks (this module)
        ├── acceptable      → deterministic validation (no model call)
        └── ambiguous       → OpenAI Batch extraction → deterministic validation
        └── unusable        → quarantine (raw evidence retained)

Routing is itself deterministic and versioned. Records are NOT routed only
on exceptions: silent errors (clean-looking but wrong output) are caught by
content signals such as measurement tokens left inside ingredient names or
dropped source lines. A configurable audit sample of passed records is also
routed so silent failures can be measured; audit requests carry
``audit: true`` to stay distinguishable from fallback requests.
"""

import hashlib
import random
import re
from typing import Any, Literal

from culinary_copilot.recipes.adapters.base import foodie_source_id
from culinary_copilot.recipes.adapters.foodie import (
    FOODIE_ADAPTER_VERSION,
    FOODIE_DATASET,
    normalize_foodie_text,
    suspected_multi_recipe,
)

ROUTING_VERSION = "2"

Route = Literal["deterministic_accept", "needs_llm", "quarantine"]


def raw_content_hash(texts: str) -> str:
    return hashlib.sha256(texts.encode()).hexdigest()


def _durations_unstructured(recipe: dict[str, Any]) -> bool:
    meta = recipe.get("durations_mentioned", False)
    return bool(meta and not recipe.get("durations_reported"))


def servings_evidence(texts: str) -> bool:
    """Only explicit people/portion counts justify recovering missing servings."""
    return bool(
        re.search(
            r"\b(?:serves?|servings?|portions?)\s*:?\s*\d+\b|"
            r"\b\d+\s+(?:servings?|portions?)\b",
            texts,
            re.I,
        )
    )


def route_record(
    texts: str,
    row_number: int,
    *,
    recipe: dict[str, Any] | None = None,
    parse_error: str | None = None,
    route_durations: bool = False,
) -> dict[str, Any]:
    """Route one source record. Never raises for content reasons."""
    source_id = foodie_source_id(row_number)
    content_hash = raw_content_hash(texts)
    base: dict[str, Any] = {
        "source_id": source_id,
        "row_number": row_number,
        "content_hash": content_hash,
        "adapter_version": FOODIE_ADAPTER_VERSION,
        "routing_version": ROUTING_VERSION,
        "audit": False,
        "reason_codes": [],
        "affected_fields": [],
    }
    if len([line for line in texts.splitlines() if line.strip()]) <= 1:
        base.update(
            route="quarantine",
            reason_codes=["single_line_requires_spans"],
            affected_fields=["ingredients", "instructions"],
            detail="Unsupported line evidence layout; retained locally without an LLM call.",
        )
        return base
    if suspected_multi_recipe(texts):
        base.update(
            route="needs_llm",
            reason_codes=["multi_recipe_split"],
            affected_fields=["ingredients", "instructions"],
            detail="Two or more Ingredients sections: probable concatenated recipes.",
        )
        return base
    if parse_error is not None or recipe is None:
        # Substantive records with failed section boundaries (marker-less
        # layouts, single-line blobs, concatenated posts) go to the LLM for
        # segmentation — quarantine is for truly unusable rows only.
        if len(texts.strip()) >= 200:
            base.update(
                route="needs_llm",
                reason_codes=["section_boundary_failed"],
                affected_fields=["ingredients", "instructions"],
                detail=f"Parser raised {parse_error or 'unknown'} on substantive "
                "text; LLM segments with traceable line evidence.",
            )
            return base
        base.update(
            route="quarantine",
            reason_codes=[f"parse_failed:{parse_error or 'unknown'}"],
            affected_fields=["ingredients", "instructions"],
            detail="Deterministic parser raised; raw evidence retained for review.",
        )
        return base
    reasons: list[str] = []
    fields: list[str] = []

    def flag(code: str, *affected: str) -> None:
        reasons.append(code)
        fields.extend(affected)

    codes = {i["code"] for i in recipe.get("quality_issues", [])}
    if "name_contains_measure" in codes:
        flag("name_contains_measure", "ingredients.canonical")
    if "omitted_source_lines" in codes:
        flag("omitted_source_lines", "ingredients")
    if "quantity_unknown" in codes:
        flag("quantity_unknown", "ingredients.amount")
    for item in recipe.get("ingredients", []):
        repaired = item.get("repaired", "")
        if (
            repaired
            and repaired != item.get("original", "")
            and (item.get("amount") is None and not item.get("qualitative"))
        ):
            flag("ambiguous_glued_token", "ingredients")
            break
    if recipe.get("line_coverage", {}).get("dropped"):
        flag("unclassified_source_lines", "ingredients")
    if recipe.get("notes_text") and not recipe.get("instructions"):
        flag("instructions_only_notes", "instructions")
    caps = recipe.get("capabilities", {})
    if caps.get("quantities_validated") and any(
        i.get("severity") == "warning" for i in recipe.get("quality_issues", [])
    ):
        flag("capability_conflict", "capabilities")
    if route_durations and _durations_unstructured(recipe):
        flag("durations_unstructured", "durations")
    from culinary_copilot.recipes.llm_contracts import numbered_source
    from culinary_copilot.recipes.source_scope import requested_lines

    _, line_map = numbered_source(texts)
    try:
        requested = requested_lines(recipe, line_map)
    except ValueError:
        base.update(
            route="quarantine",
            reason_codes=["ingredient_source_alignment_failed"],
            affected_fields=["ingredients"],
            detail="Cannot trace extraction targets.",
        )
        return base
    if requested and not reasons:
        flag("ambiguous_ingredient", "ingredients")
    if (
        recipe.get("servings") is None
        and not recipe.get("batch_yield")
        and servings_evidence(texts)
    ):
        flag("servings_unstructured", "servings")
    structural = set(reasons) - {
        "name_contains_measure",
        "quantity_unknown",
        "ambiguous_glued_token",
    }
    if reasons and not requested and not structural:
        base.update(
            route="deterministic_accept",
            reason_codes=["no_recoverable_targets"],
            affected_fields=[],
            suppressed_reason_codes=sorted(set(reasons)),
            detail="No ambiguous source fields to extract; existing quality/capabilities retained.",
        )
        return base
    if reasons:
        base.update(
            route="needs_llm",
            reason_codes=sorted(set(reasons)),
            affected_fields=sorted(set(fields)),
            detail="Ambiguity signals fired; deterministic output kept as fallible input.",
        )
        return base
    base.update(
        route="deterministic_accept",
        reason_codes=[],
        affected_fields=[],
        detail="No ambiguity signals; proceeds to deterministic validation.",
    )
    return base


def route_pilot(
    rows: list[dict[str, str]],
    selected: list[int],
    *,
    audit_rate: float = 0.0,
    audit_seed: int = 20260707,
    route_durations: bool = False,
) -> list[dict[str, Any]]:
    """Parse + route a pilot sample. Returns one result per selected row."""
    results: list[dict[str, Any]] = []
    passed: list[str] = []
    for row_number in selected:
        texts = rows[row_number - 1].get("texts") or ""
        try:
            recipe = normalize_foodie_text(texts, row_number)
            result = route_record(texts, row_number, recipe=recipe, route_durations=route_durations)
        except ValueError as exc:
            result = route_record(
                texts, row_number, parse_error=str(exc), route_durations=route_durations
            )
            recipe = None
        result["normalized_hash"] = recipe["content_hash"] if recipe else None
        results.append(result)
        if result["route"] == "deterministic_accept":
            passed.append(result["source_id"])
    if audit_rate > 0 and passed:
        sample_size = max(1, round(len(passed) * audit_rate))
        for source_id in sorted(
            random.Random(audit_seed).sample(passed, min(sample_size, len(passed)))
        ):
            for result in results:
                if result["source_id"] == source_id:
                    result["route"] = "needs_llm"
                    result["reason_codes"] = ["audit_sample"]
                    result["affected_fields"] = ["ingredients", "instructions"]
                    result["audit"] = True
                    result["detail"] = (
                        "Deterministic audit sample of a passed record; "
                        "measures silent failures, not a fallback."
                    )
    return results


def routing_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    routes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for result in results:
        routes[result["route"]] += 1
        for code in result["reason_codes"]:
            reasons[code] += 1
    return {
        "total": len(results),
        "routes": dict(routes),
        "reasons": dict(reasons),
        "audit_requests": sum(1 for r in results if r.get("audit")),
        "routing_version": ROUTING_VERSION,
        "adapter_version": FOODIE_ADAPTER_VERSION,
        "dataset_id": FOODIE_DATASET,
    }
