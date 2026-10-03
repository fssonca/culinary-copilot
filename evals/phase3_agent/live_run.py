#!/usr/bin/env python3
"""Phase 3 live runner (built and tested only; the live run is NOT authorized).

Drives the 8 frozen scenarios in ``live_scenarios.json`` against the
bounded agent loop with 2 attempts max per scenario phase, records a
spend ledger, snapshots, and grades — then reports first-attempt
outcomes separately from after-retry.

Safety (all enforced, all tested with fakes on disposable databases):

- refuses without ``--live --yes --ceiling-usd <= 0.15`` plus
  ``--expect-db-name/--expect-db-host``; ``--fake`` runs the whole
  pipeline against the fake provider on a disposable database;
- preflight refuses on: unknown model / pricing, ceiling breach,
  DB-guard mismatch, ``HF_HUB_OFFLINE != 1``, disabled Epicure (except
  the unavailable scenario's own override), a failing Epicure
  cache-only probe, missing technique tables, or an unverifiable
  snapshot;
- every paid call (model turns, query embeddings, retries) is
  reserved before the call from a true-upper-bound local input count
  (UTF-8 byte length of the full serialized request — input items,
  tools array, text.format schema — plus per-item and per-request
  overhead) plus maximum permitted output, reconciled with reported
  usage after; any excess over the reservation is recorded as
  reservation_breach and stops the run with contact-operator and no
  further calls; ambiguous failures keep the reservation, and calls
  that do not fit the remaining cap are refused;
- trial isolation: a fresh session per attempt (a retry never
  inherits answers or evidence); ask-and-resume stays inside one
  session with scripted answers frozen in the scenario file; the
  runner keeps a manifest of every session it created; pre/post
  snapshots prove no writes outside sessions/session_events.

Raw model responses and trajectories go under ``data/phase3-live/``
(git-ignored). The committed summary
(``evals/phase3_agent/live-summary.json``) is written only by a live
run (``--fake`` writes its summary under the raw dir instead).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from culinary_copilot.search.accounting import (  # noqa: E402
    SEARCH_CALL_FEE_USD,
    SearchEstimateExceeded,
)

EVALS_DIR = REPO_ROOT / "evals" / "phase3_agent"
DEFAULT_SCENARIOS = EVALS_DIR / "live_scenarios_v2.json"
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "phase3-live"
LIVE_SUMMARY = EVALS_DIR / "live-summary.json"
SPEND_HISTORY = DEFAULT_RAW_DIR / "spend-history.json"


def recorded_entry_usd(entry: dict[str, Any]) -> float:
    """Recorded cost of one entry.

    Stored history entries carry the settled ``usd`` amount (written by
    ``append_spend_history``); the live ledger path (reserve →
    reconcile/keep/breach) reads ``used_usd``/``reserved_usd`` instead
    and is unchanged.
    """
    if "usd" in entry:
        return float(entry.get("usd") or 0.0)
    decision = entry.get("decision")
    if decision == "reconciled":
        return float(entry.get("used_usd") or 0.0)
    if decision == "reservation_breach":
        # The bound failed but the call was billed: the used amount is
        # what was spent, conservatively.
        return float(entry.get("used_usd") or entry.get("reserved_usd") or 0.0)
    if decision == "kept-ambiguous":
        return float(entry.get("reserved_usd") or entry.get("usd") or 0.0)
    if decision == "search_estimate_exceeded":
        # The search was billed above its estimate: the reconciled
        # used amount is what was spent, conservatively.
        return float(entry.get("used_usd") or entry.get("reserved_usd") or 0.0)
    return 0.0


def load_spend_history(path: Path) -> list[dict[str, Any]]:
    """Prior run records (empty when the file does not exist yet)."""
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    runs = body.get("runs") if isinstance(body, dict) else None
    return list(runs) if isinstance(runs, list) else []


def recorded_spend_total(path: Path) -> float:
    """Conservative prior spend across all recorded runs."""
    total = 0.0
    for run in load_spend_history(path):
        for entry in run.get("entries", []) or []:
            if isinstance(entry, dict):
                total += recorded_entry_usd(entry)
    return total


def append_spend_history(
    path: Path,
    *,
    model: str,
    entries: list[dict[str, Any]],
    ceiling_usd: float,
    attempt: int | None = None,
) -> float:
    """Append this run's entries with a timestamp; returns the new total.

    ``attempt`` (the live attempt number) is stored when given;
    otherwise it defaults to one past the highest stored attempt, so
    new runs keep a monotonic attempt sequence.
    """
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(body, dict):
            body = {}
    except (OSError, ValueError):
        body = {}
    runs = body.get("runs")
    if not isinstance(runs, list):
        runs = []
        body["runs"] = runs
    if attempt is None:
        try:
            attempt = max(int(r.get("attempt") or 0) for r in runs) + 1
        except ValueError:
            attempt = 1
    body["ceiling_usd"] = float(ceiling_usd)
    runs.append(
        {
            "run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "attempt": attempt,
            "model": model,
            "entries": [
                {
                    "label": e.get("label"),
                    "decision": e.get("decision"),
                    "usd": recorded_entry_usd(e),
                    **({"kind": e.get("kind")} if e.get("kind") else {}),
                }
                for e in entries
                if isinstance(e, dict)
            ],
        }
    )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return recorded_spend_total(path)


LIVE_CAP_USD = 0.15
MAX_ATTEMPTS = 2
#: Phase 5 campaign ledger cap (owner item 9, prepare-only): $0.10 of the
#: $1.00 M3 ceiling. Covers agent turns, search sub-requests, tool fees,
#: embeddings and retries in one ledger.
PHASE5_CAP_USD = 0.10
#: Per-search accounting status (owner follow-up 2026-10-02, option A):
#: an estimate with acknowledged overrun risk. Agent turns and
#: embeddings keep their hard reservations; search sub-requests do not.
SEARCH_RESERVATION_STATUS = (
    "estimate with acknowledged overrun risk "
    "(phase5-decision-4-2026-10-02); agent turns and embeddings keep "
    "hard reservations"
)
#: Exact owner-acknowledgment value for search estimates (decision 4,
#: option A). Preflight checks this exact string and records it.
SEARCH_ACK_VALUE = "phase5-decision-4-2026-10-02"
#: Provisional per-search planning estimate (not a bound).
SEARCH_ESTIMATE_USD = 0.025
#: Campaign search cap: at most 4 paid searches across all Phase 5 runs.
PHASE5_CAMPAIGN_SEARCH_CAP = 4
#: Persisted Phase 5 campaign history (same format as the Phase 3 one).
PHASE5_HISTORY = REPO_ROOT / "data" / "phase5-live" / "spend-history.json"
#: Budget pools: the P3-L-13 ask-and-resume run charges Phase 3, the
#: search campaign charges Phase 5.
BUDGET_POOLS: dict[str, dict[str, Any]] = {
    "phase3": {"history": SPEND_HISTORY, "cap_usd": LIVE_CAP_USD},
    "phase5": {"history": PHASE5_HISTORY, "cap_usd": PHASE5_CAP_USD},
}
#: Live-check search slots per session (owner item 3): at most 2, inside
#: the code limit of 3.
PHASE5_MAX_SEARCHES_PER_SESSION = 2


# --- input bound ---------------------------------------------------------------

#: Reservation assumption: one token spans at least one UTF-8 byte of
#: the serialized request, so the UTF-8 byte length of the full
#: serialized request is a true upper bound on input tokens (strictly
#: above any chars-per-token heuristic). Two overhead constants cover
#: Responses-envelope framing the SDK adds around our payload: per
#: input item (role/type wrappers, call ids) and per request (model
#: name, reasoning effort, the text.format envelope, tool_choice).
_RESERVE_PER_ITEM_TOKENS = 16
_RESERVE_REQUEST_OVERHEAD_TOKENS = 256


def _text_format_schema(response_model: Any) -> Any:
    """Serializable form of the structured-output schema for the bound.

    The SDK sends the Pydantic class itself (converted to
    ``text.format`` internally via ``type_to_text_format_param``,
    which is larger than the plain JSON schema); the reservation
    measures that converted form when the SDK helper is importable,
    falling back to ``model_json_schema`` and then the class repr
    (fakes only).
    """
    if response_model is not None:
        try:
            from openai.lib._parsing._responses import type_to_text_format_param

            return type_to_text_format_param(response_model)
        except Exception:
            pass
    schema_fn = getattr(response_model, "model_json_schema", None)
    if callable(schema_fn):
        try:
            return schema_fn()
        except Exception:
            pass
    return str(response_model)


def estimate_request_tokens(
    *,
    input_items: Any,
    tools: Any,
    response_model: Any = None,
    tool_choice: Any = None,
    max_output_tokens: Any = None,
) -> int:
    """True-upper-bound input reservation for one model turn.

    Measured over the full request the SDK will send — input items,
    the tools array and the text.format JSON schema, as serialized —
    plus the per-item and per-request overhead above.
    """
    payload = {
        "input_items": input_items,
        "tools": tools,
        "text_format": _text_format_schema(response_model),
        "tool_choice": tool_choice,
        "max_output_tokens": max_output_tokens,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    n_items = len(input_items) if isinstance(input_items, list) else 0
    return (
        len(raw.encode("utf-8"))
        + _RESERVE_PER_ITEM_TOKENS * n_items
        + _RESERVE_REQUEST_OVERHEAD_TOKENS
    )


def estimate_embedding_request_tokens(texts: list[str], *, attempts: int = 1) -> int:
    """True-upper-bound input reservation for query embeddings.

    Same approach as model turns: UTF-8 byte length of the serialized
    texts plus per-text and per-request overhead, times the attempts
    the call may bill (the runner forces provider retries to zero, so
    one wrapped call bills at most ``attempts`` requests).
    """
    items = list(texts)
    raw = json.dumps({"texts": items}, sort_keys=True, default=str)
    single = (
        len(raw.encode("utf-8"))
        + _RESERVE_PER_ITEM_TOKENS * len(items)
        + _RESERVE_REQUEST_OVERHEAD_TOKENS
    )
    return single * max(1, int(attempts))


# --- spend ledger --------------------------------------------------------------


class BudgetExhausted(RuntimeError):
    """A reservation refusal: ending the run cleanly, not a provider failure.

    Raised by the ledger wrappers when a call's reservation does not fit
    the remaining cap. run_agent maps it to an internal error terminal;
    run_scenario_live detects it through the exception chain and marks
    the scenario "not_completed: budget" (never counted as an agent or
    provider failure in the grades).

    Carries ``runner_stop = True`` so the tool layer re-raises it
    instead of converting it to a tool error.
    """

    runner_stop = True


class ReservationBreach(RuntimeError):
    """Reported usage exceeded the reservation: stop everything.

    Raised by the ledger wrappers after recording the breach in the
    ledger. Carries ``reservation_breach = True`` so the agent loop's
    turn-level handler re-raises it instead of mapping it to a
    provider error; run_scenario_live maps it to a run stop with
    reason ``contact-operator`` and no further calls.
    """

    def __init__(self, message: str, *, label: str = "") -> None:
        super().__init__(message)
        self.reservation_breach = True
        self.label = label


def _caused_by_reservation_breach(exc: BaseException) -> bool:
    """True when a ReservationBreach is anywhere in the exception chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if bool(getattr(current, "reservation_breach", False)):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def find_unacknowledged_breach(path: Path | str) -> dict[str, Any] | None:
    """First unacknowledged ``reservation_breach`` entry, if any.

    Acknowledging is a manual owner step: an object with the run's
    ``run_utc`` and the entry's ``label`` under the history file's
    top-level ``breach_acknowledgments`` list (documented in
    LIVE_PLAN.md). Returns ``{"run_utc": ..., "label": ...}`` or None.
    """
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    acked = body.get("breach_acknowledgments")
    acked_set = set()
    if isinstance(acked, list):
        for item in acked:
            if isinstance(item, dict):
                acked_set.add((str(item.get("run_utc") or ""), str(item.get("label") or "")))
    runs = body.get("runs")
    if not isinstance(runs, list):
        return None
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_utc = str(run.get("run_utc") or "")
        for entry in run.get("entries", []) or []:
            if isinstance(entry, dict) and entry.get("decision") == "reservation_breach":
                key = (run_utc, str(entry.get("label") or ""))
                if key not in acked_set:
                    return {"run_utc": run_utc, "label": str(entry.get("label") or "")}
    return None


#: Ledger/history decisions that count as a dispatched paid search
#: (ambiguous dispatches count; unaffordable refusals do not).
SEARCH_DISPATCHED_DECISIONS = frozenset(
    {"reconciled", "kept-ambiguous", "reservation_breach", "search_estimate_exceeded"}
)


def count_campaign_searches(path: Path | str) -> int:
    """Paid search dispatches in a campaign history (0 when absent)."""
    total = 0
    for run in load_spend_history(Path(path)):
        if not isinstance(run, dict):
            continue
        for entry in run.get("entries", []) or []:
            if (
                isinstance(entry, dict)
                and entry.get("kind") == "search"
                and entry.get("decision") in SEARCH_DISPATCHED_DECISIONS
            ):
                total += 1
    return total


def find_unacknowledged_estimate_breach(path: Path | str) -> dict[str, Any] | None:
    """First unacknowledged ``search_estimate_exceeded`` entry, if any.

    Acknowledging is a manual owner step: an object with the run's
    ``run_utc`` and the entry's ``label`` under the history file's
    top-level ``estimate_acknowledgments`` list (documented in
    LIVE_PLAN.md). Returns ``{"run_utc": ..., "label": ...}`` or None.
    """
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    acked = body.get("estimate_acknowledgments")
    acked_set = set()
    if isinstance(acked, list):
        for item in acked:
            if isinstance(item, dict):
                acked_set.add((str(item.get("run_utc") or ""), str(item.get("label") or "")))
    runs = body.get("runs")
    if not isinstance(runs, list):
        return None
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_utc = str(run.get("run_utc") or "")
        for entry in run.get("entries", []) or []:
            if isinstance(entry, dict) and entry.get("decision") == "search_estimate_exceeded":
                key = (run_utc, str(entry.get("label") or ""))
                if key not in acked_set:
                    return {"run_utc": run_utc, "label": str(entry.get("label") or "")}
    return None


class SpendLedger:
    """Run-level ledger: reserve before each paid call, reconcile after.

    Every entry is priced by its own model: chat turns at the chat-model
    rate, query embeddings at the embedding rate from
    ``embeddings/registry.py``. The entry records the model, its kind,
    and the pricing version used.
    """

    def __init__(self, *, model: str, ceiling_usd: float) -> None:
        self.model = model
        self.ceiling_usd = float(ceiling_usd)
        self.remaining_usd = float(ceiling_usd)
        self.spent_usd = 0.0
        self.entries: list[dict[str, Any]] = []

    def _cost(
        self, model: str, kind: str, input_tokens: int, output_tokens: int
    ) -> tuple[float | None, str | None]:
        if kind == "embedding":
            from culinary_copilot.embeddings.registry import (
                EMBED_PRICING_VERSION,
            )
            from culinary_copilot.embeddings.registry import (
                estimate_cost_usd as estimate_embed_cost_usd,
            )

            return (
                estimate_embed_cost_usd(int(input_tokens) + int(output_tokens), model),
                EMBED_PRICING_VERSION,
            )
        from culinary_copilot.llm.models import PRICING_VERSION
        from culinary_copilot.recommendations.pricing import estimate_cost_usd

        return estimate_cost_usd(int(input_tokens), int(output_tokens), model), PRICING_VERSION

    def reserve(
        self,
        label: str,
        *,
        input_tokens: int,
        max_output: int,
        model: str | None = None,
        kind: str = "chat",
    ) -> bool:
        """Reserve input + maximum output. False (no state change) when unaffordable."""
        priced_model = model or self.model
        cost, pricing_version = self._cost(priced_model, kind, int(input_tokens), int(max_output))
        if cost is None:
            return False
        if cost > self.remaining_usd:
            self.entries.append(
                {
                    "label": label,
                    "decision": "refused",
                    "reserved_usd": cost,
                    "model": priced_model,
                    "kind": kind,
                    "pricing_version": pricing_version,
                }
            )
            return False
        self.remaining_usd -= cost
        self.entries.append(
            {
                "label": label,
                "decision": "reserved",
                "reserved_in": int(input_tokens),
                "reserved_out": int(max_output),
                "reserved_usd": cost,
                "model": priced_model,
                "kind": kind,
                "pricing_version": pricing_version,
            }
        )
        return True

    def reconcile(self, label: str, *, reported_in: int | None, reported_out: int | None) -> None:
        """Replace the reservation with reported usage (release the rest).

        Raises :class:`ReservationBreach` (after recording it) when the
        reported usage exceeds the reservation in input tokens, output
        tokens, or USD: the run must stop with ``contact-operator`` and
        make no further calls.
        """
        for entry in reversed(self.entries):
            if entry.get("label") == label and entry.get("decision") == "reserved":
                if reported_in is None or reported_out is None:
                    entry["decision"] = "kept-ambiguous"
                    self.spent_usd += float(entry["reserved_usd"])
                    return
                reserved_in = int(entry.get("reserved_in") or 0)
                reserved_out = int(entry.get("reserved_out") or 0)
                if int(reported_in) > reserved_in or int(reported_out) > reserved_out:
                    breached = self.record_breach(
                        label,
                        reported_in=int(reported_in),
                        reported_out=int(reported_out),
                    )
                    raise ReservationBreach(
                        f"reservation breach on {label}: {breached.get('breach')} "
                        "(contact operator; no further calls)",
                        label=label,
                    )
                actual, _ = self._cost(
                    str(entry.get("model") or self.model),
                    str(entry.get("kind") or "chat"),
                    int(reported_in),
                    int(reported_out),
                )
                actual = actual or 0.0
                if actual > float(entry["reserved_usd"]):
                    breached = self.record_breach(
                        label,
                        reported_in=int(reported_in),
                        reported_out=int(reported_out),
                    )
                    raise ReservationBreach(
                        f"reservation breach on {label}: {breached.get('breach')} "
                        "(contact operator; no further calls)",
                        label=label,
                    )
                entry["decision"] = "reconciled"
                entry["used_in"] = int(reported_in)
                entry["used_out"] = int(reported_out)
                entry["used_usd"] = actual
                self.remaining_usd += float(entry["reserved_usd"]) - actual
                self.spent_usd += actual
                return
        raise ValueError(f"no open reservation for {label!r}")

    def record_breach(
        self, label: str, *, reported_in: int | None, reported_out: int | None
    ) -> dict[str, Any]:
        """Reported usage exceeded the reservation: record it as spent.

        Marks the open ``reserved`` entry ``reservation_breach`` with
        the reported usage and its cost (the call was billed; the
        reservation math, not the money, is what failed). Callers raise
        :class:`ReservationBreach` afterwards so the run stops with
        ``contact-operator`` and no further calls.
        """
        for entry in reversed(self.entries):
            if entry.get("label") == label and entry.get("decision") == "reserved":
                actual, _ = self._cost(
                    str(entry.get("model") or self.model),
                    str(entry.get("kind") or "chat"),
                    int(reported_in or 0),
                    int(reported_out or 0),
                )
                actual = actual or 0.0
                reasons: list[str] = []
                if int(reported_in or 0) > int(entry.get("reserved_in") or 0):
                    reasons.append(f"input {reported_in} > reserved {entry.get('reserved_in')}")
                if int(reported_out or 0) > int(entry.get("reserved_out") or 0):
                    reasons.append(f"output {reported_out} > reserved {entry.get('reserved_out')}")
                if actual > float(entry.get("reserved_usd") or 0.0):
                    reasons.append(
                        f"usd {actual:.6f} > reserved {float(entry.get('reserved_usd') or 0.0):.6f}"
                    )
                entry["decision"] = "reservation_breach"
                entry["used_in"] = int(reported_in or 0)
                entry["used_out"] = int(reported_out or 0)
                entry["used_usd"] = actual
                entry["breach"] = "; ".join(reasons) or "usage exceeded reservation"
                self.remaining_usd += float(entry["reserved_usd"]) - actual
                self.spent_usd += actual
                return entry
        raise ValueError(f"no open reservation for {label!r}")

    def keep(self, label: str) -> None:
        """Ambiguous failure: the reservation stays spent."""
        for entry in reversed(self.entries):
            if entry.get("label") == label and entry.get("decision") == "reserved":
                entry["decision"] = "kept-ambiguous"
                self.spent_usd += float(entry["reserved_usd"])
                return
        raise ValueError(f"no open reservation for {label!r}")

    def reserve_search(self, label: str, *, estimate_usd: float) -> bool:
        """Reserve a search estimate (decision 4, option A: estimate, not bound).

        False (no state change beyond a refused record) when the
        estimate does not fit the remainder.
        """
        if float(estimate_usd) > self.remaining_usd:
            self.entries.append(
                {
                    "label": label,
                    "decision": "refused",
                    "reserved_usd": float(estimate_usd),
                    "kind": "search",
                    "estimate_usd": float(estimate_usd),
                }
            )
            return False
        self.remaining_usd -= float(estimate_usd)
        self.entries.append(
            {
                "label": label,
                "decision": "reserved",
                "reserved_usd": float(estimate_usd),
                "kind": "search",
                "estimate_usd": float(estimate_usd),
            }
        )
        return True

    def reconcile_search(
        self,
        label: str,
        *,
        reported_in: int | None,
        reported_out: int | None,
        call_fee_usd: float = SEARCH_CALL_FEE_USD,
        model: str | None = None,
        pricing_version: str | None = None,
        raw_usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Settle a search dispatch against its estimate.

        Returns the per-search summary report. Ambiguous usage
        (``None``) keeps the estimate spent and counts toward the
        campaign cap. A reconciled cost above the estimate marks
        ``search_estimate_exceeded`` and raises
        :class:`SearchEstimateExceeded` so the campaign stops at once.
        """
        from culinary_copilot.recommendations.pricing import estimate_cost_usd

        for entry in reversed(self.entries):
            if entry.get("label") == label and entry.get("decision") == "reserved":
                estimate = float(entry.get("estimate_usd") or 0.0)
                if reported_in is None or reported_out is None:
                    entry["decision"] = "kept-ambiguous"
                    self.spent_usd += float(entry["reserved_usd"])
                    return {
                        "label": label,
                        "status": "kept-ambiguous",
                        "estimate_usd": estimate,
                        "reconciled_usd": float(entry["reserved_usd"]),
                        "within_estimate": True,
                        "raw_usage": dict(raw_usage or {}),
                    }
                turn_cost = estimate_cost_usd(
                    int(reported_in), int(reported_out), model or self.model
                )
                actual = float(call_fee_usd) + float(turn_cost or 0.0)
                report = {
                    "label": label,
                    "status": "reconciled",
                    "reported_input_tokens": int(reported_in),
                    "reported_output_tokens": int(reported_out),
                    "call_fee_usd": float(call_fee_usd),
                    "estimate_usd": estimate,
                    "reconciled_usd": actual,
                    "within_estimate": actual <= estimate,
                    "raw_usage": dict(raw_usage or {}),
                }
                if actual > estimate:
                    entry["decision"] = "search_estimate_exceeded"
                    entry["used_in"] = int(reported_in)
                    entry["used_out"] = int(reported_out)
                    entry["used_usd"] = actual
                    entry["model"] = model or self.model
                    if pricing_version:
                        entry["pricing_version"] = pricing_version
                    self.remaining_usd += float(entry["reserved_usd"]) - actual
                    self.spent_usd += actual
                    raise SearchEstimateExceeded(
                        f"search estimate exceeded on {label}: "
                        f"${actual:.6f} > ${estimate:.6f} estimate "
                        "(campaign stops; owner acknowledgment required)",
                        label=label,
                        report=report,
                    )
                entry["decision"] = "reconciled"
                entry["used_in"] = int(reported_in)
                entry["used_out"] = int(reported_out)
                entry["used_usd"] = actual
                entry["model"] = model or self.model
                if pricing_version:
                    entry["pricing_version"] = pricing_version
                self.remaining_usd += float(entry["reserved_usd"]) - actual
                self.spent_usd += actual
                return report
        raise ValueError(f"no open search reservation for {label!r}")

    def count_search_dispatches(self) -> int:
        """Search dispatches in this ledger (ambiguous ones count)."""
        return sum(
            1
            for entry in self.entries
            if isinstance(entry, dict)
            and entry.get("kind") == "search"
            and entry.get("decision") in SEARCH_DISPATCHED_DECISIONS
        )

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "ceiling_usd": self.ceiling_usd,
            "spent_usd": self.spent_usd,
            "remaining_usd": self.remaining_usd,
            "search_dispatches": self.count_search_dispatches(),
            "entries": list(self.entries),
        }


# --- snapshots -----------------------------------------------------------------


def snapshot(engine: Any) -> dict[str, Any]:
    """Isolation snapshot: counts plus a checksum over technique hashes."""
    from sqlalchemy import text

    with engine.connect() as conn:
        recipes = conn.execute(text("SELECT count(*) FROM recipes")).scalar()
        quarantine = conn.execute(text("SELECT count(*) FROM recipe_quarantine")).scalar()
        docs = conn.execute(text("SELECT count(*) FROM technique_documents")).scalar()
        chunks = conn.execute(text("SELECT count(*) FROM technique_chunks")).scalar()
        hashes = [
            r[0]
            for r in conn.execute(
                text("SELECT sha256_normalized FROM technique_documents ORDER BY doc_id")
            )
        ]
        sessions = conn.execute(text("SELECT count(*) FROM sessions")).scalar()
        events = conn.execute(text("SELECT count(*) FROM session_events")).scalar()
        event_keys = [
            (r[0], r[1]) for r in conn.execute(text("SELECT session_id, seq FROM session_events"))
        ]
    checksum = hashlib.sha256("\n".join(hashes).encode()).hexdigest()
    return {
        "recipes": int(recipes or 0),
        "quarantine": int(quarantine or 0),
        "technique_documents": int(docs or 0),
        "technique_chunks": int(chunks or 0),
        "technique_checksum": checksum,
        "sessions": int(sessions or 0),
        "session_events": int(events or 0),
        "event_keys": [[s, q] for s, q in event_keys],
    }


def verify_isolation(
    pre: dict[str, Any], post: dict[str, Any], created_ids: set[str]
) -> tuple[bool, list[str]]:
    """Prove no writes outside sessions/session_events by created sessions."""
    problems: list[str] = []
    for key in ("recipes", "quarantine", "technique_documents", "technique_chunks"):
        if pre.get(key) != post.get(key):
            problems.append(f"{key} changed ({pre.get(key)} -> {post.get(key)})")
    if pre.get("technique_checksum") != post.get("technique_checksum"):
        problems.append("technique checksum changed")
    if int(post.get("sessions", 0)) - int(pre.get("sessions", 0)) != len(created_ids):
        problems.append("sessions growth does not match the created-session manifest")
    pre_keys = {tuple(k) for k in pre.get("event_keys", [])}
    for key in post.get("event_keys", []):
        if tuple(key) not in pre_keys and key[0] not in created_ids:
            problems.append(f"event outside created sessions: {key}")
            break
    return (not problems, problems)


# --- scenarios -----------------------------------------------------------------


def load_scenarios(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = {k: v for k, v in payload.items() if k != "freeze_sha256"}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != payload.get("freeze_sha256"):
        raise ValueError("live scenarios freeze hash mismatch")
    return payload


# --- provider wrappers ---------------------------------------------------------


class LedgerModelProvider:
    """Wraps a model provider: reserve per turn, reconcile with usage."""

    def __init__(
        self, inner: Any, ledger: SpendLedger, *, max_output: int, model: str | None = None
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._max_output = int(max_output)
        self._model = model
        self._seq = 0

    async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
        reserved_in = estimate_request_tokens(
            input_items=kwargs.get("input_items"),
            tools=kwargs.get("tools"),
            response_model=kwargs.get("response_model"),
            tool_choice=kwargs.get("tool_choice"),
            max_output_tokens=kwargs.get("max_output_tokens", self._max_output),
        )
        self._seq += 1
        label = f"model-turn-{self._seq}"
        # The call's own cap wins when the loop passes one; otherwise the
        # configured per-turn maximum.
        max_out = kwargs.get("max_output_tokens", self._max_output)
        try:
            max_out = int(max_out)
        except (TypeError, ValueError):
            max_out = self._max_output
        model = self._model or self._ledger.model
        if not self._ledger.reserve(
            label, input_tokens=reserved_in, max_output=max_out, model=model, kind="chat"
        ):
            raise BudgetExhausted(
                f"overrun guard: turn reservation exceeds remaining "
                f"${self._ledger.remaining_usd:.4f}"
            )
        try:
            result = await self._inner.complete_native_tool_turn(**kwargs)
        except Exception as exc:
            # Conservative on ambiguous failures: the reservation stays
            # spent (keep), unless nothing was sent (release). Either way
            # the HTTP status is recorded when the failure carries one.
            status: int | None = None
            for det in list(getattr(exc, "attempt_details", None) or []):
                if isinstance(det, dict) and isinstance(det.get("http_status"), int):
                    status = det["http_status"]
                    break
            if status is None and isinstance(getattr(exc, "status_code", None), int):
                status = int(exc.status_code)
            if status is not None:
                for entry in reversed(self._ledger.entries):
                    if entry.get("label") == label and entry.get("decision") == "reserved":
                        entry["http_status"] = status
                        break
            if bool(getattr(exc, "request_sent", True)):
                self._ledger.keep(label)
            else:
                # Nothing was sent: release the reservation.
                for entry in reversed(self._ledger.entries):
                    if entry.get("label") == label and entry.get("decision") == "reserved":
                        entry["decision"] = "released-unsent"
                        self._ledger.remaining_usd += float(entry["reserved_usd"])
                        break
            raise
        self._ledger.reconcile(
            label,
            reported_in=getattr(result, "input_tokens", None),
            reported_out=getattr(result, "output_tokens", None),
        )
        return result

    async def aclose(self) -> None:
        await _aclose_provider(self._inner)


class LedgerEmbedProvider:
    """Wraps an embedding provider: reserve per call, reconcile with usage.

    Exactly one request per wrapped call: the runner forces
    ``embed_max_retries=0`` on the effective settings, so retries happen
    only as runner-level scenario attempts, each reserved separately.
    """

    def __init__(
        self, inner: Any, ledger: SpendLedger, *, retries: int, model: str | None = None
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._retries = max(0, int(retries))
        self._model = model or getattr(inner, "model", None) or "text-embedding-3-small"
        self._seq = 0

    async def embed_texts(self, texts: list[str]) -> Any:
        self._seq += 1
        label = f"query-embed-{self._seq}"
        reserved = estimate_embedding_request_tokens(texts, attempts=self._retries + 1)
        if not self._ledger.reserve(
            label, input_tokens=reserved, max_output=0, model=self._model, kind="embedding"
        ):
            raise BudgetExhausted(
                f"overrun guard: embedding reservation exceeds remaining "
                f"${self._ledger.remaining_usd:.4f}"
            )
        try:
            result = await self._inner.embed_texts(texts)
        except Exception:
            self._ledger.keep(label)
            raise
        self._ledger.reconcile(
            label,
            reported_in=int(getattr(result.usage, "prompt_tokens", 0) or 0),
            reported_out=0,
        )
        return result


# --- preflight -----------------------------------------------------------------


EPICURE_UNAVAILABLE_SCENARIO = "live-epicure-unavailable"


def _epicure_probe(settings: Any) -> dict[str, dict[str, Any]]:
    """Cache-only Epicure probe (no network): one chicken query per backend.

    Builds the same ``cache_only=True`` variants the tool path uses
    (missing files fail fast, never download) and calls
    ``find_balanced_pairings("chicken", 1)`` on core/cooc/chem plus the
    substitutions backend (the core adapter). Returns
    ``{backend: {"ok": bool, "pairs": n, "error"?: str}}``.
    """
    backends = ("core", "cooc", "chem", "substitutions")
    try:
        from culinary_copilot.tools.epicure_tools import build_epicure_variants

        variants = build_epicure_variants(settings)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {str(exc)[:200]}"
        return {name: {"ok": False, "pairs": 0, "error": failure} for name in backends}
    results: dict[str, dict[str, Any]] = {}
    for name in backends:
        core = variants.get("core" if name == "substitutions" else name)
        try:
            pairs = core.find_balanced_pairings("chicken", 1)
            results[name] = {"ok": True, "pairs": len(pairs) if pairs is not None else 0}
        except Exception as exc:
            results[name] = {
                "ok": False,
                "pairs": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
    return results


def _scenario_epicure_enabled(global_enabled: bool, scenario: dict[str, Any]) -> bool:
    """Effective Epicure flag for one scenario (scenario override wins)."""
    override = (scenario.get("settings") or {}).get("epicure_enabled")
    return bool(override) if override is not None else bool(global_enabled)


def _effective_settings(settings: Any) -> Any:
    """Runner settings copy: provider-internal retries forced to zero.

    One wrapped call then equals exactly one billed request. The runner
    only uses two billed paths: model turns via
    ``complete_native_tool_turn`` (``llm_rec_max_retries``) and query
    embeddings (``embed_max_retries``); ``llm_app_max_retries`` (the
    planning path) is zeroed too so no path can retry internally.
    Retries happen only as runner-level scenario attempts, each
    reserved separately. Never edits .env; the copy lives for the run
    only.
    """
    return settings.model_copy(
        update={
            "llm_app_max_retries": 0,
            "llm_rec_max_retries": 0,
            "embed_max_retries": 0,
        }
    )


def preflight(
    args: Any,
    settings: Any,
    scenarios: dict[str, Any] | None = None,
    history_path: Path | str | None = None,
    *,
    probe: Any | None = None,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Refuse (False) on any failed check; records versions and corpus state.

    ``history_path`` enables the cumulative budget: prior recorded spend
    is subtracted from the ceiling, and the run is refused when not even
    a small first turn fits the remainder. ``scenarios`` enables the
    per-scenario Epicure check (every scenario except
    ``live-epicure-unavailable`` must inherit an enabled Epicure);
    ``probe`` overrides the cache-only Epicure probe (tests only).
    """
    problems: list[str] = []
    record: dict[str, Any] = {}
    from culinary_copilot.llm.models import PRICING_VERSION, model_spec

    model = str(args.model or settings.llm_rec_model)
    spec = model_spec(model)
    if spec is None:
        problems.append(f"unknown model {model!r} (not in registry)")
    record["model"] = model
    record["pricing_version"] = PRICING_VERSION
    record["max_attempts"] = int(getattr(args, "max_attempts", MAX_ATTEMPTS) or MAX_ATTEMPTS)
    record["scenario_keys"] = [
        str(s.get("key", "")) for s in (scenarios or {}).get("scenarios", [])
    ]
    if int(getattr(settings, "llm_app_max_retries", 0)) != 0:
        problems.append(
            "llm_app_max_retries must be 0 (one wrapped call must equal one request; "
            "retries are runner-level attempts only)"
        )
    if int(getattr(settings, "llm_rec_max_retries", 0)) != 0:
        problems.append(
            "llm_rec_max_retries must be 0 (model turns retry via "
            "llm_rec_max_retries, not llm_app_max_retries; one wrapped "
            "call must equal one request)"
        )
    if int(getattr(settings, "embed_max_retries", 0)) != 0:
        problems.append(
            "embed_max_retries must be 0 (one wrapped call must equal one request; "
            "retries are runner-level attempts only)"
        )
    if not bool(getattr(settings, "llm_recommendation_enabled", False)):
        problems.append(
            "llm_recommendation_enabled must be true "
            "(LLM_RECOMMENDATION_ENABLED=true: the native tool turn refuses "
            "when recommendation generation is disabled)"
        )
    global_epicure = bool(getattr(settings, "epicure_enabled", False))
    record["epicure"] = {"enabled": global_epicure, "probe": {}, "scenarios": {}}
    if scenarios is not None:
        # Every scenario except the unavailable one must inherit an
        # enabled Epicure (scenario overrides may only disable it for
        # live-epicure-unavailable, never enable it elsewhere).
        for item in scenarios.get("scenarios", []) or []:
            key = str(item.get("key", ""))
            effective_flag = _scenario_epicure_enabled(global_epicure, item)
            record["epicure"]["scenarios"][key] = effective_flag
            if key == EPICURE_UNAVAILABLE_SCENARIO:
                if effective_flag:
                    problems.append(
                        f"{key} must keep its epicure_enabled=false override "
                        "(the unavailable scenario probes degraded Epicure)"
                    )
            elif not effective_flag:
                problems.append(
                    f"epicure_enabled must be true for scenario {key!r} "
                    "(EPICURE_ENABLED=true; only "
                    f"{EPICURE_UNAVAILABLE_SCENARIO} may disable it)"
                )
    elif not global_epicure:
        problems.append(
            "epicure_enabled must be true "
            "(EPICURE_ENABLED=true: every live scenario except "
            f"{EPICURE_UNAVAILABLE_SCENARIO} expects Epicure)"
        )
    if global_epicure:
        # Cache-only probe, no network: one chicken query per backend
        # through the same cache-only variants the tool path uses.
        try:
            probe_results = (probe or _epicure_probe)(settings)
        except Exception as exc:
            failure = f"{type(exc).__name__}: {str(exc)[:200]}"
            probe_results = {
                name: {"ok": False, "pairs": 0, "error": failure}
                for name in ("core", "cooc", "chem", "substitutions")
            }
        record["epicure"]["probe"] = probe_results
        for name, result in (probe_results or {}).items():
            if not isinstance(result, dict) or not result.get("ok"):
                error = (
                    result.get("error", "unknown probe failure")
                    if isinstance(result, dict)
                    else "unknown probe failure"
                )
                problems.append(f"Epicure cache probe failed for {name}: {error}")
    pool = str(getattr(args, "budget_pool", "phase3") or "phase3")
    if pool not in BUDGET_POOLS:
        problems.append(f"unknown --budget-pool {pool!r} (phase3 | phase5)")
        pool = "phase3"
    pool_cap = float(BUDGET_POOLS[pool]["cap_usd"])
    record["budget_pool"] = pool
    record["pool_cap_usd"] = pool_cap
    record["pool_history"] = str(BUDGET_POOLS[pool]["history"])
    if float(args.ceiling_usd) > pool_cap:
        problems.append(
            f"ceiling ${float(args.ceiling_usd):.2f} exceeds ${pool_cap:.2f} {pool} pool cap"
        )
    record["search_reservation_status"] = SEARCH_RESERVATION_STATUS
    live_mode = bool(getattr(args, "live", False))
    selected = list((scenarios or {}).get("scenarios", []) or [])
    search_selected = any(
        bool((s.get("session", {}) or {}).get("internet_search_allowed")) for s in selected
    )
    record["search_selected"] = search_selected
    ack = str(getattr(args, "acknowledge_search_estimate", "") or "")
    record["acknowledge_search_estimate"] = ack
    if live_mode and search_selected and ack != SEARCH_ACK_VALUE:
        problems.append(
            "search accounting is an estimate with acknowledged overrun risk: "
            "live mode with search selected refused without "
            f"--acknowledge-search-estimate {SEARCH_ACK_VALUE} (decision 4, option A)"
        )
    per_live = int(getattr(args, "search_max_per_live_session", PHASE5_MAX_SEARCHES_PER_SESSION))
    if per_live > PHASE5_MAX_SEARCHES_PER_SESSION:
        problems.append(
            f"--search-max-per-live-session {per_live} exceeds "
            f"{PHASE5_MAX_SEARCHES_PER_SESSION} (owner item 3)"
        )
    record["phase5_campaign_cap_usd"] = PHASE5_CAP_USD
    # Campaign search cap: at most 4 paid searches across all Phase 5
    # runs, counted from the persisted campaign history (ambiguous
    # dispatches count; unaffordable refusals do not).
    prior_searches = count_campaign_searches(PHASE5_HISTORY)
    record["prior_campaign_searches"] = prior_searches
    record["campaign_search_cap"] = PHASE5_CAMPAIGN_SEARCH_CAP
    max_campaign = getattr(args, "max_campaign_searches", None)
    record["max_campaign_searches"] = max_campaign
    if live_mode and search_selected:
        if prior_searches >= PHASE5_CAMPAIGN_SEARCH_CAP:
            problems.append(
                f"campaign search cap reached: {prior_searches} paid searches recorded "
                f"in {PHASE5_HISTORY} (cap {PHASE5_CAMPAIGN_SEARCH_CAP}); no further searches"
            )
        if max_campaign is not None and prior_searches + int(max_campaign) > (
            PHASE5_CAMPAIGN_SEARCH_CAP
        ):
            problems.append(
                f"--max-campaign-searches {max_campaign} does not fit: "
                f"{prior_searches} already recorded, cap {PHASE5_CAMPAIGN_SEARCH_CAP}"
            )
        estimate_breach = find_unacknowledged_estimate_breach(PHASE5_HISTORY)
        record["search_estimate_breach"] = estimate_breach
        if estimate_breach is not None:
            problems.append(
                "campaign history records an unacknowledged search estimate breach "
                f"(run {estimate_breach.get('run_utc')}, {estimate_breach.get('label')}); "
                "no search run until an owner acknowledges it (LIVE_PLAN.md: add a "
                "matching entry to estimate_acknowledgments in data/phase5-live/spend-history.json)"
            )
    prior_spend = recorded_spend_total(Path(history_path)) if history_path else 0.0
    remaining_budget = float(args.ceiling_usd) - prior_spend
    record["prior_recorded_spend_usd"] = prior_spend
    record["remaining_budget_usd"] = remaining_budget
    if history_path:
        breach = find_unacknowledged_breach(Path(history_path))
        record["reservation_breach"] = breach
        if breach is not None:
            problems.append(
                "spend history records an unacknowledged reservation breach "
                f"(run {breach.get('run_utc')}, {breach.get('label')}); no paid "
                "run until an owner acknowledges it (LIVE_PLAN.md: add a "
                f"matching entry to breach_acknowledgments in {history_path})"
            )
    if history_path and spec is not None:
        from culinary_copilot.recommendations.pricing import estimate_cost_usd

        first_turn_floor = estimate_cost_usd(2000, 1000, model)
        if first_turn_floor is not None and remaining_budget < first_turn_floor:
            problems.append(
                f"remaining {pool} pool budget ${remaining_budget:.4f} cannot fit a first turn "
                f"(floor ${first_turn_floor:.4f} after ${prior_spend:.4f} prior recorded spend)"
            )
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        problems.append("HF_HUB_OFFLINE != 1 (Epicure assets must fail fast, never download)")
    db_url = args.database_url or settings.database_url.get_secret_value()
    from urllib.parse import urlparse

    parts = urlparse(db_url)
    name = parts.path.rsplit("/", 1)[-1]
    host = parts.hostname or ""
    if name != args.expect_db_name or host != args.expect_db_host:
        problems.append(
            f"DB guard mismatch: {host}/{name} != {args.expect_db_host}/{args.expect_db_name}"
        )
    record["technique"] = {}
    if bool(getattr(settings, "embeddings_enabled", False)):
        # Embeddings on: the query-embedding provider must build, or the
        # vector calls the agent may choose have no funding path.
        try:
            from culinary_copilot.tools.search_tools import build_embed_provider

            if build_embed_provider(settings) is None:
                problems.append("EMBEDDINGS_ENABLED but no query-embedding provider could be built")
        except Exception as exc:
            problems.append(
                "EMBEDDINGS_ENABLED but the query-embedding provider failed: "
                f"{type(exc).__name__}: {exc}"
            )
    if not problems:
        try:
            from sqlalchemy import create_engine

            engine = create_engine(db_url)
            snap = snapshot(engine)
            engine.dispose()
            manifest_path = REPO_ROOT / "evals" / "technique_corpus" / "manifest.json"
            manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            record["technique"] = {
                "manifest_sha256": manifest_hash,
                "documents": snap["technique_documents"],
                "chunks": snap["technique_chunks"],
                "checksum": snap["technique_checksum"],
            }
            from sqlalchemy import create_engine as _ce

            engine2 = _ce(db_url)
            with engine2.connect() as conn:
                from sqlalchemy import text

                embeddings = conn.execute(
                    text("SELECT count(*) FROM technique_embeddings")
                ).scalar()
                try:
                    recipe_embeddings = conn.execute(
                        text("SELECT count(*) FROM recipe_embeddings")
                    ).scalar()
                except Exception:
                    recipe_embeddings = 0
            engine2.dispose()
            record["technique"]["embeddings"] = int(embeddings or 0)
            record["vector"] = {
                "recipe_embeddings": int(recipe_embeddings or 0),
                "technique_embeddings": int(embeddings or 0),
            }
        except Exception as exc:
            problems.append(f"technique snapshot unverifiable: {type(exc).__name__} (use option B)")
    return (not problems, problems, record)


# --- grading -------------------------------------------------------------------


def _finish_epicure_lines(store: Any, session_id: str) -> int:
    """Epicure lines recorded on the last finish event (0 when none)."""
    try:
        events = store.list_events(session_id)
    except Exception:
        return 0
    count = 0
    for event in events:
        if getattr(event, "event_type", "") == "agent_finished":
            payload = getattr(event, "payload", None) or {}
            lines = payload.get("epicure_lines")
            if isinstance(lines, list):
                count = len(lines)
    return count


def grade_attempt(
    scenario: dict[str, Any],
    final: dict[str, Any] | None,
    stop_reason: str,
    store: Any,
    session_id: str,
    *,
    manual_review: bool = False,
) -> dict[str, Any]:
    """Structural grades (deterministic; relevance stays owner review).

    A run with no options, no plan and no final question has nothing to
    judge: evidence, constraint and Epicure grades are ``"n/a"`` rather
    than vacuous ``true``. ``manual_review`` (real-provider runs) marks
    clarification quality for the owner instead of auto-grading it.
    """
    from culinary_copilot.agent.loop import recipe_session_evidence

    expected = scenario.get("expected", {})
    grades: dict[str, Any] = {}
    options = list((final or {}).get("options") or [])
    if not options and (final or {}).get("plan"):
        # Plan flows finish on the plan; judge the persisted recommendations.
        try:
            committed = store.get(session_id)
            options = list((committed.suggestions if committed else []) or [])
        except Exception:
            options = []
    plan = (final or {}).get("plan")
    question = (final or {}).get("question") or {}
    technique_answer = (final or {}).get("technique_answer") or {}
    web_answer = (final or {}).get("web_answer") or {}
    empty_run = (
        not options and not plan and not question and not technique_answer and not web_answer
    )
    termination_ok = stop_reason == expected.get("stop_reason")
    options_ok = len(options) >= int(expected.get("min_options", 0))
    plan_ok = True
    if expected.get("plan"):
        # A plan run that ends on options (e.g. attempt-7 chicken)
        # answered without the requested cooking plan.
        plan_ok = bool(plan)
    answer_ok = True
    if expected.get("kind") == "technique_answer":
        answer_ok = bool(technique_answer)
    lines_ok = True
    min_lines = int(expected.get("min_epicure_lines", 0) or 0)
    if min_lines:
        lines_ok = _finish_epicure_lines(store, session_id) >= min_lines
    grades["task_completion"] = termination_ok and options_ok and plan_ok and answer_ok and lines_ok
    grades["termination"] = stop_reason == expected.get("stop_reason")
    hard = set((scenario.get("session", {}).get("constraints") or {}).keys())
    honored = set((final or {}).get("constraints_honored") or [])
    grades["constraint_adherence"] = "n/a" if empty_run else (not hard or hard <= honored)
    retrieved, _full = recipe_session_evidence(store=store, session_id=session_id)
    grades["evidence_support"] = (
        "n/a"
        if empty_run
        else all(
            isinstance(o, dict) and (str(o.get("dataset_id")), str(o.get("source_id"))) in retrieved
            for o in options
        )
    )
    if manual_review:
        grades["clarification_quality"] = "manual-review"
    elif expected.get("stop_reason") == "agent_needs_user_input":
        grades["clarification_quality"] = bool(question.get("question_text")) and bool(
            question.get("options")
        )
        contains = expected.get("question_contains")
        if contains:
            grades["clarification_quality"] = grades["clarification_quality"] and (
                contains in str(question.get("question_text") or "")
            )
    else:
        grades["clarification_quality"] = question == {}
    epicure_expected = str(expected.get("epicure", "consulted"))
    if empty_run:
        grades["epicure_behaviour"] = "n/a"
    elif epicure_expected == "consulted":
        grades["epicure_behaviour"] = not bool(
            (final or {}).get("epicure_skip_reason")
        ) and not bool((final or {}).get("epicure_degraded"))
    elif epicure_expected == "degraded":
        grades["epicure_behaviour"] = bool((final or {}).get("epicure_degraded"))
    elif epicure_expected.startswith("skip:"):
        grades["epicure_behaviour"] = (final or {}).get("epicure_skip_reason") == epicure_expected[
            len("skip:") :
        ]
    else:
        grades["epicure_behaviour"] = False
    grades["request_relevance"] = {
        "verdict": "manual-review",
        "request": scenario.get("request"),
        "option_titles": [str(o.get("title")) for o in options if isinstance(o, dict)],
    }
    if "web_answer" in expected:
        grades["web_answer"] = bool(web_answer) == bool(expected.get("web_answer"))
        refs = web_answer.get("web_refs") if isinstance(web_answer, dict) else []
        grades["web_refs_clickable"] = (
            bool(web_answer)
            and isinstance(refs, list)
            and all(isinstance(r, dict) and r.get("url") and r.get("title") for r in refs)
        )
    if (
        expected.get("asked")
        or expected.get("answer_recorded")
        or expected.get("resumed_used_answer")
    ):
        # P3-L-13 ask-and-resume triple: the agent asked (agent_answer
        # event + confirmed answer on the session), the answer was
        # recorded, and the resumed run used the answer (terminal
        # sufficient final after the answer).
        try:
            events = store.list_events(session_id)
        except Exception:
            events = []
        answer_events = [e for e in events or [] if getattr(e, "event_type", "") == "agent_answer"]
        try:
            committed = store.get(session_id)
            confirmed = list(getattr(committed, "confirmed_answers", None) or [])
        except Exception:
            confirmed = []
        asked_ok = bool(answer_events) or bool(confirmed)
        recorded_ok = bool(answer_events) and bool(confirmed)
        used_ok = bool(confirmed) and stop_reason == "agent_sufficient_evidence"
        if expected.get("asked"):
            grades["asked"] = asked_ok
        if expected.get("answer_recorded"):
            grades["answer_recorded"] = recorded_ok
        if expected.get("resumed_used_answer"):
            grades["resumed_used_answer"] = used_ok
    if expected.get("allergy_check"):
        # P3-L-13 allergy run (prepare-only): the missing fact
        # materially changes the answer. "not-exercised" when the model
        # did not ask — never counted as a pass.
        from culinary_copilot.recommendations.policy import ingredient_term_hit

        try:
            all_events = store.list_events(session_id)
        except Exception:
            all_events = []
        question_events = [
            e for e in all_events or [] if getattr(e, "event_type", "") == "agent_question"
        ]
        allergy_answer_events = [
            e for e in all_events or [] if getattr(e, "event_type", "") == "agent_answer"
        ]
        try:
            allergy_committed = store.get(session_id)
            allergy_confirmed = list(getattr(allergy_committed, "confirmed_answers", None) or [])
        except Exception:
            allergy_confirmed = []
        scripted = [str(a.get("answer") or "") for a in scenario.get("scripted_answers", [])]
        allergy_asked = bool(question_events)
        allergy_recorded = bool(allergy_answer_events) and any(
            any(text in str(c.get("answer") or "") for text in scripted if text)
            for c in allergy_confirmed
            if isinstance(c, dict)
        )
        peanut_lines: list[str] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            for quantity in option.get("quantities", []) or []:
                line = str((quantity or {}).get("ingredient") or "")
                if line and ingredient_term_hit(line, "peanut"):
                    peanut_lines.append(f"{option.get('title')}: {line}")
        allergy_note = str((final or {}).get("note") or "")
        honored_text = str((final or {}).get("constraints_honored") or "")
        allergy_mentioned = (
            "allerg" in (allergy_note + honored_text).lower()
            or "peanut" in (allergy_note + honored_text).lower()
        )
        if not allergy_asked:
            grades["allergy"] = "not-exercised: model did not ask"
        else:
            grades["allergy"] = {
                "asked": True,
                "answer_recorded": allergy_recorded,
                "resumed_with_options": stop_reason == "agent_sufficient_evidence"
                and bool(options),
                "no_peanut_options": not peanut_lines,
                "peanut_lines": peanut_lines,
                "allergy_mentioned": allergy_mentioned,
            }
            grades["allergy_pass"] = bool(
                allergy_recorded
                and stop_reason == "agent_sufficient_evidence"
                and bool(options)
                and not peanut_lines
            )
    return grades


# --- fake stack (--fake only) ----------------------------------------------------


_FAKE_ROWS = [
    {"dataset_id": "odunola/foodie", "source_id": "curry-1", "title": "Creamy Chicken Curry"},
    {"dataset_id": "odunola/foodie", "source_id": "lentil-2", "title": "Red Lentil Soup"},
]
_FAKE_DOCS = {
    ("odunola/foodie", "curry-1"): {
        "dataset_id": "odunola/foodie",
        "source_id": "curry-1",
        "title": "Creamy Chicken Curry",
        "servings": 4.0,
        "ingredients": [
            {"canonical": "chicken", "amount": "500", "unit": "g", "quantity_text": "500 g"}
        ],
        "instructions": ["Brown the chicken.", "Serve hot."],
    },
    ("odunola/foodie", "lentil-2"): {
        "dataset_id": "odunola/foodie",
        "source_id": "lentil-2",
        "title": "Red Lentil Soup",
        "servings": 4.0,
        "ingredients": [
            {
                "canonical": "red lentils",
                "amount": "200",
                "unit": "g",
                "quantity_text": "200 g",
            }
        ],
        "instructions": ["Simmer the lentils.", "Serve hot."],
    },
}


class FakeRunProvider:
    """Deterministic fake model: retrieval turns then a valid finish (tests only).

    Per-flow scripts (calls count turns across resume runs in one attempt):
    full: tools, options finish, then plan finishes; ask: tools, ask,
    tools, options finish; direct: tools, single finish; empty: tools,
    ask; technique: tools, skip finish.
    """

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.calls = 0

    def _tools(self, *calls: tuple[str, str, dict[str, Any]]) -> Any:
        from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult

        return NativeTurnResult(
            tool_calls=[
                NativeToolCall(call_id=c, name=n, arguments=json.dumps(a)) for c, n, a in calls
            ],
            parsed=None,
            chain_items=[
                {
                    "type": "function_call",
                    "call_id": c,
                    "name": n,
                    "arguments": json.dumps(a),
                }
                for c, n, a in calls
            ],
            input_tokens=10,
            output_tokens=5,
        )

    def _parsed(self, payload: dict[str, Any]) -> Any:
        from culinary_copilot.llm.client import NativeTurnResult

        return NativeTurnResult(
            tool_calls=[],
            parsed=payload,
            chain_items=[],
            input_tokens=10,
            output_tokens=5,
        )

    def _options_finish(self, options: list[dict[str, Any]], **kw: Any) -> Any:
        return self._parsed(
            {
                "decision": "finish",
                "move_to": "recommend",
                "result": {"options": options},
                "constraints_honored": list(
                    (self.scenario.get("session", {}).get("constraints") or {}).keys()
                ),
                "note": "fake finish",
                **kw,
            }
        )

    def _opt(self, source_id: str, title: str, quantities: list[dict[str, Any]]) -> dict[str, Any]:
        dataset = "odunola/foodie"
        return {
            "dataset_id": dataset,
            "source_id": source_id,
            "title": title,
            "quantities": quantities,
            "adaptations": [],
        }

    def _lines(self) -> list[dict[str, Any]]:
        # The fake Epicure core always returns pork: consulted fake
        # finishes evaluate it, like a real model would have to.
        return [{"ingredient": "pork", "decision": "used", "reason": "fake roast match"}]

    async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
        self.calls += 1
        flow = self.scenario.get("fake_flow", "direct")
        query = {"query": self.scenario["request"][:60]}
        if flow == "full":
            if self.calls == 1:
                return self._tools(
                    ("c1", "search_recipes", query),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                    ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                    ("c5", "search_techniques", {"query": "safe internal temperatures"}),
                )
            if self.calls == 2:
                return self._options_finish(
                    [
                        self._opt(
                            "curry-1",
                            "Creamy Chicken Curry",
                            [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                        ),
                        self._opt(
                            "lentil-2",
                            "Red Lentil Soup",
                            [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                        ),
                    ],
                    epicure_lines=self._lines(),
                )
            return self._parsed(
                {
                    "decision": "finish",
                    "move_to": "plan",
                    "result": {
                        "plan": {
                            "source": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                            "mise_en_place": ["dice chicken"],
                            "steps": ["brown chicken", "serve"],
                            "plating": "in bowls",
                            "quantities": [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                            "adaptations": [],
                            "technique_refs": [{"doc_id": "tech-fda-safe-32", "chunk_id": 0}],
                        }
                    },
                    "constraints_honored": [],
                    "note": "fake plan",
                }
            )
        if flow == "ask":
            if self.calls == 1:
                return self._tools(("c1", "search_recipes", query))
            if self.calls == 2:
                scripted = list(self.scenario.get("scripted_answers", []))
                question_id = scripted[0]["question_id"] if scripted else "q-fake"
                return self._parsed(
                    {
                        "decision": "ask_user",
                        "question": {
                            "question_id": question_id,
                            "question_text": "Do you have plain yogurt?",
                            "options": ["yes", "no"],
                        },
                        "note": "fake ask",
                    }
                )
            if self.calls == 3:
                return self._tools(
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c4", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                    ("c5", "find_substitutions", {"ingredient": "yogurt"}),
                )
            return self._options_finish(
                [
                    self._opt(
                        "curry-1",
                        "Creamy Chicken Curry",
                        [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                    ),
                    self._opt(
                        "lentil-2",
                        "Red Lentil Soup",
                        [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                    ),
                ],
                epicure_lines=self._lines(),
            )
        if flow == "ask-allergy":
            if self.calls == 1:
                return self._tools(("c1", "search_recipes", query))
            if self.calls == 2:
                return self._parsed(
                    {
                        "decision": "ask_user",
                        "question": {
                            "question_id": "q-allergy",
                            "question_text": (
                                "What is your friend allergic to? I need to avoid "
                                "it in every suggestion."
                            ),
                            "options": ["peanuts", "dairy", "gluten", "other"],
                        },
                        "note": "fake allergy ask",
                    }
                )
            if self.calls == 3:
                return self._tools(
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c4", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                )
            # Both fake options are peanut-free (chicken; red lentils).
            return self._options_finish(
                [
                    self._opt(
                        "curry-1",
                        "Creamy Chicken Curry",
                        [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                    ),
                    self._opt(
                        "lentil-2",
                        "Red Lentil Soup",
                        [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                    ),
                ],
                epicure_lines=self._lines(),
            )
        if flow == "empty":
            if self.calls == 1:
                return self._tools(("c1", "search_recipes", query))
            return self._parsed(
                {
                    "decision": "ask_user",
                    "question": {
                        "question_id": "q-fake",
                        "question_text": (
                            "I found no recipes for dragonfruit soufflé glacé. "
                            "Want me to look for a lemon dessert instead?"
                        ),
                        "options": ["yes, look", "no"],
                    },
                    "note": "fake ask",
                }
            )
        if flow == "technique":
            if self.calls == 1:
                return self._tools(
                    ("c1", "search_recipes", query),
                    ("c2", "search_techniques", query),
                )
            return self._options_finish(
                [
                    self._opt("curry-1", "Creamy Chicken Curry", []),
                    self._opt("lentil-2", "Red Lentil Soup", []),
                ],
                epicure_skip_reason="simple_technique_question",
            )
        if flow == "web-discovery":
            if self.calls == 1:
                return self._tools(("c1", "search_web", {"query": "okonomiyaki recipe"}))
            # Discovery-only text (no numbers: numeric claims fail
            # closed with no source text obtained). The ref URL must
            # equal the fake search source URL below.
            return self._parsed(
                {
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {
                        "web_answer": {
                            "text": (
                                "Okonomiyaki is a savoury Japanese pancake. "
                                "A full guide is linked below."
                            ),
                            "web_refs": [{"url": _FAKE_WEB_URL, "title": "Okonomiyaki guide"}],
                        }
                    },
                    "constraints_honored": [],
                    "note": "fake web discovery",
                }
            )
        if flow == "degraded":
            if self.calls == 1:
                return self._tools(
                    ("c1", "search_recipes", query),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                    ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                )
            return self._options_finish(
                [
                    self._opt(
                        "curry-1",
                        "Creamy Chicken Curry",
                        [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                    ),
                    self._opt(
                        "lentil-2",
                        "Red Lentil Soup",
                        [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                    ),
                ]
            )
        if self.calls == 1:
            return self._tools(
                ("c1", "search_recipes", query),
                ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                ("c3", "find_balanced_pairings", {"ingredient": "lentils"}),
            )
        return self._options_finish(
            [
                self._opt(
                    "lentil-2",
                    "Red Lentil Soup",
                    [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                )
            ],
            epicure_lines=self._lines(),
        )


def _fake_search(rows: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def _run(args: Any, context: Any) -> dict[str, Any]:
        return {"ok": True, "mode_ran": "fulltext", "cost_class": "free", "results": list(rows)}

    return _run


def _fake_get(args: Any, context: Any) -> dict[str, Any]:
    doc = _FAKE_DOCS.get((args.dataset_id, args.source_id))
    if doc is None:
        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": "tool_invalid_arguments",
            "message": "not found",
            "next_action": "change_request",
        }
    return {"ok": True, "recipe": dict(doc)}


def _fake_techniques(args: Any, context: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "mode_ran": "fulltext",
        "match": "all",
        "cost_class": "free",
        "results": [
            {
                "doc_id": "tech-fda-safe-32",
                "chunk_id": 0,
                "section": "Safe Food Handling",
                "title": "Safe Food Handling",
                "url": "https://en.wikipedia.org/wiki/Food_safety",
                "licence": "CC-BY-SA-4.0",
                "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "attribution_text": "fake attribution",
                "excerpt": "Cook poultry to 165°F (74°C).",
            }
        ],
    }


def _fake_technique_resolver(doc_id: str, chunk_id: int) -> dict[str, Any] | None:
    """Fake technique resolver: the safety chunk plans cite (tests only)."""
    if (doc_id, chunk_id) == ("tech-fda-safe-32", 0):
        return {
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "section": "Safe Food Handling",
            "title": "Safe Food Handling",
            "url": "https://en.wikipedia.org/wiki/Food_safety",
            "licence": "CC-BY-SA-4.0",
            "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
            "attribution_text": "fake attribution",
            "chunk_text": "Cook poultry to a safe internal temperature of 165°F (74°C).",
        }
    return None


class _FakeEpicureCore:
    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

    def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
        from culinary_copilot.tools.epicure import EpicureDisabledError, Pairing

        if not self._enabled:
            raise EpicureDisabledError("disabled")
        return [Pairing(ingredient="pork", score=0.5)][:k]


# --- run driver ------------------------------------------------------------------


def _new_session_id(scenario_key: str, attempt: int) -> str:
    return f"ses-live-{scenario_key[:12]}-a{attempt}-{uuid.uuid4().hex[:6]}"


# Event types projected into reviewable raw trajectories (read-only).
_TRAJECTORY_TOOL_KEYS = (
    "tool",
    "args_digest",
    "args",
    "returned_identities",
    "result_count",
    "outcome",
    "error_type",
    "reason",
)
_TRAJECTORY_ERROR_KEYS = (
    "reason",
    "error_code",
    "error_param",
    "error_message",
    "attempts",
    "request_sent",
)


def _project_trajectory_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """One session event, trimmed for review (no prompts or secrets).

    Every projected event carries ``stop``/``reason`` (the payload's
    ``stop_reason``/``reason`` when present, else None) plus the
    per-turn ``input_tokens``/``output_tokens`` (None when the event
    carries no usage); validation rejects carry their error list
    bounded to 5 x 300 characters.
    """
    stop = payload.get("stop_reason")
    reason = payload.get("reason")
    usage = {
        "input_tokens": payload.get("input_tokens"),
        "output_tokens": payload.get("output_tokens"),
    }
    if event_type == "tool_call":
        projected = {
            "type": event_type,
            **{k: payload.get(k) for k in _TRAJECTORY_TOOL_KEYS},
        }
        projected.setdefault("stop", stop)
        projected.update(usage)
        return projected
    if event_type == "provider_error":
        return {
            "type": event_type,
            **{k: payload.get(k) for k in _TRAJECTORY_ERROR_KEYS},
            "stop": stop,
            **usage,
        }
    if event_type == "agent_step":
        return {
            "type": event_type,
            "note": payload.get("note"),
            "stop": stop,
            "reason": reason,
            **usage,
        }
    if event_type == "agent_validation_reject":
        return {
            "type": event_type,
            "errors": [str(e)[:300] for e in (payload.get("errors") or [])[:5]],
            "stop": stop,
            "reason": reason,
            **usage,
        }
    if event_type == "agent_question":
        return {
            "type": event_type,
            "question_id": payload.get("question_id"),
            "question_text": payload.get("question_text"),
            "question_options": payload.get("question_options"),
            "stop": stop,
            "reason": reason,
            **usage,
        }
    if event_type == "agent_finished":
        return {
            "type": event_type,
            "note": payload.get("note"),
            "model_note": payload.get("model_note"),
            "options": payload.get("options"),
            "epicure_lines": payload.get("epicure_lines"),
            "dropped_options": payload.get("dropped_options"),
            "single_option_reason": payload.get("single_option_reason"),
            "plan_source": payload.get("plan_source"),
            "steps_source": payload.get("steps_source"),
            "stop_reason": payload.get("stop_reason"),
            "stop": stop,
            "reason": reason,
            **usage,
        }
    if event_type in ("agent_answer", "agent_select"):
        return {
            "type": event_type,
            **{k: v for k, v in payload.items() if k != "answer"},
            "stop": stop,
            "reason": reason,
            **usage,
        }
    return None


def _session_trajectories(store: Any, session_ids: list[str]) -> list[dict[str, Any]]:
    """Read-only per-session trajectories for the raw file."""
    out: list[dict[str, Any]] = []
    for sid in session_ids:
        try:
            events = store.list_events(sid)
        except Exception as exc:
            out.append({"session_id": sid, "error": type(exc).__name__})
            continue
        items: list[dict[str, Any]] = []
        questions: list[dict[str, Any]] = []
        for event in events:
            payload = dict(getattr(event, "payload", None) or {})
            projected = _project_trajectory_event(str(getattr(event, "event_type", "")), payload)
            if projected is None:
                continue
            items.append(projected)
            if projected["type"] == "agent_question":
                questions.append(
                    {
                        "question_id": projected.get("question_id"),
                        "question_text": projected.get("question_text"),
                        "options": projected.get("question_options"),
                    }
                )
        try:
            Committed = store.get(sid)
            titles = [
                str(s.get("title"))
                for s in (getattr(Committed, "suggestions", None) or [])
                if isinstance(s, dict)
            ]
        except Exception:
            titles = []
        out.append(
            {
                "session_id": sid,
                "events": items,
                "questions": questions,
                "option_titles": titles,
            }
        )
    return out


async def _aclose_provider(candidate: Any) -> None:
    """Close a provider when it offers ``aclose`` (real SDK clients hold
    loop-bound pools; fakes simply lack the method)."""
    close = getattr(candidate, "aclose", None)
    if callable(close):
        closing = close()
        if asyncio.iscoroutine(closing):
            await closing


def _caused_by_budget(exc: BaseException) -> bool:
    """True when BudgetExhausted is anywhere in the exception chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, BudgetExhausted):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _classify_loop_error(exc: Any) -> str | None:
    """Fatal run stops: provider-auth and contact-operator terminals."""
    if getattr(exc, "reason", None) == "provider_auth":
        return "provider-auth"
    if getattr(exc, "next_action", None) == "contact_operator":
        return "contact-operator"
    return None


# Terminal error reasons that stop (never complete) a scenario when no
# final was produced. Provider-side failures (including auth/refusal/
# filter: the request reached the provider stack) vs configuration
# failures (disabled generation, runner setup).
_PROVIDER_ERROR_STOPS = frozenset(
    {
        "provider_timeout",
        "provider_rate_limited",
        "provider_unavailable",
        "truncated_incomplete_response",
        "schema_failure",
        "provider_auth",
        "provider_not_found",
        "provider_bad_request",
        "provider_request_error",
        "provider_refusal",
        "provider_content_filter",
    }
)
_CONFIG_ERROR_STOPS = frozenset({"generation_disabled"})


def _final_answered(final: dict[str, Any] | None) -> bool:
    """True when the run produced an answer: options, a plan, a
    technique answer, a web answer, or an accepted final (a clarifying
    question the owner can answer)."""
    if not isinstance(final, dict):
        return False
    if final.get("options") or final.get("plan") or final.get("technique_answer"):
        return True
    if final.get("web_answer"):
        return True
    return bool(final.get("question"))


def run_scenario_live(
    *,
    engine: Any,
    store: Any,
    settings: Any,
    scenario: dict[str, Any],
    ledger: SpendLedger,
    provider_factory: Any,
    context_factory: Any,
    raw_dir: Path,
    max_attempts: int = MAX_ATTEMPTS,
    recipe_resolver: Any = None,
    technique_resolver: Any = None,
    manual_review: bool = False,
    fresh_provider_per_run: bool = False,
) -> dict[str, Any]:
    from culinary_copilot.agent.loop import (
        AgentDeps,
        AgentLoopError,
        record_answer,
        record_select,
        record_user_message,
        run_agent,
    )

    created: list[str] = []
    attempts: list[dict[str, Any]] = []
    flow = list(scenario.get("flow", ["recommend"]))
    last_final: dict[str, Any] | None = None
    last_stop = ""
    run_stop: dict[str, str] | None = None

    def _runner_error(
        attempt_record: dict[str, Any], exc: BaseException, what: str
    ) -> dict[str, str]:
        detail = f"{what}: {type(exc).__name__}: {str(exc)[:200]}"
        attempt_record["runs"].append(
            {"stop_reason": "runner-error", "error": True, "message": detail}
        )
        return {"reason": "runner-error", "detail": detail}

    def _run_once(sid: str, shared_deps: Any, tool_context: Any) -> Any:
        """One run_agent on a loop-bound provider.

        With ``fresh_provider_per_run`` (live path) each run gets a new
        provider that is used and closed inside a single event loop, so
        no AsyncOpenAI client outlives its loop. Otherwise the
        scenario-level provider is reused (scripted fakes keep
        cross-run state).
        """
        if not fresh_provider_per_run:
            return asyncio.run(run_agent(sid, deps=shared_deps))

        async def _run_and_close() -> Any:
            fresh = provider_factory(scenario)
            fresh_deps = AgentDeps(
                settings=settings,
                session_store=store,
                provider=fresh,
                tool_context=tool_context,
                recipe_resolver=recipe_resolver,
                technique_resolver=technique_resolver,
                request_text=scenario.get("request"),
            )
            try:
                return await run_agent(sid, deps=fresh_deps)
            finally:
                await _aclose_provider(fresh)

        return asyncio.run(_run_and_close())

    for attempt in range(1, max_attempts + 1):
        sid = _new_session_id(scenario["key"], attempt)
        attempt_record: dict[str, Any] = {"attempt": attempt, "session_id": sid, "runs": []}
        try:
            # Build before any write: a setup failure leaves no session row.
            # The probe doubles as the scenario provider on the shared
            # path; on the fresh path it is closed at once and each run
            # gets its own loop-bound provider via _run_once.
            provider = provider_factory(scenario)
            tool_context = context_factory(store, scenario)
            if fresh_provider_per_run:
                asyncio.run(_aclose_provider(provider))
                provider = None
        except Exception as exc:
            attempt_record["setup_error"] = {
                "type": type(exc).__name__,
                "message": str(exc)[:200],
            }
            last_final, last_stop = None, "runner-error"
            run_stop = {
                "reason": "runner-error",
                "detail": f"setup: {type(exc).__name__}: {str(exc)[:200]}",
            }
            attempts.append(attempt_record)
            break
        created.append(sid)
        session_seed = dict(scenario.get("session", {}))
        from culinary_copilot.domain.sessions import SessionState

        try:
            store.create(SessionState(id=sid, **session_seed))
            if scenario.get("request"):
                # The model sees the request via user_message events (the
                # same helper the API uses); replay/resume-safe.
                record_user_message(store, sid, text=str(scenario["request"]))
            deps = AgentDeps(
                settings=settings,
                session_store=store,
                provider=provider,
                tool_context=tool_context,
                recipe_resolver=recipe_resolver,
                technique_resolver=technique_resolver,
                request_text=scenario.get("request"),
            )
            result = _run_once(sid, deps, tool_context)
            attempt_record["runs"].append(
                {"stop_reason": result.stop_reason, "phase": result.phase, "final": result.final}
            )
            last_final, last_stop = result.final, result.stop_reason
            if result.stop_reason == "agent_needs_user_input" and "resume" in flow:
                answers = list(scenario.get("scripted_answers", []))
                if answers:
                    # Answer the actual pending question from the last
                    # final (the model invents its own IDs); the asked
                    # text is recorded for manual review, never graded.
                    asked = (last_final or {}).get("question") or {}
                    try:
                        current = store.get(sid)
                        record_answer(
                            store,
                            sid,
                            expected_revision=current.revision,
                            question_id=asked.get("question_id"),
                            answer=answers[0]["answer"],
                        )
                    except Exception as exc:
                        last_final, last_stop = None, "runner-error"
                        run_stop = _runner_error(attempt_record, exc, "answer")
                        attempts.append(attempt_record)
                        break
                    attempt_record["answered_question"] = {
                        "question_id": asked.get("question_id"),
                        "question_text": asked.get("question_text"),
                        "question_options": asked.get("options"),
                        "scripted_answer": answers[0]["answer"],
                    }
                    result2 = _run_once(sid, deps, tool_context)
                    attempt_record["runs"].append(
                        {
                            "stop_reason": result2.stop_reason,
                            "phase": result2.phase,
                            "final": result2.final,
                        }
                    )
                    last_final, last_stop = result2.final, result2.stop_reason
            if "select-first" in flow and (last_final or {}).get("options"):
                first = last_final["options"][0]
                try:
                    current = store.get(sid)
                    record_select(
                        store,
                        sid,
                        expected_revision=current.revision,
                        dataset_id=first["dataset_id"],
                        source_id=first["source_id"],
                    )
                except Exception as exc:
                    last_final, last_stop = None, "runner-error"
                    run_stop = _runner_error(attempt_record, exc, "select")
                    attempts.append(attempt_record)
                    break
                if "plan" in flow:
                    result3 = _run_once(sid, deps, tool_context)
                    attempt_record["runs"].append(
                        {
                            "stop_reason": result3.stop_reason,
                            "phase": result3.phase,
                            "final": result3.final,
                        }
                    )
                    last_final, last_stop = result3.final, result3.stop_reason
        except AgentLoopError as exc:
            if _caused_by_reservation_breach(exc):
                # Reservation breach: the bound failed, not the model.
                # Record the stop, end the scenario and the whole run
                # with contact-operator (no further calls, no grade).
                attempt_record["runs"].append(
                    {"stop_reason": "reservation-breach", "reservation_breach": True}
                )
                last_final, last_stop = None, "reservation-breach"
                run_stop = {
                    "reason": "contact-operator",
                    "detail": f"reservation breach: {str(exc)[:200]}",
                }
                attempts.append(attempt_record)
                break
            if _caused_by_budget(exc):
                # Reservation refusal: end the scenario cleanly with no
                # further attempt. Never an agent or provider failure.
                attempt_record["runs"].append(
                    {"stop_reason": "budget-exhausted", "budget_stop": True}
                )
                last_final, last_stop = None, "budget-exhausted"
                run_stop = {"reason": "budget"}
                attempts.append(attempt_record)
                break
            fatal = _classify_loop_error(exc)
            attempt_record["runs"].append(
                {
                    "stop_reason": exc.reason,
                    "error": True,
                    "http_status": exc.http_status,
                    "message": exc.message,
                    "provider_error": dict(getattr(exc, "detail", None) or {}),
                }
            )
            last_final, last_stop = None, exc.reason
            if fatal is not None:
                run_stop = {"reason": fatal}
        except Exception as exc:
            # Reservation breach (raised raw by the ledger wrappers, not
            # as an AgentLoopError): stop the run with
            # contact-operator, do not grade the wreckage.
            if _caused_by_reservation_breach(exc):
                attempt_record["runs"].append(
                    {"stop_reason": "reservation-breach", "reservation_breach": True}
                )
                last_final, last_stop = None, "reservation-breach"
                run_stop = {
                    "reason": "contact-operator",
                    "detail": f"reservation breach: {type(exc).__name__}: {str(exc)[:200]}",
                }
                attempts.append(attempt_record)
                break
            if isinstance(exc, SearchEstimateExceeded):
                # Estimate breach: the search was billed above its
                # estimate. Stop the campaign at once; the owner must
                # acknowledge it in the campaign history (LIVE_PLAN.md)
                # before preflight runs again.
                attempt_record["runs"].append(
                    {
                        "stop_reason": "search-estimate-exceeded",
                        "search_estimate_exceeded": True,
                        "search_report": dict(getattr(exc, "report", None) or {}),
                    }
                )
                last_final, last_stop = None, "search-estimate-exceeded"
                run_stop = {
                    "reason": "search-estimate-exceeded",
                    "detail": f"search estimate exceeded: {str(exc)[:200]}",
                    "search_report": dict(getattr(exc, "report", None) or {}),
                }
                attempts.append(attempt_record)
                break
            # Preflight-class failure mid-run (snapshot, store, driver):
            # stop the run, do not grade the wreckage.
            attempt_record["runs"].append(
                {"stop_reason": "runner-error", "error": True, "message": str(exc)[:200]}
            )
            last_final, last_stop = None, "runner-error"
            run_stop = {
                "reason": "runner-error",
                "detail": f"run: {type(exc).__name__}: {str(exc)[:200]}",
            }
            attempts.append(attempt_record)
            break
        attempts.append(attempt_record)
        if run_stop is not None:
            break
        if last_stop == (scenario.get("expected", {}) or {}).get("stop_reason") and last_final:
            break
    if run_stop is not None and run_stop.get("reason") == "budget":
        grades: dict[str, Any] = {"graded": False, "reason": "budget-exhausted"}
        status = "not_completed: budget"
    elif (
        last_stop == "reservation-breach"
        and run_stop is not None
        and run_stop.get("reason") == "contact-operator"
    ):
        grades = {"graded": False, "reason": "reservation-breach"}
        status = "stopped: contact-operator"
    elif not created:
        grades = {"graded": False, "reason": "runner-error"}
        status = "stopped: runner-error"
    elif last_final is None and last_stop == "runner-error":
        # A crash, not a setup problem: its own status so it is never
        # mistaken for a configuration error.
        grades = {"graded": False, "reason": "runner-error"}
        status = "stopped: runner-error"
    elif last_final is None and last_stop in _PROVIDER_ERROR_STOPS:
        # No final and no loop-stop reason: a provider error before any
        # model output the grades could judge. Never "completed".
        grades = {"graded": False, "reason": last_stop}
        status = "stopped: provider-error"
    elif last_final is None and last_stop in _CONFIG_ERROR_STOPS:
        grades = {"graded": False, "reason": last_stop}
        status = "stopped: config-error"
    else:
        grades = grade_attempt(
            scenario, last_final, last_stop, store, created[-1], manual_review=manual_review
        )
        # "completed" alone hid runs that ended without an answer:
        # answered means options, a plan, or an accepted final.
        status = (
            "completed: answered"
            if _final_answered(last_final)
            else f"completed: no-answer ({last_stop})"
        )
    return {
        "key": scenario["key"],
        "first_attempt": attempts[0] if attempts else None,
        "final_attempt": attempts[-1] if attempts else None,
        "attempts": len(attempts),
        "stop_reason": last_stop,
        "status": status,
        "run_stop": run_stop,
        "grades": grades,
        "sessions": created,
        "trajectory": _session_trajectories(store, created),
    }


# --- CLI -------------------------------------------------------------------------


def _args(argv: list[str] | None = None) -> Any:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios-file", default=str(DEFAULT_SCENARIOS))
    parser.add_argument(
        "--scenarios",
        default="",
        help="comma-separated scenario keys to run (default: all)",
    )
    parser.add_argument("--database-url", default="")
    parser.add_argument("--expect-db-name", default="")
    parser.add_argument("--expect-db-host", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-attempts", type=int, choices=(1, 2), default=MAX_ATTEMPTS)
    parser.add_argument("--ceiling-usd", type=float, default=None)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--raw-dir", default="")
    parser.add_argument("--summary-out", default="")
    parser.add_argument(
        "--search-max-per-live-session",
        type=int,
        default=PHASE5_MAX_SEARCHES_PER_SESSION,
        help="live-check searches per session (at most 2, inside code limit 3)",
    )
    parser.add_argument(
        "--budget-pool",
        choices=("phase3", "phase5"),
        default="phase3",
        help="which cap/history the run charges (phase3: $0.15 cap; phase5: $0.10 campaign)",
    )
    parser.add_argument(
        "--acknowledge-search-estimate",
        default="",
        help="owner decision 4 option A: must equal phase5-decision-4-2026-10-02 "
        "for live runs with search selected",
    )
    parser.add_argument(
        "--max-campaign-searches",
        type=int,
        default=None,
        help="paid searches this run may dispatch (campaign cap 4 across all runs; step 1 uses 1)",
    )
    parser.add_argument(
        "--stop-after-first-search",
        action="store_true",
        help="step-1 behavior: stop right after the first search's scenario completes",
    )
    return parser.parse_args(argv)


def _require_disposable_db(url: str) -> str:
    from urllib.parse import urlparse

    name = urlparse(url).path.rsplit("/", 1)[-1]
    lowered = url.lower()
    if name == "culinary_copilot" and ("localhost:5432" in lowered or "127.0.0.1:5432" in lowered):
        return "refusing --fake on the application database"
    if "test" not in name and "disposable" not in name and "check" not in name:
        return f"refusing --fake on database {name!r}"
    return ""


def _ensure_disposable_db(db_url: str) -> None:
    """Drop + create the disposable database (fake runs and tests only)."""
    from urllib.parse import urlparse

    from sqlalchemy import create_engine, text

    parts = urlparse(db_url)
    name = parts.path.rsplit("/", 1)[-1]
    head = db_url.rsplit("/", 1)[0]
    maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{name}' AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
        with create_engine(db_url).begin() as migration_conn:
            from culinary_copilot.recipes import import_data

            import_data.apply_migrations(migration_conn)
    maint.dispose()


def _drop_disposable_db(db_url: str) -> None:
    from urllib.parse import urlparse

    from sqlalchemy import create_engine, text

    parts = urlparse(db_url)
    name = parts.path.rsplit("/", 1)[-1]
    head = db_url.rsplit("/", 1)[0]
    maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{name}' AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    maint.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    from culinary_copilot.config import Settings

    scenarios = load_scenarios(Path(args.scenarios_file))
    wanted = [k.strip() for k in str(args.scenarios or "").split(",") if k.strip()]
    if wanted:
        known = [str(s.get("key", "")) for s in scenarios["scenarios"]]
        unknown = [k for k in wanted if k not in known]
        if unknown:
            print(f"error: unknown scenario keys: {', '.join(unknown)}", file=sys.stderr)
            return 2
        scenarios = {
            **scenarios,
            "scenarios": [s for s in scenarios["scenarios"] if str(s.get("key")) in wanted],
        }
    if args.fake:
        settings = Settings(_env_file=None)
        db_url = args.database_url or settings.database_url.get_secret_value()
        guard = _require_disposable_db(db_url)
        if guard:
            print(f"error: {guard}", file=sys.stderr)
            return 2
        raw_dir = Path(args.raw_dir) if args.raw_dir else Path(str(DEFAULT_RAW_DIR) + "-fake")
        summary_out = Path(args.summary_out) if args.summary_out else raw_dir / "summary.json"
        _ensure_disposable_db(db_url)
        try:
            return _run_all(args, settings, scenarios, db_url, raw_dir, summary_out, fake=True)
        finally:
            _drop_disposable_db(db_url)
    if not (args.live and args.yes and args.ceiling_usd is not None):
        print(
            "error: refusing without --live --yes --ceiling-usd (live run not "
            "authorized); --fake runs the full pipeline on a disposable DB.",
            file=sys.stderr,
        )
        return 2
    pool = str(getattr(args, "budget_pool", "phase3") or "phase3")
    pool_info = BUDGET_POOLS.get(pool, BUDGET_POOLS["phase3"])
    pool_history: Path = pool_info["history"]
    pool_cap = float(pool_info["cap_usd"])
    if float(args.ceiling_usd) > pool_cap:
        print(f"error: ceiling exceeds ${pool_cap:.2f} {pool} pool cap", file=sys.stderr)
        return 2
    if not args.expect_db_name or not args.expect_db_host:
        print("error: --expect-db-name and --expect-db-host are required", file=sys.stderr)
        return 2
    settings = _effective_settings(Settings())
    ok, problems, record = preflight(args, settings, scenarios, history_path=pool_history)
    record["scenarios_file"] = str(args.scenarios_file)
    if not ok:
        for problem in problems:
            print(f"preflight: {problem}", file=sys.stderr)
        return 2
    db_url = args.database_url or settings.database_url.get_secret_value()
    raw_dir = Path(args.raw_dir) if args.raw_dir else DEFAULT_RAW_DIR
    summary_out = Path(args.summary_out) if args.summary_out else LIVE_SUMMARY
    result = _run_all(
        args,
        settings,
        scenarios,
        db_url,
        raw_dir,
        summary_out,
        fake=False,
        history_path=pool_history,
    )
    print(json.dumps({"preflight": record}, indent=2))
    return result


def _count_session_claims(store: Any, session_ids: list[str] | None) -> int:
    """search_slot_claimed events across sessions (0 when unreadable)."""
    total = 0
    for session_id in session_ids or []:
        try:
            events = store.list_events(session_id)
        except Exception:
            continue
        for event in events or []:
            if getattr(event, "event_type", "") == "search_slot_claimed":
                total += 1
    return total


def _run_all(
    args: Any,
    settings: Any,
    scenarios: dict[str, Any],
    db_url: str,
    raw_dir: Path,
    summary_out: Path,
    *,
    fake: bool,
    history_path: Path | str | None = None,
) -> int:
    from sqlalchemy import create_engine

    from culinary_copilot.services.session_store import PostgresSessionStore

    engine = create_engine(db_url)
    store = PostgresSessionStore(engine)
    model = str(args.model or settings.llm_rec_model)
    ledger = SpendLedger(model=model, ceiling_usd=float(args.ceiling_usd or 0.0))
    if history_path:
        # The cap covers the whole evaluation: this run may only spend
        # what prior recorded runs left.
        prior = recorded_spend_total(Path(history_path))
        ledger = SpendLedger(
            model=model, ceiling_usd=max(0.0, float(args.ceiling_usd or 0.0) - prior)
        )
    max_output = int(settings.llm_rec_max_output_tokens)
    effective = _effective_settings(settings)
    pre = snapshot(engine)
    created_all: list[str] = []
    scenario_reports: list[dict[str, Any]] = []
    raw_dir.mkdir(parents=True, exist_ok=True)
    streak = 0
    stopped_early: dict[str, str] | None = None

    if not fake:
        _provider_factory = _live_provider_factory(effective, ledger, max_output)
        _context_factory = _live_context_factory(effective, engine, ledger)

    for scenario in scenarios["scenarios"]:
        try:
            if fake:
                report = _run_fake_scenario(
                    engine, store, effective, scenario, ledger, raw_dir, args.max_attempts
                )
            else:
                scenario_settings = _scenario_settings(effective, scenario)
                report = run_scenario_live(
                    engine=engine,
                    store=store,
                    settings=scenario_settings,
                    scenario=scenario,
                    ledger=ledger,
                    provider_factory=_provider_factory,
                    context_factory=_context_factory,
                    raw_dir=raw_dir,
                    max_attempts=args.max_attempts,
                    recipe_resolver=None,
                    manual_review=True,
                    fresh_provider_per_run=True,
                )
        except Exception as exc:
            # Never die with a traceback and no summary: record the
            # runner error, stop the run, keep the ledger and snapshots.
            report = {
                "key": scenario["key"],
                "status": "stopped: runner-error",
                "run_stop": {
                    "reason": "runner-error",
                    "detail": f"scenario: {type(exc).__name__}: {str(exc)[:200]}",
                },
                "sessions": [],
                "attempts": 0,
                "stop_reason": "runner-error",
                "grades": {"graded": False, "reason": "runner-error"},
                "first_attempt": None,
            }
        created_all.extend(report["sessions"])
        report["searches_dispatched"] = _count_session_claims(store, report.get("sessions"))
        scenario_reports.append(report)
        (raw_dir / f"{scenario['key']}.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        if report.get("run_stop") is not None:
            stopped_early = {
                "reason": str(report["run_stop"].get("reason")),
                "after_scenario": scenario["key"],
            }
            if report["run_stop"].get("detail"):
                stopped_early["detail"] = str(report["run_stop"]["detail"])
            break
        if (
            bool(getattr(args, "stop_after_first_search", False))
            and (report.get("searches_dispatched", 0) or 0) > 0
        ):
            # Step-1 behavior: stop right after the first search's
            # scenario completes (later steps need their own go-ahead).
            stopped_early = {
                "reason": "step-1-complete: first search done",
                "after_scenario": scenario["key"],
                "searches_dispatched": int(report["searches_dispatched"]),
            }
            break
        if report.get("stop_reason") in ("agent_no_progress", "agent_validation_failed"):
            streak += 1
            if streak >= 2:
                stopped_early = {
                    "reason": "consecutive-failures",
                    "after_scenario": scenario["key"],
                }
                break
        else:
            streak = 0

    if stopped_early is not None:
        done_keys = {r["key"] for r in scenario_reports}
        for scenario in scenarios["scenarios"]:
            if scenario["key"] not in done_keys:
                scenario_reports.append(
                    {"key": scenario["key"], "status": f"not_run: {stopped_early['reason']}"}
                )

    post = snapshot(engine)
    isolated, isolation_problems = verify_isolation(pre, post, set(created_all))

    def _attempt_stop(attempt: Any) -> str | None:
        runs = (attempt or {}).get("runs") or []
        return runs[-1].get("stop_reason") if runs else None

    expected_by_key = {
        str(s.get("key", "")): ((s.get("expected") or {}).get("stop_reason"))
        for s in scenarios["scenarios"]
    }

    def _stop_matched(report: dict[str, Any]) -> bool:
        expected = expected_by_key.get(str(report.get("key", "")))
        return expected is not None and report.get("stop_reason") == expected

    summary = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fake": fake,
        "budget_pool": str(getattr(args, "budget_pool", "phase3") or "phase3"),
        "scenarios_file": str(getattr(args, "scenarios_file", "") or ""),
        "scenarios_sha256": scenarios["freeze_sha256"],
        "model": model,
        "scenario_keys": [str(r.get("key", "")) for r in scenario_reports],
        "max_attempts": int(getattr(args, "max_attempts", MAX_ATTEMPTS) or MAX_ATTEMPTS),
        "spend": ledger.summary(),
        "isolation": {"ok": isolated, "problems": isolation_problems},
        "stopped_early": stopped_early,
        "scenarios": [
            {
                "key": r["key"],
                "status": r.get("status", "completed"),
                "attempts": r.get("attempts", 0),
                "stop_reason": r.get("stop_reason"),
                "expected_stop_matched": _stop_matched(r),
                "searches_dispatched": r.get("searches_dispatched", 0),
                "grades": r.get("grades", {"graded": False, "reason": "not-run"}),
                "first_attempt_stop": _attempt_stop(r.get("first_attempt")),
            }
            for r in scenario_reports
        ],
    }
    raw_dir.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    done_statuses = [str(r.get("status", "")) for r in scenario_reports]
    n_answered = sum(1 for s in done_statuses if s == "completed: answered" or s == "completed")
    n_no_answer = sum(1 for s in done_statuses if s.startswith("completed: no-answer"))
    n_not_run = sum(1 for s in done_statuses if s.startswith("not_run"))
    n_stopped = len(done_statuses) - n_answered - n_no_answer - n_not_run
    n_matched = sum(1 for e in summary["scenarios"] if e.get("expected_stop_matched") is True)
    print(
        f"scenarios: {n_answered} answered, {n_no_answer} no-answer, "
        f"{n_stopped} stopped, {n_not_run} not_run of {len(done_statuses)}; "
        f"{n_matched} matched expected stop; isolation ok: {isolated}"
    )
    this_run_spend = float(ledger.spent_usd)
    if history_path:
        recorded_total = append_spend_history(
            Path(history_path),
            model=model,
            entries=list(ledger.entries),
            ceiling_usd=float(args.ceiling_usd or 0.0),
        )
        print(
            f"spent ${this_run_spend:.4f} this run; "
            f"${recorded_total:.4f} recorded total of ${float(args.ceiling_usd or 0.0):.2f}"
        )
    else:
        print(f"spent ${this_run_spend:.4f} this run")
    print(f"spent ${ledger.spent_usd:.4f} of ${ledger.ceiling_usd:.2f}")
    engine.dispose()
    return 0 if isolated else 1


_FAKE_WEB_URL = "https://example.com/okonomiyaki-guide"


class _FakeWebSearchProvider:
    """Offline fake search sub-request (tests only, proves shape only)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete_web_search(
        self, *, instruction: str, query: str, max_output_tokens: int | None = None
    ) -> Any:
        from culinary_copilot.llm.client import WebSearchResult

        self.calls.append({"instruction": instruction, "query": query})
        return WebSearchResult(
            performed=True,
            parsed={
                "summary": "Okonomiyaki is a savoury Japanese pancake.",
                "sources": [
                    {
                        "url": _FAKE_WEB_URL,
                        "title": "Okonomiyaki guide",
                        "excerpt_model": "a savoury pancake",
                        "published_at": None,
                    }
                ],
            },
            web_search_call_ids=["ws_fake_1"],
            citations=[{"url": _FAKE_WEB_URL, "title": "Okonomiyaki guide"}],
            action_sources=[{"type": "url", "url": _FAKE_WEB_URL}],
            model="fake",
            latency_ms=1,
            attempts=1,
        )


def _fake_context(current_store: Any, settings: Any, scenario: dict[str, Any] | None) -> Any:
    from culinary_copilot.tools.registry import ToolContext

    rows = [] if (scenario or {}).get("fake_flow") == "empty" else list(_FAKE_ROWS)
    core = _FakeEpicureCore(enabled=bool(settings.epicure_enabled))
    return ToolContext(
        settings=settings,
        engine=None,
        session_store=current_store,
        record_tool_args=True,
        impl_overrides={
            "search_recipes": _fake_search(rows),
            "get_recipe": _fake_get,
            "search_techniques": _fake_techniques,
        },
        epicure_core=core,
        epicure_cooc=core,
        epicure_chem=core,
        search_provider=_FakeWebSearchProvider(),
    )


def _scenario_settings(settings: Any, scenario: dict[str, Any]) -> Any:
    """Per-scenario settings: scenario overrides win, never the .env file.

    The Epicure-unavailable scenario sets ``epicure_enabled: false``
    here (configuration for that scenario only). Internal retries stay
    forced to zero (runner-level attempts only).
    """
    overrides = dict(scenario.get("settings", {}))
    if not overrides:
        return _effective_settings(settings)
    data = settings.model_dump()
    data.update(overrides)
    return _effective_settings(type(settings)(_env_file=None, **data))


def _live_provider_factory(settings: Any, ledger: SpendLedger, max_output: int) -> Any:
    """Model provider factory for live runs (module-level for tests)."""

    def _factory(scenario: dict[str, Any]) -> Any:
        from culinary_copilot.llm.client import OpenAIApplicationProvider

        return LedgerModelProvider(
            OpenAIApplicationProvider(settings), ledger, max_output=max_output
        )

    return _factory


def _wrap_context_embed_provider(ctx: Any, ledger: SpendLedger, settings: Any) -> Any:
    """Install the ledger-wrapped query-embedding provider on one context.

    The single shared ``embed_provider`` serves both ``search_recipes``
    and ``search_techniques`` (each reads it from the tool context), so
    wrapping it once covers both tools' query embeddings.
    """
    if getattr(ctx, "embed_provider", None) is not None:
        ctx.embed_provider = LedgerEmbedProvider(
            ctx.embed_provider, ledger, retries=int(getattr(settings, "embed_max_retries", 0) or 0)
        )
    return ctx


def _live_context_factory(settings: Any, engine: Any, ledger: SpendLedger) -> Any:
    """Tool-context factory for live runs (module-level for tests)."""

    def _factory(current_store: Any, scenario: dict[str, Any]) -> Any:
        from culinary_copilot.llm.client import OpenAIApplicationProvider
        from culinary_copilot.tools import build_tool_context

        context = build_tool_context(settings=settings, engine=engine, session_store=current_store)
        # Reviewable raw trajectories: the live run opts into bounded
        # args in tool_call events (default stays digest-only).
        context.record_tool_args = True
        context = _wrap_context_embed_provider(context, ledger, settings)
        if bool((scenario.get("session", {}) or {}).get("internet_search_allowed")):
            # Search-on scenarios get the ledgered hosted-search
            # sub-request (decision 4, option A estimate accounting).
            context.search_provider = LedgeredSearchProvider(
                OpenAIApplicationProvider(settings),
                ledger,
                model=str(getattr(settings, "llm_rec_model", "") or ""),
            )
        return context

    return _factory


class LedgeredSearchProvider:
    """Wraps a search sub-request provider: estimate per search, settle after.

    Decision 4, option A: the $0.025 figure is an estimate with
    acknowledged overrun risk, not a bound. Each dispatch reserves the
    estimate on the campaign ledger (refusal stops the scenario with
    budget-exhausted); afterwards the reported usage reconciles it and
    a reconciled cost above the estimate stops the whole campaign via
    :class:`SearchEstimateExceeded`.
    """

    def __init__(
        self,
        inner: Any,
        ledger: SpendLedger,
        *,
        estimate_usd: float = SEARCH_ESTIMATE_USD,
        max_output_tokens: int = 1500,
        model: str | None = None,
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._estimate_usd = float(estimate_usd)
        self._max_output_tokens = int(max_output_tokens)
        self._model = model
        self._seq = 0
        self.last_report: dict[str, Any] | None = None

    async def complete_web_search(
        self, *, instruction: str, query: str, max_output_tokens: int | None = None
    ) -> Any:
        from culinary_copilot.llm.models import PRICING_VERSION

        self._seq += 1
        label = f"search-{self._seq}"
        if not self._ledger.reserve_search(label, estimate_usd=self._estimate_usd):
            raise BudgetExhausted(
                f"search estimate ${self._estimate_usd:.4f} does not fit the remainder"
            )
        try:
            result = await self._inner.complete_web_search(
                instruction=instruction,
                query=query,
                max_output_tokens=(
                    max_output_tokens if max_output_tokens is not None else self._max_output_tokens
                ),
            )
        except Exception:
            self._ledger.keep(label)
            raise
        reported_in = getattr(result, "input_tokens", None)
        reported_out = getattr(result, "output_tokens", None)
        raw_usage = {
            "input_tokens": reported_in,
            "output_tokens": reported_out,
            "model": getattr(result, "model", None),
            "response_id": getattr(result, "response_id", None),
            "attempts": getattr(result, "attempts", None),
        }
        report = self._ledger.reconcile_search(
            label,
            reported_in=reported_in,
            reported_out=reported_out,
            model=self._model or getattr(result, "model", None),
            pricing_version=PRICING_VERSION,
            raw_usage=raw_usage,
        )
        self.last_report = report
        return result


def _run_fake_scenario(
    engine: Any,
    store: Any,
    settings: Any,
    scenario: dict[str, Any],
    ledger: SpendLedger,
    raw_dir: Path,
    max_attempts: int,
) -> dict[str, Any]:
    # The fake path takes Epicure enablement through the same
    # effective-settings code as the live path: a disabled environment
    # fails offline too (scenario overrides still win, so the
    # unavailable scenario keeps its false override).
    scenario_settings = _scenario_settings(settings, scenario)

    def _provider_factory(current: dict[str, Any]) -> Any:
        return FakeRunProvider(current)

    def _context_factory(current_store: Any, current: dict[str, Any]) -> Any:
        return _fake_context(current_store, scenario_settings, current)

    def _recipe_resolver(dataset_id: str, source_id: str) -> dict[str, Any] | None:
        doc = _FAKE_DOCS.get((dataset_id, source_id))
        return dict(doc) if doc is not None else None

    return run_scenario_live(
        engine=engine,
        store=store,
        settings=scenario_settings,
        scenario=scenario,
        ledger=ledger,
        provider_factory=_provider_factory,
        context_factory=_context_factory,
        raw_dir=raw_dir,
        max_attempts=max_attempts,
        recipe_resolver=_recipe_resolver,
        technique_resolver=_fake_technique_resolver,
    )


if __name__ == "__main__":
    raise SystemExit(main())
