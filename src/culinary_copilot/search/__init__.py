"""Permission-gated web search foundations (Milestone 3, Phase 5, part 2).

Offline only: fake search providers and disposable databases. No live
calls here. See docs/proposals/phase5-web-search.md (part 1, corrected
per docs/phase5-owner-decisions.md) for the verified baseline.
"""

from culinary_copilot.search.accounting import (
    PROVISIONAL_WORDING,
    SEARCH_CALL_FEE_USD,
    SearchEstimateExceeded,
    estimate_search_usd,
    search_estimate_breakdown,
)
from culinary_copilot.search.gaps import (
    GAP_KINDS,
    GapCandidate,
    gap_candidates_for_session,
)
from culinary_copilot.search.labels import (
    USDA_FOOD_SAFETY_PATH_RES,
    classify_source,
    parse_domain_list,
)
from culinary_copilot.search.minimize import (
    minimize_error,
    minimize_query,
    minimize_summary,
    minimize_tool_args,
    minimize_url,
)
from culinary_copilot.search.provenance import (
    WebAnswer,
    WebRef,
    WebSource,
)

__all__ = [
    "PROVISIONAL_WORDING",
    "GAP_KINDS",
    "SEARCH_CALL_FEE_USD",
    "USDA_FOOD_SAFETY_PATH_RES",
    "GapCandidate",
    "SearchEstimateExceeded",
    "WebAnswer",
    "WebRef",
    "WebSource",
    "classify_source",
    "estimate_search_usd",
    "gap_candidates_for_session",
    "minimize_error",
    "minimize_query",
    "minimize_summary",
    "minimize_tool_args",
    "minimize_url",
    "parse_domain_list",
    "search_estimate_breakdown",
]
