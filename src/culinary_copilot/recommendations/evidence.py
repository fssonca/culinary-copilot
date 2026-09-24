"""Bounded internal evidence bundle for generation (Phase 3).

API summaries from retrieval are NOT complete generation evidence: they
carry truncated excerpts and ingredient-name lists only. This module builds
a bounded bundle of COMPLETE source sections (full ingredient records and
full ordered instructions) fetched by exact ``(dataset_id, source_id)``
identity, never legacy fallback lookup.

No-truncation rule: required ingredient/instruction fields are preserved
verbatim (no per-field slicing). Budgeting measures the ACTUAL serialized
provider payload (candidate blocks plus request/epicure/verdict sections
and tool-result content) and reduces WHOLE candidates until the complete
payload fits both the evidence budget and the total input budget. When no
recommendable candidate fits, the caller returns ``insufficient_evidence``
(``evidence_budget_exceeded``) without a provider call. The final
recommendation evidence prompt is never truncated.

Character limits are not token counts: budgets are measured in characters
(code points), while billing is per token. The live runner reserves cost
from UTF-8 byte lengths (every token spans at least one byte, so bytes
are a conservative token upper bound), never from a chars/4 estimate.

Source fidelity vs recipe completeness (tracked separately):
- ``sections_present``: required raw sections exist as non-empty lists.
- ``omitted_entries``: malformed entries dropped during parsing, each with
  a stable reason (``entry_not_object`` / ``missing_name`` /
  ``entry_malformed``). Any omission means the evidence does not show the
  whole source.
- ``defects_blocking``: explicit source defects that prevent a
  recommendation (error-severity quality issues), recorded with stable
  reason ``source_defect_error``. Warnings/info are preserved as context.
- ``capabilities``: existing metadata is respected; a missing capabilities
  mapping is recorded as ``capabilities_unknown`` (explicit unknown), never
  as permission and never as a block: legacy records without modern
  metadata can still be shown faithfully.
- ``recommendable``: sections present AND zero omissions AND no blocking
  defects. Only recommendable candidates enter generation evidence. This
  is structural admission, not semantic completeness: nothing here checks
  that the ingredient list covers what the steps use. A known
  inconsistency blocks only when it is recorded on the source as an
  error-severity quality issue (no automatic detection exists). The legacy
  ``complete`` key mirrors ``recommendable`` and means the same thing.
- Invalid numerics (booleans, non-numeric, non-finite, negative servings
  or amounts) are rejected to explicit unknown per field with stable
  issue codes (``servings_invalid`` / ``amount_invalid``); nothing is
  invented and invalid values are never rendered as known.

Stable source-local references: ``ing-<position>`` per preserved
ingredient and ``step-<index>`` per preserved instruction. The model must
cite these; validation checks they exist and belong to the selected
recipe.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from culinary_copilot.recipes.durations import DurationStatus, classify_total

ISSUE_ENTRY_NOT_OBJECT = "entry_not_object"
ISSUE_MISSING_NAME = "missing_name"
ISSUE_ENTRY_MALFORMED = "entry_malformed"
ISSUE_SECTION_MISSING = "section_missing"
ISSUE_SECTION_EMPTY = "section_empty"
ISSUE_SERVINGS_INVALID = "servings_invalid"
ISSUE_AMOUNT_INVALID = "amount_invalid"
ISSUE_SOURCE_DEFECT = "source_defect_error"


def strict_number(value: Any, *, allow_zero: bool) -> float | None:
    """Validate a source numeric strictly.

    Booleans, non-numerics, non-finite values, and negatives are rejected
    (``None``). Zero is accepted only when ``allow_zero`` is set; servings
    require a positive value.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    if number == 0 and not allow_zero:
        return None
    return number


def _ingredient_entries(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse ingredients verbatim; return ``(entries, omitted)``.

    Valid entries keep every required field unsliced. Malformed entries
    are omitted only with a tracked stable reason; omission always blocks
    ``recommendable``.
    """
    raw = doc.get("ingredients")
    entries: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        omitted.append({"section": "ingredients", "index": None, "reason": ISSUE_SECTION_MISSING})
        return entries, omitted
    if not raw:
        omitted.append({"section": "ingredients", "index": None, "reason": ISSUE_SECTION_EMPTY})
        return entries, omitted
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            omitted.append(
                {"section": "ingredients", "index": position, "reason": ISSUE_ENTRY_NOT_OBJECT}
            )
            continue
        name = item.get("canonical") or item.get("name") or item.get("original")
        if not isinstance(name, str) or not name.strip():
            omitted.append(
                {"section": "ingredients", "index": position, "reason": ISSUE_MISSING_NAME}
            )
            continue
        issues: list[str] = []
        amount = strict_number(item.get("amount"), allow_zero=True)
        if item.get("amount") is not None and amount is None:
            issues.append(ISSUE_AMOUNT_INVALID)
        unit = item.get("unit")
        unit_text = item.get("unit_text")
        entries.append(
            {
                "ref": f"ing-{position}",
                "canonical": " ".join(name.split()),
                "original": str(item.get("original") or ""),
                "amount": amount,
                "amount_known": amount is not None,
                "amount_text": str(item.get("amount_text") or ""),
                "quantity_text": str(item.get("quantity_text") or ""),
                "unit": unit if isinstance(unit, str) and unit.strip() else None,
                "unit_text": (
                    unit_text if isinstance(unit_text, str) and unit_text.strip() else None
                ),
                "notes": str(item.get("notes") or ""),
                "optional": bool(item.get("optional", False)),
                "issues": issues,
            }
        )
    return entries, omitted


def _instruction_entries(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = doc.get("instructions")
    entries: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        omitted.append({"section": "instructions", "index": None, "reason": ISSUE_SECTION_MISSING})
        return entries, omitted
    if not raw:
        omitted.append({"section": "instructions", "index": None, "reason": ISSUE_SECTION_EMPTY})
        return entries, omitted
    for index, step in enumerate(raw):
        if not isinstance(step, str) or not step.strip():
            omitted.append(
                {"section": "instructions", "index": index, "reason": ISSUE_ENTRY_MALFORMED}
            )
            continue
        entries.append({"ref": f"step-{index}", "text": " ".join(step.split())})
    return entries, omitted


def _defect_issues(doc: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Split quality metadata into blocking defects vs preserved context."""
    blocking: list[str] = []
    context: list[dict[str, Any]] = []
    quality = doc.get("quality_issues")
    items = quality if isinstance(quality, list) else []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        record = {
            "code": entry.get("code"),
            "severity": entry.get("severity"),
            "message": str(entry.get("message") or "")[:500],
        }
        if entry.get("severity") == "error":
            blocking.append(str(entry.get("code") or "unknown_error"))
        else:
            context.append(record)
    return blocking, context


def build_candidate(
    *, dataset_id: str, source_id: str, title: str | None, doc: dict[str, Any]
) -> dict[str, Any]:
    """Build one evidence candidate from a full source document.

    Required fields are preserved verbatim. Fidelity signals
    (``sections_present``, ``omitted_entries``, ``defects_blocking``,
    ``capabilities_unknown``) are tracked separately from
    ``recommendable``, which gates generation evidence.
    """
    ingredients, omitted_ingredients = _ingredient_entries(doc)
    instructions, omitted_instructions = _instruction_entries(doc)
    omitted_entries = omitted_ingredients + omitted_instructions
    sections_present = {
        "ingredients": isinstance(doc.get("ingredients"), list) and len(doc["ingredients"]) > 0,
        "instructions": isinstance(doc.get("instructions"), list) and len(doc["instructions"]) > 0,
    }
    servings = strict_number(doc.get("servings"), allow_zero=False)
    servings_issue = (
        [] if (doc.get("servings") is None or servings is not None) else [ISSUE_SERVINGS_INVALID]
    )
    durations = doc.get("durations_minutes")
    reported_raw = durations.get("TotalTime") if isinstance(durations, dict) else None
    duration_status, usable_total = classify_total(reported_raw)
    defects_blocking, quality_context = _defect_issues(doc)
    capabilities = doc.get("capabilities")
    capabilities_unknown = not isinstance(capabilities, dict)
    available = doc.get("available_fields")
    flags = doc.get("flags")
    provenance = doc.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {"dataset_id": dataset_id, "source_id": source_id}
    recommendable = (
        bool(sections_present["ingredients"])
        and bool(sections_present["instructions"])
        and not omitted_entries
        and not defects_blocking
    )
    return {
        "dataset_id": dataset_id,
        "source_id": source_id,
        "title": doc.get("title") if isinstance(doc.get("title"), str) else title,
        "complete": recommendable,
        "recommendable": recommendable,
        "sections_present": sections_present,
        "omitted_entries": omitted_entries,
        "preserved_complete": not omitted_entries,
        "defects_blocking": defects_blocking,
        "quality_context": quality_context,
        "ingredients": ingredients,
        "instructions": instructions,
        "ingredient_count": len(ingredients),
        "step_count": len(instructions),
        "servings": servings,
        "servings_known": servings is not None,
        "servings_issues": servings_issue,
        "total_minutes_reported": reported_raw,
        "duration_status": duration_status.value,
        "total_minutes_usable": usable_total,
        "total_minutes_known": duration_status is DurationStatus.REPORTED_POSITIVE,
        "capabilities": capabilities if isinstance(capabilities, dict) else None,
        "capabilities_unknown": capabilities_unknown,
        "available_fields": available if isinstance(available, dict) else None,
        "flags": list(flags) if isinstance(flags, list) else [],
        "quality_issues": list(doc["quality_issues"])
        if isinstance(doc.get("quality_issues"), list)
        else [],
        "provenance": provenance,
        "description": str(doc.get("description") or ""),
    }


def evidence_fingerprint(candidate: dict[str, Any]) -> str:
    """Content fingerprint of one evidence snapshot.

    Covers every built field (ingredient records, instruction text, title,
    servings, durations, flags, provenance, fidelity signals) except the
    server-issued label and derived ``_``-prefixed annotations. Two
    snapshots with equal ingredient/step counts but different content get
    different fingerprints. Used to guarantee that the evidence shown to
    the model, the evidence validated, and the evidence rendered are the
    same snapshot.
    """
    material = {k: v for k, v in candidate.items() if k != "label" and not k.startswith("_")}
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_recipe(candidate: dict[str, Any]) -> dict[str, Any]:
    """Server-render factual recipe content from the stored source.

    All quantities, units, ingredients, and instructions come from the
    validated source fields. Only fields marked known at build time are
    rendered as known; invalid numerics were already rejected to unknown
    and are never rendered as known values here.
    """
    ingredients: list[dict[str, Any]] = []
    for item in candidate.get("ingredients", []):
        amount = item.get("amount") if item.get("amount_known") else None
        unit = item.get("unit")
        ingredients.append(
            {
                "ref": item.get("ref"),
                "name": item.get("canonical"),
                "original": item.get("original") or "unknown",
                "amount": amount if isinstance(amount, (int, float)) else "unknown",
                "unit": unit if isinstance(unit, str) and unit.strip() else "unknown",
                "quantity_text": item.get("quantity_text") or "unknown",
                "optional": bool(item.get("optional", False)),
            }
        )
    steps: list[dict[str, Any]] = []
    for step in candidate.get("instructions", []):
        steps.append({"ref": step.get("ref"), "text": step.get("text")})
    return {
        "dataset_id": candidate.get("dataset_id"),
        "source_id": candidate.get("source_id"),
        "title": candidate.get("title"),
        "ingredients": ingredients,
        "instructions": steps,
        "servings": candidate.get("servings") if candidate.get("servings_known") else "unknown",
        "total_minutes_reported": candidate.get("total_minutes_reported"),
        "duration_status": candidate.get("duration_status"),
        "provenance": candidate.get("provenance"),
    }
