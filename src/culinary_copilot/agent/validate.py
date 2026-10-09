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
- unnamed restriction check (P3-L-13): an allergy, intolerance or
  dietary restriction mentioned without naming it (narrow patterns:
  allergy/allergic, intolerance/intolerant, restriction,
  can't/cannot eat) blocks a finish with options until a confirmed
  answer names the specific allergen or diet;
- allergen avoidance (P3-L-13, review rework): after a confirmed
  answer names a mapped allergen (peanut, tree nuts, shellfish, fish,
  egg, milk/dairy, wheat/gluten, soy, sesame), each option's
  ``get_recipe`` ingredient lines are checked in three tiers —
  violated (direct or hidden sources: flour, breadcrumbs, pasta,
  mayonnaise, parmesan, soy/tamari sauce, oyster/fish sauce) drops
  the option; unverified (bouillon, malt, custard, batter,
  Worcestershire, caesar dressing, furikake, ...) keeps it but is
  listed; otherwise ``"no_listed_terms_found"`` — never "checked",
  since absence of a match never means safe. Every entry carries a
  fixed incomplete-list disclaimer, and note/adaptation/constraint
  text calling an option allergen-free is rejected unless a selected
  source recipe says so;
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
import unicodedata
from typing import Any, Callable

from culinary_copilot.agent.plan_quantities import (  # noqa: F401 -- re-exported for backwards compatibility
    DESCRIPTOR_WORDS,
    FUNCTION_WORDS,
    GLUED_PREFIX_MIN,
    PREP_WORDS,
    PROSE_MEASURE_UNITS,
    _normalize_prose_text,
    _token_prefix_match,
    plan_prose_quantity_errors,
    prose_quantities,
    prose_quantities_with_spans,
    source_quantities,
)
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

#: Vegetarian-ambiguous cheeses (2026-10-05 demo finding): traditionally
#: made with animal rennet (Parmigiano-Reggiano and Grana Padano by
#: rule), so a strict vegetarian cannot rely on them. They keep the
#: option but mark it unverified, never "checked", unless the same line
#: says "vegetarian" (a line naming rennet is already a violation).
#: Vegan already treats every cheese as a violation.
VEGETARIAN_RENNET_CHEESE_TERMS = frozenset(
    {
        "parmesan",
        "parmigiano",
        "pecorino",
        "romano",
        "grana",
        "gorgonzola",
        "gruyere",
        "gruyère",
        "manchego",
        "emmental",
        "emmentaler",
        "comte",
        "comté",
    }
)
_VEGETARIAN_CHEESE_OK_RE = re.compile(r"\bvegetarian\b")

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
        if label == "vegetarian" and _VEGETARIAN_CHEESE_OK_RE.search(scrubbed) is None:
            for cheese in sorted(VEGETARIAN_RENNET_CHEESE_TERMS):
                if _word_hit(scrubbed, cheese) and cheese not in unverified:
                    unverified.append(cheese)
    if violations:
        entry: dict[str, Any] = {"status": "violated", "value": label, "terms": violations}
    elif unverified:
        entry = {"status": "unverified", "value": label, "terms": list(unverified)}
    else:
        entry = {"status": "checked", "value": label}
    return errors, entry


#: Narrow unnamed-restriction patterns (P3-L-13): an allergy,
#: intolerance or dietary restriction mentioned WITHOUT naming it
#: ("a food allergy", "she can't eat some things") must be asked
#: about before recommending — guessing is not allowed. Narrow and
#: incomplete by design: paraphrases without these words (e.g.
#: "dietary needs") are not caught here; the framing tells the model
#: to ask for those too.
UNNAMED_RESTRICTION_DESCRIPTIONS = (
    "allergy/allergies/allergic",
    "intolerance/intolerant",
    "restriction/restrictions",
    "can't eat/cannot eat",
)
_UNNAMED_RESTRICTION_RES = (
    re.compile(r"\ballerg(?:y|ies|ic)\b", re.IGNORECASE),
    re.compile(r"\bintoleran(?:ce|t)\b", re.IGNORECASE),
    re.compile(r"\brestrictions?\b", re.IGNORECASE),
    re.compile(r"\bcan(?:'t|’t|not) eat\b", re.IGNORECASE),
)

#: Fixed disclaimer carried by EVERY allergen entry: no status may
#: imply safety. Allergens hide in derived ingredients the narrow
#: term lists below do not name, so even "no_listed_terms_found"
#: reads only as "no listed term matched", never "allergen-free".
ALLERGEN_DISCLAIMER = "term list is incomplete; not an allergen-free guarantee"

#: Dairy subset shared with the P3-L-07 vegan list (named cheeses and
#: paneer included through the shared table, not duplicated here).
_DAIRY_SHARED_TERMS = frozenset(t for t in VEGAN_EXTRA_TERMS if t not in {"egg", "eggs", "honey"})

#: Single-word violated terms per allergen, matched with
#: ``recommendations.policy.ingredient_term_hit`` (whole-word,
#: plural-aware), the same matcher the P3-L-13 live grade uses.
#: The milk/dairy set reuses the P3-L-07 vegan dairy list above and
#: adds gruyere, custard, lactose and dairy; the fish and shellfish
#: sets overlap the P3-L-07 vegetarian fish/seafood list (pinned by
#: test, not duplicated from it).
ALLERGEN_VIOLATED_TERMS: dict[str, frozenset[str]] = {
    "peanut": frozenset({"peanut"}),
    "tree nuts": frozenset(
        {
            # A generic "nut(s)" line is a tree-nut violation (2026-10-07:
            # "1 cup chopped nuts" was not flagged at all). The matcher
            # never hits nutmeg, coconut, butternut or doughnut.
            "nut",
            "almond",
            "walnut",
            "cashew",
            "pecan",
            "pistachio",
            "hazelnut",
            "macadamia",
            "praline",
            "marzipan",
            "nutella",
        }
    ),
    "shellfish": frozenset(
        {"shrimp", "prawn", "crab", "lobster", "scallop", "clam", "mussel", "oyster", "shellfish"}
    ),
    "fish": frozenset({"fish", "tuna", "salmon", "cod", "anchovy", "sardine", "trout"}),
    "egg": frozenset({"egg", "mayonnaise", "aioli", "meringue"}),
    "milk/dairy": _DAIRY_SHARED_TERMS | frozenset({"gruyere", "custard", "lactose", "dairy"}),
    "wheat/gluten": frozenset(
        {
            "wheat",
            "gluten",
            "barley",
            "rye",
            "flour",
            "bread",
            "breadcrumb",
            "panko",
            "pasta",
            "spaghetti",
            "macaroni",
            "noodle",
            "udon",
            "ramen",
            "couscous",
            "semolina",
            "bulgur",
            "farro",
            "spelt",
            "seitan",
            "cracker",
            "tortilla",
            "beer",
        }
    ),
    "soy": frozenset({"soy", "tofu", "miso", "edamame", "tamari", "tempeh"}),
    "sesame": frozenset({"sesame", "tahini"}),
}

#: Multi-word violated phrases per allergen (labels without an entry
#: have none). "fish sauce" and "oyster sauce" also match their
#: single-word terms; they are listed explicitly so the table states
#: the intent.
ALLERGEN_VIOLATED_PHRASES: dict[str, tuple[str, ...]] = {
    "tree nuts": ("brazil nut", "pine nut"),
    "shellfish": ("oyster sauce",),
    "fish": ("fish sauce",),
    "milk/dairy": ("ice cream",),
    "wheat/gluten": ("soy sauce",),
    "soy": ("soy sauce",),
}

#: Single-word unverified terms per allergen: kept, but the entry
#: lists the term and the note may not claim the option was checked
#: clean (same "verified" rule as P3-L-07).
ALLERGEN_UNVERIFIED_TERMS: dict[str, frozenset[str]] = {
    # "oat" covers "oat", "oats" and "oat milk" via the plural-aware
    # matcher (Phase 7 P7-OAT-01: "oat milk" was missed by "oats" alone).
    # "oatmeal" is a single token, so word-boundary matching needs it
    # literally (Phase 7 review). Compound forms found in the Epicure
    # vocabulary (oat, oat_milk) are covered by "oat".
    "wheat/gluten": frozenset({"bouillon", "malt", "seasoning", "oat", "oatmeal"}),
    "fish": frozenset({"worcestershire"}),
    "egg": frozenset({"custard", "pasta", "batter"}),
    "soy": frozenset({"lecithin"}),
    "sesame": frozenset({"furikake"}),
    # Generic "nuts" may or may not include peanuts.
    "peanut": frozenset({"nut"}),
}

#: Multi-word unverified phrases per allergen.
ALLERGEN_UNVERIFIED_PHRASES: dict[str, tuple[str, ...]] = {
    "fish": ("caesar dressing",),
    "shellfish": ("seafood stock", "xo sauce"),
    "soy": ("vegetable oil",),
    "sesame": ("za'atar",),
}

#: Fish words shared conceptually with the P3-L-07 vegetarian
#: fish/seafood list (kept literal here; overlap pinned by test).
_FISH_OVERLAP_TERMS = frozenset({"fish", "tuna", "salmon", "cod", "anchovy", "sardine", "trout"})


def _alt_flour_phrases() -> tuple[str, ...]:
    """Alternative-flour exemptions: the flour compounds shared with
    ``recommendations.policy.COMPOUND_EXCEPTIONS`` (rice, almond,
    coconut, chickpea, oat, buckwheat) plus corn, potato and tapioca.
    A "flour" hit on a line naming one of these is not wheat. Oat
    flour is not wheat but can be gluten cross-contaminated: the
    unverified term "oat" marks it unverified, never violated (Phase 7
    review: a violated reason would falsely say it contains wheat)."""
    from culinary_copilot.recommendations.policy import COMPOUND_EXCEPTIONS

    shared = sorted(c for c in COMPOUND_EXCEPTIONS if c.endswith(" flour"))
    return tuple(shared + ["corn flour", "potato flour", "tapioca flour"])


def _phrase_hit(line: str, phrase: str) -> bool:
    """Case-insensitive whole-word phrase match, plural-aware on the
    last word ("brazil nut" hits "brazil nuts", "corn tortilla" hits
    "corn tortillas"). Single-word terms keep using
    ``ingredient_term_hit``; phrases (and spellings with apostrophes
    like "za'atar") match here against the raw line."""
    words = [w for w in re.split(r"\s+", phrase.strip().lower()) if w]
    if not words:
        return False
    parts = [re.escape(w) for w in words[:-1]]
    last = re.escape(words[-1]) + r"(?:s|es)?"
    return re.search(r"\b" + r"\s+".join(parts + [last]) + r"\b", line.lower()) is not None


def _allergen_violated_exempt(*, label: str, display: str, lowered: str) -> bool:
    """Narrow wheat/gluten exemptions for a matched violated term.

    A gluten-free-qualified line never violates ("gluten-free bread"
    is not a wheat hit); "flour" is exempt on alternative-flour
    lines; "noodle" on rice/glass-noodle lines; "tortilla" on corn-
    tortilla lines; "soy sauce" on tamari (or gluten-free) lines.
    """
    if label != "wheat/gluten":
        return False
    if re.search(r"gluten[-\s]?free", lowered):
        return True
    if display == "flour" and any(_phrase_hit(lowered, p) for p in _alt_flour_phrases()):
        return True
    if display == "noodle" and (
        _phrase_hit(lowered, "rice noodle") or _phrase_hit(lowered, "glass noodle")
    ):
        return True
    if display == "tortilla" and _phrase_hit(lowered, "corn tortilla"):
        return True
    if display == "soy sauce" and (
        "tamari" in lowered or re.search(r"gluten[-\s]?free", lowered) is not None
    ):
        return True
    return False


def _wheat_cube_unverified(lowered: str) -> bool:
    """Stock or broth cubes (but not plain stock/broth) are wheat-unverified."""
    from culinary_copilot.recommendations.policy import ingredient_term_hit

    return ingredient_term_hit(lowered, "cube") and (
        ingredient_term_hit(lowered, "stock") or ingredient_term_hit(lowered, "broth")
    )


#: Extra name words that mark a restriction as NAMED without being
#: ingredient terms themselves ("tree nut allergy", "nut allergy").
_ALLERGEN_NAME_WORDS = frozenset({"tree", "nut", "nuts"})

#: Named diet terms: "I'm vegetarian" names the restriction, so the
#: unnamed rule does not trigger (same for vegan).
NAMED_DIET_TERMS = frozenset({"vegetarian", "vegan"})


def _named_avoidance_terms() -> set[str]:
    """Every word whose presence names the restriction (not unnamed)."""
    terms: set[str] = set(NAMED_DIET_TERMS) | set(_ALLERGEN_NAME_WORDS)
    for label_terms in ALLERGEN_VIOLATED_TERMS.values():
        terms.update(label_terms)
    return terms


def mentions_restriction(text: str) -> bool:
    """True when the text matches a narrow unnamed-restriction pattern."""
    return any(rx.search(text or "") is not None for rx in _UNNAMED_RESTRICTION_RES)


def names_specific_avoidance(text: str) -> bool:
    """True when the text names a specific allergen or diet term."""
    from culinary_copilot.recommendations.policy import ingredient_term_hit

    lowered = str(text or "")
    return any(ingredient_term_hit(lowered, term) for term in sorted(_named_avoidance_terms()))


def _flatten_constraint_texts(constraints: dict[str, Any]) -> list[str]:
    """Constraint values as texts (keys never trigger the rule)."""
    texts: list[str] = []
    for value in (constraints or {}).values():
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, list):
            texts.extend(str(v) for v in value if str(v).strip())
        elif isinstance(value, dict):
            texts.extend(str(v) for v in value.values() if str(v).strip())
    return texts


def flatten_constraint_texts(constraints: dict[str, Any]) -> list[str]:
    """Public alias: constraint values as texts (keys never count)."""
    return _flatten_constraint_texts(constraints)


def unresolved_unnamed_restriction(
    request_texts: list[str],
    constraints: dict[str, Any] | None,
    confirmed_answers: list[Any] | None,
) -> bool:
    """True while an unnamed allergy/restriction needs a question first.

    Detects a narrow-pattern mention in the request texts or session
    constraint values with no specific allergen or diet term named
    anywhere in them, and no confirmed answer naming one. A named
    mention ("peanut allergy", "I'm vegetarian") or a naming answer
    ("peanuts") resolves it.
    """
    texts = [str(t) for t in (request_texts or []) if str(t or "").strip()]
    texts.extend(_flatten_constraint_texts(constraints or {}))
    if not any(mentions_restriction(text) for text in texts):
        return False
    if any(names_specific_avoidance(text) for text in texts):
        return False
    for answer in confirmed_answers or []:
        text = answer.get("answer") if isinstance(answer, dict) else answer
        if names_specific_avoidance(str(text or "")):
            return False
    return True


#: Allergen names that name a mapped label without being one of its
#: ingredient terms (2026-10-07 live session: the answer "Tree nuts"
#: mapped to no allergen, so the options were never checked). A bare
#: "nut" names both tree nuts and peanut (conservative).
ALLERGEN_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "peanut": ("nut",),
    "tree nuts": ("tree nut",),
    "wheat/gluten": ("celiac", "coeliac"),
}


def allergens_named_in_answers(answer_texts: list[str]) -> list[str]:
    """Mapped allergen labels named in the answer texts (map order)."""
    from culinary_copilot.recommendations.policy import ingredient_term_hit

    labels: list[str] = []
    texts = [str(text or "") for text in (answer_texts or [])]
    for label in ALLERGEN_VIOLATED_TERMS:
        words = sorted(ALLERGEN_VIOLATED_TERMS[label])
        aliases = ALLERGEN_NAME_ALIASES.get(label, ())
        phrases = ALLERGEN_VIOLATED_PHRASES.get(label, ()) + tuple(
            alias for alias in aliases if " " in alias
        )
        # "tree nuts" names tree nuts only: the bare "nut" alias reads
        # the text without those mentions.
        bare_texts = [_TREE_NUT_RE.sub(" ", text) for text in texts]
        if (
            any(ingredient_term_hit(text, term) for text in texts for term in words)
            or any(_phrase_hit(text, phrase) for text in texts for phrase in phrases)
            or any(
                ingredient_term_hit(text, alias)
                for text in bare_texts
                for alias in aliases
                if " " not in alias
            )
        ):
            labels.append(label)
    return labels


_TREE_NUT_RE = re.compile(r"\btree[\s-]+nuts?\b", re.IGNORECASE)


#: Avoidance cues in an answer's own words ("no peanuts", "without
#: dairy", "gluten-free", "she can't have nuts").
_ANSWER_AVOIDANCE_RE = re.compile(
    r"\b(?:no|not|without|avoid(?:s|ing)?|exclud(?:e|es|ing)|free|never)\b|n['’]t\b",
    re.IGNORECASE,
)

#: Question wording that asks about allergies, diets or avoided foods.
_AVOIDANCE_QUESTION_RE = re.compile(
    r"\b(?:avoid\w*|allerg\w*|diet\w*|intoleran\w*|restrict\w*|"
    r"can['’]?t eat|cannot eat|don['’]t eat|doesn['’]t eat)\b",
    re.IGNORECASE,
)


def allergy_answer_texts(
    confirmed_answers: list[Any] | None,
    question_texts: dict[str, str] | None,
    restriction_texts: list[str] | None,
) -> list[str]:
    """Confirmed answer texts that count as allergy/avoidance evidence.

    Live finding (2026-10-05 demo): choosing "Creamy mushroom pasta"
    from a dish-choice question was read as a wheat allergy, because
    every answer was scanned for allergen terms. Now:

    - when the request, a user message or a constraint value mentions
      an allergy, intolerance or restriction, or states an avoidance
      ("no peanuts please", "gluten-free dinner"), every answer counts
      (unchanged and conservative: the P3-L-13 flow never weakens);
    - an answer whose question is not recorded in this session (for
      example answers supplied at session creation) always counts;
    - otherwise an answer counts only when its question asks about
      allergies, diets or avoided foods, or the answer itself mentions
      a restriction or an avoidance cue ("no peanuts", "gluten-free").

    Only a dish choice to a recorded non-restriction question, in a
    session with no restriction or avoidance anywhere, is excluded.
    """
    answers: list[tuple[str, str]] = []
    for answer in confirmed_answers or []:
        if not isinstance(answer, dict):
            continue
        text = str(answer.get("answer") or "").strip()
        if text:
            answers.append((str(answer.get("question_id") or ""), text))
    if any(
        mentions_restriction(str(t or "")) or _ANSWER_AVOIDANCE_RE.search(str(t or ""))
        for t in (restriction_texts or [])
    ):
        return [text for _, text in answers]
    questions = question_texts or {}
    out: list[str] = []
    for question_id, text in answers:
        if question_id not in questions:
            out.append(text)
            continue
        question = str(questions.get(question_id) or "")
        if (
            mentions_restriction(question)
            or _AVOIDANCE_QUESTION_RE.search(question) is not None
            or mentions_restriction(text)
            or _ANSWER_AVOIDANCE_RE.search(text) is not None
        ):
            out.append(text)
    return out


#: Model-text patterns that call an option allergen-free: a bare
#: "allergen-free", any "<allergen>-free" ("peanut-free",
#: "gluten-free", "nut-free"), or "safe ... allergy" ("safe for her
#: allergy"). Narrow and deterministic, no model judge.
_ALLERGEN_SAFETY_RES = (
    re.compile(
        r"\b(?:peanut|tree[\s-]?nuts?|nut|shellfish|fish|egg|milk|dairy|wheat|gluten|soy|sesame|allergen)[\s-]?free\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bsafe\b[\s\S]{0,60}\ballerg(?:y|ies|ic)\b", re.IGNORECASE),
)


def allergen_safety_claim(text: str) -> str | None:
    """The matched allergen-free/safe-for-allergy claim, or None.

    Catches note, adaptation or constraint text that calls an option
    allergen-free. A recipe source saying so itself is the only
    exemption (see :func:`allergen_claim_allowed`).
    """
    for rx in _ALLERGEN_SAFETY_RES:
        match = rx.search(text or "")
        if match:
            return match.group(0).strip()[:80]
    return None


def allergen_claim_allowed(claim: str, selection_texts: list[str]) -> bool:
    """True when a selected source recipe says the claim itself.

    The claim text (e.g. "peanut-free") must appear in the selected
    options' source documents; anything else is rejected even when no
    listed term matched, because the term list is incomplete.
    """
    lowered = (claim or "").strip().lower()
    return bool(lowered) and any(
        lowered in str(text or "").lower() for text in (selection_texts or [])
    )


def check_allergen_option(
    index: int, option: dict[str, Any], doc: dict[str, Any], label: str
) -> tuple[list[str], dict[str, Any]]:
    """Allergen-avoidance check for one option (P3-L-13, review rework).

    Three tiers per label, same drop pattern as
    :func:`check_dietary_option`: a violated term (direct or hidden
    source, e.g. "flour", "mayonnaise", "soy sauce", "oyster sauce")
    drops the option with a readable reason; an unverified term
    (e.g. "bouillon", "malt", "custard", "worcestershire", "caesar
    dressing", "furikake") keeps the option but is listed; anything
    else reports ``"no_listed_terms_found"`` — never "checked", since
    absence of a match never means safe. Unknown labels report
    ``"not_checked"`` with the label. Every entry carries
    :data:`ALLERGEN_DISCLAIMER`. Dairy reuses the plant-milk /
    plant-butter scrub so "almond milk" never flags dairy.
    """
    label = str(label or "")
    violated_words = ALLERGEN_VIOLATED_TERMS.get(label)
    if violated_words is None:
        return [], {"status": "not_checked", "value": label, "disclaimer": ALLERGEN_DISCLAIMER}
    from culinary_copilot.recommendations.policy import ingredient_term_hit

    errors: list[str] = []
    hits: list[str] = []
    unverified: list[str] = []
    for line in _ingredient_line_texts(doc):
        lowered = line.lower()
        scrubbed = lowered
        if label == "milk/dairy":
            for rx in _NON_DAIRY_RES:
                scrubbed = rx.sub(" ", scrubbed)
        match: str | None = None
        for term in sorted(violated_words):
            if ingredient_term_hit(scrubbed, term) and not _allergen_violated_exempt(
                label=label, display=term, lowered=lowered
            ):
                match = term
                break
        if match is None:
            for phrase in ALLERGEN_VIOLATED_PHRASES.get(label, ()):
                if _phrase_hit(lowered, phrase) and not _allergen_violated_exempt(
                    label=label, display=phrase, lowered=lowered
                ):
                    match = phrase
                    break
        if match is not None:
            excerpt = line.strip()[:80]
            errors.append(
                f"option {index}: contains {label} allergen {match!r} in ingredient line "
                f"{excerpt!r} (confirmed allergy answer); drop the option or substitute"
            )
            if match not in hits:
                hits.append(match)
            continue
        for term in sorted(ALLERGEN_UNVERIFIED_TERMS.get(label, frozenset())):
            if ingredient_term_hit(scrubbed, term) and term not in unverified:
                unverified.append(term)
        for phrase in ALLERGEN_UNVERIFIED_PHRASES.get(label, ()):
            if _phrase_hit(lowered, phrase) and phrase not in unverified:
                unverified.append(phrase)
        if label == "wheat/gluten" and _wheat_cube_unverified(lowered):
            if "stock/broth cubes" not in unverified:
                unverified.append("stock/broth cubes")
    if hits:
        entry: dict[str, Any] = {
            "status": "violated",
            "value": label,
            "terms": hits,
            "unverified_terms": list(unverified),
            "disclaimer": ALLERGEN_DISCLAIMER,
        }
    elif unverified:
        entry = {
            "status": "unverified",
            "value": label,
            "terms": [],
            "unverified_terms": list(unverified),
            "disclaimer": ALLERGEN_DISCLAIMER,
        }
    else:
        entry = {
            "status": "no_listed_terms_found",
            "value": label,
            "terms": [],
            "unverified_terms": [],
            "disclaimer": ALLERGEN_DISCLAIMER,
        }
    return errors, entry


def _ingredient_entries(doc: dict[str, Any]) -> list[dict[str, Any]]:
    raw = doc.get("ingredients")
    return [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []


def _numbers_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if str(left).strip() == str(right).strip():
        return True
    # Exact rational comparison (Phase 7 P7-MIXED-01): "1 1/2" and 1.5
    # are the same quantity. Reuses the deterministic ingestion parser
    # (recipes/normalize.py::quantity), not a third parser and not float
    # tolerance: "0.33" still does not match "1/3".
    from culinary_copilot.recipes.normalize import quantity as _quantity

    try:
        parsed_left = _quantity(str(left))
        parsed_right = _quantity(str(right))
    except (TypeError, ValueError):
        return False
    if parsed_left is None or parsed_right is None:
        return False
    return parsed_left == parsed_right


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
    errors.extend(plan_prose_quantity_errors(plan, doc))
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

#: Content words ignored by the plan-step overlap check (deterministic
#: attribution): a plan step is supported by a cited direction when
#: every remaining word of the step appears in the direction.
_PLAN_ATTRIBUTION_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "with",
        "for",
        "then",
        "until",
        "while",
        "it",
        "its",
        "them",
        "then",
        "is",
        "are",
        "be",
        "as",
        "at",
        "by",
        "from",
        "into",
        "over",
    }
)

#: Claims that the source has no directions (rejected whenever the
#: stored record actually has directions).
_NO_DIRECTIONS_PATTERNS = (
    re.compile(r"\bno\s+(directions|instructions|steps|method)\b", re.IGNORECASE),
    re.compile(r"\bwithout\s+(directions|instructions)\b", re.IGNORECASE),
    re.compile(
        r"\bdoes\s+not\s+(give|include|have|list|provide|contain)\b"
        r".{0,60}\b(directions|instructions|steps)\b",
        re.IGNORECASE,
    ),
)


#: Negation cue for fidelity claims (H3 part 2, 2026-10-08): same
#: approach as agent/loop.py::_negated_mention — a cue within the
#: eight words before the claim, in the same clause, marks it as an
#: exclusion ("does not follow the source exactly") rather than a
#: fidelity claim. The trailing n't alternative has no leading word
#: boundary so "doesn't follow" counts ("doesn" + "n't" has no
#: boundary before the n).
_FIDELITY_NEGATION_RE = re.compile(
    r"\b(?:no|not|without|never|omit\w*|exclud\w*|avoid\w*|skip\w*|"
    r"instead of|rather than|free of)\b|n['\u2019]t\b",
    re.IGNORECASE,
)

#: Fidelity claims rejected on a model_adaptation plan (H3 part 2,
#: 2026-10-08): deterministic patterns for text claiming the steps
#: follow, match or reproduce the source. Each pattern names the
#: fidelity verb plus the source it points at, so honest adaptation
#: language ("adapted from the source", "based on the source",
#: "plan from selected source") never matches: it names the source
#: without a fidelity verb. A bare "recipe" needs a determiner ("the
#: recipe", not "cake recipe"), so source directions like "Follow cake
#: recipe on the back of the box" never match. "verbatim" and "word
#: for word" need no source word (they claim exact reproduction on
#: their own). A negated match (see _FIDELITY_NEGATION_RE) is not a
#: claim.
#: Source group: original/source/stored (optionally + recipe), or the,
#: this or that recipe (optionally original).
_FIDELITY_SOURCE = (
    r"(?:original(?:\s+recipe)?|source(?:\s+recipe)?|stored(?:\s+recipe)?"
    r"|the\s+(?:original\s+)?recipe|this\s+(?:original\s+)?recipe"
    r"|that\s+(?:original\s+)?recipe)"
)
_FIDELITY_CLAIM_PATTERNS = (
    re.compile(rf"\bfollow(?:s|ed|ing)?\b[\s\S]{{0,40}}\b{_FIDELITY_SOURCE}\b", re.IGNORECASE),
    re.compile(rf"\bmatch(?:es|ed|ing)?\b[\s\S]{{0,40}}\b{_FIDELITY_SOURCE}\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:reproduc(?:e|es|ed|ing|tion)?|replicat(?:e|es|ed|ing|ion)?)\b"
        rf"[\s\S]{{0,40}}\b{_FIDELITY_SOURCE}\b",
        re.IGNORECASE,
    ),
    re.compile(rf"\bidentical\s+to\b[\s\S]{{0,30}}\b{_FIDELITY_SOURCE}\b", re.IGNORECASE),
    re.compile(
        rf"\bsame\b[\s\S]{{0,30}}\bas\b[\s\S]{{0,10}}\b{_FIDELITY_SOURCE}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bverbatim\b", re.IGNORECASE),
    re.compile(r"\bword\s+for\s+word\b", re.IGNORECASE),
    re.compile(rf"\bfaithful\b[\s\S]{{0,30}}\b{_FIDELITY_SOURCE}\b", re.IGNORECASE),
    re.compile(rf"\btrue\s+to\b[\s\S]{{0,20}}\b{_FIDELITY_SOURCE}\b", re.IGNORECASE),
    re.compile(
        rf"\bexact(?:ly)?\b[\s\S]{{0,20}}\b(?:copy|copies|reproduc\w*|replic\w*)\b"
        rf"[\s\S]{{0,20}}\b{_FIDELITY_SOURCE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bexact(?:ly)?\s+as\s+in\b[\s\S]{{0,20}}\b{_FIDELITY_SOURCE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bno\s+changes?\b[\s\S]{{0,20}}\b(?:to|from|in)\b"
        rf"[\s\S]{{0,10}}\b{_FIDELITY_SOURCE}\b",
        re.IGNORECASE,
    ),
    # 2026-10-08 review: common paraphrases the verb list missed.
    re.compile(
        rf"\bstick(?:s|ing)?\s+(?:closely\s+)?to\b[\s\S]{{0,10}}\b{_FIDELITY_SOURCE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:taken|copied|lifted)\s+(?:directly\s+|straight\s+|exactly\s+)?from\b"
        rf"[\s\S]{{0,10}}\b{_FIDELITY_SOURCE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_FIDELITY_SOURCE}\b[\s\S]{{0,20}}\bwith(?:out)?\s+(?:no\s+|any\s+)?"
        r"(?:changes?|modifications?|alterations?|deviations?)\b",
        re.IGNORECASE,
    ),
)

#: A claim scoped to numbered steps ("Steps 1-3 follow the source;
#: step 4 is my addition") is an honest partial statement, not a claim
#: about the whole plan (2026-10-08 review).
_STEP_SCOPE_RE = re.compile(r"\bsteps?\s+\d", re.IGNORECASE)


def _fidelity_clause_before(text: str, start: int) -> str:
    """Text from the clause start (. ! ? ; : or comma) to the match."""
    lowered = str(text or "").lower()
    clause_start = max(lowered.rfind(mark, 0, start) for mark in ".!?;:,") + 1
    return lowered[clause_start:start]


def _fidelity_match_negated(text: str, start: int) -> bool:
    """True when a negation cue governs the claim verb.

    Only the three words right before the match, in the same clause
    (a comma also ends it), count: "does not follow the source" and
    "not a verbatim copy" are negated, but "Without changing anything,
    this follows the original" and "Nothing is skipped and every step
    matches the source" are claims. 2026-10-08 review: an eight-word
    window (agent/loop.py::_negated_mention) let 5 of 8 claims through.
    """
    clause = _fidelity_clause_before(text, start)
    if _TRAILING_ADVERB_RE.match(text[start:]):
        # "verbatim" and "word for word" trail what they modify: "not
        # from the source verbatim" is negated by the whole clause.
        return _FIDELITY_NEGATION_RE.search(clause) is not None
    before = " ".join(clause.split()[-3:])
    return _FIDELITY_NEGATION_RE.search(before) is not None


_TRAILING_ADVERB_RE = re.compile(r"verbatim|word\s+for\s+word", re.IGNORECASE)

#: Advice to the cook, not a claim about the plan (owner demo
#: 2026-10-09): a clause-initial imperative "follow" whose object is a
#: doneness cue *of* the source ("follow the source's visual doneness
#: cues for the cheese"). Narrow on purpose: the object must be a
#: possessive source plus a cue noun, so "follow the source's steps"
#: and "these steps follow the source" stay claims.
_COOK_ADVICE_RE = re.compile(
    r"follow\s+(?:the\s+)?(?:original\s+|source\s+|stored\s+)?(?:recipe|source|original)"
    r"['\u2019]s\s+(?:[a-z-]+\s+){0,2}?(?:cues?|doneness|signs?|indicators?|tests?)\b",
    re.IGNORECASE,
)
_ADVICE_LEAD_WORDS = frozenset({"", "then", "and", "and then", "just", "simply", "please"})


def _cook_advice(text: str, start: int) -> bool:
    """True when the match at ``start`` is advice to follow a source cue."""
    if not _COOK_ADVICE_RE.match(text, start):
        return False
    lead = " ".join(_fidelity_clause_before(text, start).split())
    return lead in _ADVICE_LEAD_WORDS


def fidelity_claim(text: str) -> str | None:
    """First non-negated fidelity claim in the text, or None.

    Each pattern match with a negation cue before it (for example
    "does not follow the source exactly") is skipped; only an
    unnegated claim ("follows the original recipe exactly") is
    returned, truncated for error messages.
    """
    body = str(text or "")
    if not body.strip():
        return None
    for pattern in _FIDELITY_CLAIM_PATTERNS:
        for match in pattern.finditer(body):
            if _fidelity_match_negated(body, match.start()):
                continue
            if _cook_advice(body, match.start()):
                continue
            if _STEP_SCOPE_RE.search(_fidelity_clause_before(body, match.start())):
                continue
            return match.group(0).strip()[:120]
    return None


def plan_fidelity_errors(
    plan: dict[str, Any] | None,
    note: str | None,
    steps_source: str,
) -> list[str]:
    """Fidelity-claim check for a model_adaptation plan (H3 part 2).

    When ``steps_source`` is ``model_adaptation``, the model note,
    every adaptation description, and every plan text field
    (mise_en_place, steps, plating) must not claim the steps follow,
    match or reproduce the source. A source-labelled plan is not
    affected. A negated claim ("does not follow the source") passes.
    The error names the field and the matched claim so the one
    validation retry can remove it.
    """
    if steps_source != "model_adaptation":
        return []
    errors: list[str] = []
    candidates: list[tuple[str, str]] = []
    if isinstance(note, str) and note.strip():
        candidates.append(("note", note))
    adaptations = (plan or {}).get("adaptations") if isinstance(plan, dict) else []
    if isinstance(adaptations, list):
        for index, item in enumerate(adaptations):
            if isinstance(item, dict):
                description = str(item.get("description") or "")
                if description.strip():
                    candidates.append((f"adaptation {index}", description))
    raw_plan = plan if isinstance(plan, dict) else {}
    for field in ("mise_en_place", "steps"):
        items = raw_plan.get(field)
        if isinstance(items, list):
            for index, entry in enumerate(items):
                if isinstance(entry, str) and entry.strip():
                    candidates.append((f"{field} {index}", entry))
    plating = raw_plan.get("plating")
    if isinstance(plating, str) and plating.strip():
        candidates.append(("plating", plating))
    for field, content in candidates:
        claim = fidelity_claim(content)
        if claim is not None:
            errors.append(
                f"plan is model_adaptation but {field} claims fidelity "
                f"({claim!r}); remove the claim that the steps follow, "
                "match or reproduce the source (a negated "
                "'does not follow the source' is allowed)"
            )
    return errors


#: A bare citation tag inside step text ("[Source direction 2]",
#: "(direction 3)"): it restates step_sources and carries no cooking
#: content. A tag with any other words is not matched and still counts.
_CITATION_TAG_RE = re.compile(
    r"[\[(]\s*(?:source\s+)?(?:direction|step)s?\s*#?\s*\d+"
    r"(?:\s*(?:,|&|and|-|–)\s*\d+)*\s*[\])]",
    re.IGNORECASE,
)


def _plan_content_words(text: str) -> set[str]:
    """Lowercased content words of plan/direction text for attribution.

    Accents are folded first (2026-10-07 live fix: "jalapeños" split
    into "jalape" and "os" and never matched "jalapeno"), and bare
    citation tags are dropped.
    """
    cleaned = _CITATION_TAG_RE.sub(" ", str(text or ""))
    folded = unicodedata.normalize("NFKD", cleaned)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", folded.lower())
    return {w for w in words if w not in _PLAN_ATTRIBUTION_STOPWORDS}


def direction_supports_step(direction: str, step: str) -> bool:
    """True when every content word of the step appears in the direction.

    Faithful condensations pass (dropping words is fine); synonyms,
    added ingredients, or a changed method fail into model_adaptation.
    """
    words = _plan_content_words(step)
    return bool(words) and words <= _plan_content_words(direction)


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


def _plan_attribution(
    plan: dict[str, Any], directions: list[str]
) -> tuple[str, list[int], list[int], dict[int, tuple[int, list[str]]]]:
    """(steps_source, unsupported steps, uncovered directions, missing words).

    ``missing_words`` maps an unsupported step to its cited direction
    and the step's words that direction lacks (2026-10-06 live fix: a
    faithful plan with "drizzled" for "drizzle" failed twice on a
    message that only named the step index).
    """
    raw_steps = plan.get("steps") or []
    steps = [s for s in raw_steps if isinstance(s, str) and s.strip()]
    cited = plan.get("step_sources")
    unsupported: list[int] = []
    uncovered: list[int] = []
    missing_words: dict[int, tuple[int, list[str]]] = {}
    if steps and directions and isinstance(cited, list) and len(cited) == len(raw_steps):
        cited_indices: set[int] = set()
        for index, (step, citation) in enumerate(zip(raw_steps, cited)):
            valid = (
                isinstance(citation, int)
                and not isinstance(citation, bool)
                and 0 <= citation < len(directions)
            )
            if valid and direction_supports_step(directions[citation], str(step or "")):
                cited_indices.add(citation)
                continue
            unsupported.append(index)
            if valid:
                missing_words[index] = (
                    citation,
                    sorted(
                        _plan_content_words(str(step or ""))
                        - _plan_content_words(directions[citation])
                    )[:8],
                )
        # Coverage (Phase 7 close-out): "source" also requires every
        # stored direction index to be cited by at least one step. A
        # plan that silently drops a direction (e.g. a marinade) is an
        # adaptation, even when every kept step is faithful.
        uncovered = sorted(set(range(len(directions))) - cited_indices)
    else:
        unsupported = list(range(len(steps)))
    steps_source = "source" if steps and not unsupported and not uncovered else "model_adaptation"
    return steps_source, unsupported, uncovered, missing_words


def _attribution_detail(
    unsupported: list[int],
    uncovered: list[int],
    missing_words: dict[int, tuple[int, list[str]]],
) -> str:
    detail = f"plan steps {unsupported} are not all grounded" if unsupported else ""
    if missing_words:
        detail += "; " + "; ".join(
            f"step {index} uses words not in direction {citation}: "
            + ", ".join(repr(word) for word in words)
            for index, (citation, words) in missing_words.items()
        )
    # Directions cited only by ungrounded steps are named above; listing
    # them as "not cited" contradicted step_sources (2026-10-07 live fix).
    cited_by_ungrounded = {citation for citation, _ in missing_words.values()}
    uncited = [index for index in uncovered if index not in cited_by_ungrounded]
    if uncited:
        detail += ("; " if detail else "") + (
            f"source directions {uncited} are not cited by any step"
        )
    return detail


def has_model_steps_admission(plan: dict[str, Any]) -> bool:
    """True when a plan adaptation states the steps are not from the source."""
    adaptations = plan.get("adaptations") or []
    descriptions = [str(a.get("description") or "") for a in adaptations if isinstance(a, dict)]
    return any(
        marker in description.lower()
        for description in descriptions
        for marker in MODEL_STEPS_MARKERS
    )


def plan_attribution_note(plan: dict[str, Any], doc: dict[str, Any] | None) -> str | None:
    """App-written adaptation for a model_adaptation plan the model did not label.

    Only for sources with directions (ingredient-only sources still
    need the model's own admission). The note names the steps that
    differ and why, so the label and its reason come from code.
    """
    directions = doc_directions(doc or {})
    if not directions:
        return None
    steps_source, unsupported, uncovered, missing_words = _plan_attribution(plan, directions)
    if steps_source == "source" or has_model_steps_admission(plan):
        return None
    detail = _attribution_detail(unsupported, uncovered, missing_words)
    note = f"Labelled by the app: these steps are not from the source verbatim ({detail})."
    return note if len(note) <= 500 else note[:497].rstrip() + "…"


def raw_protein_hits(doc: dict[str, Any] | None) -> list[str]:
    """Raw meat/poultry/fish/egg terms in a source's ingredient lines.

    A line containing "cooked" is exempt. A non-empty result means a
    plan for this source needs a food-safety technique_ref.
    """
    hits: list[str] = []
    for line in _ingredient_line_texts(doc or {}):
        lowered = line.lower()
        if _word_hit(lowered, "cooked"):
            continue
        hit = next((term for term in sorted(RAW_PROTEIN_TERMS) if _word_hit(lowered, term)), None)
        if hit is not None and hit not in hits:
            hits.append(hit)
    return hits


def check_plan_evidence(
    plan: dict[str, Any],
    doc: dict[str, Any] | None,
    technique_rows: list[dict[str, Any]] | None,
    *,
    auto_label: bool = False,
) -> tuple[list[str], str]:
    """Minimum plan-evidence checks (P3-L-09, Phase 7 close-out).

    Returns ``(errors, steps_source)``. ``steps_source`` is ``"source"``
    only when every plan step cites a valid stored-direction index
    (``step_sources``, one entry per step) whose direction supports it
    (deterministic content-word overlap); otherwise it is
    ``"model_adaptation"`` and needs at least one plan adaptation
    stating the steps are not from the source (with ``auto_label`` and
    a source that has directions, the caller adds
    ``plan_attribution_note`` instead of rejecting). The label comes from
    the actual plan/evidence relationship, never from the mere
    presence of stored directions. Any plan or adaptation text
    claiming the source has no directions is rejected whenever the
    stored record actually has directions. Source ingredients with raw
    meat, poultry, fish or eggs (not "cooked") need at least one
    technique_ref to a food-safety chunk.
    """
    errors: list[str] = []
    source = doc or {}
    directions = doc_directions(source)
    steps_source, unsupported, uncovered, missing_words = _plan_attribution(plan, directions)
    if steps_source == "model_adaptation" and not has_model_steps_admission(plan):
        if directions:
            # auto_label: the caller attaches plan_attribution_note
            # instead (2026-10-06): the label is computed here either
            # way, so a missing model admission no longer fails a plan.
            if not auto_label:
                errors.append(
                    f"steps_source is model_adaptation "
                    f"({_attribution_detail(unsupported, uncovered, missing_words)}): "
                    "either keep each step to its cited direction's own words "
                    "(dropping words is fine) and cite every direction, or add a "
                    "plan adaptation stating the steps are not from the source"
                )
        else:
            errors.append(
                "steps_source is model_adaptation (the source has no directions): "
                "add a plan adaptation stating the steps are not from the source"
            )
    if directions:
        steps = [s for s in (plan.get("steps") or []) if isinstance(s, str) and s.strip()]
        prose = "\n".join(
            [str(s or "") for s in steps]
            + [str(m or "") for m in (plan.get("mise_en_place") or [])]
            + [str(plan.get("plating") or "")]
            + [
                str(a.get("description") or "")
                for a in (plan.get("adaptations") or [])
                if isinstance(a, dict)
            ]
        )
        if any(pattern.search(prose) for pattern in _NO_DIRECTIONS_PATTERNS):
            errors.append(
                f"plan claims the source has no directions, but the stored "
                f"record has {len(directions)} directions; remove the claim"
            )
    raw_hits = raw_protein_hits(source)
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


_WEB_LABELS = frozenset(
    {"official_guidance", "research_publication", "culinary_source", "unclassified"}
)


def session_web_sources(store: Any, session_id: str) -> dict[str, dict[str, Any]]:
    """Successful search_web sources for the session, keyed by URL.

    Reads search_results_retrieved-style evidence: the tool result is
    not stored in tool_call events (digest only), so this helper reads
    the search_results_retrieved events' URL lists. Classifications come
    from the evidence_evaluated events recorded for the exact same URL
    (``{"url", "classification"}`` per evaluation); a URL with no
    recorded classification carries no ``classification`` key and callers
    treat it as ``"unclassified"``. Exact-URL matching only, no
    normalization. Fail-closed: store errors yield an empty mapping.
    Provenance note: these are provider citation metadata (URLs), not
    verified page text.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        events = store.list_events(session_id)
    except Exception:
        return out
    for event in events:
        if getattr(event, "event_type", "") != "search_results_retrieved":
            continue
        payload = getattr(event, "payload", None) or {}
        urls = payload.get("urls")
        if isinstance(urls, list):
            for url in urls:
                if isinstance(url, str) and url and url not in out:
                    out[url] = {"url": url}
    for event in events:
        if getattr(event, "event_type", "") != "evidence_evaluated":
            continue
        payload = getattr(event, "payload", None) or {}
        evaluations = payload.get("evaluations")
        if not isinstance(evaluations, list):
            continue
        for item in evaluations:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            classification = item.get("classification")
            if not (isinstance(url, str) and url and url in out):
                continue
            if isinstance(classification, str) and classification in _WEB_LABELS:
                out[url]["classification"] = classification
    return out


def web_label_for(session_sources: dict[str, Any], url: str) -> str:
    """Publisher-signal label for one web URL in this session.

    Uses the classification recorded for that exact URL in
    ``session_sources`` (the same mapping :func:`validate_web_refs`
    checks refs against); ``"unclassified"`` when none is recorded or
    the recorded value is not a known label.
    """
    try:
        entry = (session_sources or {}).get(url)
    except AttributeError:
        return "unclassified"
    label = entry.get("classification") if isinstance(entry, dict) else None
    return label if isinstance(label, str) and label in _WEB_LABELS else "unclassified"


def validate_web_refs(refs: Any, *, session_sources: dict[str, Any]) -> list[str]:
    """Every web_ref URL must equal a source URL from this session.

    Successful searches only (resumed runs count; failures and other
    sessions do not). Refs are clickable: url plus non-empty title.
    """
    errors: list[str] = []
    if not isinstance(refs, list) or not refs:
        return ["web_answer needs at least 1 web_ref"]
    if len(refs) > 5:
        return ["web_answer carries at most 5 web_refs"]
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            errors.append(f"web ref {index}: not a mapping")
            continue
        url = ref.get("url")
        title = ref.get("title")
        if not isinstance(url, str) or not url.strip():
            errors.append(f"web ref {index}: missing url")
            continue
        if not isinstance(title, str) or not title.strip():
            errors.append(f"web ref {index}: missing title (citations stay clickable)")
            continue
        if url not in session_sources:
            # 2026-10-03 step-4 fix: list the session's exact source
            # URLs (up to 5) so the model can correct the ref next
            # turn instead of guessing another URL.
            known = sorted(str(u) for u in session_sources.keys() if str(u or "").strip())[:5]
            errors.append(
                f"web ref {index}: {url!r} was not returned in this session; "
                f"cite one of the session source urls: {known}"
            )
    return errors


#: Phrases marking an adaptation as listing unread directions (H3,
#: 2026-10-08): the plan states which stored-direction indices it did
#: not read. Checked case-insensitive; "does not follow the source"
#: is not in this list (that is the H3-part-2 fidelity claim).
_UNREAD_DIRECTION_MARKERS = (
    "not read",
    "did not read",
    "didn't read",
    "didnt read",
    "unread",
    "not fetched",
    "not retrieved",
    "not reviewed",
)

#: Direction mention: "direction 3" or "directions 12-15, 17".
_DIRECTION_MENTION_RE = re.compile(r"directions?\s+([0-9,\s\-–andto]+)", re.IGNORECASE)

#: Standard get_recipe summary bounds (H3). agent/loop.py imports these
#: as _DIRECTIONS_SHOWN / _DIRECTION_CHARS so the summary and the plan
#: gate cannot drift apart. A ranged read returns at most
#: DIRECTIONS_SHOWN directions full.
DIRECTIONS_SHOWN = 12
DIRECTION_CHARS = 600


def next_direction_slice(uncovered: list[int], total: int) -> tuple[int, int]:
    """The ranged get_recipe call that reads the most unread directions.

    From the first unread index, a window of up to DIRECTIONS_SHOWN
    ending after the last unread index inside it: unread [1, 5, 9] is
    one call (1-10), not three.
    """
    start = int(uncovered[0])
    inside = [i for i in uncovered if i < start + DIRECTIONS_SHOWN]
    return start, min(int(inside[-1]) + 1, int(total))


def omitted_direction_indices(doc: dict[str, Any] | None) -> list[int]:
    """Stored-direction indices omitted from the standard summary.

    2026-10-08 H3: the summary shows up to DIRECTIONS_SHOWN directions
    clipped at DIRECTION_CHARS. Omitted means a shown direction cut at 600 (its tail) or a
    direction beyond index 11. Empty when the summary shows everything
    full (total <= 12 and none over 600 chars).
    """
    return [
        index
        for index, direction in enumerate(doc_directions(doc or {}))
        if index >= DIRECTIONS_SHOWN or len(str(direction)) > DIRECTION_CHARS
    ]


def _parse_direction_numbers(group: str) -> set[int]:
    """Indices named in one direction-mention group (inclusive ranges).

    "12-15" expands to {12,13,14,15}; "12, 13 and 14" to {12,13,14};
    "12 to 15" is read as a range. Out-of-range values are kept: the
    caller decides coverage (a superset still lists every uncovered).
    """
    text = str(group or "").lower()
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\bto\b", "-", text)
    text = text.replace("and", ",").replace("&", ",")
    found: set[int] = set()
    for start_s, end_s in re.findall(r"(\d+)\s*-\s*(\d+)", text):
        try:
            start, end = int(start_s), int(end_s)
        except ValueError:
            continue
        lo, hi = (start, end) if start <= end else (end, start)
        # Bound expansion: a "0-100" typo must not allocate 101 ints.
        if hi - lo > 200:
            continue
        found.update(range(lo, hi + 1))
    stripped = re.sub(r"(\d+)\s*-\s*(\d+)", " ", text)
    for num_s in re.findall(r"\d+", stripped):
        try:
            found.add(int(num_s))
        except ValueError:
            continue
    return found


def unread_directions_listed(plan: dict[str, Any] | None, uncovered: list[int]) -> bool:
    """True when the plan's adaptations name every uncovered index.

    The combined adaptation text must contain an unread marker (see
    ``_UNREAD_DIRECTION_MARKERS``) and, in a "direction(s) <numbers>"
    mention, every uncovered index — either 0-based (step_sources
    indices, preferred) or 1-based. A plan with no uncovered indices
    needs no listing (returns True).
    """
    uncovered_list = [int(i) for i in (uncovered or [])]
    if not uncovered_list:
        return True
    adaptations = (plan or {}).get("adaptations") if isinstance(plan, dict) else []
    texts: list[str] = []
    if isinstance(adaptations, list):
        for item in adaptations:
            if isinstance(item, dict):
                desc = str(item.get("description") or "")
                if desc.strip():
                    texts.append(desc)
    combined = "\n".join(texts)
    lowered = combined.lower()
    if not any(marker in lowered for marker in _UNREAD_DIRECTION_MARKERS):
        return False
    named: set[int] = set()
    for match in _DIRECTION_MENTION_RE.finditer(combined):
        named |= _parse_direction_numbers(match.group(1))
    if not named:
        return False
    uncovered_set = set(uncovered_list)
    if uncovered_set <= named:
        return True
    # 1-based listing ("directions 13-14" for 0-based 12-13).
    shifted = {i + 1 for i in uncovered_list}
    return bool(shifted and shifted <= named)


def validate_omitted_directions(
    plan: dict[str, Any] | None,
    doc: dict[str, Any] | None,
    covered: set[int] | None,
    steps_source: str,
) -> list[str]:
    """Plan gate for omitted directions (H3, 2026-10-08).

    ``covered`` holds stored-direction indices returned full in this
    session (standard fetch covers short directions among the first 12;
    ranged fetches cover their slice). Returns errors (empty = valid):

    - no omitted indices, or every omitted index covered: no requirement;
    - otherwise the plan must list each uncovered index in an adaptation
      (see ``unread_directions_listed``) and be ``model_adaptation``;
    - the rejection names the uncovered indices and the ranged call to
      read them, so the one validation retry can succeed.
    """
    directions = doc_directions(doc or {})
    total = len(directions)
    omitted = omitted_direction_indices(doc)
    if not omitted:
        return []
    covered_set = set(int(i) for i in (covered or set()))
    uncovered = sorted(i for i in omitted if i not in covered_set)
    if not uncovered:
        return []
    if steps_source == "model_adaptation" and unread_directions_listed(plan, uncovered):
        return []
    source = (plan or {}).get("source") if isinstance(plan, dict) else {}
    dataset_id = str((source or {}).get("dataset_id") or "") if isinstance(source, dict) else ""
    source_id = str((source or {}).get("source_id") or "") if isinstance(source, dict) else ""
    start, end = next_direction_slice(uncovered, total)
    return [
        f"directions {uncovered} of {total} not yet read in this run; "
        f"call get_recipe with dataset_id={dataset_id!r}, "
        f"source_id={source_id!r}, directions_from={start} directions_to={end} "
        "before finishing, or list each unread direction index in a plan "
        "adaptation (e.g. 'directions "
        + ", ".join(str(i) for i in uncovered[:6])
        + (" …' " if len(uncovered) > 6 else "' ")
        + "not read') with model_adaptation"
    ]


def web_claim_context_ok(*, claim_subject: str, source_text: str | None) -> bool:
    """Subject-context fit for a numeric claim against source text.

    The claim's subject (e.g. "chicken") must appear in the SAME
    sentence as the number in that source text (sentence-split on
    . ! ? ;). No source text (always None in this integration) fails
    closed. Checking generated text against generated text never
    counts: only source text actually obtained (provider-retrieved
    page content) verifies; model excerpts do not.
    """
    if not source_text:
        return False
    subject = str(claim_subject or "").strip().lower()
    if not subject:
        return False
    subject_rx = re.compile(r"\b" + re.escape(subject) + r"s?\b")
    number_rx = re.compile(r"\d+(?:\.\d+)?")
    for sentence in re.split(r"[.!?;]+", str(source_text)):
        if subject_rx.search(sentence.lower()) and number_rx.search(sentence):
            return True
    return False


__all__ = [
    "ALLERGEN_DISCLAIMER",
    "ALLERGEN_UNVERIFIED_PHRASES",
    "ALLERGEN_UNVERIFIED_TERMS",
    "ALLERGEN_VIOLATED_PHRASES",
    "ALLERGEN_VIOLATED_TERMS",
    "AMBIGUOUS_DIET_TERMS",
    "NAMED_DIET_TERMS",
    "RAW_PROTEIN_TERMS",
    "MODEL_STEPS_MARKERS",
    "SAFETY_DOC_IDS",
    "UNNAMED_RESTRICTION_DESCRIPTIONS",
    "VEGAN_EXTRA_TERMS",
    "VEGAN_VIOLATION_TERMS",
    "VEGETARIAN_VIOLATION_TERMS",
    "DIRECTIONS_SHOWN",
    "DIRECTION_CHARS",
    "next_direction_slice",
    "omitted_direction_indices",
    "unread_directions_listed",
    "validate_omitted_directions",
    "RecipeResolver",
    "TECHNIQUE_OPTION_KEYS",
    "TechniqueResolver",
    "allergen_claim_allowed",
    "allergen_safety_claim",
    "allergens_named_in_answers",
    "check_allergen_option",
    "check_dietary_option",
    "check_plan_evidence",
    "dietary_values",
    "direction_supports_step",
    "doc_directions",
    "doc_text",
    "fidelity_claim",
    "hard_constraint_keys",
    "mentions_restriction",
    "names_specific_avoidance",
    "plan_fidelity_errors",
    "quantity_in_source",
    "session_web_sources",
    "unresolved_unnamed_restriction",
    "web_label_for",
    "validate_one_option",
    "validate_options",
    "validate_plan",
    "validate_technique_refs",
    "validate_web_refs",
    "web_claim_context_ok",
]
