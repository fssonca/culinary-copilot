"""Build the Checkpoint 1 label-calibration sample (14 items, reproducible).

Selection is deterministic over ``review_packet_v2.json``: the lowest
``(dataset_id, source_id)`` recorded candidate matching each stratum
predicate, plus the recorded DEV-15 zero-total override fetched by exact
identity, plus one unlabelled new-case pair and two empty-pool no-match
entries. Nothing here is human-reviewed; verdict boxes are blank for the
owner. This is calibration, not approval of every label.

Usage (from repo root):
    uv run python scripts/retrieval_eval/calibration_sample.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

PACKET = REPO / "evals/results/phase1/review_packet_v2.json"
OUT_MD = REPO / "evals/results/phase1/calibration_sample.md"
OUT_JSON = REPO / "evals/results/phase1/calibration_sample.json"

# (slot, stratum, case_id, dataset_id, source_id-or-None-for-first-match)
SLOTS: list[tuple[str, str, str, str | None, str | None]] = [
    ("S01", "dish + pantry", "DEV-09", "AkashPS11/recipes_data_food.com", "000159"),
    ("S02", "pantry-only", "DEV-06", "AkashPS11/recipes_data_food.com", "001559"),
    ("S03", "time limit (eligible)", "DEV-13", "AkashPS11/recipes_data_food.com", "000322"),
    (
        "S04",
        "zero total excluded by ceiling",
        "DEV-15",
        "AkashPS11/recipes_data_food.com",
        "001379",
    ),
    ("S05", "dietary contradiction", "ANY_FAIL", None, None),
    ("S06", "dietary unknowns (vegan + gluten-free)", "DEV-24", "odunola/foodie", "foodie-015516"),
    ("S07", "dietary unknown (vegetarian)", "HELD-16", "AkashPS11/recipes_data_food.com", "000041"),
    ("S08", "available equipment (wok)", "DEV-25", "odunola/foodie", "foodie-015219"),
    ("S09", "paraphrase", "DEV-10", "odunola/foodie", "foodie-004171"),
    ("S10", "both datasets", "DEV-20", "AkashPS11/recipes_data_food.com", "000146"),
    ("S11", "missing metadata (unlabelled)", "DEV-17", "odunola/foodie", "foodie-000001"),
    ("S12", "new case (unlabelled)", "DEV-31", "AkashPS11/recipes_data_food.com", "000039"),
    ("S13", "possible no-match (realistic)", "DEV-32", None, None),
    ("S14", "possible no-match (nonsense)", "HELD-10", None, None),
]


def _full_recipe(engine: Any, dataset_id: str, source_id: str) -> dict[str, Any] | None:
    from culinary_copilot.recipes.repository import get_recipe

    try:
        doc: dict[str, Any] | None = get_recipe(engine, source_id, dataset_id=dataset_id)
        return doc
    except ValueError:
        return None


def _render_recipe(doc: dict[str, Any]) -> str:
    lines: list[str] = []
    ingredients = doc.get("ingredients") or []
    lines.append(f"Ingredients ({len(ingredients)}):")
    for item in ingredients:
        if isinstance(item, dict):
            original = item.get("original") or item.get("canonical") or "?"
            lines.append(f"  - {original} [canonical: {item.get('canonical')}]")
    lines.append("Instructions:")
    for i, step in enumerate(doc.get("instructions") or [], 1):
        lines.append(f"  {i}. {step}")
    lines.append(f"Durations (reported, unverified): {doc.get('durations_minutes')}")
    lines.append(f"Servings (reported, unverified): {doc.get('servings')}")
    raw_nutrition: object = doc.get("nutrition")
    nutrition: dict[str, object] = raw_nutrition if isinstance(raw_nutrition, dict) else {}
    lines.append(f"Nutrition keys present: {sorted(nutrition)} (values unverified)")
    lines.append(f"Flags: {doc.get('flags')}")
    return "\n".join(lines)


def main() -> int:
    packet = json.loads(PACKET.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = packet["cases"]
    entries = {e["case_id"]: e for e in cases}
    corrections = json.loads(
        (REPO / "evals/results/phase1/checkpoint1_corrections.json").read_text(encoding="utf-8")
    )
    overrides = {
        (o["case_id"], o["dataset_id"], o["source_id"]): o
        for o in corrections.get("time_overrides", [])
    }

    from sqlalchemy import create_engine

    from culinary_copilot.config import Settings

    engine = create_engine(Settings().database_url.get_secret_value())
    try:
        items: list[dict[str, Any]] = []
        for slot, stratum, case_id, dataset_id, source_id in SLOTS:
            if case_id == "ANY_FAIL":
                found = None
                for entry in sorted(cases, key=lambda e: str(e["case_id"])):
                    cands: list[dict[str, Any]] = entry["candidates"]
                    for cand in sorted(
                        cands,
                        key=lambda c: (str(c["dataset_id"]), str(c["source_id"])),
                    ):
                        rec: dict[str, Any] = cand.get("recorded") or {}
                        checks = rec.get("constraint_checks") or []
                        if isinstance(checks, dict):
                            values = list(checks.values())
                        else:
                            values = [e.get("status") if isinstance(e, dict) else e for e in checks]
                        if any(v in ("fail", "violated_by_source") for v in values):
                            found = (entry, cand)
                            break
                    if found:
                        break
                assert found is not None, "no recorded fail check found"
                entry, cand = found
                dataset_id, source_id = cand["dataset_id"], cand["source_id"]
            else:
                entry = entries[case_id]
            item: dict[str, Any] = {
                "slot": slot,
                "stratum": stratum,
                "case_id": entry["case_id"],
                "request": {
                    "dish": entry["dish"],
                    "ingredients": entry["ingredients"],
                    "dietary_constraints": entry["dietary_constraints"],
                    "equipment": entry["equipment"],
                    "time_minutes": entry["time_minutes"],
                    "query_text": next(
                        (
                            c.get("query_text", "")
                            for c in packet["cases"]
                            if c["case_id"] == entry["case_id"]
                        ),
                        "",
                    ),
                    "dataset_scope": entry["dataset_scope"],
                },
                "notes": entry["notes"],
            }
            if source_id is None:
                item["candidate"] = None
                item["empty_pool_note"] = (
                    "Empty candidate pool: reflects this query/filter/dataset "
                    "slice only and does not prove no relevant recipe exists."
                )
                item["proposed"] = {"label_status": "unlabelled"}
            else:
                assert dataset_id is not None
                doc = _full_recipe(engine, dataset_id, source_id)
                pool: list[dict[str, Any]] = entry["candidates"]
                match: dict[str, Any] | None = next(
                    (
                        c
                        for c in pool
                        if c["dataset_id"] == dataset_id and c["source_id"] == source_id
                    ),
                    None,
                )
                rec = (match or {}).get("recorded") or {}
                override = overrides.get((entry["case_id"], dataset_id, source_id))
                item["candidate"] = {
                    "dataset_id": dataset_id,
                    "source_id": source_id,
                    "title": (doc or {}).get("title") if doc else None,
                    "complete_recipe": _render_recipe(doc) if doc else "DOCUMENT UNAVAILABLE",
                }
                item["proposed"] = {
                    "label_status": (match or {}).get("label_status", "unlabelled"),
                    "topical_grade": rec.get("topical_grade"),
                    "constraint_checks": rec.get("constraint_checks"),
                    "suitability": rec.get("suitability"),
                    "rationale": rec.get("rationale"),
                    "time_override": override,
                }
            items.append(item)
    finally:
        engine.dispose()

    md: list[str] = [
        "# Checkpoint 1 label-calibration sample (AI-proposed, NOT human-reviewed)",
        "",
        "> Calibration, not approval of every label. Grade each pair on the "
        "complete recipe below, then mark agree / disagree / unsure. "
        "Disagreements go to the follow-up list at the end.",
        "",
        "- Rubric phase1-rubric-v1: 2 = highly relevant; 1 = partially relevant; "
        "0 = irrelevant. Suitability and constraint checks are separate from "
        "the topical grade.",
        "- Selection: deterministic slots over `review_packet_v2.json` (lowest "
        "recorded identity per stratum, one recorded zero-total override, one "
        "unlabelled new-case pair, two empty-pool no-match entries).",
        "",
    ]
    for item in items:
        req = item["request"]
        md.append(f"## {item['slot']} [{item['stratum']}] — {item['case_id']}")
        md.append(
            f"Request: dish={req['dish']!r} ingredients={req['ingredients']!r} "
            f"dietary={req['dietary_constraints']!r} equipment={req['equipment']!r} "
            f"time={req['time_minutes']!r} scope={req['dataset_scope']!r}"
        )
        md.append(f"Case notes: {item['notes']}")
        if item["candidate"] is None:
            md.append(f"- (empty pool) {item['empty_pool_note']}")
        else:
            cand = item["candidate"]
            prop = item["proposed"]
            md.append(f"Candidate: ({cand['dataset_id']}, {cand['source_id']}) **{cand['title']}**")
            md.append("```")
            md.append(cand["complete_recipe"][:4000])
            md.append("```")
            md.append(
                f"Proposed: status={prop['label_status']} grade={prop['topical_grade']} "
                f"checks={prop['constraint_checks']} suitability={prop['suitability']}"
            )
            if prop.get("rationale"):
                md.append(f"Rationale: {prop['rationale']}")
            if prop.get("time_override"):
                md.append(f"Time override (recorded): {prop['time_override']}")
        md.append("Owner verdict: - [ ] agree  - [ ] disagree  - [ ] unsure")
        md.append("Notes: ___")
        md.append("")
    md.append("## Follow-up list (owner fills)")
    md.append("")
    md.append("- Disagreements / unsure cases needing re-grading or case edits:")
    md.append("- [ ] ...")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    OUT_JSON.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
    print(f"items={len(items)} wrote {OUT_MD.name} and {OUT_JSON.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
