"""Provisional per-search accounting (Phase 5, part 2, owner decision 4).

Status: PROVISIONAL — not an established upper bound. The live check
needs an owner decision: either a supported bound, or an explicit
change from a hard guarantee to an estimate with acknowledged overrun
risk. Every surface (code, LIVE_PLAN, proposal) carries this wording.

Estimate (about $0.025 each):
  call fee ($0.01) + content allowance (config, default 128k tokens at
  input price) + byte-bound sub-request input + fixed output cap.
Three searches cost $0.03 in call fees alone. The runner completes what
fits; it promises no session count.
"""

from __future__ import annotations

from typing import Any

PROVISIONAL_WORDING = (
    "provisional: not an established upper bound; the live check needs an owner decision"
)

SEARCH_CALL_FEE_USD = 0.01


class SearchEstimateExceeded(RuntimeError):
    """A search's reconciled cost exceeded its estimate: stop the campaign.

    Raised after recording ``search_estimate_exceeded`` in the ledger.
    Carries ``runner_stop = True`` so the tool layer re-raises it
    instead of converting it to a tool error, plus the per-search
    report. The owner must acknowledge it in the campaign history
    before preflight runs again.
    """

    runner_stop = True

    def __init__(
        self, message: str, *, label: str = "", report: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.label = label
        self.report: dict[str, Any] = dict(report or {})


def estimate_search_usd(
    *,
    input_bytes: int,
    content_allowance_tokens: int = 128_000,
    output_cap_tokens: int = 1500,
    price_input_per_1m: float = 0.10,
    price_output_per_1m: float = 0.50,
) -> float:
    """Provisional planning estimate for one search (not a bound)."""
    content = (max(0, content_allowance_tokens) / 1_000_000) * price_input_per_1m
    input_part = (max(0, int(input_bytes)) / 1_000_000) * price_input_per_1m
    output_part = (max(0, int(output_cap_tokens)) / 1_000_000) * price_output_per_1m
    return SEARCH_CALL_FEE_USD + content + input_part + output_part


def search_estimate_breakdown(
    *,
    input_bytes: int,
    content_allowance_tokens: int = 128_000,
    output_cap_tokens: int = 1500,
    price_input_per_1m: float = 0.10,
    price_output_per_1m: float = 0.50,
) -> dict[str, Any]:
    """Estimate parts with provisional status attached."""
    fee = SEARCH_CALL_FEE_USD
    content = (max(0, content_allowance_tokens) / 1_000_000) * price_input_per_1m
    input_part = (max(0, int(input_bytes)) / 1_000_000) * price_input_per_1m
    output_part = (max(0, int(output_cap_tokens)) / 1_000_000) * price_output_per_1m
    return {
        "status": PROVISIONAL_WORDING,
        "call_fee_usd": fee,
        "content_allowance_usd": content,
        "input_usd": input_part,
        "output_cap_usd": output_part,
        "total_usd": fee + content + input_part + output_part,
    }


__all__ = [
    "PROVISIONAL_WORDING",
    "SEARCH_CALL_FEE_USD",
    "SearchEstimateExceeded",
    "estimate_search_usd",
    "search_estimate_breakdown",
]
