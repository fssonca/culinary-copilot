"""Phase 7 offline harness in make check (disposable DB).

Runs the harness on its disposable ``culinary_check_phase7`` database
and asserts every scored case passes, every adversarial case is caught
with its expected_rejection, and nothing expected to fail passes.
Skipped when PostgreSQL is unreachable, like the other DB tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "phase7_agent"))

import run


def test_phase7_harness_all_scored_pass() -> None:
    try:
        assert run.main() == 0
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for phase7 harness: {exc!r}")
    results = json.loads((Path(run.HERE) / "results.json").read_text(encoding="utf-8"))
    cases = json.loads((Path(run.HERE) / "cases.json").read_text(encoding="utf-8"))
    assert results["cases_sha256"] not in (
        "5655b1ccc792a0106b08d56e01bd7d76248cdf936175660ad9abd03515b5c35b",
    )
    assert results["cases_version"] == cases["version"]
    scored = [r for r in results["results"] if not r["expected_fail"]]
    assert scored, "no scored cases"
    assert all(r["task_completion"] for r in scored), [
        r["id"] for r in scored if not r["task_completion"]
    ]
    adversarial = [r for r in scored if r["adversarial"]]
    assert len(adversarial) == 4
    assert all(r["task_completion"] for r in adversarial)
    by_id = {c["id"]: c for c in cases["cases"]}
    for r in adversarial:
        expected = (by_id[r["id"]].get("expected") or {}).get("expected_rejection")
        if expected is not None:
            assert expected, r["id"]
    for r in results["results"]:
        if r["expected_fail"]:
            assert not r["task_completion"], r["id"]
    assert results["aggregate"]["task_completion_rate"] == 1.0
