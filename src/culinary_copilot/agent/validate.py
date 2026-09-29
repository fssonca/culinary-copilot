"""Deterministic validators for agent-loop outputs (Milestone 3, Phase 3).

The model proposes; these functions dispose, without a model call:

- sourced IDs: every option resolves through an exact
  ``(dataset_id, source_id)`` lookup (injected ``resolve``; production
  uses the recipe repository, tests use fakes);
- session retrieval (P3-A-01): every option's pair must have been
  returned by a successful ``search_recipes``/``get_recipe`` call in
  this session (resumed runs included); quantities and plans need a
  ``get_recipe`` full document, not a search row;
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

#: Resolves one technique chunk: (doc_id, chunk_id) -> row with url,
#: licence, licence_url and attribution_text, or None when unresolvable.
TechniqueResolver = Callable[[str, int], dict[str, Any] | None]


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


def validate_one_option(
    index: int,
    opt: Any,
    *,
    resolve: RecipeResolver,
    retrieved: set[tuple[str, str]] | None = None,
    full: set[tuple[str, str]] | None = None,
) -> list[str]:
    """Validate a single option (sourced IDs, quantities, labels).

    When ``retrieved`` is given, the pair must come from a successful
    ``search_recipes``/``get_recipe`` call in this session (dataset-
    qualified; other sessions and failed lookups do not count). When
    ``full`` is given, stated quantities additionally need a
    ``get_recipe`` full document for the pair.
    """
    errors: list[str] = []
    if not isinstance(opt, dict):
        return [f"option {index}: not a mapping"]
    smuggled = TECHNIQUE_OPTION_KEYS & set(opt)
    if smuggled:
        return [
            f"option {index}: technique references are not options "
            f"({sorted(smuggled)}); cite them in plan/cook steps only"
        ]
    dataset_id = opt.get("dataset_id")
    source_id = opt.get("source_id")
    if not dataset_id or not source_id:
        return [f"option {index}: missing (dataset_id, source_id)"]
    key = (str(dataset_id), str(source_id))
    try:
        doc = resolve(str(dataset_id), str(source_id))
    except Exception as exc:
        return [f"option {index}: source lookup failed ({type(exc).__name__})"]
    if doc is None:
        return [f"option {index}: ({dataset_id}, {source_id}) not in corpus (unsourced ID)"]
    if retrieved is not None and key not in retrieved:
        return [f"option {index}: ({dataset_id}, {source_id}) was not retrieved in this session"]
    claims = opt.get("quantities") or []
    if claims and full is not None and key not in full:
        errors.append(
            f"option {index}: quantities need a get_recipe result in this session "
            "(search rows are not enough)"
        )
    for claim in claims:
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
    retrieved: set[tuple[str, str]] | None = None,
    full: set[tuple[str, str]] | None = None,
) -> list[str]:
    """Validate a recommend finish. Returns error strings (empty = valid)."""
    errors: list[str] = []
    if not isinstance(options, list) or not 1 <= len(options) <= 4:
        return ["options must be a list of 1-4 sourced recipes"]
    if len(options) == 1 and not allow_single:
        errors.append("single option needs Epicure consulted in this session")
    honored_set = {str(h) for h in honored or []}
    for key in sorted(hard_keys):
        if key not in honored_set:
            errors.append(f"dropped hard constraint: {key}")
    for index, opt in enumerate(options):
        errors.extend(
            validate_one_option(index, opt, resolve=resolve, retrieved=retrieved, full=full)
        )
    return errors


def validate_plan(
    plan: Any,
    *,
    selected_dish: dict[str, Any] | None,
    resolve: RecipeResolver,
    full: set[tuple[str, str]] | None = None,
) -> list[str]:
    """Validate a plan finish. Returns error strings (empty = valid).

    When ``full`` is given, the plan source must come from a
    ``get_recipe`` full document in this session (a search row is not
    enough for a cooking plan).
    """
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
    if (
        full is not None
        and (str(source.get("dataset_id")), str(source.get("source_id"))) not in full
    ):
        return [
            "plan source needs a get_recipe result in this session (search rows are not enough)"
        ]
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


#: Option keys that would smuggle a technique reference into a dish
#: option. Technique refs are a separate evidence type: they may support
#: a technique claim in a plan/cook step but may never be an option, a
#: recipe source, or quantity evidence.
TECHNIQUE_OPTION_KEYS = frozenset({"doc_id", "chunk_id", "technique_refs", "technique"})


def validate_technique_refs(
    refs: Any,
    *,
    resolve_technique: TechniqueResolver,
    returned: set[tuple[str, int]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Validate plan/cook technique refs; returns (errors, resolved rows).

    Each ref needs ``doc_id`` + ``chunk_id`` resolving in the technique
    corpus, and — when ``returned`` is given — the pair must have been
    returned by a ``search_techniques`` call in this session. Refs
    carrying ``dataset_id``/``source_id`` are rejected: technique refs
    are never recipe sources or quantity evidence.
    """
    errors: list[str] = []
    resolved: list[dict[str, Any]] = []
    if refs is None:
        return errors, resolved
    if not isinstance(refs, list):
        return ["technique_refs must be a list"], resolved
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            errors.append(f"technique ref {index}: not a mapping")
            continue
        if "dataset_id" in ref or "source_id" in ref:
            errors.append(f"technique ref {index}: technique refs are never recipe sources")
            continue
        doc_id = ref.get("doc_id")
        chunk_id = ref.get("chunk_id")
        if not isinstance(doc_id, str) or not doc_id.strip():
            errors.append(f"technique ref {index}: missing doc_id")
            continue
        if isinstance(chunk_id, bool) or not isinstance(chunk_id, int) or chunk_id < 0:
            errors.append(f"technique ref {index}: chunk_id must be an integer >= 0")
            continue
        key = (doc_id, chunk_id)
        if returned is not None and key not in returned:
            errors.append(
                f"technique ref {index}: ({doc_id}, {chunk_id}) was not returned in this session"
            )
            continue
        try:
            row = resolve_technique(doc_id, chunk_id)
        except Exception as exc:
            errors.append(f"technique ref {index}: lookup failed ({type(exc).__name__})")
            continue
        if row is None:
            errors.append(f"technique ref {index}: ({doc_id}, {chunk_id}) does not resolve")
            continue
        resolved.append(dict(row))
    return errors, resolved


__all__ = [
    "RecipeResolver",
    "TECHNIQUE_OPTION_KEYS",
    "TechniqueResolver",
    "hard_constraint_keys",
    "quantity_in_source",
    "validate_one_option",
    "validate_options",
    "validate_plan",
    "validate_technique_refs",
]
