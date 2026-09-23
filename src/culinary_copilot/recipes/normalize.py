"""Conservative, deterministic parsing. Source text is never executed."""

import hashlib
import json
import re
import unicodedata
from fractions import Fraction
from typing import Any

from culinary_copilot.recipes.nutrition import observe_foodcom_nutrition
from culinary_copilot.recipes.quality import capabilities_for, issue
from culinary_copilot.recipes.search import SEARCH_DOCUMENT_VERSION

VERSION = "3"
MISSING = {"", "NA", "NULL"}


def canonical(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def parse_list(value: str) -> list[str | None]:
    value = value.strip()
    if value in MISSING or value == "character(0)":
        return []
    if value.startswith("c(") and value.endswith(")"):
        value = value[2:-1].strip()
    elif not value.startswith('"'):
        # R prints single-element character vectors without c(...).
        if "(" in value or ")" in value:
            raise ValueError("invalid_list")
        return [value]
    result: list[str | None] = []
    decoder = json.JSONDecoder()
    while value:
        if value.startswith("NA") and (len(value) == 2 or value[2] in ", \n\r\t"):
            item, end = None, 2
        else:
            try:
                item, end = decoder.raw_decode(value)
            except ValueError as exc:
                raise ValueError("invalid_list") from exc
            if not isinstance(item, str):
                raise ValueError("invalid_list")
        result.append(item)
        value = value[end:].strip()
        if not value:
            break
        if not value.startswith(",") or not value[1:].strip():
            raise ValueError("invalid_list")
        value = value[1:].strip()
    return result


def minutes(value: str) -> float | None:
    if value.strip() in MISSING:
        return None
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value)
    if not match or not any(match.groups()):
        raise ValueError("invalid_duration")
    h, m, s = (float(v or 0) for v in match.groups())
    return h * 60 + m + s / 60


def quantity(value: str | None) -> str | None:
    """Exact rational amount; no inferred unit or range midpoint."""
    if value is None or not re.fullmatch(r"\d+(?:\.\d+|/\d+| \d+/\d+)?", value.strip()):
        return None
    try:
        amount = sum((Fraction(part) for part in value.split()), Fraction(0))
        return str(amount) if amount > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _text(raw: dict[str, str], key: str) -> str | None:
    value = raw.get(key, "").strip()
    return None if value in MISSING else value


def _float(raw: dict[str, str], key: str) -> float | None:
    value = raw.get(key, "").strip()
    if value in MISSING:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(raw: dict[str, str], key: str) -> int | None:
    value = raw.get(key, "").strip()
    if value in MISSING:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            as_float = float(value)
        except ValueError:
            return None
        return int(as_float) if as_float.is_integer() else None


def _str_list(
    raw: dict[str, str], key: str, flags: list[str], invalid_flag: str
) -> tuple[list[str], str | None]:
    """Parse an R-style list column; never raises. Original kept on failure."""
    original = raw.get(key, "")
    try:
        items = parse_list(original)
    except ValueError:
        flags.append(invalid_flag)
        return [], original
    return [i.strip() for i in items if i is not None and i.strip()], None


def normalize(raw: dict[str, str], vocabulary: set[str]) -> dict[str, Any]:
    source_id, title = raw.get("RecipeId", "").strip(), raw.get("Name", "").strip()
    if source_id in MISSING or title in MISSING:
        raise ValueError("missing_identity")
    parts = parse_list(raw.get("RecipeIngredientParts", ""))
    steps = parse_list(raw.get("RecipeInstructions", ""))
    if not parts or not steps or any(not x or not x.strip() for x in [*parts, *steps]):
        raise ValueError("incomplete_ingredients_or_steps")
    flags: list[str] = []
    quality_issues: list[dict[str, Any]] = []
    try:
        amounts = parse_list(raw.get("RecipeIngredientQuantities", ""))
    except ValueError:
        amounts = []
        flags.append("invalid_quantities")
        quality_issues.append(
            issue(
                "invalid_quantities",
                "warning",
                "RecipeIngredientQuantities",
                "Unparseable quantity list; no amounts assigned.",
            )
        )
    aligned = len(parts) == len(amounts)
    if not aligned:
        flags.append("ingredient_quantity_mismatch")
        quality_issues.append(
            issue(
                "ingredient_quantity_mismatch",
                "warning",
                "RecipeIngredientQuantities",
                f"{len(parts)} ingredients vs {len(amounts)} quantities; "
                "no partial alignment performed.",
            )
        )
    ingredients = []
    for index, part in enumerate(parts):
        if part is None:
            raise ValueError("incomplete_ingredients_or_steps")
        name = canonical(part)
        epicure = name.replace(" ", "_")
        amount_text = amounts[index] if aligned else None
        ingredients.append(
            {
                "original": part,
                "canonical": name,
                "epicure_id": epicure if epicure in vocabulary else None,
                "quantity_text": amount_text,
                "amount": quantity(amount_text),
                "unit": None,
            }
        )
    # This source has no reliable per-ingredient unit column. Never infer units.
    flags.append("units_unknown")
    quality_issues.append(
        issue(
            "units_unknown",
            "info",
            "ingredients.unit",
            "Food.com source provides no reliable per-ingredient unit column.",
        )
    )
    if any(i["amount"] is None for i in ingredients):
        flags.append("quantity_unknown")
        quality_issues.append(
            issue(
                "quantity_unknown",
                "warning",
                "ingredients.amount",
                "At least one ingredient amount is missing or unparseable.",
            )
        )
    if any(i["epicure_id"] is None for i in ingredients):
        flags.append("epicure_unmapped")
        quality_issues.append(
            issue(
                "epicure_unmapped",
                "info",
                "ingredients.epicure_id",
                "At least one ingredient has no exact Epicure vocabulary match.",
            )
        )
    durations = {}
    for key in ("PrepTime", "CookTime", "TotalTime"):
        try:
            durations[key] = minutes(raw.get(key, ""))
        except ValueError:
            durations[key] = None
            flags.append(f"invalid_{key}")
            quality_issues.append(
                issue(
                    f"invalid_{key}",
                    "warning",
                    key,
                    "Unparseable ISO-8601 duration; retained as unknown. "
                    "Long CookTime values are reported as-is, never "
                    "reinterpreted as active time.",
                )
            )
    serving = quantity(raw.get("RecipeServings"))
    if serving is None:
        flags.append("servings_unknown")
    description = _text(raw, "Description")
    if description is None:
        flags.append("description_unknown")
    images, images_raw = _str_list(raw, "Images", flags, "invalid_images")
    if "invalid_images" in flags:
        quality_issues.append(
            issue("invalid_images", "warning", "Images", "Unparseable image list.")
        )
    flags.append("images_present" if images else "images_unknown")
    keywords, keywords_raw = _str_list(raw, "Keywords", flags, "invalid_keywords")
    if "invalid_keywords" in flags:
        quality_issues.append(
            issue(
                "invalid_keywords",
                "warning",
                "Keywords",
                "Unparseable keyword list.",
            )
        )
    if keywords:
        flags.append("keywords_present")
    else:
        flags.append("keywords_unknown")
    servings_float = float(Fraction(serving)) if serving else None
    legacy_nutrition, nutrition_observations, nutrition_issues = observe_foodcom_nutrition(
        raw, serving_amount=servings_float, serving_unit=None
    )
    quality_issues.extend(nutrition_issues)
    has_nutrition = any(v is not None for v in legacy_nutrition.values())
    flags.append("nutrition_present" if has_nutrition else "nutrition_unknown")
    rating = _float(raw, "AggregatedRating")
    reviews = _int(raw, "ReviewCount")
    rating_raw = raw.get("AggregatedRating", "").strip()
    reviews_raw = raw.get("ReviewCount", "").strip()
    if (rating is None and rating_raw not in MISSING) or (
        reviews is None and reviews_raw not in MISSING
    ):
        quality_issues.append(
            issue(
                "invalid_ratings",
                "warning",
                "AggregatedRating/ReviewCount",
                "Unparseable rating or review count; kept as unknown.",
            )
        )
    if rating is None and reviews is None:
        flags.append("ratings_unknown")
    available_fields = {
        "description": description is not None,
        "images": bool(images),
        "keywords": bool(keywords),
        "nutrition": has_nutrition,
        "ratings": not (rating is None and reviews is None),
        "servings": servings_float is not None,
        "durations": {key: value is not None for key, value in durations.items()},
        "author": _text(raw, "AuthorName") is not None or _text(raw, "AuthorId") is not None,
        "category": _text(raw, "RecipeCategory") is not None,
    }
    structural_issue = (not aligned) or ("invalid_quantities" in flags)
    capabilities = capabilities_for(
        has_identity=True,
        has_ingredients=bool(ingredients),
        has_instructions=bool(steps),
        quantities_validated=False,  # units never known for this source
        servings_known=servings_float is not None,
        durations_known=durations.get("TotalTime") is not None,
        structural_issue=structural_issue,
    )
    content = json.dumps([canonical(title), [i["canonical"] for i in ingredients], steps])
    return {
        "source_id": source_id,
        "title": title,
        "description": description,
        "ingredients": ingredients,
        "instructions": steps,
        "durations_minutes": durations,
        "servings": servings_float,
        "media": {"images": images, "images_raw": images_raw},
        "meta": {
            "author_id": _text(raw, "AuthorId"),
            "author_name": _text(raw, "AuthorName"),
            "category": _text(raw, "RecipeCategory"),
            "keywords": keywords,
            "keywords_raw": keywords_raw,
            "date_published": _text(raw, "DatePublished"),
            "barcode": _text(raw, "Barcode"),
            "recipe_yield_text": _text(raw, "RecipeYield"),
        },
        "ratings": {"aggregated": rating, "review_count": reviews},
        "nutrition": legacy_nutrition,
        "nutrition_observations": nutrition_observations,
        "flags": flags,
        "quality_issues": quality_issues,
        "available_fields": available_fields,
        "capabilities": capabilities,
        "search_document_version": SEARCH_DOCUMENT_VERSION,
        "searchable": capabilities["searchable"],
        "scalable": capabilities["scalable"],
        "final_recipe_eligible": capabilities["complete_eligible"],
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "source_url": None,
        "raw": raw,
    }
