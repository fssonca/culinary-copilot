"""Unit tests for joint relevance-and-suitability metrics (synthetic labels)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "retrieval_eval"))

from metrics import compute_metrics, joint_verdict, recall_at_k, reciprocal_rank  # noqa: E402


def _label(grade=2, checks=None, suitability="suitable", **extra):
    row: dict = {
        "case_id": "X",
        "dataset_id": "d",
        "source_id": "s",
        "topical_grade": grade,
        "constraint_checks": checks or {},
        "suitability": suitability,
    }
    row.update(extra)
    return row


def test_joint_keeps_grade_checks_and_suitability_separate() -> None:
    assert joint_verdict(_label(2, {}, "suitable")) == "suitable"
    assert joint_verdict(_label(0, {}, "suitable")) == "unsuitable"
    assert joint_verdict(_label(2, {"vegan": "fail"}, "unsuitable")) == "unsuitable"
    assert joint_verdict(_label(2, {"vegan": "unverified"}, "unknown")) == "unknown"
    # No applicable constraints: not_applicable never fails the case.
    assert joint_verdict(_label(2, {"vegan": "not_applicable"}, "suitable")) == "suitable"
    assert joint_verdict(_label(2, {}, "unknown")) == "unknown"


def test_unknowns_never_count_as_suitable() -> None:
    metrics = compute_metrics(
        [
            _label(2, {}, "suitable", case_id="A", pool_rank=1),
            _label(2, {"vegan": "unverified"}, "unknown", case_id="B", pool_rank=1),
            _label(2, {"vegan": "fail"}, "unsuitable", case_id="C", pool_rank=1),
        ]
    )
    assert metrics["cases"] == 3
    assert metrics["joint_suitable"] == 1
    assert metrics["joint_unknown"] == 1
    assert metrics["joint_unsuitable"] == 1
    assert metrics["topical_recall_at_k"] == 1.0


def test_total_miss_stays_in_denominator() -> None:
    relevant = [("d", "a"), ("d", "b")]
    assert recall_at_k(relevant, [("d", "x"), ("d", "y")]) == 0.0
    assert reciprocal_rank(relevant, [("d", "x"), ("d", "y")]) == 0.0


def test_partial_recall_counts_retrieved_relevant() -> None:
    relevant = [("d", "a"), ("d", "b"), ("d", "c"), ("d", "d")]
    retrieved = [("d", "a"), ("d", "x"), ("d", "b")]
    assert recall_at_k(relevant, retrieved) == 0.5
    assert reciprocal_rank(relevant, retrieved) == 1.0
    assert reciprocal_rank(relevant, [("d", "x"), ("d", "b")]) == 0.5


def test_irrelevant_judged_hits_do_not_count() -> None:
    relevant = [("d", "a")]
    # Grade-0 judged hits in the ranking change nothing.
    assert recall_at_k(relevant, [("d", "z"), ("d", "a")]) == 1.0
    assert reciprocal_rank(relevant, [("d", "z"), ("d", "a")]) == 0.5


def test_unjudged_hits_excluded_from_both_sides() -> None:
    relevant = [("d", "a")]
    assert recall_at_k(relevant, [("d", "unjudged"), ("d", "a")]) == 1.0
    assert recall_at_k([], [("d", "a")]) is None
    assert reciprocal_rank([], [("d", "a")]) is None


def test_cross_dataset_ids_never_cross_match() -> None:
    relevant = [("foodie", "38")]
    assert recall_at_k(relevant, [("foodcom", "38")]) == 0.0
    assert recall_at_k(relevant, [("foodie", "38")]) == 1.0
    assert recall_at_k(relevant, [("foodie", "000038")]) == 0.0


def test_shared_intent_counts_once() -> None:
    labels = [
        _label(2, {}, "suitable", case_id="H10", pool_rank=1),
        _label(2, {}, "suitable", case_id="H11", pool_rank=1),
    ]
    metrics = compute_metrics(labels, aggregate_groups={"H10": "g", "H11": "g"})
    assert metrics["cases"] == 1
    assert metrics["joint_suitable"] == 1


def test_unjudged_candidates_are_never_unsuitable() -> None:
    assert joint_verdict(_label(None, {}, "suitable")) == "unjudged"
    metrics = compute_metrics(
        [
            _label(2, {}, "suitable", case_id="A", pool_rank=1),
            _label(None, {}, "suitable", case_id="A", pool_rank=2),
            _label(None, {}, "suitable", case_id="B", pool_rank=1),
        ]
    )
    assert metrics["cases"] == 1
    assert metrics["cases_total"] == 2
    assert metrics["judged_candidates"] == 1
    assert metrics["unjudged_candidates"] == 2
    assert metrics["no_relevant_label_cases"] == ["B"]
    assert metrics["joint_suitable"] == 1
    assert metrics["joint_unsuitable"] == 0
