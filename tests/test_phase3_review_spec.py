"""Offline checks on the tracked Phase 3 review spec (no DB, no artifacts)."""

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "recommendations_live"))

from review_packet import EXAMPLES, PROPOSALS, check_spec  # noqa: E402


def _specs() -> tuple[dict, dict]:
    return json.loads(EXAMPLES.read_text()), json.loads(PROPOSALS.read_text())


def test_tracked_spec_is_valid() -> None:
    examples, proposals = _specs()
    assert check_spec(examples, proposals) == []


def test_spec_holds_references_not_source_sections() -> None:
    examples, proposals = _specs()
    for example in examples["examples"]:
        assert "ingredients" not in example and "instructions" not in example
    for proposal in proposals["proposals"]:
        for ref in proposal["evidence"]:
            assert set(ref) <= {"section", "ref", "key", "sha256"}


def test_owner_acceptance_cannot_be_recorded_by_the_spec() -> None:
    examples, proposals = _specs()
    approved = copy.deepcopy(examples)
    approved["review_status"] = "approved"
    assert any("review_status" in e for e in check_spec(approved, proposals))
    accepted = copy.deepcopy(proposals)
    accepted["proposals"][0]["owner_decision"] = "accepted"
    assert any("owner_decision" in e for e in check_spec(examples, accepted))


def test_proposals_cannot_invent_quantities() -> None:
    examples, proposals = _specs()
    invented = copy.deepcopy(proposals)
    change = next(p for p in invented["proposals"] if p["id"] == "ENR-03")["proposed_change"]
    change["amount"] = 2
    assert any("may not set" in e for e in check_spec(examples, invented))
    filled = copy.deepcopy(proposals)
    unresolved = next(p for p in filled["proposals"] if p["id"] == "ENR-02")
    unresolved["proposed_change"] = {"action": "set_unit", "to": "tbsp"}
    assert any("proposes no change" in e for e in check_spec(examples, filled))


def test_every_example_keeps_four_separate_judgments() -> None:
    examples, proposals = _specs()
    merged = copy.deepcopy(examples)
    del merged["examples"][0]["judgments"]["usefulness"]
    assert any("usefulness" in e for e in check_spec(merged, proposals))
