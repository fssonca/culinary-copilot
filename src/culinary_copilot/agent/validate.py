"""Deterministic validators for agent-loop outputs (Milestone 3, Phase 3).

The model proposes; these functions dispose, without a model call:

- sourced IDs: every option resolves through an exact
  ``(dataset_id, source_id)`` lookup (injected ``resolve``; production
  uses the recipe repository, tests use fakes);
- no invented quantities: stated quantities match the source document;
- adaptations separated: each adaptation carries ``label ==
  "adaptation"`` and never appears among source facts;
- hard constraints kept: every non-empty hard-constraint target
  (``domain/clarification.py::HARD_CONSTRAINT_TARGETS``) present in the
  session must appear in the finish's ``constraints_honored``;
- plan integrity: the plan source equals the selected dish and the plan
  sections are non-empty.

A model claim alone never establishes compliance; these checks compare
against stored data only. Failures return short error strings the loop
feeds back to the model once as a tool-style error, then stops.
"""

from __future__ import annotations

from typing import Any, Callable

from culinary_copilot.domain.clarification import HARD_CONSTRAINT_TARGETS

RecipeResolver = Callable[[str, str], dict[str, Any] | None]


def hard_constraint_keys(constraints: dict[str, Any]) -> set[str]:
    """Hard-constraint targets with non-empty session values."""
    out: set[str] = set()
    for key in HARD_CONSTRAINT_TARGETS:
        value = (constraints or {}).get(key)
        if isinstance(value, list) and any(str(v).strip() for v in value):
            out.add(key)
        elif isinstance(value, str) and value.strip():
            out.add(key)
        elif isinstance(value, dict) and value:
            out.add(key)
    return out


def _ingredient_entries(doc: dict[str, Any]) -> list[dict[str, Any]]:
    raw = doc.get("ingredients")
    return [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []


def _numbers_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if str(left).strip() == str(right).strip():
        return True
    try:
        return float(str(left).strip()) == float(str(right).strip())
    except (TypeError, ValueError):
        return False


def quantity_in_source(claim: dict[str, Any], doc: dict[str, Any]) -> bool:
    """True when a quantity claim matches one source ingredient exactly."""
    name = str(claim.get("ingredient") or "").strip().lower()
    if not name:
        return False
    for item in _ingredient_entries(doc):
        candidates = {
            str(item.get("canonical") or "").strip().lower(),
            str(item.get("name") or "").strip().lower(),
        }
        if name not in candidates:
            continue
        amount_ok = _numbers_equal(claim.get("amount"), item.get("amount", item.get("amount_text")))
        unit_claim = claim.get("unit")
        unit_src = item.get("unit")
        unit_ok = (unit_claim is None and unit_src is None) or (
            isinstance(unit_claim, str)
            and isinstance(unit_src, str)
            and unit_claim.strip().lower() == unit_src.strip().lower()
        )
        if amount_ok and unit_ok:
            return True
    return False


def validate_one_option(index: int, opt: Any, *, resolve: RecipeResolver) -> list[str]:
    """Validate a single option (sourced IDs, quantities, labels)."""
    errors: list[str] = []
    if not isinstance(opt, dict):
        return [f"option {index}: not a mapping"]
    dataset_id = opt.get("dataset_id")
    source_id = opt.get("source_id")
    if not dataset_id or not source_id:
        return [f"option {index}: missing (dataset_id, source_id)"]
    try:
        doc = resolve(str(dataset_id), str(source_id))
    except Exception as exc:
        return [f"option {index}: source lookup failed ({type(exc).__name__})"]
    if doc is None:
        return [f"option {index}: ({dataset_id}, {source_id}) not in corpus (unsourced ID)"]
    for claim in opt.get("quantities") or []:
        if not isinstance(claim, dict) or not quantity_in_source(claim, doc):
            errors.append(f"option {index}: quantity {claim!r} not in source (invented)")
    for adaptation in opt.get("adaptations") or []:
        if not isinstance(adaptation, dict) or adaptation.get("label") != "adaptation":
            errors.append(f"option {index}: adaptation not labelled as adaptation")
    return errors


def validate_options(
    options: Any,
    *,
    resolve: RecipeResolver,
    hard_keys: set[str],
    honored: list[str] | None,
    allow_single: bool,
) -> list[str]:
    """Validate a recommend finish. Returns error strings (empty = valid)."""
    errors: list[str] = []
    if not isinstance(options, list) or not 1 <= len(options) <= 4:
        return ["options must be a list of 1-4 sourced recipes"]
    if len(options) == 1 and not allow_single:
        errors.append("single option needs the direct_recipe_lookup epicure skip")
    honored_set = {str(h) for h in honored or []}
    for key in sorted(hard_keys):
        if key not in honored_set:
            errors.append(f"dropped hard constraint: {key}")
    for index, opt in enumerate(options):
        errors.extend(validate_one_option(index, opt, resolve=resolve))
    return errors


def validate_plan(
    plan: Any,
    *,
    selected_dish: dict[str, Any] | None,
    resolve: RecipeResolver,
) -> list[str]:
    """Validate a plan finish. Returns error strings (empty = valid)."""
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["plan must be a mapping"]
    if not isinstance(selected_dish, dict) or not selected_dish.get("source_id"):
        return ["plan needs a selected dish (use the select endpoint first)"]
    raw_source = plan.get("source")
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    if source.get("dataset_id") != selected_dish.get("dataset_id") or source.get(
        "source_id"
    ) != selected_dish.get("source_id"):
        errors.append("plan source must equal the selected dish")
        return errors
    try:
        doc = resolve(str(source.get("dataset_id")), str(source.get("source_id")))
    except Exception as exc:
        return [f"plan source lookup failed ({type(exc).__name__})"]
    if doc is None:
        return ["plan source not in corpus (unsourced ID)"]
    for field in ("mise_en_place", "steps"):
        items = plan.get(field)
        if (
            not isinstance(items, list)
            or not items
            or not all(isinstance(i, str) and i.strip() for i in items)
        ):
            errors.append(f"plan.{field} must be a non-empty list of steps")
    plating = plan.get("plating")
    if not isinstance(plating, str) or not plating.strip():
        errors.append("plan.plating must be a non-empty string")
    for claim in plan.get("quantities") or []:
        if not isinstance(claim, dict) or not quantity_in_source(claim, doc):
            errors.append(f"plan quantity {claim!r} not in source (invented)")
    for adaptation in plan.get("adaptations") or []:
        if not isinstance(adaptation, dict) or adaptation.get("label") != "adaptation":
            errors.append("plan adaptation not labelled as adaptation")
    return errors


__all__ = [
    "RecipeResolver",
    "hard_constraint_keys",
    "quantity_in_source",
    "validate_one_option",
    "validate_options",
    "validate_plan",
]
