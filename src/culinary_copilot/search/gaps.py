"""Gap investigation candidates (Phase 5, part 2, owner decision 9).

Events plus export: candidates are derived views over existing
session_events plus a git-ignored export. Nothing is imported
automatically; only review confirms a cause. Triggers produce
investigation candidates, never confirmed causes.
"""

from __future__ import annotations

from typing import Any

GAP_KINDS = (
    "missing_recipe",
    "missing_ingredient_alias",
    "retrieval_miss",
    "missing_technique",
    "unreliable_metadata",
)

GAP_ALTERNATIVES: dict[str, str] = {
    "missing_recipe": "zero results may be missing data or poor retrieval, not a corpus gap",
    "missing_ingredient_alias": (
        "an unknown Epicure ingredient may be vocabulary coverage, not a missing alias"
    ),
    "retrieval_miss": "zero results may be missing data or poor retrieval",
    "missing_technique": "zero technique hits may be missing data or poor retrieval",
    "unreliable_metadata": "an invented quantity may be a generation error, not source metadata",
}


class GapCandidate(dict[str, Any]):
    """One candidate: signal + proposed category + provenance + status."""


def _tool_events(events: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events:
        etype = getattr(event, "event_type", "")
        payload = getattr(event, "payload", None) or {}
        if etype == "tool_call":
            out.append(dict(payload))
    return out


def gap_candidates_for_session(
    *,
    session_id: str,
    events: list[Any],
    minimized_query: str = "",
    source_urls: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic candidate derivation from existing signals.

    Each record holds: the observed signal, the proposed category (one
    of the five kinds), provenance (session_id, call ids, timestamps,
    minimized query, URLs), review_status "unreviewed", and the
    alternative explanations under review. No model judgment.
    """
    calls = _tool_events(events)
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        by_tool.setdefault(str(call.get("tool") or ""), []).append(call)

    def _zero_result(tool: str) -> dict[str, Any] | None:
        for call in by_tool.get(tool, []):
            if call.get("outcome") == "ok" and call.get("result_count") == 0:
                return call
        return None

    candidates: list[dict[str, Any]] = []
    urls = list(source_urls or [])

    recipe_zero = _zero_result("search_recipes")
    technique_zero = _zero_result("search_techniques")
    web_ok = [c for c in by_tool.get("search_web", []) if c.get("outcome") == "ok"]

    if recipe_zero is not None and web_ok:
        candidates.append(
            {
                "observed_signal": (
                    "search_recipes result_count==0 then search_web ok in same session"
                ),
                "proposed_category": "missing_recipe",
                "alternative": GAP_ALTERNATIVES["missing_recipe"],
                "provenance": {
                    "session_id": session_id,
                    "call_ids": [
                        str(recipe_zero.get("call_id") or ""),
                        str(web_ok[0].get("call_id") or ""),
                    ],
                    "minimized_query": minimized_query,
                    "urls": urls[:5],
                },
                "review_status": "unreviewed",
            }
        )
    epicure_unknown = [
        c
        for tool in (
            "find_balanced_pairings",
            "find_conventional_pairings",
            "find_flavor_pairings",
            "find_substitutions",
        )
        for c in by_tool.get(tool, [])
        if c.get("reason") == "tool_invalid_arguments"
    ]
    if epicure_unknown:
        candidates.append(
            {
                "observed_signal": "epicure tool_invalid_arguments (unknown ingredient) in session",
                "proposed_category": "missing_ingredient_alias",
                "alternative": GAP_ALTERNATIVES["missing_ingredient_alias"],
                "provenance": {
                    "session_id": session_id,
                    "call_ids": [str(c.get("call_id") or "") for c in epicure_unknown[:5]],
                    "minimized_query": minimized_query,
                    "urls": urls[:5],
                },
                "review_status": "unreviewed",
            }
        )
    if recipe_zero is not None and any(
        c.get("tool") == "get_recipe" and c.get("outcome") == "ok"
        for c in by_tool.get("get_recipe", [])
    ):
        candidates.append(
            {
                "observed_signal": "zero-result search then get_recipe hit on a manual id",
                "proposed_category": "retrieval_miss",
                "alternative": GAP_ALTERNATIVES["retrieval_miss"],
                "provenance": {
                    "session_id": session_id,
                    "call_ids": [str(recipe_zero.get("call_id") or "")],
                    "minimized_query": minimized_query,
                    "urls": urls[:5],
                },
                "review_status": "unreviewed",
            }
        )
    if technique_zero is not None and web_ok:
        candidates.append(
            {
                "observed_signal": "zero-hit search_techniques then search_web on the same terms",
                "proposed_category": "missing_technique",
                "alternative": GAP_ALTERNATIVES["missing_technique"],
                "provenance": {
                    "session_id": session_id,
                    "call_ids": [str(technique_zero.get("call_id") or "")],
                    "minimized_query": minimized_query,
                    "urls": urls[:5],
                },
                "review_status": "unreviewed",
            }
        )
    return candidates


__all__ = ["GAP_ALTERNATIVES", "GAP_KINDS", "GapCandidate", "gap_candidates_for_session"]
