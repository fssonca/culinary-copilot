#!/usr/bin/env python3
"""Re-verify every Live claim in CHECKPOINT_C.md against the raw files.

Read-only: loads data/phase7-live/raw/*.json,
data/phase7-live/raw-rerun/*.json and the scenario files, recomputes
stops, tool counts, guards fired, final shapes, cited ids and
constraint statuses, and checks them against the packet's tables.
Exits non-zero on any mismatch.

Usage (repo root, offline, no model calls)::

    uv run python evals/phase7_agent/verify_packet.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW = REPO_ROOT / "data" / "phase7-live" / "raw"
RERUN = REPO_ROOT / "data" / "phase7-live" / "raw-rerun"

# (raw path, expected stop, expected tools {(tool, outcome): count},
#  expected reject substrings, expected final shape, cited ids,
#  constraint statuses [(source_id, status, value)] or "none").
SHAPE_ORDER = ("options", "plan", "web_answer", "question", "technique", "none")


def final_shape(final: dict) -> str:
    for shape in SHAPE_ORDER:
        if shape == "none":
            return "none"
        if final.get(shape):
            return shape
    return "none"


def brief(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    tools: Counter = Counter()
    steps = 0
    rejects: list[str] = []
    for sess in raw.get("trajectory") or []:
        for event in sess.get("events") or []:
            kind = event.get("type")
            if kind == "tool_call":
                tools[(event.get("tool"), event.get("outcome"))] += 1
            elif kind == "agent_step":
                steps += 1
            elif kind == "agent_validation_reject":
                rejects.extend(str(e) for e in event.get("errors") or [])
    attempt = raw.get("final_attempt") or {}
    finals = []
    for run in attempt.get("runs") or []:
        final = run.get("final") or {}
        finals.append(
            {
                "stop": run.get("stop_reason"),
                "shape": final_shape(final),
                "options": [
                    (o.get("dataset_id"), o.get("source_id"), o.get("title"))
                    for o in final.get("options") or []
                ],
                "plan_source": ((final.get("plan") or {}).get("source") or {}).get("source_id"),
                "plan_refs": (final.get("plan") or {}).get("technique_refs") or [],
                "web_urls": [
                    w.get("url") for w in (final.get("web_answer") or {}).get("web_refs") or []
                ],
                "checks": [
                    (c.get("source_id"), c.get("status"), c.get("value"))
                    for c in final.get("constraint_check") or []
                ],
            }
        )
    return {
        "key": raw.get("key"),
        "stop": raw.get("stop_reason"),
        "tools": dict(tools),
        "steps": steps,
        "rejects": rejects,
        "finals": finals,
    }


EXPECTATIONS: list[dict] = [
    {
        "path": "raw/live-chicken-e2e.json",
        "stop": "agent_needs_user_input",
        "tools": {
            ("find_balanced_pairings", "ok"): 1,
            ("search_recipes", "ok"): 3,
            ("get_recipe", "ok"): 1,
        },
        "rejects": ["unsupported time/temperature claim"],
        "finals": [{"stop": "agent_needs_user_input", "shape": "question"}],
        "checks": [],
    },
    {
        "path": "raw/live-yogurt-ask.json",
        "stop": "agent_sufficient_evidence",
        "tools": {
            ("find_balanced_pairings", "ok"): 1,
            ("search_recipes", "ok"): 1,
            ("get_recipe", "ok"): 3,
        },
        "rejects": ["unsupported pairing"],
        "finals": [
            {
                "stop": "agent_sufficient_evidence",
                "shape": "options",
                "options": ["foodie-013603", "foodie-013240"],
            }
        ],
        "checks": [],
    },
    {
        "path": "raw/live-peanut-allergy.json",
        "stop": "agent_needs_user_input",
        "tools": {
            ("search_recipes", "ok"): 4,
            ("find_balanced_pairings", "ok"): 1,
            ("get_recipe", "ok"): 1,
        },
        "rejects": [],
        "finals": [
            {"stop": "agent_needs_user_input", "shape": "question"},
            {"stop": "agent_needs_user_input", "shape": "question"},
        ],
        "checks": [],
    },
    {
        "path": "raw/live-vegan-conflict.json",
        "stop": "agent_needs_user_input",
        "tools": {
            ("search_recipes", "error"): 1,
            ("find_balanced_pairings", "ok"): 1,
            ("search_techniques", "ok"): 2,
            ("find_conventional_pairings", "ok"): 1,
        },
        "rejects": [],
        "finals": [{"stop": "agent_needs_user_input", "shape": "question"}],
        "checks": [],
    },
    {
        "path": "raw/live-search-once.json",
        "stop": "agent_sufficient_evidence",
        "tools": {
            ("search_recipes", "ok"): 1,
            ("search_web", "ok"): 1,
            ("find_balanced_pairings", "ok"): 1,
        },
        "rejects": [],
        "finals": [{"stop": "agent_sufficient_evidence", "shape": "web_answer"}],
        "checks": [],
    },
    {
        "path": "raw/live-search-toggle.json",
        "stop": "agent_sufficient_evidence",
        "tools": {
            ("search_recipes", "ok"): 1,
            ("get_recipe", "ok"): 3,
            ("find_balanced_pairings", "ok"): 1,
        },
        "rejects": [],
        "finals": [
            {
                "stop": "agent_sufficient_evidence",
                "shape": "options",
                "options": ["foodie-007123", "foodie-007126"],
            }
        ],
        "checks": [],
    },
    {
        "path": "raw/live-plan-safety.json",
        "stop": "agent_max_steps",
        "tools": {
            ("find_balanced_pairings", "ok"): 1,
            ("search_recipes", "ok"): 1,
            ("get_recipe", "ok"): 3,
        },
        "rejects": ["unsupported pairing", "food-safety chunk"],
        "finals": [
            {"stop": "agent_sufficient_evidence", "shape": "options"},
            {"stop": "agent_max_steps", "shape": "none"},
        ],
        "checks": [],
    },
    {
        "path": "raw-rerun/live-chicken-e2e.json",
        "stop": "agent_tool_budget_exhausted",
        "tools": {
            ("find_balanced_pairings", "ok"): 2,
            ("search_recipes", "ok"): 3,
            ("get_recipe", "ok"): 6,
            ("get_recipe", "error"): 1,
        },
        "rejects": ["unsupported time/temperature claim"],
        "finals": [
            {"stop": "agent_needs_user_input", "shape": "question"},
            {"stop": "agent_tool_budget_exhausted", "shape": "none"},
        ],
        "checks": [],
    },
    {
        "path": "raw-rerun/live-peanut-allergy.json",
        "stop": "agent_tool_budget_exhausted",
        "tools": {
            ("search_recipes", "ok"): 4,
            ("find_balanced_pairings", "ok"): 2,
            ("get_recipe", "ok"): 6,
        },
        "rejects": ["is not a session constraint"],
        "finals": [
            {"stop": "agent_needs_user_input", "shape": "question"},
            {"stop": "agent_tool_budget_exhausted", "shape": "none"},
        ],
        "checks": [],
    },
    {
        "path": "raw-rerun/live-vegan-conflict.json",
        "stop": "agent_sufficient_evidence",
        "tools": {
            ("search_recipes", "ok"): 2,
            ("get_recipe", "ok"): 3,
            ("find_balanced_pairings", "ok"): 2,
        },
        "rejects": [],
        "finals": [
            {
                "stop": "agent_sufficient_evidence",
                "shape": "options",
                "options": ["foodie-016650", "foodie-012953", "foodie-007108"],
            }
        ],
        "checks": [
            ("foodie-016650", "checked", "vegan"),
            ("foodie-012953", "checked", "vegan"),
            ("foodie-007108", "checked", "vegan"),
        ],
    },
    {
        "path": "raw-rerun/live-plan-safety.json",
        "stop": "agent_sufficient_evidence",
        "tools": {
            ("search_recipes", "ok"): 1,
            ("get_recipe", "ok"): 3,
            ("find_balanced_pairings", "ok"): 1,
            ("search_techniques", "ok"): 5,
        },
        "rejects": ["food-safety chunk"],
        "finals": [
            {"stop": "agent_sufficient_evidence", "shape": "options"},
            {"stop": "agent_sufficient_evidence", "shape": "plan"},
        ],
        "checks": [],
    },
]


def check_match_sets() -> list[str]:
    """Expected-stop match per run set, reported separately and never
    combined into one benchmark figure (close-out correction)."""
    failures: list[str] = []
    sets = {
        "live-summary-phase7.json": (
            {"live-yogurt-ask", "live-search-once", "live-search-toggle"},
            7,
        ),
        "live-summary-phase7-rerun.json": (
            {"live-vegan-conflict", "live-plan-safety"},
            4,
        ),
    }
    for filename, (matched, total) in sets.items():
        summary = json.loads((REPO_ROOT / "data" / "phase7-live" / filename).read_text())
        reports = summary.get("scenarios") or []
        if len(reports) != total:
            failures.append(f"{filename}: {len(reports)} scenarios != {total}")
        sufficient = {
            r.get("key") for r in reports if r.get("stop_reason") == "agent_sufficient_evidence"
        }
        if sufficient != matched:
            failures.append(f"{filename}: sufficient {sorted(sufficient)} != {sorted(matched)}")
        print(f"{filename}: matched {len(matched)}/{total}")
    return failures


def check_terminal_outcomes() -> list[str]:
    """5 sufficient-evidence, 3 questions, 3 budget stops across 11."""
    counts: Counter = Counter()
    for directory in (RAW, RERUN):
        for path in sorted(directory.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            counts[raw.get("stop_reason")] += 1
    failures: list[str] = []
    got = (
        counts.get("agent_sufficient_evidence", 0),
        counts.get("agent_needs_user_input", 0),
        counts.get("agent_max_steps", 0) + counts.get("agent_tool_budget_exhausted", 0),
    )
    if got != (5, 3, 3):
        failures.append(f"terminal outcomes {got} != (5, 3, 3)")
    print(f"terminal outcomes: {got[0]} sufficient, {got[1]} questions, {got[2]} budget stops")
    return failures


def check_yogurt_grades() -> list[str]:
    """Yogurt ask-and-resume was NOT exercised live (all three false)."""
    summary = json.loads(
        (REPO_ROOT / "data" / "phase7-live" / "live-summary-phase7.json").read_text()
    )
    failures: list[str] = []
    report = next(
        (r for r in summary.get("scenarios") or [] if r.get("key") == "live-yogurt-ask"),
        {},
    )
    grades = report.get("grades") or {}
    for field in ("asked", "answer_recorded", "resumed_used_answer"):
        if grades.get(field) is not False:
            failures.append(f"yogurt grades.{field} is not false: {grades.get(field)!r}")
    fields = ("asked", "answer_recorded", "resumed_used_answer")
    print(f"yogurt grades: {[(f, grades.get(f)) for f in fields]}")
    return failures


def main() -> int:
    failures: list[str] = []
    checked = 0
    for expected in EXPECTATIONS:
        path = REPO_ROOT / "data" / "phase7-live" / expected["path"]
        got = brief(path)
        label = expected["path"]
        if got["stop"] != expected["stop"]:
            failures.append(f"{label}: stop {got['stop']!r} != {expected['stop']!r}")
        if got["tools"] != expected["tools"]:
            failures.append(f"{label}: tools {got['tools']} != {expected['tools']}")
        for needle in expected["rejects"]:
            if not any(needle in text for text in got["rejects"]):
                failures.append(f"{label}: no rejection containing {needle!r}")
        if not expected["rejects"] and got["rejects"]:
            failures.append(f"{label}: unexpected rejections {got['rejects'][:2]}")
        if len(got["finals"]) != len(expected["finals"]):
            failures.append(f"{label}: {len(got['finals'])} finals != {len(expected['finals'])}")
        else:
            for index, (got_final, want_final) in enumerate(zip(got["finals"], expected["finals"])):
                if got_final["stop"] != want_final["stop"]:
                    failures.append(f"{label} run {index}: stop mismatch")
                if got_final["shape"] != want_final["shape"]:
                    failures.append(f"{label} run {index}: shape mismatch")
                for source_id in want_final.get("options", []):
                    if source_id not in [o[1] for o in got_final["options"]]:
                        failures.append(f"{label}: option {source_id} not cited")
        got_checks = [c for f in got["finals"] for c in f["checks"]]
        if got_checks != expected["checks"]:
            failures.append(f"{label}: checks {got_checks} != {expected['checks']}")
        checked += 1
    print(f"verified {checked} sessions")
    failures.extend(check_match_sets())
    failures.extend(check_terminal_outcomes())
    failures.extend(check_yogurt_grades())
    if failures:
        print(f"{len(failures)} MISMATCHES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("all packet claims match the raw files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
