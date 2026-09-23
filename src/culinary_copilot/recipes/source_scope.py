"""Trace repaired occurrences back to original source lines without changing evidence."""

import re
import unicodedata
from typing import Any


def comparison_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).replace("⁄", "/").split())


def ingredient_sources(recipe: dict[str, Any], line_map: dict[str, str]) -> list[dict[str, Any]]:
    """Ordered multiset alignment; duplicate text consumes distinct source lines."""
    remaining = list(line_map)
    result = []
    for occurrence in recipe.get("ingredients", []):
        item = dict(occurrence)
        wanted = comparison_text(item.get("original") or "")
        lid = next((lid for lid in remaining if comparison_text(line_map[lid]) == wanted), None)
        if lid is None and wanted:
            candidates = [
                lid for lid in remaining if comparison_text(line_map[lid]).startswith(wanted)
            ]
            if len(candidates) == 1:
                lid = candidates[0]
        if lid is not None:
            remaining.remove(lid)
            item["source_line_id"] = lid
        result.append(item)
    return result


def needs_interpretation(item: dict[str, Any]) -> bool:
    name = (item.get("canonical") or "").strip()
    # Cut sizes and fat percentages are ingredient descriptions, not stray quantities.
    measure = bool(re.search(r"\d", name)) and not (
        re.match(r"^\d+\s*%", name) or re.search(r"\d+(?:/\d+)?[- ]inch", name)
    )
    return bool(
        (item.get("amount") is None and not item.get("qualitative"))
        or measure
        or item.get("compound")
        or item.get("equivalent")
        or item.get("is_range")
    )


def requested_lines(recipe: dict[str, Any], line_map: dict[str, str]) -> list[str]:
    items = ingredient_sources(recipe, line_map)
    # A failed alignment must not silently disappear from the request.
    if any(needs_interpretation(i) and not i.get("source_line_id") for i in items):
        raise ValueError(
            "Cannot trace ambiguous ingredient to source; quarantine before extraction"
        )
    return [i["source_line_id"] for i in items if needs_interpretation(i)]
