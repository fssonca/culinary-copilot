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
- minimum dietary check (P3-L-07): each option's ``get_recipe``
  ingredient lines against conservative vegetarian/vegan term lists
  (word boundaries; ambiguous broth/stock/bouillon/Worcestershire
  without vegetable/vegan stays unverified, never verified);
- plan integrity: the plan source equals the selected dish and the plan
  sections are non-empty;
- minimum plan evidence (P3-L-09): ingredient-only sources set
  ``steps_source`` to ``model_adaptation`` (needs an adaptation saying
  so); raw meat/poultry/fish/eggs need a food-safety technique_ref.

A model claim alone never establishes compliance; these checks compare
against stored data only. Failures return short error strings the loop
feeds back to the model once as a tool-style error, then stops.
"""

from __future__ import annotations

import re
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


def dietary_values(constraints: dict[str, Any]) -> list[str]:
    """Raw dietary-constraint values (list or single string)."""
    raw = (constraints or {}).get("dietary_constraints")
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(v) for v in raw if str(v).strip()]
    if isinstance(raw, dict):
        return [str(v) for v in raw.values() if str(v).strip()]
    return []


#: Conservative dietary violation terms (P3-L-07), matched with word
#: boundaries so "eggplant" never flags "egg" and "vegetable broth"
#: never flags "broth" (see the ambiguous handling below). Vegetarian
#: excludes meat, poultry, fish/seafood, gelatin and similar
#: animal-derived ingredients; vegan adds dairy, eggs and honey.
VEGETARIAN_VIOLATION_TERMS = frozenset(
    {
        # meat
        "beef",
        "pork",
        "lamb",
        "veal",
        "venison",
        "goat",
        "mutton",
        "rabbit",
        "bacon",
        "ham",
        "sausage",
        "salami",
        "pepperoni",
        "chorizo",
        "prosciutto",
        "pancetta",
        "meatball",
        "meatballs",
        "jerky",
        # poultry
        "chicken",
        "turkey",
        "duck",
        "goose",
        "quail",
        "pheasant",
        "poultry",
        "hen",
        "capon",
        # fish and seafood
        "fish",
        "salmon",
        "tuna",
        "cod",
        "haddock",
        "halibut",
        "tilapia",
        "trout",
        "sardine",
        "sardines",
        "anchovy",
        "anchovies",
        "mackerel",
        "sole",
        "snapper",
        "catfish",
        "bass",
        "eel",
        "swordfish",
        "surimi",
        "shrimp",
        "prawn",
        "crab",
        "lobster",
        "crayfish",
        "scallop",
        "scallops",
        "clam",
        "clams",
        "mussel",
        "mussels",
        "oyster",
        "oysters",
        "squid",
        "calamari",
        "octopus",
        "seafood",
        # gelatin and similar animal-derived ingredients
        "gelatin",
        "gelatine",
        "lard",
        "tallow",
        "suet",
        "rennet",
    }
)

#: Vegan additions: dairy, eggs and honey.
VEGAN_EXTRA_TERMS = frozenset(
    {
        "milk",
        "cheese",
        "butter",
        "cream",
        "yogurt",
        "yoghurt",
        "whey",
        "casein",
        "caseinate",
        "ghee",
        "kefir",
        "buttermilk",
        "mozzarella",
        "parmesan",
        "cheddar",
        "feta",
        "ricotta",
        "mascarpone",
        "halloumi",
        "paneer",
        "provolone",
        "gouda",
        "brie",
        "egg",
        "eggs",
        "honey",
    }
)

VEGAN_VIOLATION_TERMS = VEGETARIAN_VIOLATION_TERMS | VEGAN_EXTRA_TERMS

#: Ambiguous terms: without "vegetable" or "vegan" in the same line
#: they keep the option but mark it unverified (never verified).
AMBIGUOUS_DIET_TERMS = frozenset({"broth", "stock", "bouillon", "worcestershire"})

#: False-positive guards: plant butters, plant milks, coconut cream
#: and cream of tartar are not dairy.
_NON_DAIRY_RES = (
    re.compile(r"\b(?:peanut|almond|cashew|sunflower|sesame|soy|tahini|apple|coconut) butter\b"),
    re.compile(r"\b(?:coconut|oat|soy|almond|cashew|rice|hemp|pea|flax|hazelnut) milk\b"),
    re.compile(r"\bcoconut cream\b"),
    re.compile(r"\bcream of tartar\b"),
)


def _word_hit(line: str, term: str) -> bool:
    """Word-boundary match of one term in a lowercased line."""
    return re.search(r"\b" + re.escape(term) + r"\b", line) is not None


def _ingredient_line_texts(doc: dict[str, Any]) -> list[str]:
    """Searchable ingredient lines: canonical/name/quantity text plus
    raw source lines when the document keeps them."""
    texts: list[str] = []
    for item in _ingredient_entries(doc):
        parts = [
            item.get("canonical"),
            item.get("name"),
            item.get("quantity_text"),
            item.get("amount_text"),
        ]
        line = " ".join(str(p).strip() for p in parts if str(p or "").strip())
        if line:
            texts.append(line)
    raw = doc.get("ingredient_lines")
    if isinstance(raw, list):
        texts.extend(str(line).strip() for line in raw if str(line or "").strip())
    return texts


def check_dietary_option(
    index: int, option: dict[str, Any], doc: dict[str, Any], value: str
) -> tuple[list[str], dict[str, Any]]:
    """Minimum hard-constraint check for one option (P3-L-07).

    Compares the option's ``get_recipe`` ingredient lines against the
    conservative term lists with word-boundary matching. Returns
    ``(violation_errors, check_entry)``: a clear violation drops the
    option with a readable reason; ambiguous terms keep it but list
    them under an ``"unverified"`` entry; unknown dietary values do
    not invent checks (``"not_checked"`` with the value).
    """
    label = str(value or "").strip().lower()
    if label == "vegetarian":
        terms = VEGETARIAN_VIOLATION_TERMS
    elif label == "vegan":
        terms = VEGAN_VIOLATION_TERMS
    else:
        return [], {"status": "not_checked", "value": str(value)}
    errors: list[str] = []
    violations: list[str] = []
    unverified: list[str] = []
    for line in _ingredient_line_texts(doc):
        scrubbed = line.lower()
        for rx in _NON_DAIRY_RES:
            scrubbed = rx.sub(" ", scrubbed)
        hit = next((term for term in sorted(terms) if _word_hit(scrubbed, term)), None)
        if hit is not None:
            excerpt = line.strip()[:80]
            errors.append(
                f"option {index}: violates dietary constraint {label!r}: "
                f"{hit!r} in ingredient line {excerpt!r}"
            )
            if hit not in violations:
                violations.append(hit)
            continue
        for ambiguous in sorted(AMBIGUOUS_DIET_TERMS):
            if _word_hit(scrubbed, ambiguous) and not (
                _word_hit(scrubbed, "vegetable") or _word_hit(scrubbed, "vegan")
            ):
                if ambiguous not in unverified:
                    unverified.append(ambiguous)
    if violations:
        entry: dict[str, Any] = {"status": "violated", "value": label, "terms": violations}
    elif unverified:
        entry = {"status": "unverified", "value": label, "terms": list(unverified)}
    else:
        entry = {"status": "checked", "value": label}
    return errors, entry


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


#: Manifest food-safety docs whose chunks may support raw-protein
#: plans (P3-L-09): every entry has topic "food safety" in
#: ``evals/technique_corpus/manifest.json``.
SAFETY_DOC_IDS = frozenset(
    {"tech-fda-safe-32", "tech-fsis-temp-34", "tech-fda-kitchen-33", "tech-fsis-leftover-36"}
)

#: Raw animal-protein terms (P3-L-09): meat, poultry, fish and eggs.
#: A source-ingredient line containing "cooked" (e.g. "fully cooked
#: chicken fillets") is exempt. Word-boundary matching, as above.
RAW_PROTEIN_TERMS = frozenset(
    {
        "chicken",
        "turkey",
        "duck",
        "goose",
        "quail",
        "pheasant",
        "poultry",
        "beef",
        "pork",
        "lamb",
        "veal",
        "venison",
        "goat",
        "mutton",
        "rabbit",
        "bacon",
        "ham",
        "sausage",
        "salami",
        "pepperoni",
        "chorizo",
        "prosciutto",
        "pancetta",
        "meatball",
        "meatballs",
        "fish",
        "salmon",
        "tuna",
        "cod",
        "haddock",
        "halibut",
        "tilapia",
        "trout",
        "sardine",
        "sardines",
        "anchovy",
        "anchovies",
        "mackerel",
        "sole",
        "snapper",
        "catfish",
        "bass",
        "eel",
        "swordfish",
        "surimi",
        "shrimp",
        "prawn",
        "crab",
        "lobster",
        "crayfish",
        "scallop",
        "scallops",
        "clam",
        "clams",
        "mussel",
        "mussels",
        "oyster",
        "oysters",
        "squid",
        "calamari",
        "octopus",
        "seafood",
        "egg",
        "eggs",
    }
)

#: Marker phrases for a model-adaptation admission (P3-L-09): an
#: adaptation counts as stating the steps are not from the source when
#: its description contains one of these (case-insensitive).
MODEL_STEPS_MARKERS = (
    "not from the source",
    "not in the source",
    "not from the recipe",
    "model-created",
    "model created",
    "created by the model",
)

#: Directions fields in a resolved recipe document, in lookup order.
_DIRECTION_FIELDS = ("instructions", "instruction_lines", "steps", "directions")


def doc_directions(doc: dict[str, Any]) -> list[str]:
    """Source directions (empty when the record is ingredient-only)."""
    for key in _DIRECTION_FIELDS:
        raw = (doc or {}).get(key)
        if isinstance(raw, list):
            steps = [str(s).strip() for s in raw if str(s or "").strip()]
            if steps:
                return steps
    return []


def doc_text(doc: dict[str, Any]) -> str:
    """Searchable source text: ingredient lines plus directions."""
    return "\n".join(_ingredient_line_texts(doc) + doc_directions(doc))


def check_plan_evidence(
    plan: dict[str, Any],
    doc: dict[str, Any] | None,
    technique_rows: list[dict[str, Any]] | None,
) -> tuple[list[str], str]:
    """Minimum plan-evidence checks (P3-L-09).

    Returns ``(errors, steps_source)``. A source without directions
    sets ``steps_source`` to ``"model_adaptation"`` and needs at least
    one plan adaptation stating the steps are not from the source.
    Source ingredients with raw meat, poultry, fish or eggs (not
    "cooked") need at least one technique_ref to a food-safety chunk.
    """
    errors: list[str] = []
    source = doc or {}
    steps_source = "source" if doc_directions(source) else "model_adaptation"
    if steps_source == "model_adaptation":
        adaptations = plan.get("adaptations") or []
        descriptions = [str(a.get("description") or "") for a in adaptations if isinstance(a, dict)]
        if not any(
            marker in description.lower()
            for description in descriptions
            for marker in MODEL_STEPS_MARKERS
        ):
            errors.append(
                "steps_source is model_adaptation (the source has no directions): "
                "add a plan adaptation stating the steps are not from the source"
            )
    raw_hits: list[str] = []
    for line in _ingredient_line_texts(source):
        lowered = line.lower()
        if _word_hit(lowered, "cooked"):
            continue
        hit = next((term for term in sorted(RAW_PROTEIN_TERMS) if _word_hit(lowered, term)), None)
        if hit is not None and hit not in raw_hits:
            raw_hits.append(hit)
    if raw_hits:
        safety = [
            row
            for row in (technique_rows or [])
            if str((row or {}).get("doc_id") or "") in SAFETY_DOC_IDS
        ]
        if not safety:
            errors.append(
                f"plan uses raw {', '.join(raw_hits)}: cite a food-safety chunk "
                "(search_techniques for safe internal temperatures) with a technique_ref"
            )
    return errors, steps_source


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
    "AMBIGUOUS_DIET_TERMS",
    "RAW_PROTEIN_TERMS",
    "MODEL_STEPS_MARKERS",
    "SAFETY_DOC_IDS",
    "VEGAN_EXTRA_TERMS",
    "VEGAN_VIOLATION_TERMS",
    "VEGETARIAN_VIOLATION_TERMS",
    "RecipeResolver",
    "TECHNIQUE_OPTION_KEYS",
    "TechniqueResolver",
    "check_dietary_option",
    "check_plan_evidence",
    "dietary_values",
    "doc_directions",
    "doc_text",
    "hard_constraint_keys",
    "quantity_in_source",
    "validate_one_option",
    "validate_options",
    "validate_plan",
    "validate_technique_refs",
]
