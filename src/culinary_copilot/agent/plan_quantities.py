"""Plan-quantity matcher (ingredient-aware, H2).

Moved verbatim from agent/validate.py with no behaviour change;
validate.py re-exports the public names for backwards compatibility.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

PROSE_MEASURE_UNITS = frozenset(
    {"mg", "g", "kg", "oz", "lb", "ml", "cl", "l", "tsp", "tbsp", "fl_oz", "cup"}
    | {"pint", "quart", "gallon"}
)

_VULGAR_FRACTIONS = {
    "½": "1/2",
    "⅓": "1/3",
    "⅔": "2/3",
    "¼": "1/4",
    "¾": "3/4",
    "⅕": "1/5",
    "⅙": "1/6",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
}

#: A number (mixed, fraction, decimal or whole) followed by up to two
#: words that may name a unit ("5 1/2 pounds", "2-cup", "4 fluid ounces").
_PROSE_QUANTITY_RE = re.compile(
    r"(?<![\w/.])(\d+\s+\d+/\d+|\d+/\d+|\d+\.\d+|\d+)(?:\s*-\s*|\s*)"
    r"([A-Za-z]+)(?:\s+([A-Za-z]+))?"
)


#: Equipment nouns a hyphenated size can modify ("a 12-cup muffin tin",
#: "a 2-quart saucepan"): such a size is not an ingredient amount.
EQUIPMENT_WORDS = frozenset(
    {
        "tin",
        "pan",
        "saucepan",
        "skillet",
        "pot",
        "dish",
        "casserole",
        "sheet",
        "tray",
        "mold",
        "mould",
        "ramekin",
        "bowl",
        "jar",
        "container",
        "measuring",
        "cooker",
        "oven",
    }
)


def _equipment_size(text: str, number_end: int, unit_end: int) -> bool:
    """Whether a hyphenated amount sizes the equipment named after it.

    H8 attempt 4 (2026-10-08): "Grease a 12-cup muffin tin" was read as
    12 cups of an ingredient and a sound plan was rejected. Only the
    hyphenated form counts ("12-cup", not "12 cup"), and only when an
    equipment noun follows within two words.
    """
    if "-" not in text[number_end:unit_end]:
        return False
    following = re.findall(r"[a-z]+", text[unit_end : unit_end + 40].lower())[:2]
    return any(_singular_token(word) in EQUIPMENT_WORDS for word in following)


def prose_quantities(text: Any) -> list[tuple[str, str, str]]:
    """Mass/volume amounts in free text as (claim, exact value, unit)."""
    from culinary_copilot.recipes.llm_validate import canonical_unit
    from culinary_copilot.recipes.normalize import quantity

    normalized = str(text or "").replace("\u2044", "/")
    for char, fraction in _VULGAR_FRACTIONS.items():
        normalized = re.sub(rf"(\d)\s*{char}", rf"\1 {fraction}", normalized)
        normalized = normalized.replace(char, fraction)
    found: list[tuple[str, str, str]] = []
    for match in _PROSE_QUANTITY_RE.finditer(normalized):
        number, first, second = match.group(1), match.group(2), match.group(3)
        unit = canonical_unit(f"{first} {second}") if second else None
        claim = match.group(0)
        if unit is None:
            unit = canonical_unit(first)
            claim = normalized[match.start() : match.end(2)]
        if unit not in PROSE_MEASURE_UNITS:
            continue
        if _equipment_size(normalized, match.end(1), match.end(2)):
            continue
        value = quantity(" ".join(number.split()))
        if value is not None:
            found.append((claim.strip(), value, unit))
    return found


def source_quantities(doc: dict[str, Any]) -> set[tuple[str, str]]:
    """Every (exact value, unit) the source states, in fields or text."""
    from culinary_copilot.agent.validate import (
        _ingredient_entries,
        _ingredient_line_texts,
        doc_directions,
    )
    from culinary_copilot.recipes.llm_validate import canonical_unit
    from culinary_copilot.recipes.normalize import quantity

    stated: set[tuple[str, str]] = set()
    texts: list[Any] = list(_ingredient_line_texts(doc)) + doc_directions(doc)
    for item in _ingredient_entries(doc):
        unit = canonical_unit(str(item.get("unit") or ""))
        value = quantity(str(item.get("amount") or ""))
        if unit and value:
            stated.add((value, unit))
        texts.append(item.get("original"))
    for text in texts:
        stated.update((value, unit) for _, value, unit in prose_quantities(text))
    return stated


def _fold_match_text(text: str) -> str:
    """Lowercased accent-folded text for ingredient-name matching."""
    folded = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return folded.lower()


def _match_tokens(text: str) -> list[str]:
    """Word tokens for name matching (splits hyphens and glue)."""
    return re.findall(r"[a-z0-9]+", _fold_match_text(text))


#: Function words that can never be ingredient mentions (2026-10-07
#: H2 revision): a partial multi-word name match must include at least
#: one content token, so "of" alone never attributes "1 cup of rice"
#: to "cream of mushroom soup". Size words count as function words
#: (2026-10-08 review): "inch" in "1 cup cubed (1/4 inch) celery root"
#: attributed the amount to "apples, cut into 1/2-inch cubes".
FUNCTION_WORDS = frozenset(
    {
        "inch",
        "inches",
        "cm",
        "mm",
        "of",
        "the",
        "a",
        "an",
        "and",
        "or",
        "in",
        "with",
        "for",
        "to",
        "into",
        "on",
        "at",
        "as",
        "by",
        "from",
        "each",
        "per",
        "de",
        "la",
        "le",
        "du",
        "des",
        "di",
        "da",
    }
)


#: Minimum length for glued prefix matching (2026-10-07 H2 revision):
#: "buttersoftened" starts with "butter" (6) and "pepperschopped"
#: starts with "peppers" (7), but "oil" (3) never matches "boiled"
#: (which does not start with "oil" either) and "salt" (4) never
#: matches "salted" via prefix.
GLUED_PREFIX_MIN = 5


#: Container and packaging words are never ingredient mentions
#: (2026-10-07 H2 second revision): can, package, jar, bottle, box,
#: bag, carton, container, tin, pouch, stick and similar. Checked
#: against canonical_unit (most map to count/container units, none in
#: PROSE_MEASURE_UNITS) and the ingredient parser (which leaves "can"
#: in names like "can diced tomatoes" when unit is None).
CONTAINER_BASE = frozenset(
    {
        "can",
        "package",
        "pkg",
        "jar",
        "bottle",
        "box",
        "bag",
        "carton",
        "container",
        "tin",
        "pouch",
        "stick",
        "pack",
        "packet",
        "tub",
        "sachet",
    }
)


#: Preparation words that may follow a glued ingredient stem
#: (2026-10-07 H2 second revision): "buttersoftened" (butter+softened)
#: and "pepperschopped" (pepper+chopped) are allowed, but "buttermilk"
#: (butter+milk, milk not prep and not listed) is not credited to
#: butter. Remainder must be prep, another listed ingredient, or the
#: line must be an entry's original (source's own glue).
PREP_WORDS = frozenset(
    {
        "softened",
        "melted",
        "chilled",
        "divided",
        "chopped",
        "minced",
        "diced",
        "sliced",
        "grated",
        "ground",
        "fresh",
        "dried",
        "frozen",
        "cooked",
        "raw",
        "roasted",
        "toasted",
        "crushed",
        "crumbled",
        "beaten",
        "whipped",
        "sifted",
        "drained",
        "rinsed",
        "peeled",
        "cored",
        "seeded",
        "stemmed",
        "trimmed",
        "cut",
        "cubed",
        "halved",
        "quartered",
        "finely",
        "coarsely",
        "thinly",
        "roughly",
        "lightly",
        "soft",
        "powdered",
    }
)


#: Descriptor words that alone must not attribute an amount when the
#: line also names another ingredient's distinctive tokens (2026-10-07
#: H2 third revision): PREP_WORDS plus adjectives fresh/ground/dried
#: (already in PREP) and large/small/whole/white/warm/cold/hot. A
#: mention made only of these without the head noun (e.g. "chopped"
#: for "chopped pecans" in "chopped walnuts") is ignored; the head
#: ("walnuts") wins, so swaps are rejected.
DESCRIPTOR_WORDS = PREP_WORDS | frozenset(
    {
        "large",
        "small",
        "whole",
        "white",
        "warm",
        "cold",
        "hot",
    }
)


def _is_container_token(token: str) -> bool:
    """Whether a token is a container/packaging word (singularized)."""
    return _singular_token(token) in CONTAINER_BASE


def _token_prefix_match(line_token: str, variant_token: str) -> bool:
    """Bounded prefix for glued text without spaces.

    Either side may be longer ("buttersoftened" vs "butter"), but the
    shared prefix must be at least GLUED_PREFIX_MIN characters, so
    short words like "oil" or "salt" never match via prefix.
    Restricted further in _line_token_matches (prep/other-ingredient
    remainder, 2026-10-07 H2 second revision).
    """
    if len(line_token) < GLUED_PREFIX_MIN or len(variant_token) < GLUED_PREFIX_MIN:
        shorter_len = min(len(line_token), len(variant_token))
        if shorter_len < GLUED_PREFIX_MIN:
            return False
    if line_token.startswith(variant_token) or variant_token.startswith(line_token):
        if min(len(line_token), len(variant_token)) >= GLUED_PREFIX_MIN:
            return True
    return False


def _token_match(left: str, right: str) -> bool:
    """Plural-aware single-token equality (mirrors policy._token_hit).

    Covers trailing s/es and ies<->y ("potatoes"/"potato",
    "parts"/"part", "cherries"/"cherry"); "leaf"/"leaves" via ves<->f.
    Word-boundary matching lives in the caller: this compares tokens.
    """
    if left == right:
        return True
    if left == right + "s" or left == right + "es" or right == left + "s" or right == left + "es":
        return True
    if left.endswith("ies") and left[:-3] + "y" == right:
        return True
    if right.endswith("ies") and right[:-3] + "y" == left:
        return True
    if left.endswith("ves") and right in (left[:-3] + "f", left[:-3] + "fe"):
        return True
    if right.endswith("ves") and left in (right[:-3] + "f", right[:-3] + "fe"):
        return True
    return False


def _normalize_prose_text(text: Any) -> str:
    """Same normalization prose_quantities uses (vulgar fractions, ⁄)."""
    normalized = str(text or "").replace("⁄", "/")
    for char, fraction in _VULGAR_FRACTIONS.items():
        normalized = re.sub(rf"(\d)\s*{char}", rf"\1 {fraction}", normalized)
        normalized = normalized.replace(char, fraction)
    return normalized


def prose_quantities_with_spans(text: Any) -> list[tuple[str, str, str, int, int]]:
    """Mass/volume amounts as (claim, exact value, unit, start, end).

    Spans are char offsets in the normalized text (see
    _normalize_prose_text) so amount positions share coordinates with
    the token spans used for ingredient attribution. Reuses
    recipes/normalize.py::quantity and
    recipes/llm_validate.py::canonical_unit; no third parser.
    """
    from culinary_copilot.recipes.llm_validate import canonical_unit
    from culinary_copilot.recipes.normalize import quantity

    normalized = _normalize_prose_text(text)
    found: list[tuple[str, str, str, int, int]] = []
    for match in _PROSE_QUANTITY_RE.finditer(normalized):
        number, first, second = match.group(1), match.group(2), match.group(3)
        unit = canonical_unit(f"{first} {second}") if second else None
        claim_start, claim_end = match.start(), match.end()
        if unit is None:
            unit = canonical_unit(first)
            claim_start, claim_end = match.start(), match.end(2)
        if unit not in PROSE_MEASURE_UNITS:
            continue
        if _equipment_size(normalized, match.end(1), match.end(2)):
            continue
        value = quantity(" ".join(number.split()))
        if value is not None:
            claim = normalized[claim_start:claim_end].strip()
            found.append((claim, value, unit, claim_start, claim_end))
    return found


def _singular_token(token: str) -> str:
    """Singular base for grouping (mirrors _token_match: s/es, ies->y, ves->f)."""
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("ves"):
        return token[:-3] + "f"
    if len(token) > 3 and token.endswith("es"):
        return token[:-2]
    if len(token) > 2 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _ingredient_group_key(entry: dict[str, Any]) -> str:
    """Grouping key: singularized canonical without amount/unit/or tokens.

    Strips numbers, closed-vocabulary units ("tsp" in "tsp.vanilla",
    "cup" excluded from matching but kept for display elsewhere) and
    "or"/"and", so "or 3/4 tsp salt" and "salt" share a group (the
    2026-10-07 H2 self-check alternative), as do "carrots" and "carrot".
    Modifiers stay ("vegetable oil" vs "olive oil" remain distinct), so
    swapped-amount protection is kept. canonical_unit on plain tokens
    cannot raise (dict lookup on str), so no try/except is needed.
    """
    from culinary_copilot.recipes.llm_validate import canonical_unit

    raw = str(entry.get("canonical") or entry.get("name") or "").strip().lower()
    tokens = [w for w in re.split(r"[^a-z0-9]+", raw) if w]
    kept: list[str] = []
    for token in tokens:
        if token.isdigit():
            continue
        if token in {"or", "and"}:
            continue
        if canonical_unit(token) is not None:
            continue
        kept.append(_singular_token(token))
    return " ".join(kept) or " ".join(_singular_token(w) for w in tokens)


def _ingredient_name_token_variants(entry: dict[str, Any]) -> list[list[str]]:
    """Token lists from canonical and name (each a matchable variant)."""
    variants: list[list[str]] = []
    for raw in (entry.get("canonical"), entry.get("name")):
        tokens = _match_tokens(str(raw or ""))
        if tokens and tokens not in variants:
            variants.append(tokens)
    return variants


def _group_source_amounts(
    entries: list[dict[str, Any]],
) -> tuple[set[tuple[str, str]], list[str]]:
    """((value, unit) set, readable source lines) for one ingredient group.

    Structured amounts come from amount/amount_text with canonical_unit;
    prose amounts come from each entry's original line (covers
    parentheticals such as "6g (1 tsp)Salt"). Both reuse the shared
    parsers.
    """
    from culinary_copilot.recipes.llm_validate import canonical_unit
    from culinary_copilot.recipes.normalize import quantity

    stated: set[tuple[str, str]] = set()
    displays: list[str] = []
    for entry in entries:
        for raw_amount in (entry.get("amount"), entry.get("amount_text")):
            if raw_amount is None:
                continue
            value = quantity(str(raw_amount))
            unit = canonical_unit(str(entry.get("unit") or ""))
            if value and unit:
                stated.add((value, unit))
        original = str(entry.get("original") or "").strip()
        for text in (
            original,
            str(entry.get("canonical") or ""),
            str(entry.get("name") or ""),
            str(entry.get("quantity_text") or ""),
        ):
            for _, value, unit in prose_quantities(text):
                stated.add((value, unit))
        amount_text = str(entry.get("amount_text") or entry.get("amount") or "").strip()
        unit_text = str(entry.get("unit") or "").strip()
        if amount_text and unit_text and original:
            displays.append(f"{amount_text} {unit_text} (from {original[:80]!r})")
        elif amount_text and unit_text:
            displays.append(f"{amount_text} {unit_text}")
        elif original:
            displays.append(f"from {original[:80]!r}")
    return stated, displays


def _prefix_remainder_allowed(remainder: str, all_tokens: set[str]) -> bool:
    """Whether a glued suffix allows prefix credit (2026-10-07 H2R).

    Remainder must be a known preparation word ("softened", "chopped",
    "finely") or another listed ingredient's name ("milk" in
    "buttermilk" when milk is listed). Otherwise "1 cup buttermilk"
    must not credit "butter" (repro: butter 1 cup, flour 2 cups).
    """
    rem = remainder.lower()
    if rem in PREP_WORDS:
        return True
    return any(_token_match(rem, tok) for tok in all_tokens)


def _line_token_matches(
    line_token: str, variant_token: str, all_tokens: set[str] | None = None
) -> bool:
    """One token pair matches: plural-aware equality or restricted prefix.

    Prefix covers glued text ("buttersoftened"→"butter") with min
    GLUED_PREFIX_MIN, restricted to prep/other-ingredient remainders so
    "buttermilk" never credits "butter" unless "milk" is prep or listed
    (2026-10-07 H2 second revision). Exact equality always allowed
    (containers/function filtered by caller).
    """
    if _token_match(line_token, variant_token):
        return True
    if not _token_prefix_match(line_token, variant_token):
        return False
    if all_tokens is None:
        return True
    if line_token.startswith(variant_token):
        remainder = line_token[len(variant_token) :]
    else:
        remainder = variant_token[len(line_token) :]
    if not remainder:
        return True
    return _prefix_remainder_allowed(remainder, all_tokens)


def _line_mentions(
    normalized: str,
    groups: list[dict[str, Any]],
    amount_spans: list[tuple[int, int]] | None = None,
) -> dict[int, list[tuple[int, int, int, int, bool]]]:
    """Ingredient mentions per group as {group: [(start, end, len, varlen, exact)]}.

    A mention is a contiguous token run where line tokens match a
    contiguous run of one name variant (plural-aware per token, plus
    bounded glued prefix). Matching is token-contiguous, never
    substring: "oil" never matches "boiled" (2026-10-07 H2). Glue
    without spaces ("170gplain", "Buttersoftened") still matches its
    clean token because only that token needs boundaries. Tokens inside
    any amount span are unavailable, so a unit word never counts
    (2026-10-07 H2: "tsp.vanilla" must not steal "2 tsp vanilla").
    A partial multi-word match must include at least one content token
    (not in FUNCTION_WORDS), so "of" alone never attributes "1 cup of
    rice" to "cream of mushroom soup" (2026-10-07 H2 revision). A
    descriptor-only run without the head noun (PREP/descriptors like
    "chopped", "ground", "fresh") is ignored when another ingredient's
    distinctive tokens appear, so "chopped walnuts" attributes to
    walnuts, not pecans (2026-10-07 H2 third revision).
    """
    folded = _fold_match_text(normalized)
    line_tokens = re.findall(r"[a-z0-9]+", folded)
    spans: list[tuple[int, int]] = [
        (match.start(), match.end()) for match in re.finditer(r"[a-z0-9]+", folded)
    ]
    blocked = [False] * len(line_tokens)
    for start, end in amount_spans or []:
        for idx, (tok_start, tok_end) in enumerate(spans):
            if not (tok_end <= start or tok_start >= end):
                blocked[idx] = True
    tmp: dict[int, list[tuple[int, int, int, int, bool, bool, bool]]] = {
        i: [] for i in range(len(groups))
    }
    all_tokens: set[str] = set()
    for group in groups:
        for variant in group["variants"]:
            all_tokens.update(variant)
    for gi, group in enumerate(groups):
        for variant in group["variants"]:
            if not variant:
                continue
            for line_pos in range(len(line_tokens)):
                if blocked[line_pos]:
                    continue
                if line_tokens[line_pos] in FUNCTION_WORDS:
                    continue
                if _is_container_token(line_tokens[line_pos]):
                    continue
                if line_tokens[line_pos].isdigit():
                    continue
                for var_pos in range(len(variant)):
                    if variant[var_pos] in FUNCTION_WORDS:
                        continue
                    if _is_container_token(variant[var_pos]):
                        continue
                    if variant[var_pos].isdigit():
                        continue
                    length = 0
                    exact = True
                    while (
                        line_pos + length < len(line_tokens)
                        and not blocked[line_pos + length]
                        and var_pos + length < len(variant)
                        and _line_token_matches(
                            line_tokens[line_pos + length],
                            variant[var_pos + length],
                            all_tokens,
                        )
                    ):
                        if not _token_match(
                            line_tokens[line_pos + length], variant[var_pos + length]
                        ):
                            exact = False
                        length += 1
                    if not length:
                        continue
                    # Head noun: last distinctive token (not
                    # descriptor/function/container/numeric), so "cubed"
                    # (prep) is not the head of "processed cheese, cubed"
                    # — "cheese" is (2026-10-07 H2 third revision).
                    head_idx: int | None = None
                    for idx in range(len(variant) - 1, -1, -1):
                        tok = variant[idx]
                        if tok in DESCRIPTOR_WORDS:
                            continue
                        if tok in FUNCTION_WORDS:
                            continue
                        if _is_container_token(tok):
                            continue
                        if tok.isdigit():
                            continue
                        head_idx = idx
                        break
                    if head_idx is None:
                        head_idx = len(variant) - 1
                    has_head = var_pos <= head_idx < var_pos + length
                    line_run = line_tokens[line_pos : line_pos + length]
                    is_descr_only = (not has_head) and all(
                        tok in DESCRIPTOR_WORDS for tok in line_run
                    )
                    start = spans[line_pos][0]
                    end = spans[line_pos + length - 1][1]
                    tmp[gi].append(
                        (start, end, length, len(variant), exact, has_head, is_descr_only)
                    )
    # Any distinctive mention, not only one holding the head: the head
    # is the last distinctive token of the whole name, so for "carrots,
    # cut into large chunks" it is "chunks" and no line mention holds
    # it; "3 pound cut carrots" then went to the corned beef ("..., cut
    # in half") through "cut" alone (2026-10-08 review).
    distinctive_any = any(
        not is_descr_only for items in tmp.values() for (*_, is_descr_only) in items
    )
    out: dict[int, list[tuple[int, int, int, int, bool]]] = {i: [] for i in range(len(groups))}
    for gi, items in tmp.items():
        for start, end, length, varlen, exact, _has_head, is_descr_only in items:
            if distinctive_any and is_descr_only:
                continue
            out[gi].append((start, end, length, varlen, exact))
    return out


def _norm_line_key(text: str) -> str:
    """Normalized line for original-equality (fold, lower, collapse space)."""
    return " ".join(_fold_match_text(text).split())


def _nearest_with_ties(
    amount_start: int,
    amount_end: int,
    mentions: dict[int, list[tuple[int, int, int, int, bool]]],
) -> tuple[int | None, list[int]]:
    """Best group and tied candidates for one amount.

    Nearest wins; contained mentions never count (2026-10-07 H2). Ties
    on (distance, -length, not-exact) pool equally specific matches so
    amounts disambiguate same-name duplicates (butter chilled/melted,
    water plain/divided via model-style stripped modifiers); longer
    still wins (bouillon over stock), preserving swaps (2026-10-07 H2R).
    Exact beats glued prefix ("butter" beats "buttermilk").
    """
    best: int | None = None
    best_key: tuple[int, int, int] | None = None
    tied: list[int] = []
    for gi, items in mentions.items():
        if not items:
            continue
        for start, end, length, _variant_len, exact in items:
            if amount_start <= start and end <= amount_end:
                continue
            if amount_end <= start:
                distance = start - amount_end
            elif end <= amount_start:
                distance = amount_start - end
            else:
                distance = 0
            key = (distance, -length, 0 if exact else 1)
            if best_key is None or key < best_key:
                best_key = key
                best = gi
                tied = [gi]
            elif key == best_key and gi not in tied:
                tied.append(gi)
    if best is None:
        return None, []
    if len(tied) > 1:
        return None, sorted(tied)
    return best, []


def _forward_list_attachment(
    text: str,
    amount_start: int,
    amount_end: int,
    mentions: dict[int, list[tuple[int, int, int, int, bool]]],
) -> int | None:
    """Group an amount leads in an "AMOUNT UNIT [prep] INGREDIENT" list.

    H8 attempt 3 (2026-10-08): in "..., 1 1/2 cup white sugar, 3 tbsp
    softened butter, ..." the nearest mention to "3 tbsp" was the white
    sugar before it (the prep word "softened" pushed the butter further
    away), and a correct plan was rejected. When a comma or semicolon
    separates the amount from the mention before it, and only prep,
    descriptor or function words lie between the amount and the next
    mention, the amount belongs to the next mention. Name-then-amount
    lists ("chicken, 5 1/2 lb, potatoes") have a separator after the
    amount, so they never take this path. Returns None when the rule
    does not apply or the next mention is tied between groups.
    """
    after: list[tuple[int, int]] = []
    before_end: int | None = None
    for gi, items in mentions.items():
        for start, end, _length, _variant_len, _exact in items:
            if start >= amount_end:
                after.append((start, gi))
            elif end <= amount_start and (before_end is None or end > before_end):
                before_end = end
    if not after or before_end is None:
        return None
    if not re.search(r"[,;]", text[before_end:amount_start]):
        return None
    next_start = min(start for start, _ in after)
    next_groups = {gi for start, gi in after if start == next_start}
    if len(next_groups) != 1:
        return None
    gap = text[amount_end:next_start]
    if re.search(r"[^a-z0-9\s]", _fold_match_text(gap)):
        return None
    gap_words = re.findall(r"[a-z0-9]+", _fold_match_text(gap))
    if any(word not in DESCRIPTOR_WORDS and word not in FUNCTION_WORDS for word in gap_words):
        return None
    return next_groups.pop()


def _nearest_group(
    amount_start: int,
    amount_end: int,
    mentions: dict[int, list[tuple[int, int, int, int, bool]]],
) -> int | None:
    """Index of the ingredient group, or None when missing/ambiguous."""
    best, _ = _nearest_with_ties(amount_start, amount_end, mentions)
    return best


def _is_parenthetical(text: str, start: int, end: int) -> bool:
    """Whether an amount span sits inside parentheses (2026-10-07 H2R).

    Parenthetical second amounts ("3/4 cup (178 ml)") belong to the same
    ingredient as the preceding amount, so "3/4 cup (178 ml) of chicken
    stock" passes (both stock) while "... of chicken bouillon powder"
    is rejected (bouillon is 1 tbsp).
    """
    open_idx = text.rfind("(", 0, start)
    if open_idx < 0:
        return False
    if ")" in text[open_idx:start]:
        return False
    close_idx = text.find(")", end)
    if close_idx < 0:
        return False
    if "(" in text[end:close_idx]:
        return False
    return True


def _direction_supports_group(
    direction: str,
    value: str,
    unit: str,
    group_index: int,
    groups: list[dict[str, Any]],
) -> bool:
    """Whether a cited direction states (value, unit) for the same group.

    Applies the same attribution rule to the direction text (2026-10-07
    H2 revision): a pooled amount match is not enough, so "Add 1 1/2 lb
    chicken" citing "Add 1 1/2 pounds potatoes" still fails.
    """
    dir_amounts = prose_quantities_with_spans(direction)
    if not dir_amounts:
        return False
    dir_mentions = _line_mentions(
        _normalize_prose_text(direction),
        groups,
        [(s, e) for _, _, _, s, e in dir_amounts],
    )
    for _, d_value, d_unit, d_start, d_end in dir_amounts:
        if (d_value, d_unit) != (value, unit):
            continue
        best, _ = _nearest_with_ties(d_start, d_end, dir_mentions)
        if best == group_index:
            return True
    return False


def plan_prose_quantity_errors(plan: dict[str, Any], doc: dict[str, Any]) -> list[str]:
    """Mass/volume amounts in plan text tied to their ingredient (H2).

    2026-10-07 live plan: the mise en place said "1 1/2 lb cut-up
    chicken parts" for a source amount of 5 1/2 pounds (stored "11/2"),
    and only the structured quantities were checked. Each amount in
    mise_en_place, steps and plating must equal (exactly, any notation)
    an amount the source states for that same ingredient with the same
    unit: the ingredient's structured entries plus prose amounts in its
    original/canonical lines. A plan line equal to an entry's original
    line attributes to that entry; otherwise token-contiguous,
    plural-aware matching with glued prefix (min 5) and content-token
    gating applies. Lines with equal numbers of amounts and ingredients
    pair in order (covers "chicken, 5 1/2 lb, potatoes, 1 1/2 lb").
    An attributed step amount may also match a cited direction only
    when that direction states the amount for the same ingredient;
    unattributed amounts (no ingredient) may match any cited direction.
    Ambiguous amounts name their candidates. Nothing is accepted
    silently. Adaptations descriptions are unchecked; model_adaptation
    steps are checked like any other step.
    """
    from culinary_copilot.agent.validate import _ingredient_entries, doc_directions

    entries = _ingredient_entries(doc)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(_ingredient_group_key(entry), []).append(entry)
    groups: list[dict[str, Any]] = []
    original_to_groups: dict[str, list[int]] = {}
    for key, items in grouped.items():
        variants: list[list[str]] = []
        for item in items:
            for variant in _ingredient_name_token_variants(item):
                if variant not in variants:
                    variants.append(variant)
        stated, displays = _group_source_amounts(items)
        gi = len(groups)
        originals: set[str] = set()
        for item in items:
            norm_orig = _norm_line_key(str(item.get("original") or ""))
            if norm_orig:
                originals.add(norm_orig)
                original_to_groups.setdefault(norm_orig, []).append(gi)
        groups.append(
            {
                "key": key,
                "variants": variants,
                "stated": stated,
                "displays": displays,
                "originals": originals,
            }
        )
    directions = doc_directions(doc)
    direction_amounts: list[set[tuple[str, str]]] = []
    for direction in directions:
        direction_amounts.append({(value, unit) for _, value, unit in prose_quantities(direction)})
    raw_steps = plan.get("steps") or []
    cited_list = plan.get("step_sources")
    texts: list[tuple[str, str, int | None]] = [
        ("mise_en_place", item, None)
        for item in plan.get("mise_en_place") or []
        if isinstance(item, str)
    ]
    for index, item in enumerate(raw_steps):
        if isinstance(item, str):
            texts.append(("steps", item, index))
    if isinstance(plan.get("plating"), str):
        texts.append(("plating", plan["plating"], None))
    errors: list[str] = []
    for field, text, step_index in texts:
        normalized = _normalize_prose_text(text)
        amounts = prose_quantities_with_spans(text)
        if not amounts:
            continue
        line_key = _norm_line_key(text)
        direct_groups = original_to_groups.get(line_key, [])
        direct_group: int | None = None
        unique_direct = sorted(set(direct_groups))
        union_stated: set[tuple[str, str]] = set()
        if len(unique_direct) == 1:
            direct_group = unique_direct[0]
        elif len(unique_direct) > 1:
            # One original lists several ingredients (alternatives like
            # "fresh or dried sage", conglomerate summary lines): any
            # amount in that source line itself passes (union of those
            # groups). Swaps across different originals still fail.
            for gi in unique_direct:
                union_stated.update(groups[gi]["stated"])
        mentions = _line_mentions(
            normalized, groups, [(start, end) for _, _, _, start, end in amounts]
        )
        cited_idx: int | None = None
        cited_ok = False
        if field == "steps" and step_index is not None and isinstance(cited_list, list):
            if 0 <= step_index < len(cited_list):
                citation = cited_list[step_index]
                if (
                    isinstance(citation, int)
                    and not isinstance(citation, bool)
                    and 0 <= citation < len(directions)
                ):
                    cited_ok = True
                    cited_idx = citation
        # Faithful copy bypass (2026-10-07 H2): a step equal to its cited
        # direction verbatim passes (source-text self-check, model copies).
        # Swaps still fail because chicken vs potatoes texts differ.
        if (
            field == "steps"
            and cited_ok
            and cited_idx is not None
            and _norm_line_key(text) == _norm_line_key(directions[cited_idx])
        ):
            continue
        # Order pairing for name-then-amount lists (2026-10-07 H2).
        # Guarded: earliest mention starts must be distinct (shared head
        # words like "chicken" in stock/bouillon must not force pairing).
        order_pair: dict[int, int] = {}
        distinct = [gi for gi, items in mentions.items() if items]
        if len(amounts) == len(distinct) and len(amounts) > 1:
            earliest = {gi: min(s for s, _, _, _, _ in mentions[gi]) for gi in distinct}
            if len(set(earliest.values())) == len(distinct):
                by_mention = sorted(distinct, key=lambda gi: earliest[gi])
                by_amount = sorted(range(len(amounts)), key=lambda i: amounts[i][3])
                order_pair = {by_amount[k]: by_mention[k] for k in range(len(amounts))}
        prev_group: int | None = None
        for pos, (claim, value, unit, start, end) in enumerate(amounts):
            holding = [
                idx
                for idx, amounts_set in enumerate(direction_amounts)
                if (value, unit) in amounts_set
            ]
            if union_stated and (value, unit) in union_stated:
                prev_group = None
                continue
            candidates: list[int] = []
            group_index: int | None = None
            if direct_group is not None:
                group_index = direct_group
            elif _is_parenthetical(normalized, start, end) and prev_group is not None:
                group_index = prev_group
            elif pos in order_pair:
                group_index = order_pair[pos]
            elif (
                forward := _forward_list_attachment(normalized, start, end, mentions)
            ) is not None:
                group_index = forward
            else:
                # Candidate set (2026-10-07 H2R): tied best-key groups
                # pass if any states it (butter chilled/melted); longer
                # wins (bouillon over stock), preserving swaps.
                best_one, tied_all = _nearest_with_ties(start, end, mentions)
                if best_one is None and not tied_all:
                    group_index = None
                    candidates = []
                elif tied_all:
                    candidates = sorted(tied_all)
                    if any((value, unit) in groups[gi]["stated"] for gi in candidates):
                        stating = [gi for gi in candidates if (value, unit) in groups[gi]["stated"]]
                        prev_group = stating[0] if len(stating) == 1 else None
                        continue
                    group_index = None
                else:
                    group_index = best_one
                    candidates = []
                if group_index is None and candidates:
                    # Tied, none states -> ambiguous rejection below.
                    pass
                if len(candidates) > 1:
                    names = ", ".join(
                        f"{groups[gi]['key']!r} ({'; '.join(groups[gi]['displays'][:1])})"
                        for gi in candidates
                    )
                    error = (
                        f"plan {field} says {text.strip()[:120]!r} ({claim!r}), "
                        f"but the amount is ambiguous between {names}; rewrite "
                        "as 'AMOUNT UNIT INGREDIENT' with the full name"
                    )
                    if error not in errors:
                        errors.append(error)
                    prev_group = None
                    continue
            if group_index is not None:
                group = groups[group_index]
                prev_group = group_index
                if (value, unit) in group["stated"]:
                    continue
                if (
                    cited_ok
                    and cited_idx is not None
                    and _direction_supports_group(
                        directions[cited_idx], value, unit, group_index, groups
                    )
                ):
                    continue
                if group["stated"]:
                    source = "; ".join(group["displays"][:3]) or "no amount stated"
                    error = (
                        f"plan {field} says {text.strip()[:120]!r} ({claim!r}), "
                        f"but the source states {group['key']!r} as {source}; "
                        "copy each amount exactly as get_recipe shows it"
                    )
                else:
                    source = "; ".join(group["displays"][:2])
                    detail = f" ({source})" if source else ""
                    error = (
                        f"plan {field} says {text.strip()[:120]!r} ({claim!r}), "
                        f"but the source states no amount for {group['key']!r}"
                        f"{detail}; omit the amount"
                    )
                if field == "steps" and holding:
                    if (
                        cited_ok
                        and cited_idx is not None
                        and (value, unit) in direction_amounts[cited_idx]
                    ):
                        error += "; cited direction states it for a different ingredient"
                    else:
                        error += (
                            f"; direction(s) {holding} state {claim!r} — cite one via step_sources"
                        )
            else:
                prev_group = None
                if cited_ok and cited_idx is not None:
                    if (value, unit) in direction_amounts[cited_idx]:
                        continue
                error = (
                    f"plan {field} says {text.strip()[:120]!r} ({claim!r}), but no "
                    f"source ingredient matches that amount; the source states no "
                    f"{unit} amount of that size there — copy each amount exactly "
                    "as get_recipe shows it, or cite the direction that states it"
                )
                if field == "steps" and holding:
                    error += f"; direction(s) {holding} state {claim!r} — cite one via step_sources"
            if error not in errors:
                errors.append(error)
            prev_group = group_index if group_index is not None else None
    return errors[:5]
