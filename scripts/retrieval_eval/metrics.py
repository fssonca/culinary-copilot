"""Joint relevance-and-suitability metrics for recorded retrieval labels.

Separation of concerns:

- ``topical_grade`` (0/1/2) measures dish/ingredient topical relevance only.
- ``constraint_checks`` map a constraint code to ``pass`` / ``fail`` /
  ``unverified`` / ``not_applicable``. No constraints means
  ``not_applicable``: such cases never fail on constraints.
- ``suitability`` is the joint verdict: ``suitable`` / ``unsuitable`` /
  ``unknown`` / ``unresolved``. Unknowns are reported as their own share,
  never silently counted as suitable.

A case is jointly suitable when it is topically relevant, every
applicable constraint check passes (or is not applicable), and the
recorded suitability is ``suitable``. Cases with no applicable
constraints fall back to topical relevance alone. Shared-intent groups
(see the case file ``aggregate_group``) contribute once, using the best
topical grade and the least favorable suitability.
"""

from __future__ import annotations

from typing import Any

NOT_APPLICABLE = "not_applicable"
UNVERIFIED = "unverified"
SUITABLE = "suitable"

# Recorded review-layer vocabularies (checkpoint1 corrections) normalize to
# the canonical pass/fail/unverified/not_applicable and
# suitable/unsuitable/unknown/not_applicable used here.
CHECK_STATUS_MAP = {
    "satisfied_by_reported_value": "pass",
    "violated_by_source": "fail",
    "unresolved": UNVERIFIED,
    "pass": "pass",
    "fail": "fail",
    "unverified": UNVERIFIED,
    "not_applicable": NOT_APPLICABLE,
    "n/a": NOT_APPLICABLE,
    "na": NOT_APPLICABLE,
}
SUITABILITY_MAP = {
    "supported": SUITABLE,
    "suitable": SUITABLE,
    "not_suitable": "unsuitable",
    "unsuitable": "unsuitable",
    "unresolved": "unknown",
    "unknown": "unknown",
    "not_applicable": NOT_APPLICABLE,
}


def _normalize_check(value: Any) -> str:
    text = str(value or "").strip().lower()
    return CHECK_STATUS_MAP.get(text, UNVERIFIED)


def _normalize_suitability(value: Any) -> str:
    text = str(value or "").strip().lower()
    return SUITABILITY_MAP.get(text, "unknown")


def normalize_label(label: dict[str, Any]) -> dict[str, Any]:
    """Convert a recorded review-layer label to canonical metric inputs."""
    raw_checks = label.get("constraint_checks") or {}
    checks: dict[str, str] = {}
    if isinstance(raw_checks, dict):
        for code, raw in raw_checks.items():
            checks[str(code)] = _normalize_check(raw)
    elif isinstance(raw_checks, list):
        for entry in raw_checks:
            if isinstance(entry, dict) and entry.get("constraint"):
                checks[str(entry["constraint"])] = _normalize_check(entry.get("status"))
    out = dict(label)
    out["constraint_checks"] = checks
    out["suitability"] = _normalize_suitability(label.get("suitability"))
    return out


UNJUDGED = "unjudged"


def joint_verdict(label: dict[str, Any]) -> str:
    """Joint verdict for one recorded label.

    One of suitable / unsuitable / unknown / unjudged. A missing topical
    grade means the candidate was never judged: it resolves to
    ``unjudged`` and must never be counted as unsuitable or irrelevant.
    ``na`` is returned only when topical relevance itself is not asserted
    (caller signals this separately); otherwise unknowns resolve to
    ``unknown`` and failures to ``unsuitable``.
    """
    label = normalize_label(label)
    raw_grade = label.get("topical_grade")
    if raw_grade is None:
        return UNJUDGED
    grade = int(raw_grade)
    if grade < 1:
        return "unsuitable"
    checks = label.get("constraint_checks") or {}
    applicable = [code for code, raw in checks.items() if _normalize_check(raw) != NOT_APPLICABLE]
    failed = [code for code in applicable if _normalize_check(checks[code]) == "fail"]
    if failed:
        return "unsuitable"
    unknown_checks = [code for code in applicable if _normalize_check(checks[code]) == UNVERIFIED]
    suitability = str(label.get("suitability") or "").strip().lower()
    if unknown_checks or suitability in ("unknown", ""):
        return "unknown"
    if not applicable:
        # No constraints: suitability is not_applicable and the joint verdict
        # falls back to topical relevance (already established above).
        if suitability in (SUITABLE, "", NOT_APPLICABLE):
            return SUITABLE
        return suitability
    return SUITABLE if suitability == SUITABLE else "unsuitable"


Pair = tuple[str, str]


def recall_at_k(relevant: list[Pair], retrieved: list[Pair], k: int = 5) -> float | None:
    """Recall@k over a frozen relevant set; None when the set is empty.

    Eligibility comes from the frozen judgments, never from retrieval
    output: an empty intersection is a scored miss (0.0), not an exclusion.
    Identity matching is on exact ``(dataset_id, source_id)`` pairs, so the
    same ``source_id`` in two datasets never cross-matches.
    """
    if not relevant:
        return None
    top = retrieved[:k]
    hits = sum(1 for pair in relevant if pair in top)
    return hits / len(relevant)


def reciprocal_rank(relevant: list[Pair], retrieved: list[Pair], k: int = 5) -> float | None:
    """Reciprocal rank of the first relevant hit in the top-k, else 0.0."""
    if not relevant:
        return None
    for rank, pair in enumerate(retrieved[:k], 1):
        if pair in relevant:
            return 1.0 / rank
    return 0.0


def _collapse_shared_intents(
    labels: list[dict[str, Any]], aggregate_groups: dict[str, str]
) -> list[dict[str, Any]]:
    """Collapse labels sharing an aggregate intent into one entry each."""
    order = ["unsuitable", "unknown", SUITABLE]
    grouped: dict[str, list[dict[str, Any]]] = {}
    single: list[dict[str, Any]] = []
    for label in labels:
        key = aggregate_groups.get(str(label.get("case_id", "")))
        if key is None:
            single.append(label)
        else:
            grouped.setdefault(key, []).append(label)
    out = list(single)
    for members in grouped.values():
        best = max(members, key=lambda m: int(m.get("topical_grade", 0) or 0))
        verdicts = [joint_verdict(m) for m in members]
        worst = min(verdicts, key=lambda v: order.index(v) if v in order else 1)
        merged = dict(best)
        merged["suitability"] = worst if worst != SUITABLE else best.get("suitability")
        merged["aggregate_members"] = len(members)
        out.append(merged)
    return out


def compute_metrics(
    labels: list[dict[str, Any]],
    *,
    aggregate_groups: dict[str, str] | None = None,
    k: int = 5,
) -> dict[str, Any]:
    """Topical Recall@k / MRR plus joint suitability shares.

    ``aggregate_groups`` maps case_id to a shared intent id counted once.
    Rankings per case order by recorded pool position when present,
    otherwise by (dataset_id, source_id).
    """
    collapsed = _collapse_shared_intents(labels, aggregate_groups or {})
    by_case: dict[str, list[dict[str, Any]]] = {}
    for label in collapsed:
        by_case.setdefault(str(label.get("case_id", "")), []).append(label)
    recalls: list[float] = []
    reciprocal: list[float] = []
    joint = {"suitable": 0, "unsuitable": 0, "unknown": 0}
    judged_cases = 0
    judged_candidates = 0
    unjudged_candidates = 0
    no_relevant_label_cases: list[str] = []
    for case_id, members in by_case.items():
        judged = [m for m in members if m.get("topical_grade") is not None]
        unjudged_candidates += len(members) - len(judged)
        judged_candidates += len(judged)
        if not judged:
            # No judged candidates: excluded from recall/joint denominators
            # and reported separately. Never a verified no-match case.
            no_relevant_label_cases.append(case_id)
            continue
        judged_cases += 1
        ranked = sorted(
            judged,
            key=lambda m: (
                m.get("pool_rank", 999),
                str(m.get("dataset_id", "")),
                str(m.get("source_id", "")),
            ),
        )
        top = ranked[:k]
        relevant = [m for m in top if int(m.get("topical_grade", 0) or 0) >= 1]
        recalls.append(1.0 if relevant else 0.0)
        first = next(
            (i for i, m in enumerate(top, 1) if int(m.get("topical_grade", 0) or 0) >= 1),
            None,
        )
        reciprocal.append(1.0 / first if first else 0.0)
        # Case-level joint: suitable when any top-k member is jointly
        # suitable; unknown when none is but some member is unresolved;
        # otherwise unsuitable.
        verdicts = [joint_verdict(m) for m in top]
        if any(v == SUITABLE for v in verdicts):
            joint["suitable"] += 1
        elif any(v == "unknown" for v in verdicts):
            joint["unknown"] += 1
        else:
            joint["unsuitable"] += 1
    n = judged_cases
    return {
        "cases": n,
        "cases_total": len(by_case),
        "cases_with_judgments": judged_cases,
        "judged_candidates": judged_candidates,
        "unjudged_candidates": unjudged_candidates,
        "no_relevant_label_cases": sorted(no_relevant_label_cases),
        "topical_recall_at_k": sum(recalls) / n if n else 0.0,
        "topical_mrr_at_k": sum(reciprocal) / n if n else 0.0,
        "k": k,
        "joint_suitable": joint["suitable"],
        "joint_unsuitable": joint["unsuitable"],
        "joint_unknown": joint["unknown"],
    }
