"""Offline unit tests for the blind scoring metrics (no DB, no network)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))

from score_blind import (  # noqa: E402
    EXPECTED,
    apply_decision,
    dcg_at_5,
    gain_of,
    hit_and_mrr,
    ndcg_at_5,
    net_wins,
)


def test_gain_zeroed_by_violation() -> None:
    assert gain_of(2, False) == 2
    assert gain_of(1, False) == 1
    assert gain_of(2, True) == 0
    assert gain_of(0, True) == 0


def test_dcg_discount_and_short_lists() -> None:
    assert dcg_at_5([]) == 0.0
    assert dcg_at_5([2]) == 2.0
    assert dcg_at_5([2, 1]) == 2.0 + 1.0 / math.log2(3)
    assert dcg_at_5([1, 1, 1, 1, 1, 1]) == dcg_at_5([1, 1, 1, 1, 1])


def test_ndcg_ideal_and_zero_ideal() -> None:
    assert ndcg_at_5([2, 1, 0], [2, 1, 0]) == 1.0
    assert ndcg_at_5([], []) == 0.0
    assert ndcg_at_5([0, 0], [0, 0]) == 0.0
    partial = ndcg_at_5([1, 0], [2, 1])
    assert 0.0 < partial < 1.0


def test_hit_and_mrr_first_rank() -> None:
    assert hit_and_mrr([]) == (False, 0.0)
    assert hit_and_mrr([1]) == (True, 1.0)
    assert hit_and_mrr([3]) == (True, 1 / 3)


def test_net_wins_counts_both_directions() -> None:
    cand = {"a": True, "b": True, "c": False, "d": False}
    full = {"a": False, "b": True, "c": True, "d": False}
    assert net_wins(cand, full) == 0  # one win, one loss


def _summary(**overrides: float) -> dict[str, dict[str, float]]:
    base = {
        "hitrate": 0.5,
        "mrr": 0.5,
        "ndcg": 0.5,
        "net_wins": 0.0,
        "violations_total": 0.0,
        "abstain_results_total": 1.0,
        "clean_abstention": 0.5,
        "fp_per_query": 1.0,
    }
    full = dict(base)
    vec = dict(base, **overrides)
    hyb = dict(base)
    return {"fulltext": full, "vector_c": vec, "hybrid_c": hyb}


def test_decision_requires_every_gate() -> None:
    passing = _summary(net_wins=2.0, mrr=0.6, ndcg=0.6, abstain_results_total=1.0)
    assert apply_decision(passing)["winner"] == "vector_c"
    failing = _summary(net_wins=1.0, mrr=0.6, ndcg=0.6)
    assert apply_decision(failing)["winner"] == "fulltext"
    viol = _summary(net_wins=5.0, mrr=0.9, ndcg=0.9, violations_total=1.0)
    assert apply_decision(viol)["winner"] == "fulltext"


def test_expected_covers_every_blind_case() -> None:
    assert sorted(EXPECTED) == [f"BLIND-{i:02d}" for i in range(1, 21)]
    assert set(EXPECTED.values()) <= {"should_retrieve", "should_abstain", "corpus_dependent"}
