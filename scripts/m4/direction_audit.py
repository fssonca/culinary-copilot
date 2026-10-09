#!/usr/bin/env python3
"""Per-direction audit for non-instructions (Milestone 4, R1; read-only).

Stored directions sometimes hold text that is not a cooking step: an
author's name, a site name, a placeholder, a section heading, a
sign-off. Plan coverage counts every stored direction, so a faithful
plan of such a recipe cannot be labelled ``source``. This audit
classifies candidate directions per (recipe, direction index), with the
rule that decided each call, so a later flag can be applied per index
rather than per recipe.

Candidates are directions of at most ``--max-words`` words, plus any
direction matching a credit or site pattern at any length. Classes:

- ``step``: names a cooking action or state (``serve``, ``stir``,
  ``cool``, ...); a real step even when short.
- ``site_name``, ``placeholder``, ``heading``, ``sign_off``,
  ``credit_phrase``: non-instructions matched by an explicit rule.
- ``name_like``: no cooking word, short, letters only; most are author
  names, but the class needs review before any flag.
- ``yield_note``: servings or yield text.
- ``unclear``: none of the above; manual review.

Outputs under ``--out-dir`` (default ``data/m4-r1/``, gitignored):
``direction_audit.jsonl`` (one row per candidate, text truncated) and
``direction_audit_summary.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "m4-r1"

#: Words that make a short direction a real step.
COOKING_WORDS = frozenset(
    """
    add bake beat blend boil braise broil brown brush chill chop coat combine cook cool
    cover cream crumble cut dice dip drain drizzle dust enjoyable fill flip fold freeze
    fry garnish gather grate grease grill heat knead layer let marinate mash measure melt
    microwave mix pour preheat press puree reduce refrigerate repeat rest rinse roast roll
    saute sauté scoop season serve set shake sift simmer slice soak spoon spread
    sprinkle stand steam stir store strain stuff toast top toss transfer turn whip whisk
    wrap warm cold hot immediately ingredients well done aside overnight chilled
    oven check use bring place remove return
    """.split()
)
SITE_PATTERN = re.compile(
    r"allrecipes|food\.com|foodnetwork|epicurious|https?://|www\.|\.com\b", re.IGNORECASE
)
CREDIT_PATTERN = re.compile(
    r"\b(recipe|photo|photos|submitted|adapted|courtesy|contributed)\s+(by|of|from)\b",
    re.IGNORECASE,
)
PLACEHOLDERS = frozenset({"unknown", "n/a", "na", "none", "null", "-", "--", "tbd", "."})
SIGN_OFFS = frozenset(
    {"enjoy", "enjoy!", "bon appetit", "bon appétit", "yum", "yummy", "eat", "delicious"}
)
YIELD_PATTERN = re.compile(r"\b(serves|servings?|makes|yields?)\b[^.]*\d", re.IGNORECASE)
NOTE_HEADING_PATTERN = re.compile(r"^[\w’' ]{0,30}\b(notes?|tips?|variations?)\b[\s:.]*$")
USERNAME_PATTERN = re.compile(r"^[a-z_]+\d+[a-z_\d]*$")


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿ']+", text)


def classify(direction: str) -> tuple[str, str]:
    """(class, rule) for one direction text."""
    text = " ".join(str(direction).split())
    lowered = text.lower().strip()
    bare = lowered.strip(" .!")
    words = [w.lower() for w in _words(text)]
    if SITE_PATTERN.search(text):
        return "site_name", "site pattern"
    if CREDIT_PATTERN.search(text):
        return "credit_phrase", "credit pattern"
    if bare in PLACEHOLDERS or not words:
        return "placeholder", "placeholder word or no letters"
    if lowered in SIGN_OFFS or bare in SIGN_OFFS:
        return "sign_off", "sign-off word"
    if YIELD_PATTERN.search(text):
        return "yield_note", "yield word with a number"
    if NOTE_HEADING_PATTERN.match(lowered):
        return "heading", "note or tip heading"
    if USERNAME_PATTERN.match(bare):
        return "name_like", "single username-like token with digits"
    if lowered.endswith(":") and len(words) <= 5:
        return "heading", "ends with a colon"
    if any(w in COOKING_WORDS for w in words):
        return "step", "cooking word"
    if len(words) <= 3 and not re.search(r"\d", text):
        return "name_like", "short, no cooking word, no digits"
    return "unclear", "no rule matched"


def is_candidate(direction: str, max_words: int) -> bool:
    text = str(direction)
    return (
        len(_words(text)) <= max_words
        or bool(SITE_PATTERN.search(text))
        or bool(CREDIT_PATTERN.search(text))
    )


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--max-words", type=int, default=3)
    parser.add_argument("--database-url", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from sqlalchemy import create_engine, text

    from culinary_copilot.agent.validate import doc_directions
    from culinary_copilot.config import Settings

    args = _args(argv)
    url = args.database_url or Settings().database_url.get_secret_value()
    engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_class: Counter[tuple[str, str]] = Counter()
    recipes_with: Counter[tuple[str, str]] = Counter()
    distinct: dict[str, Counter[str]] = {}
    position: Counter[tuple[str, str]] = Counter()
    with (
        engine.connect() as conn,
        (out_dir / "direction_audit.jsonl").open("w", encoding="utf-8") as sink,
    ):
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(text("SELECT dataset_id, source_id, document FROM recipes"))
        for dataset_id, source_id, doc in rows:
            if not isinstance(doc, dict):
                continue
            directions = doc_directions(doc)
            seen: set[str] = set()
            for index, direction in enumerate(directions):
                if not is_candidate(direction, args.max_words):
                    continue
                cls, rule = classify(direction)
                by_class[(str(dataset_id), cls)] += 1
                seen.add(cls)
                distinct.setdefault(cls, Counter())[" ".join(str(direction).lower().split())] += 1
                where = (
                    "last" if index == len(directions) - 1 else "first" if index == 0 else "middle"
                )
                position[(cls, where)] += 1
                sink.write(
                    json.dumps(
                        {
                            "dataset_id": dataset_id,
                            "source_id": source_id,
                            "index": index,
                            "of": len(directions),
                            "class": cls,
                            "rule": rule,
                            "text": str(direction)[:80],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            for cls in seen:
                recipes_with[(str(dataset_id), cls)] += 1
    summary = {
        "max_words": args.max_words,
        "directions_by_class": [
            {"dataset_id": ds, "class": c, "directions": n}
            for (ds, c), n in sorted(by_class.items())
        ],
        "recipes_by_class": [
            {"dataset_id": ds, "class": c, "recipes": n}
            for (ds, c), n in sorted(recipes_with.items())
        ],
        "distinct_texts_by_class": {c: len(v) for c, v in sorted(distinct.items())},
        "position_by_class": [
            {"class": c, "position": p, "directions": n} for (c, p), n in sorted(position.items())
        ],
    }
    (out_dir / "direction_audit_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
