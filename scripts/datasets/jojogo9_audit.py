"""Read-only jojogo9/Food_Recipes audit (Workstream 3B–3C). No bulk import.

Usage (from repo root)::

    uv run --with pyarrow python scripts/datasets/jojogo9_audit.py

Reads the cached ``food_recipes.parquet`` (or ``--parquet PATH``), the local
Food.com preview ``normalized.jsonl`` and the odunola pilot artifacts. Writes
``data/jojogo9-audit/audit-report.json`` (ignored). Full-file scalar counts
are exact; list-parsing findings are sampled and labeled as such.

Development tool: lives outside ``src/``; production modules never import
from ``scripts/``.
"""

import argparse
import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any

SAMPLE_SIZE = 2000
SAMPLE_SEED = 7
MAX_FIELD_CHARS = 200_000
MAX_ELEMENTS = 500


def safe_list(value: Any, *, element: type) -> tuple[list[Any] | None, str | None]:
    """Bounded safe literal parser. Returns (parsed, problem_code)."""
    if value is None:
        return None, "missing"
    if not isinstance(value, str):
        return None, "not_a_string"
    if len(value) > MAX_FIELD_CHARS:
        return None, "field_too_large"
    text = value.strip()
    if text in {"", "NA", "NULL", "[]"}:
        return [], None
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError):
        return None, "invalid_literal"
    if not isinstance(parsed, list):
        return None, "not_a_list"
    if len(parsed) > MAX_ELEMENTS:
        return None, "too_many_elements"
    for item in parsed:
        if not isinstance(item, element):
            return None, "bad_element"
        if element is str:
            assert isinstance(item, str)
            if not item.strip():
                return None, "bad_element"
    return list(parsed), None


def main() -> None:
    import pyarrow.parquet as pq  # type: ignore[import-not-found]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parquet",
        type=Path,
        default=Path("data/.tmp-jojogo9-inspect/food_recipes.parquet"),
    )
    parser.add_argument("--output", type=Path, default=Path("data/jojogo9-audit"))
    parser.add_argument("--sample", type=int, default=SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=SAMPLE_SEED)
    args = parser.parse_args()

    table = pq.read_table(args.parquet)
    assert table.num_rows > 0
    cols = {name: table.column(name).to_pylist() for name in table.schema.names}
    total = table.num_rows
    ids = cols["id"]
    unique_ids = len(set(ids))
    missing_titles = sum(1 for v in cols["name"] if not (v or "").strip())
    missing_ing = sum(1 for v in cols["ingredients"] if not (v or "").strip())
    missing_steps = sum(1 for v in cols["steps"] if not (v or "").strip())
    minutes = [m for m in cols["minutes"] if isinstance(m, int)]
    anomalies = {
        "minutes_negative": sum(1 for m in minutes if m < 0),
        "minutes_zero": sum(1 for m in minutes if m == 0),
        "minutes_over_24h": sum(1 for m in minutes if m > 1440),
        "minutes_over_7d": sum(1 for m in minutes if m > 10080),
        "minutes_max": max(minutes) if minutes else None,
    }
    # Deterministic stride sample for deep parsing.
    stride = max(total // args.sample, 1)
    picked = sorted({((args.seed + i * stride) % total) for i in range(args.sample)})
    problems: Counter[str] = Counter()
    nut_lengths: Counter[int] = Counter()
    step_mismatch = ing_mismatch = 0
    empty_parsed = Counter[str]()
    fingerprints: Counter[str] = Counter()
    titles: Counter[str] = Counter()
    int_ids: set[int] = set()
    import hashlib

    from culinary_copilot.recipes.normalize import canonical

    for pos in picked:
        tags, p1 = safe_list(cols["tags"][pos], element=str)
        nutrition, p2 = _safe_numbers(cols["nutrition"][pos])
        steps, p3 = safe_list(cols["steps"][pos], element=str)
        ingredients, p4 = safe_list(cols["ingredients"][pos], element=str)
        for field, problem in (("tags", p1), ("nutrition", p2), ("steps", p3), ("ingredients", p4)):
            if problem:
                problems[f"{field}:{problem}"] += 1
        if nutrition is not None:
            nut_lengths[len(nutrition)] += 1
            if any(v < 0 for v in nutrition):
                problems["nutrition:negative_value"] += 1
        if steps is not None and cols["n_steps"][pos] != len(steps):
            step_mismatch += 1
        if ingredients is not None and cols["n_ingredients"][pos] != len(ingredients):
            ing_mismatch += 1
        for field, parsed in (("steps", steps), ("ingredients", ingredients), ("tags", tags)):
            if parsed == []:
                empty_parsed[field] += 1
        if isinstance(cols["id"][pos], int):
            int_ids.add(cols["id"][pos])
        if steps is not None and ingredients is not None:
            payload = json.dumps(
                [canonical(cols["name"][pos] or ""), [canonical(i) for i in ingredients], steps]
            )
            fingerprints[hashlib.sha256(payload.encode()).hexdigest()] += 1
            titles[canonical(cols["name"][pos] or "")] += 1

    # Overlap vs current Food.com corpus (preview fingerprints; v2/v3 share the
    # same content-hash inputs: normalized title + ingredient names + steps).
    corpus_hashes: set[str] = set()
    corpus_int_ids: set[int] = set()
    preview = Path("data/recipe-preview/normalized.jsonl")
    if preview.exists():
        with preview.open() as stream:
            for line in stream:
                record = json.loads(line)
                corpus_hashes.add(record["content_hash"])
                try:
                    corpus_int_ids.add(int(record["source_id"]))
                except ValueError:
                    pass
    pilot_hashes: set[str] = set()
    pilot_preview = Path("data/odunola-pilot/normalized.jsonl")
    if pilot_preview.exists():
        with pilot_preview.open() as stream:
            for line in stream:
                pilot_hashes.add(json.loads(line)["content_hash"])
    sample_hashes = set(fingerprints)
    exact_in_corpus = len(sample_hashes & corpus_hashes)
    title_overlap = sum(1 for t in titles if t in _corpus_titles())
    id_overlap_full = len(set(ids) & corpus_int_ids) if corpus_int_ids else 0

    report = {
        "source": {
            "dataset_id": "jojogo9/Food_Recipes",
            "file": str(args.parquet),
            "rows_full_file": total,
            "columns": table.schema.names,
            "license": None,
            "card": None,
            "note": "No README/license in repo (verified 2026-09-22). "
            "Schema matches Kaggle shuyangli94 RAW_recipes.csv (unverified page render).",
        },
        "full_file_counts": {
            "rows": total,
            "unique_ids": unique_ids,
            "duplicate_ids": total - unique_ids,
            "missing_titles": missing_titles,
            "missing_ingredients_raw": missing_ing,
            "missing_steps_raw": missing_steps,
            **anomalies,
        },
        "sampled_parse": {
            "sampled": len(picked),
            "seed": args.seed,
            "problems": dict(problems),
            "nutrition_lengths": dict(nut_lengths),
            "n_steps_mismatch": step_mismatch,
            "n_ingredients_mismatch": ing_mismatch,
            "empty_parsed": dict(empty_parsed),
            "duplicate_fingerprints_in_sample": sum(c - 1 for c in fingerprints.values() if c > 1),
        },
        "structure_notes": {
            "quantities": "Ingredient entries are bare names; no quantity columns exist.",
            "servings": "No servings column exists.",
            "nutrition_semantics": "7-element float list per row (sampled lengths: "
            f"{dict(nut_lengths)}). Labels/units are NOT stored in this repo; "
            "positional semantics ([kcal, fat_PDV, sugar_PDV, sodium_PDV, "
            "protein_PDV, satfat_PDV, carbs_PDV] per serving, 2000-kcal diet) "
            "come from the related tiptoghosh/food-recipes-15k README only and "
            "must not be treated as verified for this repo until corroborated.",
        },
        "overlap": {
            "method": "Full-file int-ID overlap + sampled fingerprint overlap. "
            "Title equality alone is NOT duplication evidence.",
            "id_overlap_full_file": id_overlap_full,
            "exact_fingerprint_matches_in_sample": exact_in_corpus,
            "title_matches_in_sample_needing_review": title_overlap,
            "pilot_fingerprint_matches_in_sample": len(sample_hashes & pilot_hashes),
            "corpus_size": len(corpus_hashes),
        },
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "audit-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def _safe_numbers(value: Any) -> tuple[list[float] | None, str | None]:
    if value is None:
        return None, "missing"
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        return None, "not_a_string" if not isinstance(value, str) else "field_too_large"
    text = value.strip()
    if text in {"", "NA", "NULL", "[]"}:
        return [], None
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError):
        return None, "invalid_literal"
    if not isinstance(parsed, list) or len(parsed) > MAX_ELEMENTS:
        return None, "not_a_list" if not isinstance(parsed, list) else "too_many_elements"
    numbers: list[float] = []
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None, "bad_element"
        numbers.append(float(item))
        if numbers[-1] != numbers[-1] or numbers[-1] in (float("inf"), float("-inf")):
            return None, "nonfinite_value"
    return numbers, None


def _corpus_titles() -> set[str]:
    from culinary_copilot.recipes.normalize import canonical

    titles: set[str] = set()
    preview = Path("data/recipe-preview/normalized.jsonl")
    if preview.exists():
        with preview.open() as stream:
            for line in stream:
                titles.add(canonical(json.loads(line)["title"]))
    return titles


if __name__ == "__main__":
    main()
