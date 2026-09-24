"""Phase 1 case-file tests: load and validate the actual committed cases.

Offline: schema validation only, no database or model use.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "retrieval_eval"))

from case_schema import check_case_file, load_case_file  # noqa: E402

CASE_FILE = Path(__file__).parent / "cases" / "phase1_retrieval.json"


def _data():
    return load_case_file(str(CASE_FILE))


def test_case_file_validates_without_problems() -> None:
    data = _data()
    assert check_case_file(data) == []


def test_split_and_kinds_are_frozen() -> None:
    data = _data()
    by_id = {c.case_id: c for c in data.cases}
    assert len(by_id) == len(data.cases)
    assert sum(1 for c in data.cases if c.split == "development") == 32
    assert sum(1 for c in data.cases if c.split == "held_out") == 20
    assert {c.kind for c in data.cases} == {"integration", "retrieval_only"}


def test_owner_approved_v3_decisions_present() -> None:
    by_id = {c.case_id: c for c in _data().cases}
    assert by_id["DEV-14"].relevance_expectation == "not_asserted"
    assert "zero_total_never_satisfies_ceiling" in by_id["DEV-14"].behavioral
    assert by_id["DEV-16"].dish == "mystery garlic dish"  # preserved regression
    assert by_id["DEV-31"].dish == "biryani" and by_id["DEV-31"].time_minutes == 300
    assert by_id["DEV-20"].query_text == "apple pie"
    assert by_id["DEV-22"].dish == "zzzznomatch xyzzy feast"  # preserved
    assert by_id["DEV-32"].query_text == "bouillabaisse"
    assert by_id["DEV-32"].relevance_expectation == "not_asserted"
    assert by_id["DEV-24"].dietary_constraints == ["vegan", "gluten_free"]
    assert by_id["DEV-25"].equipment == ["wok"]
    assert by_id["HELD-16"].dietary_constraints == ["vegetarian"]
    assert by_id["HELD-18"].category == "missing_metadata"
    assert by_id["HELD-10"].aggregate_group == by_id["HELD-11"].aggregate_group is not None
