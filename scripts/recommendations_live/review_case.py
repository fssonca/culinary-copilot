"""Print a live recommendation beside its stored source (read-only).

Usage: uv run python scripts/recommendations_live/review_case.py <case-json-path>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from culinary_copilot.config import Settings
from culinary_copilot.db import create_db_engine
from culinary_copilot.recipes.repository import get_recipe


def same(a: Any, b: Any) -> bool:
    """Equal after whitespace normalization (the renderer collapses spaces)."""
    return " ".join(str(a).split()) == " ".join(str(b).split())


def fetch_source(selection: dict[str, Any]) -> dict[str, Any] | None:
    """Read-only exact-pair fetch of the selected source."""
    engine = create_db_engine(Settings())
    try:
        doc: dict[str, Any] | None = get_recipe(
            engine, selection["source_id"], dataset_id=selection["dataset_id"]
        )
        return doc
    finally:
        engine.dispose()


def step_text(step: Any) -> str:
    return str(step.get("text") if isinstance(step, dict) else step)


def review(record: dict[str, Any]) -> int:
    resp = record["response"]
    if resp.get("outcome") != "recommendation":
        print(f"outcome is {resp.get('outcome') or resp.get('detail')}, nothing to review")
        return 1

    sel = resp["selection"]
    recipe = resp["recipe"]
    print(f"== {record['case_id']}: {sel['title']}  ({sel['dataset_id']} / {sel['source_id']})\n")

    source = None if record.get("synthetic_fixture") else fetch_source(sel)
    src_ings = (source or {}).get("ingredients", [])
    src_steps = (source or {}).get("instructions", [])

    ingredients = recipe.get("ingredients", [])
    print("-- INGREDIENTS (response  |  source original)")
    for i, ing in enumerate(ingredients):
        src = src_ings[i].get("original") if i < len(src_ings) else "<missing in source>"
        flag = "" if same(ing.get("original"), src) else "   <-- DIFFERS"
        amount = str(ing.get("amount"))
        unit = str(ing.get("unit"))
        print(f"{ing['ref']:>6}  amount={amount:<8} unit={unit:<6} {ing.get('original')}")
        print(f"{'':>6}  source: {src}{flag}")
    if len(src_ings) > len(ingredients):
        print(f"  !! source has {len(src_ings)} ingredients, response {len(ingredients)}")

    steps = recipe.get("instructions", [])
    print("\n-- STEPS (response  |  source)")
    for i, step in enumerate(steps):
        text = step_text(step)
        src = step_text(src_steps[i]) if i < len(src_steps) else "<missing in source>"
        flag = "" if same(text, src) else "   <-- DIFFERS"
        print(f"{i + 1:>3}. {text}{flag}")
    if len(src_steps) > len(steps):
        print(f"  !! source has {len(src_steps)} steps, response {len(steps)}")

    print("\n-- SERVINGS / DURATIONS in response")
    for key in ("servings", "durations", "durations_minutes", "total_minutes"):
        if key in recipe:
            print(f"  {key}: {recipe[key]}")

    print("\n-- MODEL-WRITTEN (check these for invented facts)")
    print("  reasons:", json.dumps(resp.get("selection_reasons"), ensure_ascii=False, indent=2))
    print("  needs:  ", json.dumps(resp.get("needs"), ensure_ascii=False, indent=2))
    print("\n-- CONSTRAINTS")
    print(json.dumps(resp.get("constraints"), ensure_ascii=False, indent=2))
    print("\n-- EPICURE")
    print(json.dumps(resp.get("epicure"), ensure_ascii=False, indent=2)[:1500])
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2
    record: dict[str, Any] = json.loads(Path(args[0]).read_text())
    return review(record)


if __name__ == "__main__":
    raise SystemExit(main())
