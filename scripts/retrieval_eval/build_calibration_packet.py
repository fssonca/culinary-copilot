"""Build the owner-accepted calibration packet v2 (reproducible, read-only).

Reads structured records (case file, v2 annotation pools, corrections,
owner decisions, combined review, rubric) and the application corpus
read-only. Writes ``calibration_packet_v2.json`` + ``.md``. Existing
artifacts (including ``calibration_sample_pre_owner_acceptance.*`` and the
working ``calibration_sample.*``) are never modified.

Layering per item (never merged, never silently promoted):

- ``ai_proposal``: verbatim recorded AI label from the corrections file.
- ``combined_review``: the accepted slot conclusion (owner decisions JSON)
  plus the combined-review document hash. Session transcripts were not
  supplied; this layer is owner-accepted AI review, not human verification.
- ``owner_acceptance``: the per-item acceptance record verbatim.
- ``adjudication``: new AI-authored layer under the accepted policy and
  rubric_v1 (reviewer_type ``ai_policy_adjudication``). New topical
  judgments (anchors, S13 family grades, S09/S10 query restorations) live
  here, disclosed as AI-proposed and not covered by the prior reviews.

Input provenance: case files never recorded original user messages, so
``original_message`` is always null with status ``not_recorded``;
structured inputs are reconstructed from the case record and labelled as
such. Nothing is invented.

Usage (from repo root):
    uv run python scripts/retrieval_eval/build_calibration_packet.py
"""

from __future__ import annotations

import hashlib
import html
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))

from adjudicate import (  # noqa: E402
    adjudication_layer,
    vegan_status,
    vegetarian_status,
)

PHASE1 = REPO / "evals" / "results" / "phase1"
OUT_JSON = PHASE1 / "calibration_packet_v2.json"
OUT_MD = PHASE1 / "calibration_packet_v2.md"

STATUS_LINE = (
    "AI-assisted review accepted by the project owner; "
    "not independently human-verified or culinarily validated."
)

# slot, stratum, case_id, dataset_id, source_id, anchor kind
PRIOR_SLOTS: list[tuple[str, str, str, str | None, str | None]] = [
    ("S01", "dish + pantry", "DEV-09", "AkashPS11/recipes_data_food.com", "000159"),
    ("S02", "pantry-only", "DEV-06", "AkashPS11/recipes_data_food.com", "001559"),
    ("S03", "time limit (eligible)", "DEV-13", "AkashPS11/recipes_data_food.com", "000322"),
    ("S04", "elapsed-time exclusion", "DEV-15", "AkashPS11/recipes_data_food.com", "001379"),
    ("S05", "dietary contradiction", "DEV-24", "odunola/foodie", "foodie-001308"),
    ("S06", "dietary mixed", "DEV-24", "odunola/foodie", "foodie-015516"),
    ("S07", "dietary conditional", "HELD-16", "AkashPS11/recipes_data_food.com", "000041"),
    ("S08", "available equipment", "DEV-25", "odunola/foodie", "foodie-015219"),
    ("S09", "paraphrase", "DEV-10", "odunola/foodie", "foodie-004171"),
    ("S10", "both datasets", "DEV-20", "AkashPS11/recipes_data_food.com", "000146"),
    ("S11", "missing metadata", "DEV-17", "odunola/foodie", "foodie-000001"),
    ("S12", "reported-time variability", "DEV-31", "AkashPS11/recipes_data_food.com", "000039"),
    ("S13", "possible no-match (realistic)", "DEV-32", None, None),
    ("S14", "possible no-match (nonsense)", "HELD-10", None, None),
]

# slot, stratum, case_id, dataset_id, source_id — all new AI-proposed anchors.
ANCHORS: list[tuple[str, str, str, str, str]] = [
    ("A1", "topical grade 0", "DEV-12", "odunola/foodie", "foodie-013173"),
    ("A2", "topical grade 1", "DEV-13", "AkashPS11/recipes_data_food.com", "000503"),
    ("A3", "pantry-only partial overlap", "DEV-06", "AkashPS11/recipes_data_food.com", "000327"),
    ("A4", "supported dietary compatibility", "HELD-16", "odunola/foodie", "foodie-010685"),
    ("A5", "unresolved dietary, no known violation", "DEV-24", "odunola/foodie", "foodie-015775"),
    (
        "A6",
        "equipment compatibility (positive evidence)",
        "DEV-25",
        "AkashPS11/recipes_data_food.com",
        "000251",
    ),
    ("A7", "unknown-duration counterpart", "DEV-31", "odunola/foodie", "foodie-001470"),
]

# S13 related recipes, graded against the bouillabaisse query (new AI layer).
S13_RELATED: list[tuple[str, str]] = [
    ("odunola/foodie", "foodie-003982"),
    ("odunola/foodie", "foodie-012903"),
    ("odunola/foodie", "foodie-007439"),
    ("odunola/foodie", "foodie-003913"),
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _revision() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=REPO
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _names(doc: dict[str, Any]) -> list[str]:
    out = []
    for item in doc.get("ingredients") or []:
        if isinstance(item, dict):
            name = item.get("canonical") or item.get("original")
            if isinstance(name, str) and name.strip():
                out.append(" ".join(name.split()))
    return out


def _render(doc: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Render display evidence; decode entities for display, keep raw."""
    raw_value: Any = doc.get("title")
    raw_title: str = raw_value if isinstance(raw_value, str) else ""
    display_title = html.unescape(raw_title)
    lines = [f"Ingredients ({len(doc.get('ingredients') or [])}):"]
    for name in _names(doc):
        lines.append(f"  - {name}")
    lines.append("Instructions:")
    for i, step in enumerate(doc.get("instructions") or [], 1):
        lines.append(f"  {i}. {step}" if isinstance(step, str) else f"  {i}. [non-text step]")
    lines.append(f"Durations (reported, unverified): {doc.get('durations_minutes')}")
    lines.append(f"Servings (reported, unverified): {doc.get('servings')}")
    nutrition = doc.get("nutrition")
    keys = sorted(nutrition) if isinstance(nutrition, dict) else []
    lines.append(f"Nutrition keys present: {keys} (values unverified)")
    lines.append(f"Flags: {doc.get('flags')}")
    completeness = {
        "raw_title": raw_title,
        "entity_decoded_for_display": raw_title != display_title,
        "ingredient_names_shown": len(_names(doc)),
        "instruction_steps_shown": len(doc.get("instructions") or []),
        "full_text_in_json": True,
    }
    return display_title, "\n".join(lines), completeness


def _production_output(engine: Any, case: dict[str, Any]) -> dict[str, Any]:
    """Production-equivalent query output (read-only; readiness gating noted)."""
    from culinary_copilot.recipes.repository import search_all, search_recipes
    from culinary_copilot.retrieval.query import MATCH_DISH, map_request_to_query
    from culinary_copilot.services.answers import init_state

    limit = int(case.get("limit", 5) or 5)
    dataset_id = case.get("dataset_id")
    search = search_recipes if dataset_id else search_all
    extra: dict[str, Any] = {"dataset_id": dataset_id} if dataset_id else {}
    if case.get("kind") == "integration":
        request: dict[str, Any] = {"ingredients": list(case.get("ingredients") or [])}
        for field in ("time_minutes", "dietary_constraints", "equipment"):
            if case.get(field):
                request[field] = case[field]
        state = init_state(
            request_id=f"cal-{case['case_id']}", request=request, dish=case.get("dish")
        )
        query = map_request_to_query(state, dataset_id=dataset_id)
        mode = query.match_mode
        if mode == MATCH_DISH:
            rows = search(
                engine,
                query.query_text,
                max_minutes=query.max_minutes,
                limit=limit,
                rank_pantry_terms=query.rank_pantry_terms,
                **extra,
            )
        else:
            rows = search(
                engine,
                query.query_text or "recipe",
                max_minutes=query.max_minutes,
                limit=limit,
                match_any_ingredients=query.match_any_ingredients,
                **extra,
            )
        query_text = query.query_text
    else:
        mode = "retrieval_only_text"
        query_text = case.get("query_text") or ""
        rows = search(
            engine, query_text, max_minutes=case.get("time_minutes"), limit=limit, **extra
        )
    return {
        "method": "mapping-plus-repository, case limit, read-only; production service "
        "additionally gates on readiness and revisions",
        "match_mode": mode,
        "query_text": query_text,
        "limit": limit,
        "top_identities": [[r["dataset_id"], r["source_id"]] for r in rows],
        "empty": not rows,
    }


def main() -> int:
    cases = {
        c["case_id"]: c
        for c in json.loads((REPO / "evals/cases/phase1_retrieval.json").read_text())["cases"]
    }
    packet = json.loads((PHASE1 / "review_packet_v2.json").read_text())
    pool_by_case = {e["case_id"]: e for e in packet["cases"]}
    corrections = json.loads((PHASE1 / "checkpoint1_corrections.json").read_text())
    recorded = {
        (label["case_id"], label["dataset_id"], label["source_id"]): label
        for label in corrections.get("labels", [])
    }
    decisions = json.loads((PHASE1 / "calibration_owner_decisions.json").read_text())
    accepted = {p["slot"]: p for p in decisions.get("per_item", [])}

    from sqlalchemy import create_engine

    from culinary_copilot.config import Settings
    from culinary_copilot.recipes.repository import get_recipe

    engine = create_engine(Settings().database_url.get_secret_value())
    try:
        with engine.connect() as conn:
            from sqlalchemy import text

            corpus = {
                "total": conn.execute(text("select count(*) from recipes")).scalar_one(),
                "by_dataset": [
                    [r[0], r[1]]
                    for r in conn.execute(
                        text("select dataset_id, count(*) from recipes group by 1 order by 1")
                    ).all()
                ],
            }

        items: list[dict[str, Any]] = []
        for slot, stratum, case_id, dataset_id, source_id in PRIOR_SLOTS:
            items.append(
                _build_slot(
                    engine,
                    get_recipe,
                    cases,
                    pool_by_case,
                    recorded,
                    accepted,
                    slot,
                    stratum,
                    case_id,
                    dataset_id,
                    source_id,
                    anchor=False,
                )
            )
        for slot, stratum, case_id, dataset_id, source_id in ANCHORS:
            items.append(
                _build_slot(
                    engine,
                    get_recipe,
                    cases,
                    pool_by_case,
                    recorded,
                    accepted,
                    slot,
                    stratum,
                    case_id,
                    dataset_id,
                    source_id,
                    anchor=True,
                )
            )
        # S13 related-recipes grading (new AI layer on the DEV-32 query).
        s13 = next(i for i in items if i["slot"] == "S13")
        s13["related_adjudications"] = [
            _related(engine, get_recipe, ds, sid) for ds, sid in S13_RELATED
        ]
    finally:
        engine.dispose()

    tallies = _tallies(items)
    record = {
        "packet": "calibration_packet_v2",
        "status": STATUS_LINE,
        "provenance": {
            "case_file": "evals/cases/phase1_retrieval.json",
            "cases_sha256": _sha(REPO / "evals/cases/phase1_retrieval.json"),
            "annotation_pool": "evals/results/phase1/review_packet_v2.json",
            "annotation_pool_method": packet["provenance"]["discovery_method"],
            "corrections": "evals/results/phase1/checkpoint1_corrections.json",
            "corrections_sha256": _sha(PHASE1 / "checkpoint1_corrections.json"),
            "owner_decisions": "evals/results/phase1/calibration_owner_decisions.json",
            "owner_decisions_sha256": _sha(PHASE1 / "calibration_owner_decisions.json"),
            "combined_review": "evals/results/phase1/calibration_combined_review.txt",
            "combined_review_sha256": _sha(PHASE1 / "calibration_combined_review.txt"),
            "rubric": "evals/rubric_v1.md",
            "rubric_sha256": _sha(REPO / "evals/rubric_v1.md"),
            "corpus_fingerprint": corpus,
            "code_revision": _revision(),
            "command": "uv run python scripts/retrieval_eval/build_calibration_packet.py",
            "production_method": "deterministic mapping plus repository search at case "
            "limit, read-only; readiness/revision gating noted, not executed",
        },
        "exposure": {
            "held_out_slots": ["S07", "S14", "A4"],
            "aggregate_intent": "HELD-10/HELD-11 share nonsense-no-match; counted once",
            "blind_confirmation_set": "separate future work; these cases are exposed",
        },
        "scoring_exclusions": {
            "quarantined": [],
            "quarantine_note": "No judgment is quarantined from scoring. S05 truncation "
            "and S08 malformed canonicals are completeness concerns that do not "
            "affect the judged constraints (see data_quality). S10 entity fixed "
            "at packet render with raw preserved.",
        },
        "data_quality": [
            {
                "item": "S05",
                "observation": "final instruction ends mid-sentence ('cook tortellini')",
                "trace": "present in the stored normalized document; packet renders it "
                "fully, so the truncation predates packet rendering. Raw source not "
                "inspected; ingestion vs source cause undetermined.",
                "impact": "none on the judged constraints (vegan/gluten evidence is in "
                "the ingredient list). Completeness concern flagged separately.",
                "quarantined": False,
            },
            {
                "item": "S10",
                "observation": "title contains the HTML entity '&quot;21&quot;'",
                "trace": "entity is in the stored document title (source-level); "
                "packet render decodes entities for display and preserves the raw "
                "title. Demonstrated rendering presentation choice, fixed here.",
                "impact": "display-only; matching uses stored text consistently.",
                "quarantined": False,
            },
            {
                "item": "S08",
                "observation": "canonicals such as '- cup coconut milk' and "
                "'or more curry spice/powder' carry quantity phrasing",
                "trace": "present in the stored normalized document (normalization "
                "level); packet renders them verbatim. Cause beyond that undetermined.",
                "impact": "none on the judged equipment constraint; possible "
                "exact-overlap recall effect for pantry queries is noted, not "
                "scored here.",
                "quarantined": False,
            },
        ],
        "tallies": tallies,
        "items": items,
    }
    OUT_JSON.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    OUT_MD.write_text(_render_md(record), encoding="utf-8")
    print(f"items={len(items)} tallies={tallies}")
    return 0


def _build_slot(
    engine: Any,
    get_recipe: Any,
    cases: dict[str, Any],
    pool_by_case: dict[str, Any],
    recorded: dict[tuple[str, str, str], Any],
    accepted: dict[str, Any],
    slot: str,
    stratum: str,
    case_id: str,
    dataset_id: str | None,
    source_id: str | None,
    anchor: bool,
) -> dict[str, Any]:
    case = cases[case_id]
    pool = pool_by_case.get(case_id, {})
    production = _production_output(engine, case)
    item: dict[str, Any] = {
        "slot": slot,
        "stratum": stratum,
        "case_id": case_id,
        "anchor": anchor,
        "review_coverage": "new_ai_proposed" if anchor else "prior_accepted",
        "request_structured": {
            "query_text": case.get("query_text"),
            "dish": case.get("dish"),
            "pantry": list(case.get("ingredients") or []),
            "dietary_constraints": list(case.get("dietary_constraints") or []),
            "equipment": list(case.get("equipment") or []),
            "time_minutes": case.get("time_minutes"),
            "dataset_scope": case.get("dataset_id") or "combined",
            "limit": case.get("limit", 5),
        },
        "input_provenance": {
            "original_message": None,
            "original_message_status": "not_recorded_never_invented",
            "structured_source": "evals/cases/phase1_retrieval.json",
            "reconstructed_by": "build_calibration_packet.py",
        },
        "case_notes": case.get("notes", ""),
        "production": production,
        "annotation_pool": {
            "pool_size": pool.get("pool_size", 0),
            "discovery_note": "broader than production top-N; pool membership is not "
            "a production result",
        },
    }
    if source_id is None:
        item["candidate"] = None
        item["empty_pool_note"] = (
            "Empty annotation pool and empty production-equivalent output for this "
            "slice. Not a verified no-match case: the no-match question stays "
            "pending with abstain-vs-clarify behavior to be defined."
        )
        item["layers"] = _layers(recorded.get((case_id, "", "")), accepted.get(slot), None)
        return item
    assert dataset_id is not None
    try:
        doc = get_recipe(engine, source_id, dataset_id=dataset_id)
    except ValueError:
        doc = None
    if doc is None:
        item["candidate"] = {
            "dataset_id": dataset_id,
            "source_id": source_id,
            "document": "UNAVAILABLE",
        }
        item["layers"] = _layers(
            recorded.get((case_id, dataset_id, source_id)), accepted.get(slot), None
        )
        return item
    display_title, body, completeness = _render(doc)
    names: list[str] = []
    for entry in doc.get("ingredients") or []:
        if isinstance(entry, dict):
            name = entry.get("canonical") or entry.get("original")
            if isinstance(name, str) and name.strip():
                names.append(" ".join(name.split()))
    item["candidate"] = {
        "dataset_id": dataset_id,
        "source_id": source_id,
        "title_display": display_title,
        "complete_recipe": body,
        "completeness": completeness,
        "truncation_markdown_cap": 4000,
    }
    label = recorded.get((case_id, dataset_id, source_id))
    adjudication = _adjudicate(slot, anchor, case, doc, names, label)
    item["layers"] = _layers(label, accepted.get(slot), adjudication)
    return item


def _layers(label: Any, acceptance: Any, adjudication: Any) -> dict[str, Any]:
    return {
        "ai_proposal": {"status": "recorded" if label else "unlabelled", "label": label},
        "combined_review": {
            "accepted_conclusion": (acceptance or {}).get("accepted_review_conclusion"),
            "judgment_state": (acceptance or {}).get("judgment_state"),
            "note": "Owner-accepted AI review; session transcripts not supplied; "
            "not human verification.",
        },
        "owner_acceptance": acceptance,
        "adjudication": adjudication,
    }


def _reported_total(doc: dict[str, Any]) -> float | None:
    durations = doc.get("durations_minutes")
    total = durations.get("TotalTime") if isinstance(durations, dict) else None
    return (
        total
        if isinstance(total, (int, float)) and total == total and abs(total) != float("inf")
        else None
    )


def _adjudicate(
    slot: str, anchor: bool, case: dict[str, Any], doc: dict[str, Any], names: list[str], label: Any
) -> dict[str, Any]:
    base = {"slot": slot, "anchor": anchor}
    time_minutes = case.get("time_minutes")
    total = _reported_total(doc)

    def time_check() -> dict[str, str] | None:
        if time_minutes is None:
            return None
        if total is not None and total > 0 and total <= time_minutes:
            return {
                "constraint": f"max_minutes<={time_minutes:g}",
                "status": "supported",
                "evidence": f"reported total {total:g} <= {time_minutes:g}",
            }
        if total is None:
            return {
                "constraint": f"max_minutes<={time_minutes:g}",
                "status": "unresolved",
                "evidence": "no usable reported total; excluded by ceiling per system policy",
            }
        return {
            "constraint": f"max_minutes<={time_minutes:g}",
            "status": "violated",
            "evidence": f"reported total {total:g} > {time_minutes:g}",
        }

    if slot == "S01":
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="Chicken Curry is the requested dish; pantry items only rank",
            checks=[],
            system_enforced={},
            basis="owner-accepted combined review",
        )
    elif slot == "S02":
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="roast chicken centers both pantry items",
            checks=[],
            system_enforced={},
            basis="owner-accepted combined review",
        )
    elif slot == "S03":
        checks = [c for c in [time_check()] if c] + [
            {
                "constraint": "practical_elapsed_time_fit",
                "status": "unresolved",
                "evidence": "untimed cooking steps; reported total is not proof of deadline fit",
            }
        ]
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="Curry Chicken is the requested dish",
            checks=checks,
            system_enforced={"max_minutes": "enforced"},
            basis="owner-accepted combined review",
        )
    elif slot == "S04":
        layer = adjudication_layer(
            topical_grade=None,
            topical_reason="pending: query/time interpretation needs clarification",
            checks=[
                {
                    "constraint": "elapsed_time_ready_to_use",
                    "status": "violated",
                    "evidence": "instructions require a two-week steep; reported TotalTime PT0S",
                }
            ],
            system_enforced={"max_minutes": "enforced"},
            basis="owner-accepted combined review (elapsed-time reading)",
        )
    elif slot == "S05":
        status, ev = vegan_status(names)
        flour = [n for n in names if "flour" in n.lower()]
        checks = [
            {"constraint": "vegan", "status": status, "evidence": ev},
            {
                "constraint": "gluten_free",
                "status": "violated" if flour else "unresolved",
                "evidence": f"explicit all-purpose flour: {flour}"
                if flour
                else "no explicit flour in displayed names",
            },
        ]
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="tortellini is a pasta dish",
            checks=checks,
            system_enforced={"dietary": "carried_unchecked"},
            basis="accepted review; truncation traced as completeness-only concern",
        )
    elif slot == "S06":
        status, ev = vegan_status(names)
        checks = [
            {"constraint": "vegan", "status": status, "evidence": ev},
            {
                "constraint": "gluten_free",
                "status": "unresolved",
                "evidence": "spaghetti not specified gluten-free",
            },
        ]
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="Bang Bang Shrimp Pasta is a pasta dish",
            checks=checks,
            system_enforced={"dietary": "carried_unchecked"},
            basis="owner-accepted combined review",
        )
    elif slot == "S07":
        status, ev = vegetarian_status(names)
        checks = [{"constraint": "vegetarian", "status": status, "evidence": ev}]
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="tofu-vegetable skewers are kebabs",
            checks=checks,
            system_enforced={"dietary": "carried_unchecked"},
            basis="displayed-evidence standard; stricter alternative documented",
        )
    elif slot == "S08":
        checks = [
            {
                "constraint": "equipment",
                "status": "unresolved",
                "evidence": "wok non-exclusive; skillet/slow-cooker needs unknown; no adaptation",
            }
        ]
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="curry chicken is a chicken dinner",
            checks=checks,
            system_enforced={"equipment": "carried_unchecked"},
            basis="owner-accepted combined review",
        )
    elif slot == "S09":
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="chicken-parmesan dinner matches (query restored)",
            checks=[],
            system_enforced={},
            basis="new AI adjudication on restored query; not owner-accepted",
        )
    elif slot == "S10":
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="exact apple-pie match; title entity decoded for display",
            checks=[],
            system_enforced={},
            basis="new AI adjudication on restored query; not owner-accepted",
        )
    elif slot == "S11":
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="Chimodho is the requested dish",
            checks=[],
            system_enforced={},
            basis="owner-accepted combined review; servings unknown recorded as observation",
        )
    elif slot == "S12":
        checks = [c for c in [time_check()] if c] + [
            {
                "constraint": "practical_elapsed_time_fit",
                "status": "unresolved",
                "evidence": "six-hour marinade may exceed the deadline; interpretation ambiguous",
            }
        ]
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="Biryani is the requested dish",
            checks=checks,
            system_enforced={"max_minutes": "enforced"},
            basis="owner-accepted combined review",
        )
    elif slot == "A1":
        layer = adjudication_layer(
            topical_grade=0,
            topical_reason="pork wonton soup is not a vegetable soup (recorded)",
            checks=[],
            system_enforced={},
            basis="recorded AI proposal reused as anchor",
        )
    elif slot == "A2":
        checks = [c for c in [time_check()] if c]
        layer = adjudication_layer(
            topical_grade=1,
            topical_reason="satay shares elements with chicken curry but is not it (recorded)",
            checks=checks,
            system_enforced={"max_minutes": "enforced"},
            basis="recorded AI proposal reused as anchor",
        )
    elif slot == "A3":
        overlap = [n for n in ("chicken", "garlic") if n in names]
        layer = adjudication_layer(
            topical_grade=1,
            topical_reason=f"pantry-only partial overlap: exact overlap {overlap}",
            checks=[],
            system_enforced={},
            basis="new AI anchor; not covered by prior reviews",
        )
    elif slot == "A4":
        status, ev = vegetarian_status(names)
        checks = [{"constraint": "vegetarian", "status": status, "evidence": ev}]
        layer = adjudication_layer(
            topical_grade=1,
            topical_reason="paneer tikka is skewered-dish family, not kebabs by name",
            checks=checks,
            system_enforced={"dietary": "carried_unchecked"},
            basis="new AI anchor; displayed-source-evidence standard",
        )
    elif slot == "A5":
        raw_checks: Any = (label or {}).get("constraint_checks") or []
        norm: list[dict[str, str]] = []
        if isinstance(raw_checks, list):
            for entry in raw_checks:
                if isinstance(entry, dict):
                    norm.append(
                        {
                            "constraint": str(entry.get("constraint")),
                            "status": str(entry.get("status")),
                            "evidence": str(entry.get("evidence")),
                        }
                    )
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="sausage pasta is a pasta dish (recorded)",
            checks=norm
            or [
                {
                    "constraint": "vegan",
                    "status": "unresolved",
                    "evidence": "truncated displayed list",
                }
            ],
            system_enforced={"dietary": "carried_unchecked"},
            basis="recorded AI proposal reused as anchor",
        )
    elif slot == "A6":
        wok = "wok" in " ".join((doc.get("instructions") or [])).lower()
        checks = [
            {
                "constraint": "equipment_wok",
                "status": "supported" if wok else "unresolved",
                "evidence": "source explicitly fries in a wok (available)"
                if wok
                else "no wok use in displayed instructions",
            },
            {
                "constraint": "equipment_overall",
                "status": "unresolved",
                "evidence": "skillet and other needs unknown; availability non-exclusive",
            },
        ]
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="Thai Chicken Noodles is a chicken dinner",
            checks=checks,
            system_enforced={"equipment": "carried_unchecked"},
            basis="new AI anchor; positive wok evidence with overall unresolved",
        )
    elif slot == "A7":
        checks = [
            {
                "constraint": "max_minutes<=300",
                "status": "unresolved",
                "evidence": "unknown duration; ceiling-excluded with explanation",
            }
        ]
        layer = adjudication_layer(
            topical_grade=2,
            topical_reason="Chicken Biryani is the requested dish",
            checks=checks,
            system_enforced={"max_minutes": "enforced"},
            basis="new AI anchor; unknown-duration counterpart to S12",
        )
    else:
        layer = adjudication_layer(
            topical_grade=None,
            topical_reason="pending per accepted review",
            checks=[],
            system_enforced={},
            basis="owner-accepted combined review",
        )
    layer.update(base)
    return layer


def _related(engine: Any, get_recipe: Any, dataset_id: str, source_id: str) -> dict[str, Any]:
    try:
        doc = get_recipe(engine, source_id, dataset_id=dataset_id)
    except ValueError:
        doc = None
    if doc is None:
        return {"dataset_id": dataset_id, "source_id": source_id, "document": "UNAVAILABLE"}
    display_title, body, completeness = _render(doc)
    layer = adjudication_layer(
        topical_grade=1,
        topical_reason="same fish-soup/stew family as bouillabaisse; not the dish itself",
        checks=[],
        system_enforced={},
        basis="new AI adjudication against the DEV-32 query; not owner-accepted",
    )
    return {
        "dataset_id": dataset_id,
        "source_id": source_id,
        "title_display": display_title,
        "complete_recipe": body,
        "completeness": completeness,
        "layers": {"adjudication": layer},
    }


def _tallies(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, Any] = {"prior_accepted": 0, "new_ai_proposed": 0, "quarantined": 0}
    suit: dict[str, int] = {}
    topical_pending = 0
    for item in items:
        if item.get("anchor"):
            counts["new_ai_proposed"] += 1
        else:
            counts["prior_accepted"] += 1
        adj = (item.get("layers") or {}).get("adjudication") or {}
        if adj.get("topical_grade") is None:
            topical_pending += 1
        key = str(adj.get("suitability"))
        suit[key] = suit.get(key, 0) + 1
    counts["topical_pending"] = topical_pending
    counts["suitability"] = suit
    return counts


def _render_md(record: dict[str, Any]) -> str:
    md = [
        "# Calibration packet v2 (AI-assisted, owner-accepted policy; NOT independently verified)",
        "",
        f"> {STATUS_LINE}",
        "",
        "- Layers per item: `ai_proposal` (recorded verbatim) → `combined_review` "
        "(accepted conclusion) → `owner_acceptance` (verbatim record) → "
        "`adjudication` (new AI-authored under the accepted policy). Nothing is "
        "human-verified; new anchors and new adjudications are AI-proposed and "
        "not covered by the prior reviews.",
        "",
    ]
    md.append(
        f"- Provenance: cases {record['provenance']['cases_sha256'][:12]}, "
        f"corrections {record['provenance']['corrections_sha256'][:12]}, "
        f"decisions {record['provenance']['owner_decisions_sha256'][:12]}, "
        f"combined review {record['provenance']['combined_review_sha256'][:12]}, "
        f"rubric {record['provenance']['rubric_sha256'][:12]}."
    )
    md.append(
        f"- Corpus: total {record['provenance']['corpus_fingerprint']['total']}; "
        + ", ".join(f"{d}={n}" for d, n in record["provenance"]["corpus_fingerprint"]["by_dataset"])
        + "."
    )
    md.append(f"- Tallies: {record['tallies']}")
    md.append(
        f"- Exposure: held-out slots {record['exposure']['held_out_slots']}; "
        f"blind confirmation is separate future work."
    )
    md.append("")
    for item in record["items"]:
        req = item["request_structured"]
        md.append(
            f"## {item['slot']} [{item['stratum']}] — {item['case_id']}"
            + (" (NEW AI ANCHOR)" if item.get("anchor") else "")
        )
        md.append(f"Query: `{req['query_text']}` | dish={req['dish']!r} pantry={req['pantry']!r}")
        md.append(
            f"dietary={req['dietary_constraints']!r} equipment={req['equipment']!r} "
            f"time={req['time_minutes']!r} scope={req['dataset_scope']!r} limit={req['limit']!r}"
        )
        md.append(
            "Input provenance: original message not recorded (never invented); "
            "structured input reconstructed from the case record."
        )
        md.append(f"Case notes: {item['case_notes']}")
        prod = item["production"]
        top = ", ".join(f"({d}, {s})" for d, s in prod["top_identities"][:5])
        md.append(
            f"Production-equivalent output ({prod['match_mode']}, limit {prod['limit']}): "
            f"{top or '(empty — not a verified no-match)'}"
        )
        md.append(
            f"Annotation pool: {item['annotation_pool']['pool_size']} candidates "
            f"(broader than production top-N; not a production result)."
        )
        if item["candidate"] is None:
            md.append(f"- (empty pool) {item['empty_pool_note']}")
        else:
            cand = item["candidate"]
            md.append(f"Candidate: ({cand['dataset_id']}, {cand['source_id']})")
            md.append(f"**{cand['title_display']}**")
            md.append("```")
            md.append(cand["complete_recipe"][:4000])
            if len(cand["complete_recipe"]) > 4000:
                md.append("[truncated in Markdown at 4000 chars; full text in JSON]")
            md.append("```")
            md.append(f"Completeness: {cand['completeness']}")
        layers = item["layers"]
        prop = layers["ai_proposal"]
        extra = ""
        if prop["label"]:
            grade = prop["label"].get("topical_grade")
            suit = prop["label"].get("suitability")
            extra = f" grade={grade} suitability={suit}"
        md.append(f"AI proposal: {prop['status']}{extra}")
        combined = layers["combined_review"]
        md.append(f"Combined review (accepted): [{combined['judgment_state']}]")
        md.append(f"{combined['accepted_conclusion']}")
        adj = layers["adjudication"]
        if adj:
            md.append(
                f"Adjudication (AI-authored): grade={adj['topical_grade']} "
                f"suitability={adj['suitability']} ({adj['basis']})"
            )
            md.append(f"Checks: {adj['constraint_checks']}")
        if item.get("related_adjudications"):
            md.append("Related-recipe adjudications (new AI layer):")
            for rel in item["related_adjudications"]:
                a = rel["layers"]["adjudication"]
                md.append(
                    f"- ({rel['dataset_id']}, {rel['source_id']}) "
                    f"grade={a['topical_grade']}: {a['topical_reason']}"
                )
        md.append("")
    return "\n".join(md)


if __name__ == "__main__":
    raise SystemExit(main())
