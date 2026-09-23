"""odunola/foodie adapter (Workstream 2).

Source: https://huggingface.co/datasets/odunola/foodie
Pinned revision: 20a451c2a8f22e9161a13346f08e0d7cdd555727
File: recipes.csv (single ``texts`` column), SHA-256
``76dd8f8692a4c751d5ea7ce30dcd7dd9d4c5d17a1ec4cef163edf83966b98e4c``.
Rows: 19,566. Declared license in card front-matter: apache-2.0
(datasets-server info license empty; provenance_status stays ``pending``).
No per-recipe source URLs exist; source_url is always None (never invented).

Text shape (verified): Title line, optional description, ``Ingredients``
marker, one ingredient per line, ``Introduction`` marker (ends ingredients,
starts a prose paragraph), ``Directions``/``directions`` marker (malformed
variants like ``</adirections`` occur), then one step per line.

Conservative rules: never split ingredient lines on commas, never infer a
quantity from instructions, never convert volume<->mass, never assume a
regional cup, never collapse ranges to midpoints, never sum step durations,
never invent servings/units/ingredients/instructions. Ambiguity is retained
in ``original`` text with a quality issue and lowered capability; records
the parser cannot resolve cleanly are routed to LLM extraction (see
:mod:`culinary_copilot.recipes.routing`) instead of accumulating
recipe-specific regular expressions.

``available_fields`` semantics: SOURCE presence — whether the source text
contains evidence for a field — independent of whether structured
extraction succeeded. A field can be present-but-unstructured (flagged),
never silently marked absent when the source mentions it.
"""

import hashlib
import json
import re
import unicodedata
from fractions import Fraction
from typing import Any

from culinary_copilot.recipes.adapters.base import foodie_source_id
from culinary_copilot.recipes.normalize import canonical
from culinary_copilot.recipes.quality import capabilities_for, issue
from culinary_copilot.recipes.search import SEARCH_DOCUMENT_VERSION

FOODIE_ADAPTER_VERSION = "4"
FOODIE_DATASET = "odunola/foodie"
FOODIE_REVISION = "20a451c2a8f22e9161a13346f08e0d7cdd555727"
FOODIE_FILE = "recipes.csv"
FOODIE_SHA256 = "76dd8f8692a4c751d5ea7ce30dcd7dd9d4c5d17a1ec4cef163edf83966b98e4c"
FOODIE_LICENSE = "apache-2.0"

UNICODE_FRACTIONS = {
    "½": "1/2",
    "¼": "1/4",
    "¾": "3/4",
    "⅓": "1/3",
    "⅔": "2/3",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
    "⅕": "1/5",
    "⅖": "2/5",
    "⅗": "3/5",
    "⅘": "4/5",
    "⅙": "1/6",
    "⅚": "5/6",
    "⁄": "/",
}

UNIT_ALIASES: dict[str, set[str]] = {
    "g": {"g", "gram", "grams", "gm", "gms"},
    "kg": {"kg", "kilogram", "kilograms", "kilo", "kilos"},
    "mg": {"mg", "milligram", "milligrams"},
    "ml": {"ml", "milliliter", "millilitre", "milliliters", "millilitres"},
    "l": {"l", "liter", "litre", "liters", "litres", "litre.", "liter."},
    "cup": {"cup", "cups", "c"},
    "tbsp": {"tbsp", "tablespoon", "tablespoons", "tbs", "tblsp"},
    "tsp": {"tsp", "teaspoon", "teaspoons"},
    "oz": {"oz", "ounce", "ounces"},
    "fl_oz": {"fluid ounce", "fluid ounces", "fl oz", "floz"},
    "lb": {"lb", "lbs", "pound", "pounds"},
    "cl": {"cl", "centiliter", "centiliters", "centilitre", "centilitres"},
    "pint": {"pint", "pints"},
    "quart": {"quart", "quarts"},
    "gallon": {"gallon", "gallons"},
    "pinch": {"pinch", "pinches"},
    "dash": {"dash", "dashes"},
    "splash": {"splash", "splashes"},
    "wedge": {"wedge", "wedges"},
    "leaf": {"leaf", "leaves"},
    "clove": {"clove", "cloves"},
    "slice": {"slice", "slices"},
    "piece": {"piece", "pieces"},
    "can": {"can", "cans", "tin", "tins"},
    "package": {"package", "packages", "pack", "packs", "packet", "packets"},
    "bunch": {"bunch", "bunches"},
    "stalk": {"stalk", "stalks"},
    "sprig": {"sprig", "sprigs"},
    "cube": {"cube", "cubes"},
    "envelope": {"envelope", "envelopes"},
    "jar": {"jar", "jars"},
    "bottle": {"bottle", "bottles"},
    "box": {"box", "boxes"},
    "bag": {"bag", "bags"},
    "stick": {"stick", "sticks"},
    "scoop": {"scoop", "scoops"},
    "drop": {"drop", "drops"},
    "strip": {"strip", "strips"},
    "head": {"head", "heads"},
    "rib": {"rib", "ribs"},
}
_ALIAS_LOOKUP = {alias: norm for norm, aliases in UNIT_ALIASES.items() for alias in aliases}

QUALITATIVE_RE = re.compile(
    r"(?i)\b(to taste|as needed|as desired|to serve|for garnish|for serving|"
    r"for dusting|optional|as required)\b"
)
SERVINGS_RE = re.compile(
    r"(?i)\b(serves|servings?|makes?|yields?)\s*[:\-]?\s*(\d+(?:\s*(?:-|–|—|to|or)\s*\d+)?)"
)
# Batch yield ("makes 2 dozen cookies") is NEVER servings. Dozen arithmetic
# is exact: 1 dozen = 12 count.
DOZEN_YIELD_RE = re.compile(r"(?i)\b(makes?|yields?|recipe makes?)\s+(\d+(?:\.\d+)?)\s*dozen\b")
DURATION_MENTION_RE = re.compile(
    r"(?i)\b\d+(?:\s*(?:-|–|—|to|or)\s*\d+)?\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|overnight)\b"
    r"|\b\d+\s*°?\s*[CF]\b"
)
TEMP_RE = re.compile(
    r"(\d{2,3})\s*°?\s*([CF])\b|(\d{2,3})\s*degrees?\s*([CF])?\b|gas\s*mark\s*(\d)",
    re.IGNORECASE,
)
LABELED_DURATION_RE = re.compile(
    r"(?i)\b(prep(?:aration)?|cook|total|bake|chill|rest|freeze|marinate|"
    r"simmer|boil|roast|grill|fry|saute|sauté|brown|soak|refrigerate|"
    r"steam|reduce|microwave|preheat)"
    r"[^.\n]{0,24}?(\d+(?:\s*(?:-|–|—|to|or)\s*\d+)?)\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|h)\b"
)
# Section-boundary headers that end the ordered cooking instructions. The
# note text is preserved in ``notes_text``; it is never emitted as steps.
NOTES_HEADER_RE = re.compile(
    r"(?i)^\s*(important\s+notes?|notes?|tips?|editor'?s\s+notes?|"
    r"cook'?s\s+notes?|chef'?s\s+notes?|cooking\s+tips?|recipe\s+notes?|"
    r"author'?s\s+note|variations?|substitutions?|storage|faqs?)\s*:?\s*$"
)
COOKING_VERBS = frozenset(
    "add bake beat blend boil braise brown brush chill chop combine cook cool "
    "cover crush cut dice drain drizzle fry garnish grate grease grill heat "
    "knead layer marinate mash melt mix muddle peel pour preheat refrigerate "
    "reduce roast roll saute scale scoop season serve simmer slice soak spread "
    "sprinkle steam stir strain sift simmer toast toss transfer whisk whip "
    "whisk freeze thaw brown drain rinse trim".split()
)
# Ingredient-group heading cues (a bare line is a heading only with ":" or
# one of these; unquantified ingredients like "ice" stay ingredients).
HEADING_CUE_RE = re.compile(
    r"(?i)\b(for\s+the|to\s+make|to\s+serve|for\s+serving|filling|dough|"
    r"sauce|topping|glaze|marinade|dressing|garnish|batter|crust|"
    r"frosting|coating|breading)\b"
)
# "or" glued to a word ("syrupor honey"). Common English -or words are
# excluded so "flavor ", "color " etc. never split.
OR_WORD_STOPLIST = frozenset(
    "for nor or favor flavour flavor colour color error errors mirror "
    "donor donor arbor armor armour actor author candor decor demeanor "
    "favor favor fervor flavor horror honor labor major mayor minor odor "
    "parlor prior rumor savor squalor stupor tenor terror valor vapor "
    "visitor tractor motor rotor".split()
)
OR_GLUE_RE = re.compile(r"(?i)([A-Za-z]{2,})or(?=\s)")
# Multi-char unit prefix glued to a name ("poundfully" -> "pound fully").
# Single-letter prefixes are NEVER stripped here ("large" keeps its "l").
UNIT_PREFIX_RE = re.compile(
    r"(?i)^(teaspoons?|tablespoons?|ounces?|pounds?|cups?|pints?|quarts?|"
    r"gallons?|kilos?|grams?|milliliters?|millilitres?|liters?|litres?|"
    r"centiliters?|centilitres?|cloves?|slices?|pieces?|cans?|pinchs?|"
    r"dashes?)([a-z][a-z]+.*)$"
)

DIRECTION_MARKERS = {
    "directions",
    "direction",
    "method",
    "instructions",
    "preparation",
    "steps",
    "step",
}


def _replace_unicode_fractions(text: str) -> str:
    # A fraction glued to a digit is a mixed number ("1½" -> "1 1/2",
    # never "11/2").
    for uni, ascii_ in UNICODE_FRACTIONS.items():
        if uni == "⁄":
            text = text.replace(uni, ascii_)
        else:
            text = re.sub(r"(?<=\d)" + re.escape(uni), " " + ascii_, text)
            text = text.replace(uni, ascii_)
    return text


PREP_SUFFIX_RE = re.compile(
    r"(?i)([a-z])(chopped|diced|crushed|sliced|minced|grated|peeled|quartered|"
    r"halved|beaten|melted|softened|drained|rinsed|trimmed|divided|chilled|"
    r"softened|toasted)$"
)

UNIT_GLUE_RE = re.compile(
    r"(?i)(?<=\d)(kg|mg|ml|cups?|tsp|tbsp|oz|lbs?|pounds?|"
    r"pints?|quarts?|cloves?|slices?|pieces?|cans?)(?=[a-z]{2,})"
)
# Single-letter g is handled conservatively in strip_unit_prefix: 3garlic is
# a count of garlic, not grams of "arlic".
# NOTE: single-letter "l" is deliberately absent above. "6leek" is count 6
# of leeks, not 6 liters; bare "l" only counts as a unit when
# whitespace-separated ("1 l water"), never from glue-splitting.


def repair_spacing(line: str) -> str:
    """Insert spaces lost in scraping (``170gplain`` -> ``170 g plain``).

    Only deterministic separators: digit->letter, lower->Upper (glued
    markers like ``PowderIntroduction``), unit-prefix->name
    (``gplain`` -> ``g plain``), ``)``->letter, and letter->``(``.
    Original text is always preserved separately; this output is for
    parsing only.
    """
    text = _replace_unicode_fractions(line)
    # Attached unit glued to a name ("170gplain" -> "170g plain") must split
    # BEFORE the generic digit->letter rule, and only when the number touches
    # the unit ("2 large" already spaced is never touched).
    text = UNIT_GLUE_RE.sub(r"\1 ", text)
    text = re.sub(r"(?<=\d)(?=[A-Za-z°℃])", " ", text)
    text = _split_or_glue(text)
    text = re.sub(r"(?<=[a-z)])(?=[A-Z])", " ", text)
    text = re.sub(r"\)(?=[A-Za-z])", ") ", text)
    text = re.sub(r"(?<=[A-Za-z0-9])\(", " (", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _split_or_glue(text: str) -> str:
    """Split whitespace-lost "or" ("syrupor honey" -> "syrup or honey").

    Common English -or words ("flavor", "color", ...) are excluded via a
    finite stoplist. Anything still ambiguous is left for LLM routing.
    """

    def fix(match: re.Match[str]) -> str:
        word = match.group(0)
        if word.casefold() in OR_WORD_STOPLIST:
            return word
        return match.group(1) + " or"

    return OR_GLUE_RE.sub(fix, text)


def _split_leading_unit(rest: str) -> tuple[str | None, str | None, str]:
    """Split an optional leading unit (incl. "fluid ounces") off ``rest``.

    Returns (normalized_unit, unit_text, name_remainder). Single-letter
    prefixes are never stripped from glued tokens ("large" keeps its "l").
    """
    first, sep, remainder = rest.partition(" ")
    norm = _ALIAS_LOOKUP.get(first.lower().rstrip(".,"))
    if norm is not None:
        return norm, first, remainder.strip()
    if sep and remainder.strip():
        two, _, two_rest = remainder.partition(" ")
        combined = f"{first} {two}".lower().rstrip(".,")
        norm = _ALIAS_LOOKUP.get(combined)
        if norm is not None:
            return norm, f"{first} {two}", two_rest.strip()
    if first:
        prefix, glued = strip_unit_prefix(first)
        if prefix is not None and glued:
            norm = _ALIAS_LOOKUP.get(prefix.lower())
            if norm is not None:
                tail = f"{glued} {remainder}".strip() if sep else glued
                return norm, prefix, tail
    return None, None, rest


def strip_unit_prefix(token: str) -> tuple[str | None, str | None]:
    """Split a unit prefix glued to a name ("poundfully" -> pound + fully).

    Only multi-character unit prefixes are stripped, so words starting with
    "l"/"c" ("large", "cumin") are never touched. Grams additionally handles
    the doubled ("ggrated" -> "g grated") and hyphenated ("gsemi-salted")
    scrape artifacts. Returns (unit_text, remainder) or (None, None).
    """
    low = token.lower()
    # Only explicit, known scrape suffixes justify a bare gram prefix.
    # Unknown glued words remain unstructured rather than losing a letter.
    if re.fullmatch(r"g(?:plain|corn|spinach|flour|sugar|butter|chocolate|rice|oats|grated)", low):
        return token[:1], token[1:]
    if low == "gsemi-salted":
        return token[:1], token[1:]
    match = UNIT_PREFIX_RE.match(token)
    if match:
        return match.group(1), match.group(2)
    return None, None


QUALIFIER_AFTER_OR_RE = re.compile(
    r"(?i)\bor\s+(more|as needed|as desired|to taste|to serve|as required)\b"
)


def _has_alternative(name: str, notes: list[str], qualitative: bool) -> bool:
    """True only for genuine ingredient alternatives.

    "flank or skirt steak" and parenthetical "(or ...)" count. Quantity
    qualifiers ("or more as needed", "or to taste") never count, even though
    they contain the word "or".
    """
    first_clause = re.split(r",", name, maxsplit=1)[0]
    if re.search(r"(?i)\bor\b", first_clause):
        if not QUALIFIER_AFTER_OR_RE.search(first_clause):
            return True
    for note in notes:
        if re.search(r"(?i)\bor\b", note) and not QUALIFIER_AFTER_OR_RE.search(note):
            return True
    return False


def split_prep_suffix(name: str) -> tuple[str, str | None]:
    """Split glued/trailing preparation notes (``Onionchopped`` -> onion + chopped)."""
    match = PREP_SUFFIX_RE.search(name.strip())
    if match:
        base = name[: match.start(2)].strip()
        prep = match.group(2).lower()
        if base:
            return base, prep
    trailing = re.match(r"(?i)^(.*?)\s+(chopped|diced|crushed|sliced|minced)$", name.strip())
    if trailing and trailing.group(1).strip():
        return trailing.group(1).strip(), trailing.group(2).lower()
    return name, None


def split_glued_markers(text: str) -> str:
    """Split section markers glued to content without newlines."""
    text = re.sub(r"(?i)</?[a-z]*directions?\s*>?", "\nDirections\n", text)
    text = re.sub(
        r"(?i)([^\n\s])(Ingredients|Introduction|Directions|Method|Instructions)(?=[\n]|$)",
        r"\1\n\2",
        text,
    )
    return text


def _rational(text: str) -> str | None:
    text = text.strip()
    if not re.fullmatch(r"\d+(?:\.\d+|/\d+| \d+/\d+)?", text):
        return None
    try:
        amount = sum((Fraction(part) for part in text.split()), Fraction(0))
        return str(amount) if amount > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def parse_ingredient_line(original: str) -> dict[str, Any]:
    """Parse one ingredient line conservatively; never raises."""
    repaired = repair_spacing(original.strip())
    working = repaired
    notes: list[str] = []
    for match in re.finditer(r"\(([^()]*)\)", repaired):
        notes.append(match.group(1).strip())
    qualitative = bool(QUALITATIVE_RE.search(working))
    optional = bool(re.search(r"(?i)\boptional\b", working))
    # Range: "40-45 minutes", "1-2 cups", "3 or 4 minutes" — retained with
    # the full span text, never collapsed to a midpoint.
    range_match = re.match(
        r"^(?P<a>\d+(?:\s+\d+/\d+|\.\d+|/\d+)?)\s*(?:-|–|—|to|or)\s*"
        r"(?P<b>\d+(?:\s+\d+/\d+|\.\d+|/\d+)?)\s*(?P<rest>.*)$",
        working,
    )
    is_range = bool(
        range_match
        and (range_match.group("rest") or "").strip()
        and not re.match(r"(?i)^(to taste|as needed)", (range_match.group("rest") or "").strip())
    )
    compound = bool(re.search(r"(?i)\bplus\b", working.split("(")[0]) and re.search(r"\d", working))
    equivalent = bool(
        re.match(r"^\d[^(]*\([^()]*\d[^()]*\)", working)
        and ("g" in working.lower() or "tsp" in working.lower() or "cup" in working.lower())
    )
    amount: str | None = None
    amount_text: str | None = None
    unit: str | None = None
    unit_text: str | None = None
    name = working
    quantity_text: str | None = None
    single = re.match(r"^(?P<num>\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)\s*(?P<rest>.*)$", working)
    if compound:
        # Compound ("1/3 cup plus 2 tablespoons sugar"): retain full text,
        # never sum. The head quantity parses normally; the trailing
        # plus-clause moves to notes. When the head leaves no name
        # ("1/3 cup ..."), the name comes from the tail segment instead.
        # A tail carrying its own quantity ("2 tablespoons") makes the total
        # unrepresentable as one scalar, so amount stays None and capability
        # is lowered rather than overstated.
        segments = re.split(r"(?i)\bplus\b", working, maxsplit=1)
        head = segments[0].strip()
        tail = segments[1].strip() if len(segments) > 1 else ""
        notes.append("plus " + tail if tail else "plus")
        head_match = re.match(
            r"^(?P<num>\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)\s*(?P<rest>.*)$", head
        )
        head_name = ""
        if head_match:
            head_num = head_match.group("num")
            head_rest = (head_match.group("rest") or "").strip()
            head_unit, head_unit_text, head_name = _split_leading_unit(head_rest)
            if head_unit is not None or not head_name:
                amount = _rational(head_num)
                amount_text = head_num
                unit, unit_text = head_unit, head_unit_text
                quantity_text = f"{head_num} {head_unit_text or ''}".strip()
        if head_name:
            name = head_name
        else:
            tail_match = re.match(
                r"^(?P<num>\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)\s*(?P<rest>.*)$", tail
            )
            if tail_match:
                tail_rest = (tail_match.group("rest") or "").strip()
                _, _, tail_name = _split_leading_unit(tail_rest)
                name = tail_name or tail_rest or working
                amount = None  # two quantities: no single scalar
                amount_text = None
                unit = unit_text = None
                quantity_text = working
            else:
                name = tail or working
        if amount is None:
            quantity_text = working
    elif single and not is_range:
        num = single.group("num")
        rest = (single.group("rest") or "").strip()
        head_unit, head_unit_text, head_name = _split_leading_unit(rest)
        if head_unit is not None and head_name:
            amount = _rational(num)
            amount_text = num
            unit = head_unit
            unit_text = head_unit_text
            name = head_name
            quantity_text = f"{num} {head_unit_text}".strip()
        elif head_unit is not None and not head_name:
            amount = _rational(num)
            amount_text = num
            unit = head_unit
            unit_text = head_unit_text
            name = ""
            quantity_text = f"{num} {head_unit_text}".strip()
        else:
            # Bare count ("1 Eggs", "2 bananas") or unrecognized unit:
            # amount is valid; unit stays None (count), never guessed.
            amount = _rational(num)
            amount_text = num
            unit = "count" if rest else None
            unit_text = None
            name = rest
            quantity_text = num if rest else num
    elif is_range and range_match is not None:
        a, b, rest = (
            range_match.group("a"),
            range_match.group("b"),
            (range_match.group("rest") or "").strip(),
        )
        quantity_text = f"{a}-{b} {rest}".strip()
        amount = None
        head_unit, head_unit_text, head_name = _split_leading_unit(rest)
        if head_unit is not None:
            unit = head_unit
            unit_text = head_unit_text
            name = head_name
        else:
            name = rest
    else:
        quantity_text = None
    if not name.strip() and not qualitative:
        name = working
    name = name.strip().rstrip(" ,;")
    name, prep_note = split_prep_suffix(re.sub(r"\([^()]*\)", " ", name).strip() or name)
    # Prep-suffix cuts ("sausage, sliced" -> "sausage,") can leave a stranded
    # comma; cosmetic cleanup only — no information is removed here.
    name = name.strip().rstrip(" ,;")
    if prep_note:
        notes.append(prep_note)
    canonical_name = canonical(name)
    alternatives = _has_alternative(name, notes, qualitative)
    return {
        "original": original,
        "repaired": repaired,
        "canonical": canonical_name,
        "name": canonical_name,
        "amount": amount,
        "amount_text": amount_text,
        "quantity_text": quantity_text,
        "unit": unit,
        "unit_text": unit_text,
        "notes": "; ".join(notes) if notes else None,
        "alternatives": alternatives,
        "optional": optional,
        "qualitative": qualitative,
        "is_range": is_range,
        "compound": compound,
        "equivalent": equivalent,
    }


def split_sections(text: str) -> dict[str, Any]:
    """Split a foodie ``texts`` value into title/description/blocks."""
    normalized = unicodedata.normalize("NFKC", split_glued_markers(text))
    lines = [line.strip() for line in normalized.splitlines()]
    non_empty = [(i, line) for i, line in enumerate(lines) if line]
    if not non_empty:
        raise ValueError("missing_identity")
    title = non_empty[0][1]
    idx_ingredients: int | None = None
    idx_intro: int | None = None
    idx_directions: int | None = None
    for i, line in non_empty:
        low = line.casefold()
        if idx_ingredients is None and low == "ingredients":
            idx_ingredients = i
            continue
        if idx_ingredients is not None and idx_intro is None and "introduction" in low:
            idx_intro = i
            continue
        if (
            idx_ingredients is not None
            and idx_directions is None
            and (low in DIRECTION_MARKERS or low.startswith("direction"))
        ):
            idx_directions = i
    if idx_ingredients is None:
        raise ValueError("incomplete_ingredients_or_steps")
    by_index = dict(non_empty)
    ordered = [i for i, _ in non_empty]
    first_content = ordered[1] if len(ordered) > 1 else None

    def block(start: int | None, end: int | None) -> list[str]:
        if start is None:
            return []
        return [by_index[i] for i in ordered if i > start and (end is None or i < end)]

    desc_lines = (
        [
            by_index[i]
            for i in ordered
            if first_content is not None and i >= first_content and i < idx_ingredients
        ]
        if first_content is not None
        else []
    )
    end_ingredients = idx_intro if idx_intro is not None else idx_directions
    ingredient_lines = block(idx_ingredients, end_ingredients)
    intro_lines = block(idx_intro, idx_directions) if idx_intro is not None else []
    raw_steps = [
        re.sub(r"(?i)</?[a-z]+>?", "", line).strip()
        for line in block(idx_directions if idx_directions is not None else idx_intro, None)
    ]
    instruction_lines, notes_text, attribution = split_steps_notes(
        [line for line in raw_steps if line]
    )
    description_parts = [
        p for p in [" ".join(desc_lines).strip(), " ".join(intro_lines).strip()] if p
    ]
    return {
        "title": title,
        "description": " ".join(description_parts) if description_parts else None,
        "ingredient_lines": [line for line in ingredient_lines if line],
        "instruction_lines": instruction_lines,
        "notes_text": notes_text,
        "attribution": attribution,
        "has_intro_marker": idx_intro is not None,
        "has_directions_marker": idx_directions is not None,
    }


ATTRIBUTION_RE = re.compile(r"^([A-Z][\w'&.\-]*\s+){1,3}[A-Z][\w'&.\-]*$")
# Blog-roll tail (related-posts boilerplate after the recipe): date lines and
# quoted category tags are never cooking steps. The text is preserved in
# notes; routing lets the LLM verify the exact boundary.
ROLL_LINE_RE = re.compile(
    r"(?i)^\s*(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},\s+\d{4}\s*.*$"
    r"|^\s*In\s+\"[^\"]+\"\s*$"
)


def split_steps_notes(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Separate ordered steps from trailing notes/attribution.

    - A notes-header line ("Important Notes", "Editor's Note:", "Tips")
      ends the steps; the header and everything after it is preserved in
      ``notes_text``, never emitted as cooking steps.
    - Trailing bare proper-noun phrases ("Dotdash Meredith Food Studios",
      "Chef John") move to ``attribution``. Guards (all words capitalized,
      no digits, no terminal period, first word not a cooking verb, at
      least 3 steps remain) keep short real steps ("Mix well", "Serve.")
      where they belong.
    """
    boundary: int | None = None
    for pos, line in enumerate(lines):
        if NOTES_HEADER_RE.match(line) or ROLL_LINE_RE.match(line):
            boundary = pos
            break
    if boundary is None:
        steps = list(lines)
        notes = []
    else:
        steps = lines[:boundary]
        notes = lines[boundary:]
    # Publisher credits / bare proper-noun phrases ("Dotdash Meredith Food
    # Studios") are attribution at ANY position, not cooking steps. Guards
    # (2-4 capitalized words, no digits, no terminal punctuation, first word
    # not a cooking verb) keep real steps ("Mix well", "Serve.") in place.
    attribution: list[str] = []
    kept: list[str] = []
    for line in steps:
        first_word = line.split()[0].lower() if line.split() else ""
        if (
            ATTRIBUTION_RE.match(line)
            and re.search(r"\d", line) is None
            and not line.endswith((".", "!", "?", ":", ";"))
            and first_word not in COOKING_VERBS
        ):
            attribution.append(line)
        else:
            kept.append(line)
    if not kept:
        # Degenerate block: keep the lines as steps rather than orphaning
        # the record; routing signals still apply.
        kept, attribution = steps, []
    return kept, notes, attribution


def extract_servings_temps(full_text: str) -> dict[str, Any]:
    servings: float | None = None
    servings_text: str | None = None
    servings_range = False
    batch_yield: dict[str, Any] | None = None
    dozen_match = DOZEN_YIELD_RE.search(full_text)
    if dozen_match:
        # Exact dozen arithmetic: N dozen = 12*N count. This is batch yield,
        # never servings ("makes 2 dozen cookies" -> 24 cookies, not 2).
        dozens = float(dozen_match.group(2))
        batch_yield = {
            "count": int(dozens * 12) if dozens.is_integer() else dozens * 12,
            "unit": "count",
            "unit_text": "dozen",
            "text": dozen_match.group(0).strip(),
        }
    else:
        match = SERVINGS_RE.search(full_text)
        if match:
            servings_text = match.group(0).strip()
            span = match.group(2)
            if re.search(r"(-|–|—|to|or)", span):
                servings_range = True
            else:
                try:
                    servings = float(int(span.strip()))
                except ValueError:
                    servings = None
    temps: list[dict[str, Any]] = []
    for match in TEMP_RE.finditer(full_text):
        raw = match.group(0).strip()
        if match.group(1) and match.group(2):
            temps.append({"value": int(match.group(1)), "unit": match.group(2).upper(), "raw": raw})
        elif match.group(3):
            temps.append(
                {
                    "value": int(match.group(3)),
                    "unit": (match.group(4) or "unknown").upper(),
                    "raw": raw,
                }
            )
        elif match.group(5):
            temps.append({"value": int(match.group(5)), "unit": "GAS_MARK", "raw": raw})
    durations: list[dict[str, Any]] = []
    for match in LABELED_DURATION_RE.finditer(full_text):
        durations.append(
            {
                "label": match.group(1).lower(),
                "text": match.group(0).strip(),
                "range": bool(re.search(r"(-|–|—|to|or)", match.group(2))),
            }
        )
    # Source-presence signal: any duration/temperature-like mention, even
    # unlabeled ("cook ... for 5 minutes"). Structured extraction may still
    # be partial; availability records that the source HAS the evidence.
    durations_mentioned = bool(DURATION_MENTION_RE.search(full_text))
    return {
        "servings": servings,
        "servings_text": servings_text,
        "servings_range": servings_range,
        "batch_yield": batch_yield,
        "temperatures": temps,
        "durations_reported": durations,
        "durations_mentioned": durations_mentioned,
    }


def suspected_multi_recipe(texts: str) -> bool:
    """True when the raw text looks like concatenated recipes.

    Two or more ``Ingredients`` section markers (or repeated
    Ingredients+Directions pairs) mean a second recipe starts mid-record.
    The deterministic parser must not silently absorb it; routing sends it
    to LLM splitting, which preserves text boundaries and child identities.
    """
    markers = re.findall(r"(?im)^\s*ingredients\s*$", texts)
    if len(markers) >= 2:
        return True
    directions = re.findall(r"(?im)^\s*(directions|method|instructions)\s*$", texts)
    return len(directions) >= 2


def normalize_foodie_text(
    texts: str,
    row_number: int,
    *,
    file_sha256: str = FOODIE_SHA256,
    revision: str = FOODIE_REVISION,
) -> dict[str, Any]:
    """Normalize one foodie ``texts`` value; raises ValueError when unusable."""
    if not texts or not texts.strip():
        raise ValueError("missing_identity")
    sections = split_sections(texts)
    title = sections["title"]
    if not title or not title.strip():
        raise ValueError("missing_identity")
    ingredient_lines = sections["ingredient_lines"]
    instruction_lines = sections["instruction_lines"]
    if not ingredient_lines or not instruction_lines:
        raise ValueError("incomplete_ingredients_or_steps")
    quality_issues: list[dict[str, Any]] = []
    flags: list[str] = []
    if not sections["has_directions_marker"]:
        quality_issues.append(
            issue(
                "directions_marker_unknown",
                "info",
                "instructions",
                "No explicit directions marker found.",
            )
        )
    parsed = [parse_ingredient_line(line) for line in ingredient_lines]
    # Ingredient section headings: a bare line is a heading only with a
    # trailing ":" or an explicit heading cue ("For the sauce:"). Bare
    # unquantified lines ("ice", "Nonstick cooking spray") are ingredient
    # occurrences with unknown amounts — never synthetic groups.
    groups: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    current_group = "main"
    for pos, (line, item) in enumerate(zip(ingredient_lines, parsed, strict=True)):
        stripped = line.strip()
        looks_heading = stripped.endswith(":") or (
            item["amount"] is None
            and not item["qualitative"]
            and len(stripped) < 60
            and re.search(r"\d", stripped) is None
            and bool(HEADING_CUE_RE.search(stripped))
        )
        if looks_heading:
            current_group = stripped.rstrip(":")
            groups.append({"heading": current_group, "position": pos})
            continue
        occurrences.append({**item, "group": current_group, "position": pos})
    if not occurrences:
        raise ValueError("incomplete_ingredients_or_steps")
    for item in occurrences:
        if item["is_range"]:
            quality_issues.append(
                issue(
                    "quantity_range",
                    "info",
                    "ingredients.amount",
                    f"Range retained without midpoint: {item['original'][:80]}",
                )
            )
        if item["compound"]:
            quality_issues.append(
                issue(
                    "compound_quantity",
                    "info",
                    "ingredients.amount",
                    f"Compound quantity retained without summing: {item['original'][:80]}",
                )
            )
        if item["equivalent"]:
            quality_issues.append(
                issue(
                    "equivalent_quantity",
                    "info",
                    "ingredients.amount",
                    f"Equivalent measures retained without summing: {item['original'][:80]}",
                )
            )
        if item["amount"] is None and not item["qualitative"]:
            flags.append("quantity_unknown")
    if "quantity_unknown" in flags:
        quality_issues.append(
            issue(
                "quantity_unknown",
                "warning",
                "ingredients.amount",
                "At least one ingredient amount is missing or unparseable.",
            )
        )
    name_measure = [o for o in occurrences if re.search(r"\d", o["canonical"] or "")]
    if name_measure:
        quality_issues.append(
            issue(
                "name_contains_measure",
                "warning",
                "ingredients.canonical",
                "Measurement tokens remain embedded in "
                f"{len(name_measure)} ingredient name(s); quantities not validated.",
            )
        )
    units_known = (
        all(
            (o["unit"] is not None) or o["qualitative"]
            for o in occurrences
            if o["amount"] is not None
        )
        and all(o["amount"] is not None or o["qualitative"] for o in occurrences)
        and not name_measure
    )
    if not units_known:
        quality_issues.append(
            issue(
                "units_unknown",
                "info",
                "ingredients.unit",
                "At least one amount lacks a recognized unit; no conversions applied.",
            )
        )
    meta = extract_servings_temps(texts)
    if meta["durations_mentioned"] and not meta["durations_reported"]:
        quality_issues.append(
            issue(
                "durations_unstructured",
                "info",
                "durations",
                "Source mentions timings without extractable labels; "
                "available_fields.durations stays true (source presence).",
            )
        )
    if meta["servings"] is None and meta["batch_yield"] is None:
        flags.append("servings_unknown")
    if meta["batch_yield"] is not None:
        quality_issues.append(
            issue(
                "batch_yield_not_servings",
                "info",
                "servings",
                f"Source states batch yield ({meta['batch_yield']['text']}); "
                "servings left unknown rather than misread.",
            )
        )
    if meta["servings_range"]:
        quality_issues.append(
            issue(
                "servings_range",
                "info",
                "servings",
                f"Serving range retained without midpoint: {meta['servings_text']}",
            )
        )
    ingredients = [
        {
            "original": o["original"],
            "canonical": o["canonical"],
            "name": o["canonical"],
            "epicure_id": None,
            "quantity_text": o["quantity_text"],
            "amount": o["amount"],
            "amount_text": o["amount_text"],
            "unit": o["unit"] if o["unit"] != "count" else None,
            "unit_text": o["unit_text"],
            "notes": o["notes"],
            "alternatives": o["alternatives"],
            "optional": o["optional"],
            "qualitative": o["qualitative"],
            "group": o["group"],
            "position": o["position"],
        }
        for o in occurrences
    ]
    input_line_count = len([line for line in ingredient_lines if line.strip()])
    accounted = len(occurrences) + len(groups)
    line_coverage = {
        "input_lines": input_line_count,
        "occurrences": len(occurrences),
        "groups": len(groups),
        "dropped": max(input_line_count - accounted, 0),
    }
    if line_coverage["dropped"]:
        quality_issues.append(
            issue(
                "omitted_source_lines",
                "warning",
                "ingredients",
                f"{line_coverage['dropped']} ingredient-block line(s) produced "
                "neither an occurrence nor a group heading.",
            )
        )
    fingerprint = hashlib.sha256(
        json.dumps(
            [canonical(title), [i["canonical"] for i in ingredients], instruction_lines],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    source_id = foodie_source_id(row_number)
    # available_fields = SOURCE presence (evidence in the source text),
    # not extraction success. Partially structured evidence still counts.
    available_fields = {
        "description": sections["description"] is not None,
        "images": False,
        "keywords": False,
        "nutrition": False,
        "ratings": False,
        "servings": meta["servings"] is not None or meta["batch_yield"] is not None,
        "batch_yield": meta["batch_yield"] is not None,
        "durations": bool(meta["durations_mentioned"]),
        "durations_structured": bool(meta["durations_reported"]),
        "temperatures": bool(meta["temperatures"]),
        "ingredient_groups": len(groups) > 0,
        "notes": bool(sections["notes_text"] or sections["attribution"]),
    }
    structural_issue = any(
        i["severity"] == "warning" and i["code"] in {"quantity_unknown", "invalid_quantities"}
        for i in quality_issues
    )
    capabilities = capabilities_for(
        has_identity=True,
        has_ingredients=bool(ingredients),
        has_instructions=bool(instruction_lines),
        quantities_validated=bool(units_known),
        servings_known=meta["servings"] is not None,
        durations_known=bool(meta["durations_reported"]),
        structural_issue=structural_issue,
    )
    return {
        "dataset_id": FOODIE_DATASET,
        "source_id": source_id,
        "row_number": row_number,
        "title": title,
        "description": sections["description"],
        "ingredients": ingredients,
        "ingredient_groups": groups,
        "instructions": instruction_lines,
        "servings": meta["servings"],
        "servings_text": meta["servings_text"],
        "batch_yield": meta["batch_yield"],
        "durations_reported": meta["durations_reported"],
        "durations_mentioned": meta["durations_mentioned"],
        "temperatures": meta["temperatures"],
        "notes_text": sections["notes_text"],
        "attribution": sections["attribution"],
        "line_coverage": line_coverage,
        "durations_minutes": {"TotalTime": None, "PrepTime": None, "CookTime": None},
        "media": {"images": [], "images_raw": None},
        "meta": {
            "author_id": None,
            "author_name": None,
            "category": None,
            "keywords": [],
            "date_published": None,
            "barcode": None,
            "recipe_yield_text": meta["servings_text"],
        },
        "ratings": {"aggregated": None, "review_count": None},
        "nutrition": {
            k: None
            for k in (
                "calories",
                "fat",
                "saturated_fat",
                "cholesterol",
                "sodium",
                "carbohydrate",
                "fiber",
                "sugar",
                "protein",
            )
        },
        "nutrition_observations": [],
        "flags": sorted(set(flags + (["units_unknown"] if not units_known else []))),
        "quality_issues": quality_issues,
        "available_fields": available_fields,
        "capabilities": capabilities,
        "search_document_version": SEARCH_DOCUMENT_VERSION,
        "searchable": capabilities["searchable"],
        "scalable": capabilities["scalable"],
        "final_recipe_eligible": capabilities["complete_eligible"],
        "content_hash": fingerprint,
        "source_url": None,
        "language": "en",
        "provenance": {
            "dataset_id": FOODIE_DATASET,
            "revision": revision,
            "file_path": FOODIE_FILE,
            "file_sha256": file_sha256,
            "row_number": row_number,
            "source_id": source_id,
            "adapter": "foodie",
            "adapter_version": FOODIE_ADAPTER_VERSION,
            "language": "en",
            "source_url": None,
            "license_declared": FOODIE_LICENSE,
            "provenance_status": "pending",
        },
        "raw": {"texts": texts},
    }
