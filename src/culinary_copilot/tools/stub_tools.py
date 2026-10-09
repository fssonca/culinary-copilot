"""Permission-gated web search (Milestone 3, Phase 5, part 2, offline).

Replaces the Phase 2 stub: ``search_web`` keeps its name and cost
class (``network``) but is now server-bound. The session id is bound
from the server run context (``ToolContext.bound_session_id``); the
model-facing arguments carry only the query. A spoofed id is
impossible by construction: the args model has no session field and
``extra="forbid"`` rejects one, and the impl reads only the bound id.

Atomic slot claim before dispatch (one transaction in
``PostgresSessionStore.claim_search_slot``, called via
``asyncio.to_thread`` so parallel calls overlap): lock the session row
(SELECT ... FOR UPDATE), re-read internet_search_allowed, count
claimed slots, append search_slot_claimed + search_requested. If the
claim cannot be recorded, do not search. Permission-denied calls claim
no slot. Dispatched attempts count even when they fail or are
ambiguous. Call ids come from the registry's per-call context copy
(uuid4 fallback); millisecond clocks are never used. Existing tables
only; no migration. Toggle-off blocks every search not yet claimed; it
cannot undo a request already dispatched. Minimization is fail-closed:
a minimizer failure refuses the search with no slot, no provider call
and no stored text.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from culinary_copilot.domain.recommendations import (
    REASON_SEARCH_BUDGET_EXHAUSTED,
    REASON_SEARCH_NOT_PERFORMED,
    REASON_SEARCH_UNVERIFIED,
    REASON_TOOL_INTERNAL_ERROR,
    REASON_TOOL_INVALID_ARGUMENTS,
    REASON_TOOL_NOT_CONFIGURED,
    REASON_TOOL_PERMISSION_DENIED,
    REASON_TOOL_UNAVAILABLE,
    next_action_for,
)
from culinary_copilot.tools.registry import ToolContext, ToolDefinition

SEARCH_TOOL_NAME = "search_web"


class SearchWebArgs(BaseModel):
    """Model-facing arguments: query only (session bound server-side)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)


class _MinimizationFailed(RuntimeError):
    """A minimizer threw: refuse the search, store no raw text."""


def _settings_value(context: ToolContext, name: str, default: Any) -> Any:
    settings = getattr(context, "settings", None)
    if settings is not None and hasattr(settings, name):
        try:
            return getattr(settings, name)
        except Exception:
            return default
    return default


def _minimized(query: str) -> str:
    """Minimized query; raises _MinimizationFailed (fail-closed, fix 1)."""
    from culinary_copilot.search.minimize import minimize_query as _min

    try:
        return _min(query)
    except Exception as exc:
        raise _MinimizationFailed(f"query minimization failed: {type(exc).__name__}") from exc


def _min_summary(text: str, limit: int) -> str:
    """Minimized model text; raises _MinimizationFailed (fail-closed)."""
    from culinary_copilot.search.minimize import minimize_summary as _min_sum

    try:
        return _min_sum(text, limit)
    except Exception as exc:
        raise _MinimizationFailed(f"summary minimization failed: {type(exc).__name__}") from exc


def _min_url(url: str) -> str:
    """Minimized URL; raises _MinimizationFailed (fail-closed)."""
    from culinary_copilot.search.minimize import minimize_url as _min_u

    try:
        return _min_u(url)
    except Exception as exc:
        raise _MinimizationFailed(f"url minimization failed: {type(exc).__name__}") from exc


def _min_error(text: str) -> str:
    """Minimized error; type-name only when the minimizer throws (closed)."""
    from culinary_copilot.search.minimize import minimize_error as _min_err

    try:
        return _min_err(text)
    except Exception:
        return "ProviderError"


def _provenance_key(url: str) -> str:
    """Normalized URL for provider-evidence matching (Checkpoint B 1).

    Both sides go through the same normalization: _min_url (strips
    query and fragment — citation URLs often carry ?utm_source
    parameters), lowercase scheme and host, no trailing slash except
    the root ("" path becomes "/"). Raises _MinimizationFailed, so a
    minimizer failure stays fail-closed with the rest of shaping.
    """
    from urllib.parse import urlsplit, urlunsplit

    minimized = _min_url(url)
    parts = urlsplit(minimized)
    path = parts.path
    if len(path) > 1:
        path = path.rstrip("/")
    if not path:
        path = "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _classify(url: str, context: ToolContext) -> str:
    try:
        from culinary_copilot.search.labels import classify_source, parse_domain_list

        settings = getattr(context, "settings", None)
        official = parse_domain_list(getattr(settings, "web_official_guidance_domains", ""))
        research = parse_domain_list(getattr(settings, "web_research_domains", ""))
        culinary = parse_domain_list(getattr(settings, "web_culinary_domains", ""))
        return classify_source(url, official=official, research=research, culinary=culinary)
    except Exception:
        return "unclassified"


def _unverified_result(
    store: Any,
    session_id: str,
    call_id: str,
    started: float,
    outcome: str,
    message: str,
) -> dict[str, Any]:
    """Typed failure with no evidence (Checkpoint B 1c): no summary, no
    sources. The slot stays spent. Event writes stay fail-closed."""
    try:
        store.append_event(session_id, "search_outcome", {"call_id": call_id, "outcome": outcome})
        store.append_event(
            session_id,
            "search_operations",
            {
                "call_id": call_id,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
    except Exception:
        reason = REASON_TOOL_UNAVAILABLE
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": "search_web unavailable: event write failed",
            "next_action": next_action_for(reason),
        }
    reason = REASON_SEARCH_UNVERIFIED
    return {
        "ok": False,
        "error_type": "unavailable",
        "reason": reason,
        "message": message,
        "next_action": next_action_for(reason),
    }


async def search_web_impl(args: SearchWebArgs, context: ToolContext) -> dict[str, Any]:
    """Bounded web search: claim a slot, then one sub-request, then events."""
    started = time.monotonic()
    session_id = getattr(context, "bound_session_id", None)
    if not session_id:
        reason = REASON_TOOL_NOT_CONFIGURED
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": "search_web not configured: no bound session",
            "next_action": next_action_for(reason),
        }
    store = getattr(context, "session_store", None)
    if store is None:
        reason = REASON_TOOL_NOT_CONFIGURED
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": "search_web not configured: no session store",
            "next_action": next_action_for(reason),
        }
    query_min: str
    try:
        query_min = _minimized(args.query)
    except _MinimizationFailed:
        # Fail-closed (review fix 1): claim no slot, call no provider,
        # store no query text. A defect marker, not a user error.
        reason = REASON_TOOL_INTERNAL_ERROR
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": "search_web refused: query minimization failed",
            "next_action": next_action_for(reason),
        }
    if not query_min.strip():
        reason = REASON_TOOL_INVALID_ARGUMENTS
        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": reason,
            "message": "search_web needs a non-empty query",
            "next_action": next_action_for(reason),
        }
    max_slots = int(_settings_value(context, "search_max_per_session", 3) or 3)
    limits = getattr(context, "search_limits", None)
    if limits is not None and getattr(limits, "max_per_session", None) is not None:
        # Live-run flag bound (2026-10-03 overrun fix): the claim's max
        # is min(code limit, --search-max-per-live-session).
        max_slots = min(max_slots, int(limits.max_per_session))
    # Registry call id via the per-call context copy (fix 3); uuid4
    # only for direct impl calls outside run_tool. Millisecond clocks
    # collide under parallel calls, so they are never used.
    call_id = str(getattr(context, "call_id", None) or f"web-{uuid.uuid4().hex[:12]}")
    try:
        # Sync store call off the event loop (fix 2): blocking the loop
        # here would serialize parallel calls behind the DB round-trip.
        claim = await asyncio.to_thread(
            store.claim_search_slot,
            session_id,
            max_slots=max_slots,
            call_id=call_id,
            minimized_query=query_min,
        )
    except Exception as exc:
        reason = REASON_TOOL_UNAVAILABLE
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": f"search_web unavailable: slot claim failed ({type(exc).__name__})",
            "next_action": next_action_for(reason),
        }
    if not claim.get("ok"):
        if claim.get("reason") == "permission_denied":
            reason = REASON_TOOL_PERMISSION_DENIED
            return {
                "ok": False,
                "error_type": "permission_denied",
                "reason": reason,
                "message": "search_web denied: internet_search_allowed is off for this session",
                "next_action": next_action_for(reason),
            }
        reason = REASON_SEARCH_BUDGET_EXHAUSTED
        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": reason,
            "message": (
                f"search budget exhausted: {claim.get('slots_used', max_slots)}/{max_slots} "
                "searches used in this session; start a new session"
            ),
            "next_action": next_action_for(reason),
        }
    if limits is not None:
        # Campaign/run hook (2026-10-03 overrun fix): after the slot
        # claim, before the provider call. A refused search returns
        # search_budget_exhausted with no provider call; the claim is
        # recorded as refused (the slot stays claimed).
        allowed, limit_reason = limits.check_and_claim()
        if not allowed:
            try:
                store.append_event(
                    session_id,
                    "search_outcome",
                    {"call_id": call_id, "outcome": "refused", "reason": limit_reason},
                )
            except Exception:
                pass
            reason = REASON_SEARCH_BUDGET_EXHAUSTED
            return {
                "ok": False,
                "error_type": "invalid_arguments",
                "reason": reason,
                "message": f"search budget exhausted: {limit_reason}; no provider call made",
                "next_action": next_action_for(reason),
            }
    provider = getattr(context, "search_provider", None)
    if provider is None:
        try:
            store.append_event(
                session_id,
                "search_outcome",
                {"call_id": call_id, "outcome": "not_configured"},
            )
        except Exception:
            pass
        reason = REASON_TOOL_NOT_CONFIGURED
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": "search_web not configured: search provider not implemented here",
            "next_action": next_action_for(reason),
        }
    try:
        from culinary_copilot.llm.client import WEB_SEARCH_INSTRUCTION

        instruction = WEB_SEARCH_INSTRUCTION
    except Exception:
        instruction = "Summarize the search results for the query."
    max_output = int(_settings_value(context, "search_max_output_tokens", 1500) or 1500)
    # Provider request timeout (owner decision 2026-10-03): at most
    # the tool timeout, so an abandoned request cannot keep running
    # long after the tool gave up. Defaults: tool 30 s, provider
    # min(rec 20 s, 30 s) = 20 s.
    from culinary_copilot.tools.registry import tool_timeout_s as _tool_timeout_s

    settings = getattr(context, "settings", None)
    tool_timeout = _tool_timeout_s("search_web", settings)
    try:
        rec_timeout = float(getattr(settings, "llm_rec_timeout_s", tool_timeout))
    except (TypeError, ValueError):
        rec_timeout = tool_timeout
    provider_timeout = min(tool_timeout, rec_timeout) if rec_timeout > 0 else tool_timeout
    try:
        result = await provider.complete_web_search(
            instruction=instruction,
            query=query_min,
            max_output_tokens=max_output,
            timeout=provider_timeout,
        )
    except asyncio.CancelledError:
        # Tool-level timeout cancelled the provider call (residual
        # case: with defaults the provider's own 20 s timeout fires
        # first and is recorded as outcome "error" below). Best
        # effort, then re-raise: no results are recorded, ledger
        # handling is unchanged (the run keeps the search spent), and
        # the cancellation still surfaces as a tool timeout upstream.
        try:
            store.append_event(
                session_id,
                "search_outcome",
                {"call_id": call_id, "outcome": "cancelled"},
            )
            store.append_event(
                session_id,
                "search_operations",
                {
                    "call_id": call_id,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    "error": "cancelled (tool timeout)",
                },
            )
        except Exception:
            pass
        raise
    except Exception as exc:
        if getattr(exc, "runner_stop", False):
            # Runner control flow (budget refusal, estimate breach):
            # the slot stays claimed (a dispatch happened), the
            # outcome is recorded without text, then propagation
            # stops the run instead of feeding the agent.
            try:
                store.append_event(
                    session_id,
                    "search_outcome",
                    {"call_id": call_id, "outcome": "runner-stop"},
                )
            except Exception:
                pass
            raise
        # Fail-closed: type-name-or-minimized error only, never raw text.
        err_text = _min_error(f"{type(exc).__name__}: {exc}")
        try:
            store.append_event(
                session_id, "search_outcome", {"call_id": call_id, "outcome": "error"}
            )
            store.append_event(
                session_id,
                "search_operations",
                {
                    "call_id": call_id,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    "error": err_text,
                },
            )
        except Exception:
            reason = REASON_TOOL_UNAVAILABLE
            return {
                "ok": False,
                "error_type": "unavailable",
                "reason": reason,
                "message": "search_web unavailable: event write failed",
                "next_action": next_action_for(reason),
            }
        reason = REASON_TOOL_UNAVAILABLE
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": f"search_web unavailable: {err_text}",
            "next_action": next_action_for(reason),
        }
    performed = bool(getattr(result, "performed", False))
    if not performed:
        try:
            store.append_event(
                session_id,
                "search_outcome",
                {"call_id": call_id, "outcome": "search_not_performed"},
            )
        except Exception:
            pass
        reason = REASON_SEARCH_NOT_PERFORMED
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": "search_web found no search performed: no web_search_call of type search",
            "next_action": next_action_for(reason),
        }
    parsed = getattr(result, "parsed", None) or {}
    # Citation provenance (Checkpoint B condition 1): a parsed source
    # is accepted only when its normalized URL appears in the
    # provider's own evidence (action_sources URLs plus citation
    # URLs). Invented URLs are dropped here and never reach the agent
    # or the session. Fail-closed shaping (review fix 1) still
    # applies: any minimizer failure yields no evidence at all.
    try:
        summary = _min_summary(str(parsed.get("summary", "") or ""), 1000)
        raw_sources = parsed.get("sources", []) if isinstance(parsed, dict) else []
        provider_keys: set[str] = set()
        provider_urls: list[str] = []
        citation_title_by_key: dict[str, str] = {}

        def _collect_evidence_url(raw: Any) -> None:
            if isinstance(raw, dict):
                found = str(raw.get("url", "") or "")
            else:
                found = str(getattr(raw, "url", "") or "")
            if not found:
                return
            key = _provenance_key(found)
            if key not in provider_keys:
                provider_keys.add(key)
                if len(provider_urls) < 10:
                    provider_urls.append(_min_url(found))

        for entry in list(getattr(result, "action_sources", None) or []):
            _collect_evidence_url(entry)
        for entry in list(getattr(result, "citations", None) or []):
            _collect_evidence_url(entry)
            if isinstance(entry, dict):
                url = str(entry.get("url", "") or "")
                title = str(entry.get("title", "") or "")
                if url and title:
                    citation_title_by_key.setdefault(_provenance_key(url), title)
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sources: list[dict[str, Any]] = []
        unverified_dropped = 0
        for item in raw_sources[:5]:
            if not isinstance(item, dict):
                continue
            url = _min_url(str(item.get("url", "") or ""))
            if not url:
                continue
            if _provenance_key(url) not in provider_keys:
                unverified_dropped += 1
                continue
            excerpt = _min_summary(str(item.get("excerpt_model", "") or ""), 500)
            title = _min_summary(str(item.get("title", "") or ""), 300)
            citation_title = _min_summary(
                str(citation_title_by_key.get(_provenance_key(url), "") or ""), 300
            )
            if citation_title:
                title = citation_title
            sources.append(
                {
                    "url": url,
                    "title": title,
                    "excerpt_model": excerpt,
                    "citation_title": citation_title,
                    "published_at": (
                        str(item.get("published_at", "") or "")[:100]
                        if item.get("published_at")
                        else None
                    ),
                    "retrieved_at": retrieved_at,
                    "classification": _classify(url, context),
                    "source_text": None,
                }
            )
    except _MinimizationFailed:
        try:
            store.append_event(
                session_id,
                "search_outcome",
                {"call_id": call_id, "outcome": "minimization_failed"},
            )
        except Exception:
            pass
        reason = REASON_TOOL_INTERNAL_ERROR
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": "search_web refused: result minimization failed; no evidence kept",
            "next_action": next_action_for(reason),
        }
    if not provider_keys:
        return _unverified_result(
            store,
            session_id,
            call_id,
            started,
            "no_provider_sources",
            "search_web unverified: provider returned no source evidence; no evidence kept",
        )
    if not sources:
        return _unverified_result(
            store,
            session_id,
            call_id,
            started,
            "no_verified_sources",
            f"search_web unverified: {unverified_dropped} parsed source(s) "
            "matched no provider evidence; no evidence kept",
        )
    try:
        logged_urls = [s["url"] for s in sources]
        call_ids = list(getattr(result, "web_search_call_ids", None) or [])
        store.append_event(
            session_id,
            "search_results_retrieved",
            {
                "call_id": call_id,
                "urls": logged_urls,
                "web_search_call_ids": call_ids[:5],
                "retrieved_at": retrieved_at,
                "provider_url_count": len(provider_keys),
                "unverified_dropped": unverified_dropped,
                "provider_urls": provider_urls,
            },
        )
        store.append_event(
            session_id,
            "evidence_evaluated",
            {
                "call_id": call_id,
                "evaluations": [
                    {"url": u, "classification": s["classification"], "decision": "kept"}
                    for u, s in zip(logged_urls, sources)
                ],
            },
        )
        store.append_event(session_id, "search_outcome", {"call_id": call_id, "outcome": "ok"})
        store.append_event(
            session_id,
            "search_operations",
            {
                "call_id": call_id,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "model": getattr(result, "model", ""),
                "input_tokens": getattr(result, "input_tokens", None),
                "output_tokens": getattr(result, "output_tokens", None),
                "estimate_status": "provisional",
            },
        )
    except Exception:
        reason = REASON_TOOL_UNAVAILABLE
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": "search_web unavailable: event write failed",
            "next_action": next_action_for(reason),
        }
    return {
        "ok": True,
        "summary": summary,
        "sources": sources,
        "cost_class": "network",
    }


def tool_definitions(timeout_s: float = 10.0) -> list[ToolDefinition]:
    """search_web definition: query only; session bound server-side."""
    return [
        ToolDefinition(
            name=SEARCH_TOOL_NAME,
            description=(
                "Search the web for the minimized query (permission-gated, "
                "at most 3 per session). Returns a bounded summary plus up "
                "to 5 verified sources: a parsed source is kept only when "
                "its URL matches the provider's own evidence "
                "(url_citation/action sources), with model-written excerpts "
                "(never verified quotations) and publisher-signal "
                "classifications."
            ),
            args_model=SearchWebArgs,
            timeout_s=timeout_s,
            idempotent=False,
            cost_class="network",
        )
    ]


__all__ = [
    "SEARCH_TOOL_NAME",
    "SearchWebArgs",
    "search_web_impl",
    "tool_definitions",
]
