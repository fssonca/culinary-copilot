#!/usr/bin/env python3
"""Data profile and category map for reference envelopes (Milestone 4, R1).

Read-only. Assigns each stored recipe a coarse category and, where the
method matters for proportions, a subtype (rule-based on title,
ingredients and directions; every assignment names its rule). Then
profiles, per dataset and category, what proportion checks could use:

- ingredient lines with an amount and a unit in the mass or volume
  family (the only lines comparable after normalization);
- lines with a count or other unit, lines with no amount, amounts the
  ingestion parser cannot read;
- recipes with stated servings;
- recipes with at least ``--min-measured`` measured lines
  ("ratio-ready"), counted once per near-duplicate group (same
  normalized title and ingredient set).

Outputs under ``--out-dir`` (default ``data/m4-r1/``, gitignored):
``category_map.jsonl`` (one row per recipe: ids, category, subtype,
rule) and ``data_profile.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "m4-r1"

MASS_UNITS = frozenset({"mg", "g", "kg", "oz", "lb"})
VOLUME_UNITS = frozenset(
    {"ml", "cl", "l", "tsp", "tbsp", "fl_oz", "cup", "pint", "quart", "gallon"}
)

#: (category, title pattern), first match wins. Order puts specific
#: dishes before broad words ("cheesecake" before "cake").
CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    ("beverage", r"\b(smoothie|punch|lemonade|cocktail|latte|tea|coffee|shake|sangria)\b"),
    ("cheesecake", r"cheesecake"),
    ("cupcake", r"cupcake"),
    ("cake", r"\bcake\b|torte|gateau"),
    ("bar_cookie", r"\b(bars?|brownies?|blondies?|squares)\b"),
    ("cookie", r"cookie|biscotti|shortbread|snickerdoodle|macaroon"),
    ("muffin", r"muffin"),
    ("scone_biscuit", r"\b(scones?|biscuits?)\b"),
    ("pancake_waffle", r"pancake|waffle|crepe"),
    ("pie_tart", r"\b(pies?|tarts?|tartlets?|galette|cobbler|crisp|crumble)\b"),
    ("bread", r"\b(bread|loaf|rolls?|buns?|focaccia|bagels?|brioche|challah|naan|pita)\b"),
    ("custard_pudding", r"pudding|custard|flan|creme brulee|crème brûlée|panna cotta|mousse"),
    ("frozen_dessert", r"ice cream|sorbet|gelato|popsicle|semifreddo|granita"),
    ("candy", r"fudge|candy|truffles?|brittle|toffee|caramels?"),
    ("soup_stew", r"\b(soup|stew|chowder|chili|bisque|gumbo|broth)\b"),
    ("curry", r"\bcurry\b|masala|korma|vindaloo"),
    ("salad", r"\bsalad\b|slaw"),
    ("sauce_dressing", r"\b(sauce|dressing|vinaigrette|gravy|salsa|pesto|dip|aioli)\b"),
    ("pasta", r"pasta|spaghetti|lasagn|macaroni|penne|fettuccine|linguine|noodle|ravioli"),
    ("rice_grain", r"\b(rice|risotto|pilaf|quinoa|couscous|paella|biryani)\b"),
    ("casserole", r"casserole|bake\b|gratin"),
    ("roast_braise", r"\b(roast|roasted|braised?|pot roast|brisket|ribs)\b"),
    ("stir_fry", r"stir.?fry"),
    ("egg_dish", r"omelet|frittata|quiche|scrambled|deviled eggs"),
)

#: Method subtypes for bakes, from ingredients and directions.
YEAST_PATTERN = re.compile(r"\byeast\b")


def _words_text(doc: dict[str, Any]) -> tuple[str, str]:
    title = str(doc.get("title") or "").lower()
    names = " ".join(
        str(i.get("canonical") or i.get("name") or "").lower()
        for i in (doc.get("ingredients") or [])
        if isinstance(i, dict)
    )
    return title, names


def categorize(doc: dict[str, Any]) -> tuple[str, str, str]:
    """(category, subtype, rule) for one recipe document."""
    title, names = _words_text(doc)
    category = "unknown"
    rule = "no title rule matched"
    for name, pattern in CATEGORY_RULES:
        if re.search(pattern, title):
            category, rule = name, f"title matches /{pattern}/"
            break
    subtype = ""
    if category == "cake":
        has_butter = re.search(r"\bbutter\b|margarine|shortening", names) is not None
        has_oil = re.search(r"\boil\b", names) is not None
        if re.search(r"angel food|chiffon|sponge|genoise", title):
            subtype = "foam"
        elif "pound" in title:
            subtype = "pound"
        elif re.search(r"cake mix", names):
            subtype = "from_mix"
        elif has_butter:
            subtype = "butter"
        elif has_oil:
            subtype = "oil"
        else:
            subtype = "other"
    elif category == "bread":
        subtype = "yeast" if YEAST_PATTERN.search(names) else "quick"
    elif category == "cookie":
        subtype = "rolled" if re.search(r"roll(ed)? out|cookie cutter", title) else "drop_or_other"
    return category, subtype, rule


def _unit_family(unit: Any) -> str:
    from culinary_copilot.recipes.llm_validate import canonical_unit

    code = canonical_unit(str(unit)) if isinstance(unit, str) and unit.strip() else None
    if code in MASS_UNITS:
        return "mass"
    if code in VOLUME_UNITS:
        return "volume"
    if code is None and not (isinstance(unit, str) and unit.strip()):
        return "none"
    return "other"


def profile_lines(doc: dict[str, Any]) -> Counter[str]:
    """Per-line measurement classes for one recipe."""
    from culinary_copilot.recipes.normalize import quantity

    counts: Counter[str] = Counter()
    for entry in doc.get("ingredients") or []:
        if not isinstance(entry, dict):
            continue
        counts["lines"] += 1
        raw = entry.get("amount")
        raw_text = entry.get("amount_text")
        if raw in (None, "") and raw_text in (None, ""):
            counts["no_amount"] += 1
            continue
        parsed = quantity(str(raw if raw not in (None, "") else raw_text))
        if parsed is None:
            counts["unparseable_amount"] += 1
            continue
        family = _unit_family(entry.get("unit"))
        counts[f"amount_{family}_unit"] += 1
    return counts


def duplicate_key(doc: dict[str, Any]) -> str:
    title, _ = _words_text(doc)
    names = sorted(
        {
            str(i.get("canonical") or i.get("name") or "").strip().lower()
            for i in (doc.get("ingredients") or [])
            if isinstance(i, dict)
        }
    )
    payload = json.dumps([" ".join(title.split()), names])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--min-measured", type=int, default=3)
    parser.add_argument("--database-url", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from sqlalchemy import create_engine, text

    from culinary_copilot.config import Settings

    args = _args(argv)
    url = args.database_url or Settings().database_url.get_secret_value()
    engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    recipes: Counter[tuple[str, str]] = Counter()
    servings: Counter[tuple[str, str]] = Counter()
    ready_groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    subtypes: Counter[tuple[str, str]] = Counter()
    with (
        engine.connect() as conn,
        (out_dir / "category_map.jsonl").open("w", encoding="utf-8") as sink,
    ):
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(text("SELECT dataset_id, source_id, document FROM recipes"))
        for dataset_id, source_id, doc in rows:
            if not isinstance(doc, dict):
                continue
            category, subtype, rule = categorize(doc)
            key = (str(dataset_id), category)
            recipes[key] += 1
            if subtype:
                subtypes[(category, subtype)] += 1
            counts = profile_lines(doc)
            lines[key].update(counts)
            if doc.get("servings"):
                servings[key] += 1
            dup = duplicate_key(doc)
            groups[key].add(dup)
            measured = counts["amount_mass_unit"] + counts["amount_volume_unit"]
            if measured >= args.min_measured:
                ready_groups[key].add(dup)
            sink.write(
                json.dumps(
                    {
                        "dataset_id": dataset_id,
                        "source_id": source_id,
                        "category": category,
                        "subtype": subtype,
                        "rule": rule,
                    }
                )
                + "\n"
            )
    table: list[dict[str, Any]] = []
    for key in sorted(recipes):
        ds, category = key
        table.append(
            {
                "dataset_id": ds,
                "category": category,
                "recipes": recipes[key],
                "distinct_recipes": len(groups[key]),
                "ratio_ready_distinct": len(ready_groups[key]),
                "with_servings": servings[key],
                "lines": dict(lines[key]),
            }
        )
    profile = {
        "min_measured_lines": args.min_measured,
        "by_dataset_category": table,
        "subtypes": [
            {"category": c, "subtype": s, "recipes": n} for (c, s), n in sorted(subtypes.items())
        ],
    }
    (out_dir / "data_profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    for row in table:
        print(
            str(row["dataset_id"])[:8],
            row["category"],
            row["recipes"],
            row["distinct_recipes"],
            row["ratio_ready_distinct"],
            row["with_servings"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
