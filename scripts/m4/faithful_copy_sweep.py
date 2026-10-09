#!/usr/bin/env python3
"""Faithful-copy sweep of the plan checks (Milestone 4, R1; read-only).

For every stored recipe, build plans copied from the recipe itself and
run the plan checks the agent loop runs. Failures are split in two:

- attribution: a faithful copy must pass these (structured quantity
  claims, prose quantity attribution, the source label, fidelity
  wording). A failure is a candidate false positive or a source
  inconsistency, to be classified before any check changes.
- eligibility: checks that may rightly reject a faithful copy (a raw
  protein without a cited food-safety chunk).

Three mise-en-place variants per recipe, the steps always the stored
directions verbatim with every index cited:

- ``original``: each ingredient's original line, verbatim;
- ``lines``: one "AMOUNT UNIT INGREDIENT" line per ingredient;
- ``list``: one "Gather AMOUNT UNIT INGREDIENT, ..." line, the shape
  live plans used.

Reads run in a READ ONLY transaction. Outputs go to ``--out-dir``
(default ``data/m4-r1/``, gitignored): ``faithful_copy_failures.jsonl``
(one row per failing check, no recipe text beyond the failing line) and
``faithful_copy_summary.json``.

Usage (repo root)::

    uv run python scripts/m4/faithful_copy_sweep.py
    uv run python scripts/m4/faithful_copy_sweep.py --limit 500
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "m4-r1"
VARIANTS = ("original", "lines", "list")

#: Error-text buckets, in match order. Attribution unless listed in
#: ELIGIBILITY_BUCKETS.
BUCKETS: tuple[tuple[str, str], ...] = (
    ("structured_quantity", r"not in source \(invented\)"),
    ("prose_wrong_ingredient", r"but the source states '.+' as"),
    ("prose_no_amount", r"but the source states no amount for"),
    ("prose_ambiguous", r"the amount is ambiguous between"),
    ("prose_unattributed", r"no source ingredient matches that amount"),
    ("food_safety", r"food.safety|safe internal|technique_refs"),
    ("fidelity", r"claims fidelity|follow, match or reproduce"),
    ("omitted_directions", r"omitted direction|unread direction"),
)
ELIGIBILITY_BUCKETS = frozenset({"food_safety"})


def bucket_for(error: str) -> str:
    """The bucket an error string belongs to ("other" when none)."""
    for name, pattern in BUCKETS:
        if re.search(pattern, error, flags=re.IGNORECASE):
            return name
    return "other"


def _amount_text(entry: dict[str, Any]) -> str:
    """The amount as get_recipe shows it to the model (``readable_amount``)."""
    from culinary_copilot.agent.loop import readable_amount

    shown = readable_amount(entry)
    return "" if shown is None else str(shown).strip()


def _ingredient_name(entry: dict[str, Any]) -> str:
    return str(entry.get("canonical") or entry.get("name") or "").strip()


def _measured_phrase(entry: dict[str, Any]) -> str:
    parts = [_amount_text(entry), str(entry.get("unit") or "").strip(), _ingredient_name(entry)]
    return " ".join(part for part in parts if part)


def build_plan(doc: dict[str, Any], pair: tuple[str, str], variant: str) -> dict[str, Any]:
    """A plan copied from the recipe itself (see the module docstring)."""
    from culinary_copilot.agent.validate import _ingredient_entries, doc_directions

    entries = [e for e in _ingredient_entries(doc) if _ingredient_name(e)]
    if variant == "original":
        mise = [
            str(e.get("original") or "").strip() or _measured_phrase(e)
            for e in entries
            if (str(e.get("original") or "").strip() or _measured_phrase(e))
        ]
    elif variant == "lines":
        mise = [_measured_phrase(e) for e in entries]
    else:
        mise = ["Gather " + ", ".join(_measured_phrase(e) for e in entries) + "."]
    directions = [str(d) for d in doc_directions(doc)]
    quantities = [
        {
            "ingredient": _ingredient_name(e),
            "amount": _amount_text(e),
            "unit": e.get("unit") if isinstance(e.get("unit"), str) and e.get("unit") else None,
        }
        for e in entries
        if _amount_text(e)
    ][:20]
    return {
        "source": {"dataset_id": pair[0], "source_id": pair[1]},
        "mise_en_place": [line for line in mise if line] or ["Gather the ingredients."],
        "steps": directions or ["Cook."],
        "step_sources": list(range(len(directions))) if directions else [None],
        "plating": "Serve.",
        "quantities": quantities,
        "adaptations": [],
        "technique_refs": [],
    }


def check_plan(doc: dict[str, Any], pair: tuple[str, str], plan: dict[str, Any]) -> list[str]:
    """Run the loop's plan checks; return error strings plus a label error."""
    from culinary_copilot.agent.validate import (
        check_plan_evidence,
        doc_directions,
        plan_fidelity_errors,
        validate_omitted_directions,
        validate_plan,
    )

    errors = list(
        validate_plan(
            plan,
            selected_dish={"dataset_id": pair[0], "source_id": pair[1]},
            resolve=lambda ds, sid: doc,
            full={pair},
        )
    )
    evidence_errors, steps_source = check_plan_evidence(plan, doc, [], auto_label=True)
    errors.extend(evidence_errors)
    covered = set(range(len(doc_directions(doc))))
    errors.extend(validate_omitted_directions(plan, doc, covered, steps_source))
    errors.extend(plan_fidelity_errors(plan, "", steps_source))
    if doc_directions(doc) and steps_source != "source":
        errors.append(f"label: faithful copy labelled {steps_source}")
    return errors


def _bucket_with_label(error: str) -> str:
    return "label_not_source" if error.startswith("label: ") else bucket_for(error)


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--limit", type=int, default=0, help="stop after N recipes (0 = all)")
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
    counts: Counter[tuple[str, str, str, str]] = Counter()
    recipes_failing: Counter[tuple[str, str, str]] = Counter()
    recipes: Counter[str] = Counter()
    with (
        engine.connect() as conn,
        (out_dir / "faithful_copy_failures.jsonl").open("w", encoding="utf-8") as sink,
    ):
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(
            text(
                "SELECT dataset_id, source_id, document FROM recipes ORDER BY dataset_id, source_id"
            )
        )
        for index, (dataset_id, source_id, doc) in enumerate(rows):
            if args.limit and index >= args.limit:
                break
            if not isinstance(doc, dict):
                continue
            pair = (str(dataset_id), str(source_id))
            recipes[pair[0]] += 1
            for variant in VARIANTS:
                errors = check_plan(doc, pair, build_plan(doc, pair, variant))
                failed_kinds: set[str] = set()
                for error in errors:
                    bucket = _bucket_with_label(error)
                    kind = "eligibility" if bucket in ELIGIBILITY_BUCKETS else "attribution"
                    failed_kinds.add(kind)
                    counts[(pair[0], variant, kind, bucket)] += 1
                    sink.write(
                        json.dumps(
                            {
                                "dataset_id": pair[0],
                                "source_id": pair[1],
                                "variant": variant,
                                "kind": kind,
                                "bucket": bucket,
                                "error": error[:400],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                for kind in failed_kinds:
                    recipes_failing[(pair[0], variant, kind)] += 1
    summary = {
        "recipes": dict(recipes),
        "recipes_failing": [
            {"dataset_id": ds, "variant": v, "kind": k, "recipes": n}
            for (ds, v, k), n in sorted(recipes_failing.items())
        ],
        "errors_by_bucket": [
            {"dataset_id": ds, "variant": v, "kind": k, "bucket": b, "errors": n}
            for (ds, v, k, b), n in sorted(counts.items())
        ],
    }
    (out_dir / "faithful_copy_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary["recipes_failing"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
