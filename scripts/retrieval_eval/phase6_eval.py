"""Phase 6 pure evaluation helpers (offline, no DB, no network).

Derivation, mechanical violations, pool building, cutoff selection and
configuration hashing. All post-retrieval filters, so one raw run (full-text
top-20 plus vector top-20 per query) derives every configuration. Live
retrieval lives in ``run_phase6.py``; scoring against labels happens in
Part C/E analysis using these primitives.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

Identity = tuple[str, str]

#: Stable abstention reason (mirrors recipes/vector_search.py).
VECTOR_CUTOFF_ABSTENTION = "vector_cutoff_abstention"

DERIVED_LIMIT = 5
RAW_N = 20


def _identity(row: dict[str, Any]) -> Identity:
    return (str(row.get("dataset_id")), str(row.get("source_id")))


def _finite_total(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def mechanical_violations(
    *,
    total_minutes: Any,
    ceiling: float | None,
    dataset_id: str,
    scope: str | None,
) -> list[str]:
    """Mechanical violation codes for one candidate: subset of time/dataset.

    Time violation: the request has a time limit and the result's stored
    total is missing, zero, negative, non-finite or above the limit.
    Dataset violation: the request has a dataset scope and the result is
    from another dataset.
    """
    violations: list[str] = []
    if ceiling is not None:
        usable = _finite_total(total_minutes)
        if usable is None or usable > float(ceiling):
            violations.append("time")
    if scope is not None and str(dataset_id) != str(scope):
        violations.append("dataset")
    return violations


def apply_cutoff_rows(rows: list[dict[str, Any]], cutoff: float | None) -> list[dict[str, Any]]:
    """Keep rows with finite cosine distance <= cutoff; None keeps all."""
    if cutoff is None:
        return list(rows)
    bound = float(cutoff)
    kept: list[dict[str, Any]] = []
    for row in rows:
        raw_distance: Any = row.get("distance")
        try:
            distance = float(raw_distance)
        except (TypeError, ValueError):
            continue
        if math.isfinite(distance) and distance <= bound:
            kept.append(row)
    return kept


def vector_abstention(
    raw: list[dict[str, Any]], kept: list[dict[str, Any]], cutoff: float | None
) -> str | None:
    """Reason code when a cutoff (not eligibility) emptied the vector side."""
    if cutoff is not None and raw and not kept:
        return VECTOR_CUTOFF_ABSTENTION
    return None


def derive_configs(
    fulltext20: list[dict[str, Any]],
    vector20: list[dict[str, Any]],
    *,
    cutoff: float | None,
    rrf_k: int = 60,
    limit: int = DERIVED_LIMIT,
) -> dict[str, Any]:
    """Derive every configuration's top-N from one query's raw record."""
    from culinary_copilot.recipes.vector_search import rrf_fuse

    cut = apply_cutoff_rows(vector20, cutoff)
    gated_nocut = vector20 if fulltext20 else []
    hybrid_c = rrf_fuse(fulltext20, cut, k=rrf_k, limit=limit)
    derived: dict[str, Any] = {
        "fulltext": fulltext20[:limit],
        "vector_a": vector20[:limit],
        "vector_c": cut[:limit],
        "hybrid_a": rrf_fuse(fulltext20, vector20, k=rrf_k, limit=limit),
        "hybrid_b": rrf_fuse(fulltext20, gated_nocut, k=rrf_k, limit=limit),
        "hybrid_c": hybrid_c,
        "vector_abstention_c": vector_abstention(vector20, cut, cutoff),
        "hybrid_abstention_c": (
            VECTOR_CUTOFF_ABSTENTION
            if cutoff is not None and vector20 and not cut and not hybrid_c
            else None
        ),
    }
    return derived


def build_pool(derived_lists: list[list[dict[str, Any]]]) -> list[Identity]:
    """Union of configuration top-N lists, deduped by exact identity."""
    seen: dict[Identity, None] = {}
    for rows in derived_lists:
        for row in rows:
            seen.setdefault(_identity(row), None)
    return sorted(seen)


def config_hash(params: dict[str, Any]) -> str:
    """SHA-256 over a configuration's canonical parameter JSON."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cutoff_grid() -> list[float | None]:
    """Fixed selection grid: none, then 0.20..1.00 in 0.01 steps."""
    grid: list[float | None] = [None]
    value = 20
    while value <= 100:
        grid.append(value / 100)
        value += 1
    return grid


def select_cutoff(curve: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the frozen cutoff selection rule mechanically.

    Each curve entry needs ``cutoff`` (None allowed), ``nonfood_abstention``
    (0..1), ``hitrate`` and ``mrr``. Returns ``chosen`` (the winning entry),
    ``constraint_met`` and the ``rule`` description. Ties: higher MRR, then
    the larger (looser) cutoff; None counts as the loosest. When no value
    reaches 0.90 abstention, the highest abstention wins (ties: higher
    hitrate) and ``constraint_met`` is False.
    """

    def looseness(entry: dict[str, Any]) -> float:
        cutoff = entry.get("cutoff")
        if cutoff is None:
            return 99.0  # 'none' counts as the loosest value
        return float(cutoff)

    eligible = [e for e in curve if float(e["nonfood_abstention"]) >= 0.90]
    if eligible:
        best = max(
            eligible,
            key=lambda e: (float(e["hitrate"]), float(e["mrr"]), looseness(e)),
        )
        return {
            "chosen": best,
            "constraint_met": True,
            "rule": (
                "highest hybrid-(c) HitRate@5 with NonFoodAbstention>=0.90; "
                "ties MRR@5 then looser cutoff"
            ),
        }
    best = max(
        curve,
        key=lambda e: (
            float(e["nonfood_abstention"]),
            float(e["hitrate"]),
            looseness(e),
        ),
    )
    return {
        "chosen": best,
        "constraint_met": False,
        "rule": "constraint failed: highest NonFoodAbstention wins (ties HitRate@5)",
    }
