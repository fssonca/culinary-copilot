"""Deterministic validation and merge of LLM extraction output.

What validation CAN establish: schema/type conformance, source-identity and
content-hash matching, evidence references that resolve to real source
lines with matching excerpts, exact numeric consistency (fractions, dozen
math), unit allow-listing, ingredient/step coverage and order, absence of
invented servings/URLs, and configuration freshness (prompt/schema/parser
versions unchanged since the request).

What it CANNOT establish: semantic correctness — whether "2 cups" truly
belongs to "flour" when the source is ambiguous, or whether a step
paraphrase preserves meaning. Schema validity and resolvable evidence are
necessary but not sufficient; residual risk is carried explicitly in
`uncertainty_notes`, lowered capabilities, and the `validated_partial` /
`unresolved` states. The model's self-reported uncertainty is recorded but
never used as an acceptance threshold.

Verdicts: accepted | accepted_partial | rejected_validation |
not_a_recipe | stale (never applied).
"""

import re
from fractions import Fraction
from typing import Any

from culinary_copilot.recipes.adapters.foodie import _ALIAS_LOOKUP, UNIT_ALIASES
from culinary_copilot.recipes.llm_contracts import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ExtractionResponse,
    prompt_hash,
)
from culinary_copilot.recipes.quality import capabilities_for

# Logic versions: part of cache identity (see llm_cache.entry_is_fresh).
# Bump VALIDATOR_VERSION for any rule change in validate_response
# (verdict ladder, problem codes, evidence checks); bump MERGE_VERSION for
# any rule change in merge_response (overlay, inheritance, capabilities).
# Stale cached merges are then revalidated/remerged automatically —
# never raised to ready under corrected logic. Prompt/schema versions are
# untouched by logic fixes (they describe the model contract, not our code).
VALIDATOR_VERSION = "3"
MERGE_VERSION = "3"


def current_logic_versions() -> dict[str, str]:
    return {"validator_version": VALIDATOR_VERSION, "merge_version": MERGE_VERSION}


ALLOWED_UNITS = set(UNIT_ALIASES) | {"count", "container"}


def canonical_unit(raw: str | None) -> str | None:
    """Canonicalize a model unit against our closed vocabulary.

    Any known alias spelling ("tablespoon", "fluid ounces") maps to its
    code ("tbsp", "fl_oz") deterministically in scripts. Unknown words
    ("bathtub") stay None and are rejected as unsupported.
    """
    if raw is None:
        return None
    key = raw.strip().lower().rstrip(".,")
    if key == "containers":
        return "container"
    if key in ALLOWED_UNITS:
        return key
    return _ALIAS_LOOKUP.get(key)


TEMP_UNITS = {"C", "F", "GAS_MARK", "UNKNOWN"}
URL_RE = re.compile(r"(?i)\bhttps?://|www\.")
SERVINGS_HINT_RE = re.compile(r"(?i)\b(serves?|servings?|makes?|yields?|serving)\b")


def _norm(text: str) -> str:
    return " ".join(_normalize_amount_text(text).casefold().split())


def gram_source_evidence(line: str) -> bool:
    """Independent raw-text grammar: never invoke the parser being checked."""
    # An explicit metric unit, even digit-adjacent, must end before a word.
    if re.search(r"(?<![A-Za-z])(?:g|gm|gms|gram|grams)(?![A-Za-z-])", line, re.I):
        return True
    # Fully spelled plural grams glued to a following ingredient is explicit.
    if re.search(r"(?<![A-Za-z])grams(?=[A-Za-z])", line, re.I):
        return True
    # Scraped g+Capital and a finite supported lowercase suffix vocabulary.
    if re.search(r"(?<![A-Za-z])g(?=[A-Z][a-z])", line):
        return True
    return bool(
        re.search(
            r"(?<![A-Za-z])g(?:plain|corn|spinach|flour|sugar|butter|chocolate|rice|oats|grated|semi-salted)\b",
            line,
            re.I,
        )
    )


def ambiguous_gram_glue(line: str) -> bool:
    # Observed ambiguous scrape tokens: preserve their source and hold rather
    # than turn them into confident counts or guess the intended ingredient.
    return bool(re.search(r"(?i)(?<![a-z])(?:grbeef|ggrained|glow-fat|gground|ggroats)\b", line))


def _unit_evidenced(unit: str, line: str) -> bool:
    """A structured unit must appear in its cited source line (synonym-aware).

    Single-word aliases match whole tokens; multi-word aliases ("fluid
    ounce") match as phrases. Missing-space scrape artifacts ("ggrated",
    "gsemi-salted") de-glue through the same strip_unit_prefix the
    deterministic parser uses — single letters are never stripped from
    ordinary words ("large" keeps its "l"). Unicode fractions are
    normalized first so "½" and "1/2" compare equally.
    """
    from culinary_copilot.recipes.adapters.foodie import repair_spacing, strip_unit_prefix

    if unit == "g":
        return gram_source_evidence(line)
    aliases = UNIT_ALIASES.get(unit, set())
    normalized = _norm(repair_spacing(line))
    # Hyphens stay inside tokens so scrape gluings ("gsemi-salted") reach
    # strip_unit_prefix intact.
    tokens = set(re.findall(r"[a-z%°-]+", normalized))
    if aliases & tokens:
        return True
    for token in tokens:
        prefix, glued = strip_unit_prefix(token)
        if prefix is not None and glued and prefix.lower().rstrip(".,") in aliases:
            return True
        # Abbreviation glued to a following word ("tspblack"): aliases of
        # length >= 3 match as token prefixes. Shorter aliases stay
        # whole-token-only so "cloves" never evidences "cl" and "large"
        # never evidences "l". Residual risk ("cupcake" evidencing "cup")
        # is documented: it needs a model to unit-tag a count noun.
        for alias in aliases:
            if len(alias) >= 3 and len(token) > len(alias) and token.startswith(alias):
                return True
    return any(alias in normalized for alias in aliases if " " in alias or "-" in alias)


def _line_numbers(line: str) -> set[Fraction]:
    """Exact rational values of every number token in a source line."""
    values: set[Fraction] = set()
    normalized = _normalize_amount_text(line)
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
    }
    # Literal number words are source evidence, not inferred quantities.
    for word in re.findall(r"[a-z]+", normalized.casefold()):
        if word in words:
            values.add(Fraction(words[word]))
    for token in re.findall(r"\d+(?:\.\d+|/\d+| \d+/\d+)?", normalized):
        try:
            values.add(sum((Fraction(part) for part in token.split()), Fraction(0)))
        except (ValueError, ZeroDivisionError):
            continue
    return values


def _amount_evidenced(amount_text: str, line: str) -> bool:
    """Every number in the reported amount must occur in the cited line.

    Values compare as exact rationals, so "1/2" matches "½" but "0.5"
    written for "1/2" does not: silent rescaling is never assumed.
    """
    line_values = _line_numbers(line)
    for token in re.findall(r"\d+(?:\.\d+|/\d+| \d+/\d+)?", _normalize_amount_text(amount_text)):
        try:
            value = sum((Fraction(part) for part in token.split()), Fraction(0))
        except (ValueError, ZeroDivisionError):
            return False
        if value not in line_values:
            return False
    return True


def _hint_numbers(line_map: dict[str, str]) -> list[tuple[float, float]]:
    """(low, high) spans from numbered servings statements; ranges inclusive."""
    spans: list[tuple[float, float]] = []
    for line in line_map.values():
        if not (SERVINGS_HINT_RE.search(line) and re.search(r"\d", line)):
            continue
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to|or)?\s*(\d+(?:\.\d+)?)?", line):
            first = float(match.group(1))
            second = match.group(2)
            if second is None:
                spans.append((first, first))
            else:
                spans.append((min(first, float(second)), max(first, float(second))))
    return spans


# Bounded item vocabulary: time, weights, servings, ranges and bare numbers
# must not become a batch yield. Unknown yield forms remain unsupported.
YIELD_ITEMS = (
    r"(?:rolls?|buns?|cookies?|biscuits?|muffins?|cupcakes?|patties|pizzas?|loaves|pieces?)"
)
DIRECT_YIELD_RE = re.compile(
    rf"(?i)(?:^|[.!?]\s+)(?:(?:this|the) recipe\s+)?"
    rf"(?:makes?|yields?)\s+(\d+)\s+{YIELD_ITEMS}\b"
)
GET_YIELD_RE = re.compile(rf"(?i)\bgets?\s+(\d+)\s+{YIELD_ITEMS}\s+from (?:this|the) recipe\b")


def _batch_yield_numbers(line_map: dict[str, str]) -> set[int]:
    """Exact dozen math plus explicit finished-item counts, never servings."""
    counts: set[int] = set()
    for line in line_map.values():
        for match in re.finditer(r"(?i)(\d+(?:\.\d+)?)\s*dozen\b", line):
            count = Fraction(match.group(1)) * 12
            if count > 0 and count.denominator == 1:
                counts.add(int(count))
        for pattern in (DIRECT_YIELD_RE, GET_YIELD_RE):
            for match in pattern.finditer(line):
                prefix = re.split(r"[.!?]", line[: match.start()])[-1]
                if re.search(r"(?i)\b(?:not|never|no)\b|n't", prefix):
                    continue
                items = int(match.group(1))
                if items > 0:
                    counts.add(items)
    return counts


FRACTION_GLYPHS = {
    "½": "1/2",
    "¼": "1/4",
    "¾": "3/4",
    "⅓": "1/3",
    "⅔": "2/3",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
}


def _normalize_amount_text(text: str) -> str:
    """Deterministic exact-arithmetic normalization (never leniency).

    Unicode fractions ("½", "1 ½") become ASCII rationals ("1/2", "1 1/2")
    before parsing, mirroring the deterministic parser. Anything still
    unparseable stays invalid.
    """
    out = text.strip().replace("⁄", "/")
    for glyph, ascii_ in FRACTION_GLYPHS.items():
        out = re.sub(r"(?<=\d)" + glyph, " " + ascii_, out)
        out = out.replace(glyph, ascii_)
    out = re.sub(r"(?<=\d)\s*/\s*(?=\d)", "/", out)
    # Hyphenated mixed fractions are distinct from ranges such as 3-4.
    out = re.sub(r"(?<![\d/])(\d+)\s*(?:-|and)\s*(\d+/\d+)", r"\1 \2", out)
    return re.sub(r"\s+", " ", out).strip()


def _rational_str(text: str) -> str | None:
    text = _normalize_amount_text(text)
    if not re.fullmatch(r"\d+(?:\.\d+|/\d+| \d+/\d+)?", text):
        return None
    try:
        amount = sum((Fraction(part) for part in text.split()), Fraction(0))
        return str(amount) if amount > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def validate_response(
    response: dict[str, Any],
    *,
    source_id: str,
    content_hash: str,
    line_map: dict[str, str],
    ingredient_line_ids: list[str],
    step_line_ids: list[str],
    request_versions: dict[str, str],
    current_versions: dict[str, str],
    heading_line_ids: list[str] | None = None,
    prose_line_ids: list[str] | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one model interpretation. Returns a validation report.

    `baseline` carries deterministic facts scripts already preserve
    (servings, steps presence). The model fills gaps; contradicting settled
    fields is a major failure. Every problem carries a `class`:
    "critical" (quantities, units, identity, instructions, servings),
    "uncertainty" (faithfully preserved ranges and honestly absent amounts —
    not extraction errors, but still capping the verdict and capabilities), or
    "optional" (prose/metadata omissions that never affect the verdict).
    """
    problems: list[dict[str, str]] = []
    baseline = baseline or {}

    def problem(code: str, detail: str) -> None:
        problems.append({"code": code, "detail": detail})

    try:
        parsed = ExtractionResponse.model_validate(response)
    except Exception as exc:
        return {
            "verdict": "rejected_validation",
            "problems": [{"code": "schema_invalid", "detail": str(exc)[:300]}],
            "retry_eligible": True,
        }
    if parsed.source_id != source_id:
        problem("identity_mismatch", f"response for {parsed.source_id}, expected {source_id}")
    if parsed.content_hash != content_hash:
        problem("stale_source", "content hash differs from the requested source")
    for key, requested_version in request_versions.items():
        current = current_versions.get(key)
        if current != requested_version:
            problem(
                "stale_configuration",
                f"{key} changed {requested_version} -> {current}; result must not apply",
            )
    line_ids = set(line_map)

    def check_evidence(where: str, line_refs: list[str], excerpt: str) -> None:
        if not line_refs or not (excerpt or "").strip():
            problem("missing_evidence", f"{where}: empty evidence references")
            return
        for lid in line_refs:
            if lid not in line_ids:
                problem("evidence_unknown_line", f"{where}: {lid} not in source")
        haystack = " ".join(line_map[lid] for lid in line_refs if lid in line_map)
        if _norm(excerpt) not in _norm(haystack):
            problem("fabricated_evidence", f"{where}: excerpt not found in cited lines")

    covered_ing: set[str] = set()
    for pos, item in enumerate(parsed.ingredients):
        where = f"ingredients[{pos}]"
        if item.source_line_id not in line_ids:
            problem("evidence_unknown_line", f"{where}: {item.source_line_id}")
        covered_ing.add(item.source_line_id)
        check_evidence(where, item.evidence.line_ids, item.evidence.excerpt)
        if (
            item.amount_value is None
            and item.amount_text
            and re.search(r"[0-9¼½¾⅓⅔⅛⅜⅝⅞]", item.amount_text)
        ):
            if item.is_range:
                # A faithfully preserved range is uncertainty, not an error:
                # the text is kept, no single scalable value is forced.
                problem(
                    "quantity_uncertain",
                    f"{where}: range {item.amount_text!r} preserved without a single value",
                )
            elif item.uncertain:
                # An honestly flagged absent/ambiguous amount is uncertainty.
                problem(
                    "quantity_uncertain",
                    f"{where}: amount honestly unresolvable from source",
                )
            else:
                problem(
                    "quantity_unresolved",
                    f"{where}: reported quantity remains uninterpreted",
                )
        if item.amount_value is not None:
            try:
                numeric = _rational_str(item.amount_value)
                if numeric is None:
                    raise ValueError("not a positive rational")
                value = Fraction(numeric)
            except (ValueError, ZeroDivisionError):
                problem("nonfinite_amount", f"{where}: {item.amount_value!r}")
                continue
            if value <= 0:
                problem("nonpositive_amount", f"{where}: {item.amount_value!r}")
            if item.amount_text:
                expected = _rational_str(item.amount_text)
                if expected is not None and Fraction(expected) != value:
                    problem(
                        "amount_inconsistent",
                        f"{where}: text {item.amount_text!r} != value {item.amount_value!r}",
                    )
        if item.unit_normalized is not None and canonical_unit(item.unit_normalized) is None:
            problem("unsupported_unit", f"{where}: {item.unit_normalized!r}")
        # A clean ingredient name never starts with a quantity ("2 % milk"
        # percent designations exempted). Mid-name measures ("1/4-inch
        # strips") are cut sizes, not extraction failures.
        if re.match(r"\d", (item.name or "").strip()) and not re.match(
            r"\d+\s*%", (item.name or "").strip()
        ):
            problem("name_contains_measure", f"{where}: name starts with a quantity")
        if canonical_unit(item.unit_normalized) == "container" and not re.search(
            r"\bcontainers?\b", line_map.get(item.source_line_id, ""), re.I
        ):
            problem("unsupported_unit", f"{where}: container not supported by source line")
        # Structured amounts and units must be supported by the cited source
        # line and agree with each other: 19380 reached ready rows with units
        # inherited onto lines that never stated them. "count" is exempt: it
        # is the normalized fallback for bare nouns, evidenced by the noun
        # itself (checked via name/line coverage, not unit tokens).
        line_text = line_map.get(item.source_line_id, "")
        unit = canonical_unit(item.unit_normalized)
        if unit is not None and unit not in ("count", "container"):
            if not _unit_evidenced(unit, line_text):
                problem(
                    "unit_unsupported_by_line",
                    f"{where}: unit {item.unit_normalized!r} not evidenced in source line",
                )
        if (
            item.amount_value is not None
            and item.amount_text
            and re.search(r"[0-9¼½¾⅓⅔⅛⅜⅝⅞]", item.amount_text)
            and not _amount_evidenced(item.amount_text, line_text)
        ):
            problem(
                "amount_unsupported_by_line",
                f"{where}: amount {item.amount_text!r} not evidenced in source line",
            )
        for alt in item.alternatives:
            if _norm(alt) not in _norm(
                item.evidence.excerpt + " " + line_map.get(item.source_line_id, "")
            ):
                problem("invented_alternative", f"{where}: {alt!r} not in source line")
    covered_steps: list[str] = []
    for pos, step in enumerate(parsed.steps):
        where = f"steps[{pos}]"
        if step.source_line_id not in line_ids:
            problem("evidence_unknown_line", f"{where}: {step.source_line_id}")
        covered_steps.append(step.source_line_id)
        check_evidence(where, step.evidence.line_ids, step.evidence.excerpt)
        # Steps are preserved text, never rewritten: the text must match the
        # cited source line verbatim (normalized whitespace/case).
        cited = line_map.get(step.source_line_id, "")
        if cited and _norm(step.text) != _norm(cited):
            problem("step_paraphrase", f"{where}: text differs from cited source line")
    if not baseline.get("has_steps", True) and not parsed.steps:
        if parsed.status != "not_a_recipe":
            problem("steps_unresolved", "no deterministic steps and model supplied none")
    order = [lid for lid in covered_steps if lid in line_map]
    if order != sorted(order, key=lambda lid: int(lid[1:])):
        problem("instruction_order_violation", "steps do not follow source line order")
    unclassified = set(parsed.unclassified_line_ids)
    for lid in unclassified:
        if lid not in line_ids:
            problem("evidence_unknown_line", f"unclassified: {lid}")
    covered_headings: set[str] = set()
    for pos, heading in enumerate(parsed.headings):
        where = f"headings[{pos}]"
        if heading.source_line_id not in line_ids:
            problem("evidence_unknown_line", f"{where}: {heading.source_line_id}")
        covered_headings.add(heading.source_line_id)
        check_evidence(where, heading.evidence.line_ids, heading.evidence.excerpt)
    requested = baseline.get("requested_ingredient_line_ids")
    required_ingredients = ingredient_line_ids if requested is None else requested
    for lid in required_ingredients:
        if lid not in covered_ing:
            problem("ingredient_coverage_gap", f"{lid} remains unresolved")
    # Targeted extraction: blocks scripts already preserve (deterministic
    # steps/groups present) stand as-is — the model is not required to
    # re-emit them. Only genuinely missing blocks must come from the model.
    if not baseline.get("has_steps", True):
        for lid in step_line_ids:
            if lid not in set(covered_steps) and lid not in unclassified:
                problem("instruction_coverage_gap", f"{lid} neither interpreted nor unclassified")
    if not baseline.get("has_groups", True):
        for lid in heading_line_ids or []:
            if lid not in covered_headings and lid not in unclassified:
                problem("heading_coverage_gap", f"{lid} neither interpreted nor unclassified")
    # Description/intro prose must surface in description, notes or steps —
    # or be explicitly unclassified. Silent paragraph loss caps at partial.
    prose_text = " ".join(
        [
            parsed.description or "",
            *[n.text for n in parsed.notes],
            *[s.text for s in parsed.steps],
        ]
    )
    for lid in prose_line_ids or []:
        line = line_map.get(lid, "")
        if (
            lid not in covered_ing
            and lid not in set(covered_steps)
            and lid not in covered_headings
            and lid not in unclassified
            and _norm(line)[:80] not in _norm(prose_text)
        ):
            problem("prose_coverage_gap", f"{lid} prose not preserved or classified")
    # Settled deterministic fields: the model fills gaps but never
    # contradicts scripts. A conflicting value is a major failure.
    if baseline.get("servings") is not None and parsed.servings is not None:
        if parsed.servings != baseline["servings"]:
            problem(
                "deterministic_conflict",
                f"servings {parsed.servings} contradicts deterministic {baseline['servings']}",
            )
    base_yield = (baseline.get("batch_yield") or {}).get("count")
    if base_yield is not None and parsed.batch_yield_count is not None:
        if parsed.batch_yield_count != base_yield:
            problem(
                "deterministic_conflict",
                f"batch yield {parsed.batch_yield_count} contradicts deterministic {base_yield}",
            )
    if parsed.servings is not None:
        # A servings claim needs a numbered servings statement in the source;
        # a bare imperative ("Serve hot.") is not servings evidence. The
        # claimed value must also match a number in that statement, so
        # "Serves 2" can never justify 999 servings.
        hint_numbers = _hint_numbers(line_map)
        if not hint_numbers:
            problem("invented_servings", "servings without a source servings statement")
        elif not any(low <= parsed.servings <= high for low, high in hint_numbers):
            problem(
                "servings_mismatch",
                f"servings {parsed.servings} matches no source servings number",
            )
        if parsed.servings <= 0:
            problem("nonpositive_amount", "servings must be positive")
    if parsed.batch_yield_count is not None:
        yields = _batch_yield_numbers(line_map)
        if not yields:
            problem("invented_batch_yield", "batch yield without an explicit source yield count")
        elif parsed.batch_yield_count not in yields:
            problem(
                "batch_yield_mismatch",
                f"yield {parsed.batch_yield_count} matches no explicit source yield count",
            )
    for temp in parsed.temperatures:
        if not isinstance(temp, dict) or temp.get("unit") not in TEMP_UNITS:
            problem("unsupported_temperature", f"{temp!r}"[:120])
    blobs = [
        *(i.name for i in parsed.ingredients),
        *(h.text for h in parsed.headings),
        *(s.text for s in parsed.steps),
        *(n.text for n in parsed.notes),
        *parsed.uncertainty_notes,
    ]
    if any(URL_RE.search(blob) for blob in blobs):
        problem("invented_url", "URL in model output without source evidence")
    if parsed.status == "not_a_recipe" and not parsed.second_recipe_boundary:
        problem("missing_boundary", "not_a_recipe requires second_recipe_boundary")
    if parsed.second_recipe_boundary and parsed.second_recipe_boundary not in line_ids:
        problem("evidence_unknown_line", "second_recipe_boundary not in source")

    # Verdict ladder: detected errors ALWAYS affect acceptance. Fatal codes
    # reject without retry (untrustworthy source/model pairing); major codes
    # reject with bounded retry (possibly transient); gaps and uncertainty
    # cap at accepted_partial; only a clean resolved response is accepted.
    fatal = {
        "identity_mismatch",
        "stale_source",
        "stale_configuration",
        "fabricated_evidence",
        "invented_servings",
        "invented_batch_yield",
        "invented_url",
    }
    major = {
        "schema_invalid",
        "nonfinite_amount",
        "nonpositive_amount",
        "amount_inconsistent",
        "amount_unsupported_by_line",
        "unit_unsupported_by_line",
        "unsupported_unit",
        "unsupported_temperature",
        "invented_alternative",
        "missing_evidence",
        "evidence_unknown_line",
        "servings_mismatch",
        "batch_yield_mismatch",
        "missing_boundary",
        "instruction_order_violation",
        "name_contains_measure",
        "deterministic_conflict",
        "step_paraphrase",
        "empty_extraction",
    }
    # Optional prose/metadata omissions are reported but never affect the
    # verdict or capabilities; honest uncertainty is reported as uncertainty
    # (still capping verdict and capabilities via the gaps set below);
    # everything else is recipe-critical.
    for entry in problems:
        if entry["code"] == "prose_coverage_gap":
            entry["class"] = "optional"
        elif entry["code"] == "quantity_uncertain":
            entry["class"] = "uncertainty"
        else:
            entry["class"] = "critical"
    if (
        parsed.status == "resolved"
        and not parsed.ingredients
        and not parsed.steps
        and not parsed.headings
        and not (requested == [] and baseline.get("has_deterministic"))
    ):
        problem("empty_extraction", "resolved status with no interpreted content")
        problems[-1]["class"] = "critical"
    codes = {p["code"] for p in problems}
    if codes & fatal:
        return {
            "verdict": "rejected_validation",
            "problems": problems,
            "retry_eligible": False,
        }
    if "schema_invalid" in codes:
        return {"verdict": "rejected_validation", "problems": problems, "retry_eligible": True}
    if parsed.status == "not_a_recipe" and not (codes & major):
        return {"verdict": "not_a_recipe", "problems": problems, "retry_eligible": False}
    if codes & major:
        return {"verdict": "rejected_validation", "problems": problems, "retry_eligible": True}
    if baseline.get("has_deterministic") is False and len(line_map) == 1:
        # Explicit abstention until the contract supports character-span coverage.
        problems.append(
            {
                "code": "single_line_requires_spans",
                "class": "critical",
                "detail": "Single-line scrape needs span-aware extraction; quarantine.",
            }
        )
        return {
            "verdict": "accepted_partial",
            "problems": problems,
            "retry_eligible": False,
            "load_eligible": False,
        }
    gaps = codes & {
        "ingredient_coverage_gap",
        "instruction_coverage_gap",
        "heading_coverage_gap",
        "steps_unresolved",
        "quantity_unresolved",
        "quantity_uncertain",
    }
    uncertain = any(i.uncertain for i in parsed.ingredients) or any(
        s.uncertain for s in parsed.steps
    )
    if parsed.status == "resolved" and (gaps or uncertain):
        problems.append(
            {
                "code": "status_downgraded",
                "detail": "resolved claimed with gaps/uncertainty -> partially_resolved",
                "class": "critical",
            }
        )
        return {
            "verdict": "accepted_partial",
            "problems": problems,
            "retry_eligible": False,
            "requested_ingredient_line_ids": requested,
        }
    if parsed.status in ("partially_resolved", "unresolved") or gaps or uncertain:
        return {
            "verdict": "accepted_partial",
            "problems": problems,
            "retry_eligible": False,
            "requested_ingredient_line_ids": requested,
        }
    return {
        "verdict": "accepted",
        "problems": problems,
        "retry_eligible": False,
        "requested_ingredient_line_ids": requested,
    }


def current_version_map(*, adapter_version: str, routing_version: str) -> dict[str, str]:
    return {
        "prompt_version": PROMPT_VERSION,
        "prompt_hash": prompt_hash(),
        "schema_version": SCHEMA_VERSION,
        "adapter_version": adapter_version,
        "routing_version": routing_version,
    }


def merge_response(
    response: ExtractionResponse,
    *,
    deterministic: dict[str, Any] | None,
    validation: dict[str, Any],
    line_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Deterministically merge a validated interpretation into the recipe contract.

    Overlay rules (scripts own everything reliable):
    - title/description/notes/attribution: deterministic wins when present.
    - steps/servings/batch/temperatures/durations: deterministic wins when
      present; the model fills only genuine gaps.
    - ingredients: per-line overlay — model interpretations replace the
      deterministic occurrence for covered lines; deterministic occurrences
      persist (tagged) for lines the model left unclassified. Every
      occurrence carries `origin` for audit.
    Capabilities are RECOMPUTED from merged fields — the model never sets
    them. Unresolved fields stay explicit; nothing is invented.
    """
    quality_issues: list[dict[str, Any]] = [
        {
            "code": "llm_assisted",
            "severity": "info",
            "field": "provenance",
            "message": f"LLM-assisted extraction, validation {validation['verdict']}.",
        }
    ]
    for note in response.uncertainty_notes:
        quality_issues.append(
            {
                "code": "llm_uncertain",
                "severity": "info",
                "field": "extraction",
                "message": note[:200],
            }
        )
    line_map = line_map or {}
    from culinary_copilot.recipes.source_scope import ingredient_sources, needs_interpretation

    det_occurrences = ingredient_sources(deterministic or {}, line_map)
    scope = validation.get("requested_ingredient_line_ids")
    model_items = [
        item for item in response.ingredients if scope is None or item.source_line_id in scope
    ]
    covered_ids = {item.source_line_id for item in model_items}
    for pos, occurrence in enumerate(det_occurrences):
        occurrence.setdefault("origin", "deterministic")
        occurrence.setdefault("position", pos)
    kept_det = [item for item in det_occurrences if item.get("source_line_id") not in covered_ids]
    prior_by_line = {item.get("source_line_id"): item for item in det_occurrences}
    # A deterministic occurrence shared with several model ingredients (glued
    # source lines) cannot attribute its amount/unit to each of them: 19380
    # inherited unit 'lb' from a chicken occurrence onto lemon, salt and eggs
    # sharing one blob line. Such lines get no prior inheritance.
    line_model_counts: dict[str | None, int] = {}
    for item in model_items:
        line_model_counts[item.source_line_id] = line_model_counts.get(item.source_line_id, 0) + 1
    ingredients: list[dict[str, Any]] = []
    for pos, item in enumerate(model_items):
        amount: str | None = None
        if item.amount_value is not None:
            amount = _rational_str(item.amount_value)
        prior = prior_by_line.get(item.source_line_id, {})
        if line_model_counts.get(item.source_line_id, 0) > 1:
            prior = {}
        # Missing model values never erase settled amounts or normalized units.
        if amount is None and not needs_interpretation(prior):
            amount = prior.get("amount")
        unit = canonical_unit(item.unit_normalized)
        if unit is None:
            unit = prior.get("unit")
        if (
            unit is None
            and item.unit_text
            and _norm(item.unit_text) in _norm(line_map.get(item.source_line_id, ""))
        ):
            unit = canonical_unit(item.unit_text)
        ingredients.append(
            {
                "original": line_map.get(item.source_line_id, line_hint(item.source_line_id)),
                "canonical": item.name.casefold().strip(),
                "name": item.name.casefold().strip(),
                "epicure_id": None,
                "quantity_text": item.amount_text,
                "amount": amount,
                "amount_text": item.amount_text,
                "unit": unit,
                "unit_text": item.unit_text,
                "notes": item.notes,
                "alternatives": bool(item.alternatives),
                "alternative_options": item.alternatives,
                "optional": item.optional,
                "qualitative": item.qualitative,
                "group": item.group,
                "position": pos,
                "source_line_id": item.source_line_id,
                "uncertain": item.uncertain,
                "is_range": item.is_range,
                "compound": item.compound,
                "equivalent": item.equivalent,
                "origin": "llm",
            }
        )
    # Deterministic occurrences persist for lines the model left alone,
    # ordered after model interpretations.
    ingredients.extend(kept_det)
    ingredients.sort(key=lambda i: int(i.get("source_line_id", "L999999")[1:]))
    for position, item_dict in enumerate(ingredients):
        item_dict["position"] = position
    name_measure = [i for i in ingredients if re.search(r"\d", str(i.get("canonical") or ""))]
    if name_measure:
        quality_issues.append(
            {
                "code": "name_contains_measure",
                "severity": "warning",
                "field": "ingredients.canonical",
                "message": "Measurement tokens remain in merged names.",
            }
        )
    # Capabilities are gated on the validation verdict: anything other than
    # a clean `accepted` (partial, unresolved, downgraded) can never claim
    # validated quantities, completeness or scalability. Uncertain amounts
    # never count as validated either.
    fully_accepted = validation.get("verdict") == "accepted"
    if not fully_accepted:
        quality_issues.append(
            {
                "code": "llm_partial_or_unresolved",
                "severity": "warning",
                "field": "capabilities",
                "message": f"Validation verdict {validation.get('verdict')}: "
                "capabilities capped at evidence-only.",
            }
        )
    units_known = (
        fully_accepted
        and all(
            ((i["unit"] is not None) and not i.get("uncertain", False)) or i["qualitative"]
            for i in ingredients
            if i["amount"] is not None
        )
        and all(
            (i["amount"] is not None and not i.get("uncertain", False)) or i["qualitative"]
            for i in ingredients
        )
        and not name_measure
        and bool(ingredients)
    )
    warnings = any(i["severity"] == "warning" for i in quality_issues)
    base = deterministic or {}
    merged_steps = base.get("instructions", []) or [s.text for s in response.steps]
    merged_servings = (
        base.get("servings") if base.get("servings") is not None else response.servings
    )
    merged_servings_text = (
        base.get("servings_text") if base.get("servings") is not None else response.servings_text
    )
    merged_durations = base.get("durations_reported", []) or response.durations
    caps = capabilities_for(
        has_identity=True,
        has_ingredients=bool(ingredients),
        has_instructions=bool(merged_steps),
        quantities_validated=bool(units_known and not warnings),
        servings_known=merged_servings is not None,
        durations_known=bool(merged_durations),
        structural_issue=warnings,
    )
    return {
        **base,
        # Identity always present: response.source_id matched the request
        # (identity_mismatch is fatal, so merged responses agree by construction).
        "source_id": response.source_id,
        # Scripts own preserved text: deterministic wins when present.
        "title": base.get("title") or response.title,
        "description": base.get("description") or response.description,
        "notes_text": base.get("notes_text", []),
        "attribution": base.get("attribution", []),
        "ingredients": ingredients,
        "ingredient_groups": _merge_groups(
            [
                {"heading": h.text.rstrip(":"), "source_line_id": h.source_line_id}
                for h in response.headings
            ],
            base.get("ingredient_groups", []),
        ),
        "instructions": merged_steps,
        "temperatures": base.get("temperatures")
        or [
            {"value": t.get("value"), "unit": t.get("unit"), "raw": t.get("raw")}
            for t in response.temperatures
        ],
        "servings": merged_servings,
        "servings_text": merged_servings_text,
        "durations_reported": merged_durations,
        "batch_yield": (
            base.get("batch_yield")
            if base.get("batch_yield") is not None
            else (
                {
                    "count": response.batch_yield_count,
                    "unit": "count",
                    "text": response.batch_yield_text,
                }
                if response.batch_yield_count is not None
                else None
            )
        ),
        "quality_issues": [*base.get("quality_issues", []), *quality_issues],
        "capabilities": caps,
        "searchable": caps["searchable"],
        "scalable": caps["scalable"],
        "final_recipe_eligible": caps["complete_eligible"],
        "unresolved_fields": [
            p["detail"][:160]
            for p in validation.get("problems", [])
            if p["code"] != "status_downgraded"
        ],
        "llm_status": response.status,
    }


def merged_ingredient_problems(recipe: dict[str, Any]) -> list[dict[str, str]]:
    """Check final fields from BOTH model and deterministic origins.

    This is an evidence check, not semantic certification: numbers elsewhere
    on a shared line cannot prove ingredient attribution. Missing quantities
    stay unknown; unsupported populated fields block loading.
    """
    problems: list[dict[str, str]] = []
    for pos, item in enumerate(recipe.get("ingredients", [])):
        source = item.get("original")
        if not source:
            problems.append(
                {
                    "code": "merged_source_missing",
                    "class": "critical",
                    "detail": f"ingredients[{pos}]: missing original source",
                }
            )
            continue
        if ambiguous_gram_glue(source):
            problems.append(
                {
                    "code": "ambiguous_gram_glue",
                    "class": "critical",
                    "detail": f"ingredients[{pos}]: ambiguous source {source!r}",
                }
            )
        unit = item.get("unit")
        valid_unit = unit is None or unit == "count"
        if unit == "container":
            valid_unit = bool(re.search(r"\bcontainers?\b", source, re.I))
        elif unit is not None and unit != "count":
            valid_unit = canonical_unit(unit) is not None and _unit_evidenced(
                canonical_unit(unit) or unit, source
            )
        if not valid_unit:
            problems.append(
                {
                    "code": "merged_unit_unsupported",
                    "class": "critical",
                    "detail": f"ingredients[{pos}]: {unit!r} absent from {source!r}",
                }
            )
        amount = item.get("amount")
        if amount is not None:
            rational = _rational_str(str(amount))
            if rational is None or Fraction(rational) not in _line_numbers(source):
                problems.append(
                    {
                        "code": "merged_amount_unsupported",
                        "class": "critical",
                        "detail": f"ingredients[{pos}]: {amount!r} absent from {source!r}",
                    }
                )
    return problems


def line_hint(line_id: str) -> str:
    return f"source:{line_id}"


def _merge_groups(
    model_groups: list[dict[str, Any]], base_groups: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union of model headings and deterministic groups, deduped by heading.

    A model that emits one of three headings must not erase the other two:
    deterministic groups persist for headings the model did not cover.
    """
    seen = {
        str(g.get("heading", "")).rstrip(":").casefold()
        for g in model_groups
        if isinstance(g, dict)
    }
    merged = list(model_groups)
    for group in base_groups:
        if not isinstance(group, dict):
            continue
        if str(group.get("heading", "")).rstrip(":").casefold() not in seen:
            merged.append(group)
    return merged


def current_versions_for(adapter_version: str, routing_version: str) -> dict[str, str]:
    return current_version_map(adapter_version=adapter_version, routing_version=routing_version)
