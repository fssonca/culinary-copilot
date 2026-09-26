"""Phase 6 pure-helper tests (offline, no DB, no network)."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))
sys.path.insert(0, str(REPO / "src"))

from phase6_eval import (  # noqa: E402
    build_pool,
    config_hash,
    cutoff_grid,
    derive_configs,
    mechanical_violations,
    select_cutoff,
)

FOODCOM = "AkashPS11/recipes_data_food.com"
FOODIE = "odunola/foodie"


def _v(dataset: str, source: str, distance: float) -> dict[str, Any]:
    return {"dataset_id": dataset, "source_id": source, "distance": distance}


def _f(dataset: str, source: str) -> dict[str, Any]:
    return {"dataset_id": dataset, "source_id": source}


def test_time_violations() -> None:
    assert mechanical_violations(total_minutes=20, ceiling=30, dataset_id=FOODIE, scope=None) == []
    assert mechanical_violations(total_minutes=30, ceiling=30, dataset_id=FOODIE, scope=None) == []
    assert mechanical_violations(total_minutes=31, ceiling=30, dataset_id=FOODIE, scope=None) == [
        "time"
    ]
    for bad in (None, 0, 0.0, -5.0, float("nan"), float("inf"), "20", True):
        assert mechanical_violations(
            total_minutes=bad, ceiling=30, dataset_id=FOODIE, scope=None
        ) == ["time"]
    # No ceiling: missing totals are not violations.
    assert (
        mechanical_violations(total_minutes=None, ceiling=None, dataset_id=FOODIE, scope=None) == []
    )


def test_dataset_violations() -> None:
    assert (
        mechanical_violations(total_minutes=10, ceiling=None, dataset_id=FOODIE, scope=FOODIE) == []
    )
    assert mechanical_violations(
        total_minutes=10, ceiling=None, dataset_id=FOODCOM, scope=FOODIE
    ) == ["dataset"]
    assert (
        mechanical_violations(total_minutes=10, ceiling=None, dataset_id=FOODCOM, scope=None) == []
    )


def test_derive_filters_and_gate() -> None:
    fulltext = [_f(FOODIE, "ft1"), _f(FOODIE, "ft2")]
    vector = [_v(FOODIE, "near", 0.3), _v(FOODIE, "far", 0.8)]
    out = derive_configs(fulltext, vector, cutoff=None, rrf_k=60, limit=5)
    assert [r["source_id"] for r in out["vector_a"]] == ["near", "far"]
    assert [r["source_id"] for r in out["vector_c"]] == ["near", "far"]
    assert [r["source_id"] for r in out["hybrid_b"]] == [r["source_id"] for r in out["hybrid_a"]]
    assert out["vector_abstention_c"] is None

    cut = derive_configs(fulltext, vector, cutoff=0.5, rrf_k=60, limit=5)
    assert [r["source_id"] for r in cut["vector_c"]] == ["near"]
    assert all(r["source_id"] in {"ft1", "ft2", "near"} for r in cut["hybrid_c"])
    assert "far" not in [r["source_id"] for r in cut["hybrid_c"]]

    strict = derive_configs(fulltext, vector, cutoff=0.1, rrf_k=60, limit=5)
    assert strict["vector_c"] == []
    assert strict["vector_abstention_c"] == "vector_cutoff_abstention"
    # Hybrid keeps full-text exactly as they are.
    assert [r["source_id"] for r in strict["hybrid_c"]] == ["ft1", "ft2"]
    assert strict["hybrid_abstention_c"] is None


def test_hybrid_abstention_only_when_cutoff_empties_fusion() -> None:
    vector = [_v(FOODIE, "far", 0.9)]
    empty = derive_configs([], vector, cutoff=0.1, rrf_k=60, limit=5)
    assert empty["hybrid_c"] == []
    assert empty["hybrid_abstention_c"] == "vector_cutoff_abstention"
    nocut = derive_configs([], vector, cutoff=None, rrf_k=60, limit=5)
    assert nocut["hybrid_abstention_c"] is None
    noraw = derive_configs([], [], cutoff=0.1, rrf_k=60, limit=5)
    assert noraw["hybrid_abstention_c"] is None


def test_derive_gate_with_empty_fulltext() -> None:
    vector = [_v(FOODIE, "v1", 0.2)]
    out = derive_configs([], vector, cutoff=None, rrf_k=60, limit=5)
    assert [r["source_id"] for r in out["hybrid_a"]] == ["v1"]
    assert out["hybrid_b"] == []


def test_pool_dedups_exact_identity() -> None:
    rows = [_f(FOODCOM, "000159"), _f(FOODIE, "000159"), _f(FOODIE, "000159")]
    assert build_pool([rows]) == [(FOODCOM, "000159"), (FOODIE, "000159")]


def test_cutoff_grid_shape() -> None:
    grid = cutoff_grid()
    assert grid[0] is None
    assert grid[1] == 0.20 and grid[-1] == 1.00
    assert len(grid) == 82


def test_select_cutoff_prefers_hitrate_then_mrr_then_looser() -> None:
    curve = [
        {"cutoff": 0.5, "nonfood_abstention": 0.95, "hitrate": 0.6, "mrr": 0.5},
        {"cutoff": 0.6, "nonfood_abstention": 0.95, "hitrate": 0.7, "mrr": 0.4},
        {"cutoff": 0.7, "nonfood_abstention": 0.80, "hitrate": 0.9, "mrr": 0.9},
    ]
    result = select_cutoff(curve)
    assert result["chosen"]["cutoff"] == 0.6
    assert result["constraint_met"] is True

    tie = [
        {"cutoff": 0.5, "nonfood_abstention": 1.0, "hitrate": 0.6, "mrr": 0.5},
        {"cutoff": 0.6, "nonfood_abstention": 1.0, "hitrate": 0.6, "mrr": 0.5},
    ]
    assert select_cutoff(tie)["chosen"]["cutoff"] == 0.6

    failed = [
        {"cutoff": 0.5, "nonfood_abstention": 0.5, "hitrate": 0.9, "mrr": 0.9},
        {"cutoff": 0.6, "nonfood_abstention": 0.7, "hitrate": 0.1, "mrr": 0.1},
    ]
    result = select_cutoff(failed)
    assert result["chosen"]["cutoff"] == 0.6
    assert result["constraint_met"] is False


def test_config_hash_stable() -> None:
    params: dict[str, Any] = {"mode": "hybrid", "rrf_k": 60, "cutoff": 0.5}
    assert config_hash(params) == config_hash(dict(params))
    assert len(config_hash(params)) == 64
    assert math.isfinite(1.0)
