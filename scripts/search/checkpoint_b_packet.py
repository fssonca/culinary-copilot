#!/usr/bin/env python3
"""Checkpoint B review packet (Phase 5, owner decision 2026-10-03).

Read-only and rerunnable: it SELECTs live sessions from the
application database, derives completeness / privacy / gap / spend
views, writes full exports under data/phase5-search/checkpoint-b/
(gitignored) and a human-readable evals/phase5_search/REVIEW.md
(committed, no model output quoted verbatim). It never writes to the
database (read-only transaction plus a SELECT-only guard), never
makes paid calls, and deletes nothing (purge output shown dry-run
only; the SQL procedure is printed, not run).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

SCOPE_DATE = "2026-10-03"
SESSION_PREFIXES = ("ses-live-live-search-", "ses-live-live-ask-res-")
PHASE3_DATE = "2026-09-30"
# Phase 3 live id stems (ses-live-<key[:12]>-a<attempt>-<rand>; verified
# against the 2026-09-30 application-database rows).
PHASE3_PREFIXES = (
    "ses-live-live-chicken-",
    "ses-live-live-yogurt-",
    "ses-live-live-direct-",
    "ses-live-live-empty-r-",
    "ses-live-live-epicure-",
    "ses-live-live-techniq-",
    "ses-live-live-roast-p-",
    "ses-live-live-vegetar-",
)

SEARCH_EVENT_TYPES = (
    "search_slot_claimed",
    "search_requested",
    "search_results_retrieved",
    "evidence_evaluated",
    "search_outcome",
    "search_operations",
)
# Required per dispatched search, in pipeline order, plus the
# search_web tool_call correlated by call_id.
REQUIRED_CHAIN = (
    "search_slot_claimed",
    "search_requested",
    "search_results_retrieved",
    "evidence_evaluated",
    "search_outcome",
    "search_operations",
    "tool_call",
)

SUMMARY_FILES = (
    "live-summary-p3l13-attempt1.json",
    "live-summary-p3l13-attempt2.json",
    "live-summary-phase5-step1.json",
    "live-summary-phase5-step2.json",
    "live-summary-phase5-step3.json",
    "live-summary-phase5-step4.json",
    "live-summary-phase5-step5.json",
)

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

# Final path segments that hold identifiers, not free text (call ids,
# corpus ids, tool names): excluded from the free-text inventory so the
# owner sees actual prose/argument fields.
_ID_FIELD_RE = re.compile(r"(call_id|question_id|tool|dataset_id|web_search_call_ids)(\[\d+\])?$")


# --- read-only database access -----------------------------------------------


def _guard_select(sql: str) -> None:
    """SELECT-only guard: the packet issues no writes, ever."""
    head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in {"SELECT", "WITH"}:
        raise ValueError(f"refusing non-read statement: {sql[:80]!r}")


@contextlib.contextmanager
def read_only_connection(engine: Any) -> Iterator[Any]:
    """A connection that cannot write (READ ONLY transaction)."""
    from sqlalchemy import text

    with engine.connect() as conn:
        ro = conn.execution_options(postgresql_readonly=True)
        ro.execute(text("SET TRANSACTION READ ONLY"))
        yield ro


def select_rows(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    from sqlalchemy import text

    _guard_select(sql)
    return list(conn.execute(text(sql), params or {}).mappings())


def discover_sessions(
    conn: Any, day: str, prefixes: tuple[str, ...] = SESSION_PREFIXES
) -> list[dict[str, Any]]:
    """Live sessions for one UTC date, ordered by creation."""
    likes = " OR ".join(f"id LIKE '{prefix}%'" for prefix in prefixes)
    rows = select_rows(
        conn,
        "SELECT id, created_at FROM sessions "
        f"WHERE ({likes}) AND created_at >= :start AND created_at < :end "
        "ORDER BY created_at",
        {"start": f"{day}T00:00:00Z", "end": _next_day(day)},
    )
    return [{"session_id": r["id"], "created_at": str(r["created_at"])} for r in rows]


def _next_day(day: str) -> str:
    from datetime import timedelta

    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")


def fetch_events(conn: Any, session_id: str) -> list[dict[str, Any]]:
    rows = select_rows(
        conn,
        "SELECT id, seq, event_type, payload, created_at FROM session_events "
        "WHERE session_id = :sid ORDER BY id",
        {"sid": session_id},
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {"_raw": payload}
        out.append(
            {
                "id": r["id"],
                "seq": r["seq"],
                "event_type": r["event_type"],
                "payload": payload if isinstance(payload, dict) else {"_value": payload},
                "created_at": str(r["created_at"]),
            }
        )
    return out


# --- preserved summaries and raw trails --------------------------------------


def load_preserved_summaries(summaries_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in SUMMARY_FILES:
        raw = json.loads((summaries_dir / name).read_text(encoding="utf-8"))
        scenarios = raw.get("scenarios") or []
        scenario = scenarios[0] if scenarios else {}
        grades = scenario.get("grades") or {}
        out.append(
            {
                "file": name,
                "run_utc": str(raw.get("generated_utc") or ""),
                "pool": str(raw.get("budget_pool") or ""),
                "scenario": str(scenario.get("key") or ""),
                "status": str(scenario.get("status") or ""),
                "stop_reason": str(scenario.get("stop_reason") or ""),
                "searches_dispatched": scenario.get("searches_dispatched"),
                "grades": {
                    k: grades.get(k)
                    for k in ("task_completion", "asked", "web_answer", "scenario_pass")
                    if k in grades
                },
                "spend_entries": list((raw.get("spend") or {}).get("entries") or []),
            }
        )
    return out


def load_raw_trail_sessions(raw_dir: Path) -> dict[str, str]:
    """session_id -> raw trail file (corroboration; trails are
    overwritten per scenario key, so only the last run survives)."""
    mapping: dict[str, str] = {}
    for path in sorted(raw_dir.glob("*.json")):
        if path.name == "spend-history.json":
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        for sid in sessions or []:
            if isinstance(sid, str):
                mapping[sid] = path.name
    return mapping


def map_sessions_to_runs(
    sessions: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    trail_sessions: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Order-based mapping: search sessions by creation time onto the
    phase5 step summaries by run time (same for the two ask-resume
    attempts). Verified against the raw-trail ids where present."""
    warnings: list[str] = []
    search = [s for s in sessions if "-live-search-" in s["session_id"]]
    askres = [s for s in sessions if "-live-ask-res-" in s["session_id"]]
    phase5 = [s for s in summaries if "phase5" in s["file"]]
    p3l13 = [s for s in summaries if "p3l13" in s["file"]]
    rows: list[dict[str, Any]] = []
    for group, group_sums, label in ((search, phase5, "phase5"), (askres, p3l13, "p3l13")):
        ordered = sorted(group, key=lambda s: s["created_at"])
        sums = sorted(group_sums, key=lambda s: s["run_utc"])
        if len(ordered) != len(sums):
            warnings.append(
                f"{label}: {len(ordered)} sessions but {len(sums)} summaries; "
                "mapping by order, verify timestamps"
            )
        for session, summary in zip(ordered, sums):
            try:
                delta = abs(
                    (
                        datetime.fromisoformat(session["created_at"])
                        - datetime.fromisoformat(summary["run_utc"])
                    ).total_seconds()
                )
            except ValueError:
                delta = -1.0
            if delta < 0 or delta > 1800:
                warnings.append(
                    f"{session['session_id']}: {delta:.0f}s from {summary['file']}; "
                    "mapping uncertain"
                )
            trail = trail_sessions.get(session["session_id"])
            if trail is None:
                warnings.append(
                    f"{session['session_id']}: id absent from data/phase3-live "
                    "(trail overwritten by a later run; mapped from DB + summary)"
                )
            rows.append({**session, **summary, "raw_trail": trail})
    rows.sort(key=lambda r: r["created_at"])
    return rows, warnings


# --- completeness ------------------------------------------------------------


def chain_rows(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One row per dispatched search chain (keyed by call_id) plus the
    refused claims (search_budget_exhausted, no provider call)."""
    by_call: dict[str, dict[str, Any]] = {}

    def _bucket(call_id: str) -> dict[str, Any]:
        return by_call.setdefault(call_id, {"call_id": call_id, "events": {}})

    for event in events:
        etype = str(event.get("event_type") or "")
        payload = event.get("payload") or {}
        if etype in SEARCH_EVENT_TYPES:
            call_id = str(payload.get("call_id") or "")
            if call_id:
                _bucket(call_id)["events"][etype] = payload
        elif etype == "tool_call" and str(payload.get("tool") or "") == "search_web":
            call_id = str(payload.get("call_id") or "")
            if call_id:
                _bucket(call_id)["events"]["tool_call"] = payload
    rows: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for call_id in sorted(by_call, key=lambda c: _first_seq(by_call[c], events)):
        present = by_call[call_id]["events"]
        tool = present.get("tool_call") or {}
        is_refused = str(tool.get("reason") or "") == "search_budget_exhausted"
        missing = [name for name in REQUIRED_CHAIN if name not in present]
        operations = present.get("search_operations") or {}
        slot = present.get("search_slot_claimed") or {}
        evaluations = (present.get("evidence_evaluated") or {}).get("evaluations") or []
        classes: dict[str, int] = {}
        for item in evaluations if isinstance(evaluations, list) else []:
            key = f"{item.get('classification')}/{item.get('decision')}"
            classes[key] = classes.get(key, 0) + 1
        retrieved = present.get("search_results_retrieved") or {}
        row = {
            "call_id": call_id,
            "dispatched": "search_slot_claimed" in present,
            "refused": is_refused,
            "present": sorted(present.keys()),
            "missing": missing,
            "latency_ms": operations.get("latency_ms"),
            "input_tokens": operations.get("input_tokens"),
            "output_tokens": operations.get("output_tokens"),
            "slots_used": slot.get("slots_used"),
            "slots_max": slot.get("slots_max"),
            "source_count": len(retrieved.get("urls") or []),
            "classifications": classes,
            "urls": list(retrieved.get("urls") or []),
            # Provenance audit (Checkpoint B 1d): None when the event
            # predates the check (the 7 live searches did not store
            # action_sources).
            "provider_url_count": retrieved.get("provider_url_count"),
            "unverified_dropped": retrieved.get("unverified_dropped"),
        }
        if is_refused:
            refused.append(row)
        else:
            rows.append(row)
    return rows, refused


def session_outcome_label(row: dict[str, Any], final_event_type: str) -> str:
    """Outcome label derived from the stop reason, corroborated by the
    final session event (Checkpoint B condition 6).

    A session that stopped with agent_needs_user_input asked the user
    and is never 'completed: answered'. A mismatch between the stop
    reason and the final event is surfaced, not silently resolved.
    """
    grades = row.get("grades") or {}
    stop = str(row.get("stop_reason") or "")
    if grades.get("web_answer") is True and stop == "agent_sufficient_evidence":
        label = (
            "web_answer accepted — one demonstrated discovery answer "
            "after fixes, not a reliability result"
        )
        expected = "agent_finished"
    elif stop == "agent_needs_user_input":
        label = "asked the user (awaiting input)"
        expected = "agent_question"
    elif stop == "agent_token_budget_exhausted":
        label = "stopped: token budget exhausted (no answer)"
        expected = ""
    elif stop == "agent_no_progress":
        label = "stopped: no progress (no answer)"
        expected = ""
    elif grades.get("asked"):
        label = "asked instead of answering"
        expected = ""
    else:
        label = str(row.get("status") or "")
        expected = ""
    if expected and final_event_type and final_event_type != expected:
        return f"{label} [final event {final_event_type} ≠ expected {expected}]"
    return label


def provenance_text(chain: dict[str, Any]) -> str:
    """Provenance audit rendering: 'not recorded (pre-fix)' when the
    event predates the citation check."""
    count = chain.get("provider_url_count")
    if count is None:
        return "provenance: not recorded (pre-fix)"
    return f"provenance: {count} provider URLs, {chain.get('unverified_dropped')} dropped"


def _first_seq(bucket: dict[str, Any], events: list[dict[str, Any]]) -> int:
    call_id = bucket["call_id"]
    for event in events:
        payload = event.get("payload") or {}
        if str(payload.get("call_id") or "") == call_id:
            seq = event.get("seq")
            return int(seq) if isinstance(seq, int) else 0
    return 0


def attach_costs(
    chains: list[dict[str, Any]],
    run_utc: str,
    history_searches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Match campaign-history search entries (same run_utc, search-N in
    dispatch order, plus *-correction entries such as the appended
    kept-ambiguous correction) onto chains in first-event order."""
    mine = [e for e in history_searches if str(e.get("run_utc") or "") == run_utc]
    base = [e for e in mine if not str(e.get("label") or "").endswith("-correction")]
    for index, chain in enumerate(chains):
        entry = base[index] if index < len(base) else None
        correction = next(
            (e for e in mine if str(e.get("label") or "") == f"search-{index + 1}-correction"),
            None,
        )
        if entry is not None:
            chain["cost_usd"] = entry.get("usd")
            chain["cost_decision"] = entry.get("decision")
        if correction is not None:
            chain["cost_usd"] = correction.get("usd")
            chain["cost_decision"] = correction.get("decision")
            chain["cost_note"] = f"{correction.get('label')}: kept-ambiguous after timeout"
    return chains


# --- privacy -----------------------------------------------------------------


def _detectors() -> list[tuple[str, Any]]:
    import culinary_copilot.search.minimize as minimizers

    return [
        ("email", minimizers._EMAIL_RE),
        ("phone", minimizers._PHONE_RE),
        ("street_address", minimizers._ADDRESS_RE),
        ("my_name", minimizers._MY_NAME_RE),
    ]


def walk_leaves(value: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from walk_leaves(value[key], f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_leaves(item, f"{path}[{index}]")


def privacy_scan(records: list[dict[str, str]]) -> dict[str, Any]:
    """records: {origin, field, text}. Heuristic detectors only (see
    limits in the report): counts per pattern/field, URL hygiene, and
    the free-text field inventory."""
    detectors = _detectors()
    hits: list[dict[str, Any]] = []
    urls: dict[str, list[str]] = {}
    free_text: dict[str, dict[str, Any]] = {}
    for record in records:
        origin, field, text = record["origin"], record["field"], record["text"]
        terminal = field.split(".")[-1]
        if len(text) > 20 and not _ID_FIELD_RE.fullmatch(terminal):
            info = free_text.setdefault(
                f"{origin} :: {field}",
                {"origin": origin, "field": field, "max_chars": 0, "samples": 0},
            )
            info["max_chars"] = max(int(info["max_chars"]), len(text))
            info["samples"] = int(info["samples"]) + 1
        for name, pattern in detectors:
            count = len(pattern.findall(text))
            if count:
                hits.append({"pattern": name, "origin": origin, "field": field, "count": count})
        for url in _URL_RE.findall(text):
            urls.setdefault(url, []).append(f"{origin} :: {field}")
    url_issues: list[dict[str, Any]] = []
    for url, where in sorted(urls.items()):
        try:
            from urllib.parse import urlsplit

            parts = urlsplit(url)
            if parts.query or parts.fragment:
                url_issues.append({"url": url, "issue": "query/fragment present", "in": where[:3]})
        except ValueError:
            url_issues.append({"url": url[:120], "issue": "unparseable", "in": where[:3]})
    raw_query_fields = sorted(
        {
            f"{origin} :: {field}"
            for origin, field, text in ((r["origin"], r["field"], r["text"]) for r in records)
            if re.search(r"query|args|content|text", field, re.IGNORECASE)
            and len(text) > 0
            and "minimized" not in field.lower()
            and not field.lower().endswith("digest")
        }
    )
    return {
        "hits": hits,
        "hit_total": sum(h["count"] for h in hits),
        "urls_checked": len(urls),
        "url_issues": url_issues,
        "raw_query_fields": raw_query_fields,
        "free_text_fields": sorted(free_text.values(), key=lambda f: str(f["field"])),
        "free_text_groups": privacy_groups(free_text),
    }


def _minimized_field(field: str) -> bool:
    """Whether a stored field passed a scrubber: minimized_query and
    tool_call.args (minimize_query / minimize_tool_args), stored URLs
    (minimize_url strips query strings and fragments)."""
    lowered = field.lower()
    if "minimized" in lowered:
        return True
    if lowered == "args":
        return True
    if lowered == "url" or lowered.endswith(".url") or "urls[" in lowered:
        return True
    return False


def privacy_groups(free_text: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Grouped free-text table: source, field count, max length,
    minimized or not. The per-field list stays in privacy.json."""
    groups: dict[tuple[str, bool | None], dict[str, Any]] = {}
    for item in free_text.values():
        origin = str(item["origin"])
        if origin.startswith("db:"):
            parts = origin.split(":")
            source = f"db event {parts[2]}" if len(parts) > 2 else origin
        elif origin.startswith("export:"):
            source = "packet exports"
        elif origin.startswith("file:"):
            source = f"raw file {origin[len('file:') :]}"
        else:
            source = origin
        minimized = _minimized_field(str(item["field"]))
        if source == "packet exports":
            # Minimized payloads plus raw trajectory text: neither yes nor no.
            minimized = None  # type: ignore[assignment]
        key = (source, minimized)
        group = groups.setdefault(
            key, {"source": source, "minimized": minimized, "fields": 0, "max_chars": 0}
        )
        group["fields"] = int(group["fields"]) + 1
        group["max_chars"] = max(int(group["max_chars"]), int(item["max_chars"]))
    return sorted(groups.values(), key=lambda g: (str(g["source"]), not bool(g["minimized"])))


def phone_hit_disposition(hits: list[dict[str, Any]]) -> str:
    """Disposition of the phone-pattern hits for this packet run."""
    targets = [h for h in hits if h.get("pattern") == "phone"]
    others = [h for h in hits if h.get("pattern") != "phone"]
    if not targets and not others:
        return "No detector hits at all."
    if (
        len(targets) == 1
        and int(targets[0].get("count", 0)) == 4
        and str(targets[0].get("origin", "")).endswith("live-technique-question.json")
        and not others
    ):
        return (
            "Disposition (verified 2026-10-03): all 4 phone-pattern matches sit in "
            "the technique-source attribution metadata of "
            "data/phase3-live/live-technique-question.json (whole-file scan). Each "
            "match was extracted with its surrounding context and all four are the "
            "same repeated 10-digit Wikipedia revision identifier, not a phone "
            "number. No email, address or name hits anywhere."
        )
    return "Hit set changed since verification: every hit above needs a fresh manual disposition."


def collect_privacy_records(
    sessions_events: dict[str, list[dict[str, Any]]],
    export_texts: list[dict[str, str]],
    raw_files: list[Path],
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for session_id, events in sessions_events.items():
        for event in events:
            for path, text in walk_leaves(event.get("payload"), ""):
                records.append(
                    {
                        "origin": f"db:{session_id}:{event.get('event_type')}",
                        "field": path or "(root)",
                        "text": text,
                    }
                )
    for item in export_texts:
        records.append(item)
    for raw_path in raw_files:
        try:
            content = raw_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        try:
            shown = str(raw_path.relative_to(REPO_ROOT))
        except ValueError:
            shown = str(raw_path)
        records.append({"origin": f"file:{shown}", "field": "(whole file)", "text": content})
    return records


# --- gaps --------------------------------------------------------------------


def run_gap_export(
    session_id: str,
    events: list[dict[str, Any]],
    minimized_query: str,
    urls: list[str],
    out_path: Path,
) -> list[dict[str, Any]]:
    events_path = out_path.parent / f"events-{session_id}.json"
    events_path.write_text(
        json.dumps(
            [{"event_type": e.get("event_type"), "payload": e.get("payload")} for e in events],
            indent=1,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "search" / "gap_export.py"),
        "--session-id",
        session_id,
        "--events-json",
        str(events_path),
        "--query",
        minimized_query,
        "--out",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gap_export failed for {session_id}: {proc.stderr[-500:]}")
    raw = json.loads(out_path.read_text(encoding="utf-8"))
    for candidate in raw if isinstance(raw, list) else []:
        if isinstance(candidate, dict):
            candidate.setdefault("provenance", {}).update(
                {"session_id": session_id, "urls": urls[:5]}
            )
    out_path.write_text(json.dumps(raw, indent=1, sort_keys=True), encoding="utf-8")
    return raw if isinstance(raw, list) else []


def session_query_and_urls(events: list[dict[str, Any]]) -> tuple[str, list[str]]:
    query = ""
    urls: list[str] = []
    for event in events:
        etype = str(event.get("event_type") or "")
        payload = event.get("payload") or {}
        if etype == "search_requested" and not query:
            query = str(payload.get("minimized_query") or "")
        if etype == "search_results_retrieved":
            for url in payload.get("urls") or []:
                if url not in urls:
                    urls.append(str(url))
    return query, urls


# --- spend -------------------------------------------------------------------


def compute_spend(phase5_path: Path, phase3_path: Path) -> dict[str, Any]:
    def _summarize(path: Path, pool_name: str) -> dict[str, Any]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        runs = raw.get("runs") or []
        total = 0.0
        reconciled = 0.0
        reservation = 0.0
        searches: list[dict[str, Any]] = []
        for run in runs:
            for entry in run.get("entries") or []:
                usd = float(entry.get("usd") or 0.0)
                total += usd
                if str(entry.get("decision") or "") == "reconciled":
                    reconciled += usd
                else:
                    reservation += usd
                if str(entry.get("kind") or "") == "search":
                    searches.append(
                        {
                            "run_utc": run.get("run_utc"),
                            "label": entry.get("label"),
                            "usd": entry.get("usd"),
                            "decision": entry.get("decision"),
                        }
                    )
        return {
            "pool": pool_name,
            "ceiling_usd": raw.get("ceiling_usd"),
            "total_usd": round(total, 6),
            "reconciled_usd": round(reconciled, 6),
            "reservation_usd": round(reservation, 6),
            "searches": searches,
        }

    phase5 = _summarize(phase5_path, "phase5")
    phase3 = _summarize(phase3_path, "phase3")
    return {
        "phase5": phase5,
        "phase3": phase3,
        "milestone3_total_usd": round(phase5["total_usd"] + phase3["total_usd"], 6),
    }


# --- report ------------------------------------------------------------------


def _check(value: bool) -> str:
    return "present" if value else "MISSING"


def write_review(
    path: Path,
    *,
    session_rows: list[dict[str, Any]],
    mapping_warnings: list[str],
    chains: dict[str, list[dict[str, Any]]],
    refused: dict[str, list[dict[str, Any]]],
    final_events: dict[str, str],
    privacy: dict[str, Any],
    gaps_phase5: list[dict[str, Any]],
    gaps_phase3: list[dict[str, Any]],
    spend: dict[str, Any],
    purge_output: str,
    purge_sql: str,
    generated_utc: str,
) -> None:
    lines: list[str] = []
    add = lines.append
    add("# Checkpoint B review packet (Phase 5 search)")
    add("")
    add(
        f"Generated {generated_utc} by scripts/search/checkpoint_b_packet.py "
        "(read-only, rerunnable). Full exports under "
        "data/phase5-search/checkpoint-b/ (gitignored). No model output "
        "is quoted verbatim below: answer text, titles, excerpts and "
        "summaries are paraphrased or counted; URLs are operational "
        "facts from the logs."
    )
    add("")
    add("## 1. Sessions in scope")
    add("")
    add(
        "Every live session from the Phase 5 runs and the P3-L-13 runs "
        f"on {SCOPE_DATE}. Session ids come from the application "
        "database (authoritative: raw trails under data/phase3-live/ "
        "are overwritten per scenario key, so only the last run "
        "survives there) corroborated against the raw-trail ids and "
        "mapped onto the preserved summaries by timestamp order."
    )
    add("")
    add("| Session (suffix) | Run | Scenario | Stop | Outcome |")
    add("|---|---|---|---|---|")
    for row in session_rows:
        outcome = session_outcome_label(row, final_events.get(row["session_id"], ""))
        add(
            f"| {row['session_id'][-6:]} | {row['file']} | {row['scenario']} "
            f"| {row['stop_reason']} | {outcome} |"
        )
    add("")
    if mapping_warnings:
        add("Mapping notes:")
        for warning in mapping_warnings:
            add(f"- {warning}")
        add("")
    add("## 2. Search log completeness")
    add("")
    add(
        "Required per dispatched search: search_slot_claimed, "
        "search_requested, search_results_retrieved, evidence_evaluated, "
        "search_outcome, search_operations, and the search_web tool_call "
        "(correlated by call_id). Costs are reconciled ledger figures "
        "from the spend histories, including the appended correction. "
        "The 7 live searches predate the citation provenance check and "
        "did not store action_sources, so their URL provenance cannot "
        "be verified after the fact (this does not mean the live links "
        "were invented); their audit fields show 'not recorded (pre-fix)'."
    )
    add("")
    for row in session_rows:
        sid = row["session_id"]
        add(f"### {sid[-6:]} ({row['file']}, {row['scenario']})")
        for chain in chains.get(sid, []):
            flags = ", ".join(
                f"{name}: {_check(name in chain['present'])}" for name in REQUIRED_CHAIN
            )
            cost = chain.get("cost_usd")
            cost_text = (
                f"${cost:.5f} ({chain.get('cost_decision')})"
                if isinstance(cost, (int, float))
                else "no ledger entry"
            )
            note = f" NOTE: {chain['cost_note']}." if chain.get("cost_note") else ""
            add(f"- call {chain['call_id'][:12]}: {flags}.")
            add(
                f"  latency {chain.get('latency_ms')} ms; tokens "
                f"in/out {chain.get('input_tokens')}/{chain.get('output_tokens')}; "
                f"cost {cost_text}; slots {chain.get('slots_used')}/{chain.get('slots_max')}; "
                f"sources {chain.get('source_count')} {chain.get('classifications')}; "
                f"{provenance_text(chain)}.{note}"
            )
            if chain.get("missing"):
                add(f"  MISSING EVENTS: {', '.join(chain['missing'])}.")
        for chain in refused.get(sid, []):
            add(
                f"- call {chain['call_id'][:12]}: REFUSED search_budget_exhausted "
                "(tool_call only, no slot, no provider call)."
            )
        if not chains.get(sid) and not refused.get(sid):
            add("- no searches dispatched in this session.")
        add("")
    add(
        "Completeness findings: the timed-out search (step 3) holds only "
        "slot + request + tool_call — results, evaluation, outcome and "
        "operations were never recorded because the tool-level timeout "
        "fired outside the implementation. That gap is historical: step 3 "
        "ran under the old 10 s tool / 10 s provider timeouts, and it "
        "stays shown as is. Under the new defaults the provider's own "
        "20 s timeout fires before the 30 s tool timeout and is logged "
        "as outcome 'error'; search_web_impl now also records outcome "
        "'cancelled' when tool-level cancellation interrupts the "
        "provider call. Every other dispatched search has the full "
        "chain; the three refusals (one step-3 retry after the timeout, "
        "two step-4) are tool_call-only by design."
    )
    add("")
    add("## 3. Privacy check (heuristics)")
    add("")
    add(
        'Method: the email / phone / street-address / "my <Name>" '
        "patterns from search/minimize.py run as detectors over every "
        "stored payload for these sessions (event payloads incl. "
        "recorded tool args), the packet exports, and the raw files "
        "under data/phase3-live/ and data/phase5-live/. Limits, stated "
        "honestly: regexes miss paraphrases, non-English text and novel "
        "formats; a zero count is not a guarantee. Queries were "
        "synthetic, so zero PII hits are expected."
    )
    add("")
    add(
        f"- detector hits: {privacy['hit_total']} across {len(privacy['hits'])} field report(s). "
        "Any hit needs a manual disposition: the detectors also match "
        "digit strings such as revision ids, so a count alone is not a PII verdict."
    )
    for hit in privacy["hits"]:
        add(f"  - {hit['pattern']}: {hit['count']} in {hit['origin']} :: {hit['field']}")
    add(f"- {phone_hit_disposition(privacy['hits'])}")
    add(
        f"- URLs checked: {privacy['urls_checked']}; "
        f"query/fragment issues: {len(privacy['url_issues'])}."
    )
    for issue in privacy["url_issues"]:
        add(f"  - {issue['url']}: {issue['issue']}")
    add(
        "- recorded args are minimized too: tool_call.args is stored only "
        "because the live runner sets record_tool_args=True (the "
        "ToolContext default is False, digest only). Every live "
        "search_web tool_call row carries args as minimize_tool_args "
        "JSON — the same scrubber as minimized_query — verified from "
        "the stored rows. The default application path stores "
        "args_digest (truncated sha256 hex) only, never free text."
    )
    add("- stored free text that is NOT minimized (owner judges risk):")
    add("  - user_message text; agent_question question_text, options and note;")
    add("  - agent_finished note; agent_validation_reject errors;")
    add("  - result_facts titles in search_web tool_call events;")
    add("  - the raw trails under data/phase3-live/ (whole files).")
    add(
        "- retention coverage after the fix: the purge procedure deletes "
        "expired search_* events and search_web tool_call events "
        "(minimized args, urls, result_facts titles included). Everything "
        "above the procedure line persists as session data: user_message, "
        "agent_question, agent_finished, agent_validation_reject, other "
        "agent and non-search tool events, and the raw-trail files "
        "(owner file-purge only)."
    )
    add("- free-text inventory, grouped (full per-field list in privacy.json):")
    add("  | source | fields | max chars | minimized |")
    add("  |---|---|---|---|")
    for group in privacy["free_text_groups"]:
        flag = {True: "yes", False: "no", None: "mixed"}[group["minimized"]]
        add(f"  | {group['source']} | {group['fields']} | {group['max_chars']} | {flag} |")
    add("")
    add("## 4. Gap candidates (unreviewed)")
    add("")
    add(
        "Derived read-only via scripts/search/gap_export.py over the "
        "exported events (nothing imported; review_status unreviewed; "
        "triggers are candidates, never causes)."
    )
    add("")
    for title, candidates in (
        ("Phase 5 + P3-L-13 (2026-10-03)", gaps_phase5),
        (f"Phase 3 live ({PHASE3_DATE})", gaps_phase3),
    ):
        add(f"### {title}: {len(candidates)} candidate(s)")
        by_cat: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            by_cat.setdefault(str(candidate.get("proposed_category")), []).append(candidate)
        if not by_cat:
            add("- none.")
        for category in sorted(by_cat):
            items = by_cat[category]
            signal = str(items[0].get("observed_signal") or "")
            sessions = sorted(
                {str((c.get("provenance") or {}).get("session_id", ""))[-6:] for c in items}
            )
            add(f"- {category}: {len(items)} (sessions {', '.join(sessions)}).")
            add(f"  signal: {signal}.")
            add(f"  alternative: {items[0].get('alternative')}.")
            add(f"  usefulness: {usefulness_note(category)}")
        add("")
    add("## 5. Retention and access")
    add("")
    add(
        "- stored where: session rows + session_events in the "
        "application database (append-only); raw trails "
        "data/phase3-live/*.json; spend histories "
        "data/phase5-live/spend-history.json and "
        "data/phase3-live/spend-history.json; this packet's exports "
        "under data/phase5-search/checkpoint-b/ (all gitignored)."
    )
    add(
        "- expiry: 90 days for search events, 30 days for process logs "
        "(Settings SEARCH_EVENT_RETENTION_DAYS / "
        "PROCESS_LOG_RETENTION_DAYS)."
    )
    add(
        "- purge procedure (fixed for this packet, still NOT run): one "
        "transaction that DISABLEs the append-only trigger, DELETEs "
        "expired rows, re-ENABLEs the trigger and COMMITs — ALTER TABLE "
        "is transactional, so any failure rolls back with the trigger "
        "still enabled (brief ACCESS EXCLUSIVE lock, table-owner "
        "privilege, tested on a disposable DB). Scope: the six search_* "
        "event types plus search_web tool_call events (recorded args, "
        "hosts, titles). user_message, agent_question and all other "
        "agent events persist as session data. Backup first, 90-day "
        "default (single value in the SQL file)."
    )
    add(
        "- purge commands below are shown dry-run only; nothing was "
        "deleted. The SQL procedure was NOT run."
    )
    add("```")
    add(purge_output.strip())
    add("```")
    add("purge_search_events.sql (procedure, not run):")
    add("```sql")
    add(purge_sql.strip())
    add("```")
    add(
        "- readers: the owner only, through review exports that stay out "
        "of git. Nothing is stored for real users; the search toggle "
        "stays off by default."
    )
    add("")
    add("## 6. Spend (recomputed from the history files)")
    add("")
    phase5 = spend["phase5"]
    phase3 = spend["phase3"]
    n_entries = len(phase5["searches"])
    n_corrections = sum(1 for s in phase5["searches"] if str(s["label"]).endswith("-correction"))
    search_reconciled = sum(
        float(s["usd"] or 0.0) for s in phase5["searches"] if s["decision"] == "reconciled"
    )
    add(
        f"- Phase 5 pool, budget accounting (not invoiced spend): reconciled "
        f"${phase5['reconciled_usd']:.5f} (searches ${search_reconciled:.5f} + "
        f"model/embedding ${phase5['reconciled_usd'] - search_reconciled:.5f}) + "
        f"timeout reservation ${phase5['reservation_usd']:.5f} = "
        f"${phase5['total_usd']:.4f} of ${phase5['ceiling_usd']} "
        f"({n_entries - n_corrections} dispatched searches, {n_entries} ledger entries)."
    )
    for search in phase5["searches"]:
        label = str(search["label"])
        extra = ""
        if search["decision"] == "reserved" and any(
            str(s["label"]) == f"{label}-correction" for s in phase5["searches"]
        ):
            extra = " (superseded by the appended correction)"
        add(
            f"  - {search['run_utc']} {label}: "
            f"${float(search['usd'] or 0.0):.5f} ({search['decision']}){extra}"
        )
    add(f"- Phase 3 pool: ${phase3['total_usd']:.4f} of ${phase3['ceiling_usd']}.")
    add(f"- Milestone 3 total: ${spend['milestone3_total_usd']:.4f}.")
    add("")
    add("## 7. Checkpoint B questions for the owner (unanswered)")
    add("")
    add(
        "Owner decisions: docs/phase5-owner-decisions.md, section "
        '"Checkpoint B: owner decisions (2026-10-03)". The questions '
        "below restate the packet context for the owner; they are not "
        "answered here."
    )
    add("")
    add("")
    for title, context, recommendation in checkpoint_questions(spend):
        add(f"{title}")
        add(f"   context: {context}")
        add(f"   recommendation: {recommendation}")
        add("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def usefulness_note(category: str) -> str:
    notes = {
        "missing_recipe": (
            "shows which dishes users ask for but the corpus lacks; "
            "needs review since weak retrieval mimics a gap."
        ),
        "missing_ingredient_alias": (
            "shows vocabulary users employ that Epicure rejects; "
            "needs review since coverage gaps mimic missing aliases."
        ),
        "retrieval_miss": (
            "shows queries the corpus could answer but retrieval "
            "missed; useful for retrieval tuning, not corpus growth."
        ),
        "missing_technique": (
            "shows technique questions the corpus cannot support; "
            "needs review since weak technique retrieval mimics a gap."
        ),
        "unreliable_metadata": (
            "flags invented quantities against source metadata; "
            "needs review since generation errors mimic bad metadata."
        ),
    }
    return notes.get(category, "owner judges usefulness after review.")


def checkpoint_questions(spend: dict[str, Any]) -> list[tuple[str, str, str]]:
    phase5 = spend["phase5"]
    return [
        (
            "1. Approve the provider (OpenAI hosted web_search via the search_web wrapper)",
            "6 of 7 searches reconciled at about $0.011 against the $0.025 "
            "estimate; one timed out and is kept-ambiguous at $0.025. The "
            "wrapper enforces permission, slot limits, minimization and "
            "the 30 s tool / 20 s provider timeouts; the agent sees only "
            "the checked summary plus up to 5 sources. Provenance caveat: "
            "the 7 live searches predate the citation check and did not "
            "store action_sources, so their URL provenance cannot be "
            "verified after the fact.",
            "approve the wrapper as the Phase 5 provider path.",
        ),
        (
            "2. The per-session limit (3)",
            "step 2 ran three searches although the owner had authorized 2 "
            "for that step: the limit flags were checked only in preflight, "
            "not enforced in-run (recorded in docs/phase5-owner-decisions.md). "
            "Fixed by SearchRunLimits: steps 3-5 ran one slot each with "
            "refusals working (one step-3 retry and two step-4 attempts "
            "refused with search_budget_exhausted).",
            "keep 3 in code with 1-2 for live checks.",
        ),
        (
            "3. The Phase 5 ceiling as finally used ($0.13)",
            f"recomputed pool ${phase5['total_usd']:.4f} of "
            f"${phase5['ceiling_usd']}; the campaign is complete at 7 of "
            "7 searches with no further searches planned.",
            "accept $0.13 as the final Phase 5 ceiling.",
        ),
        (
            "4. The logs: useful and privacy-acceptable?",
            "every dispatched search is traceable call by call "
            "(slot, request, results, evaluation, outcome, operations); "
            "the timed-out search left no outcome/operations events. "
            "Privacy scan over stored payloads, exports and raw files is "
            "reported above; all queries were synthetic and the "
            "detectors are narrow heuristics.",
            "accept the logs as useful; accept privacy for development "
            "data and re-review before any real-user storage.",
        ),
        (
            "5. The gap queue: useful? Keep events plus export, or plan a table later?",
            "candidates are derived views with provenance and "
            "alternatives, all unreviewed; nothing is imported "
            "automatically.",
            "keep events plus export; revisit a table only if Phase 6/7 needs a review interface.",
        ),
        (
            "6. Anything to change before Phase 6 (the minimal UI)?",
            "under the new defaults the provider's own 20 s timeout fires "
            "before the 30 s tool timeout and is logged as outcome 'error'; "
            "search_web_impl now also records outcome 'cancelled' when "
            "tool-level cancellation interrupts the provider call. The "
            "step-3 gap is historical under the old 10 s/10 s settings.",
            "no changes before Phase 6.",
        ),
    ]


# --- main --------------------------------------------------------------------


def build_packet(
    *,
    engine: Any,
    summaries_dir: Path,
    raw_dir: Path,
    phase5_history: Path,
    phase3_history: Path,
    out_dir: Path,
    day: str = SCOPE_DATE,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with read_only_connection(engine) as conn:
        sessions = discover_sessions(conn, day)
        in_scope = [s for s in sessions if s["session_id"].startswith(SESSION_PREFIXES)]
        sessions_events = {s["session_id"]: fetch_events(conn, s["session_id"]) for s in in_scope}
        phase3_sessions = discover_sessions(conn, PHASE3_DATE, PHASE3_PREFIXES)
        phase3_events = {
            s["session_id"]: fetch_events(conn, s["session_id"]) for s in phase3_sessions
        }
    summaries = load_preserved_summaries(summaries_dir)
    trail_sessions = load_raw_trail_sessions(raw_dir)
    session_rows, mapping_warnings = map_sessions_to_runs(in_scope, summaries, trail_sessions)
    spend = compute_spend(phase5_history, phase3_history)
    history_searches = list(spend["phase5"]["searches"])

    chains: dict[str, list[dict[str, Any]]] = {}
    refused: dict[str, list[dict[str, Any]]] = {}
    for row in session_rows:
        rows, ref = chain_rows(sessions_events[row["session_id"]])
        chains[row["session_id"]] = attach_costs(
            rows, str(row.get("run_utc") or ""), history_searches
        )
        refused[row["session_id"]] = ref

    (out_dir / "sessions.json").write_text(
        json.dumps(session_rows, indent=1, sort_keys=True, default=str), encoding="utf-8"
    )
    completeness = {sid: {"chains": chains[sid], "refused": refused[sid]} for sid in chains}
    (out_dir / "completeness.json").write_text(
        json.dumps(completeness, indent=1, sort_keys=True, default=str), encoding="utf-8"
    )

    gaps_phase5: list[dict[str, Any]] = []
    for row in session_rows:
        sid = row["session_id"]
        query, urls = session_query_and_urls(sessions_events[sid])
        gaps_phase5.extend(
            run_gap_export(sid, sessions_events[sid], query, urls, out_dir / f"gaps-{sid}.json")
        )
    (out_dir / "gaps-phase5.json").write_text(
        json.dumps(gaps_phase5, indent=1, sort_keys=True), encoding="utf-8"
    )
    gaps_phase3: list[dict[str, Any]] = []
    for sid, events in sorted(phase3_events.items()):
        query, urls = session_query_and_urls(events)
        gaps_phase3.extend(
            run_gap_export(sid, events, query, urls, out_dir / f"gaps-phase3-{sid[-6:]}.json")
        )
    (out_dir / "gaps-phase3-0930.json").write_text(
        json.dumps(gaps_phase3, indent=1, sort_keys=True), encoding="utf-8"
    )

    export_texts = [
        {
            "origin": f"export:{p.name}",
            "field": "(whole file)",
            "text": p.read_text(encoding="utf-8"),
        }
        for p in sorted(out_dir.glob("*.json"))
    ]
    raw_files = sorted(raw_dir.glob("*.json")) + [phase5_history, phase3_history]
    records = collect_privacy_records(sessions_events, export_texts, raw_files)
    privacy = privacy_scan(records)
    (out_dir / "privacy.json").write_text(
        json.dumps(privacy, indent=1, sort_keys=True, default=str), encoding="utf-8"
    )

    (out_dir / "spend.json").write_text(
        json.dumps(spend, indent=1, sort_keys=True), encoding="utf-8"
    )

    purge_output = run_purge_dry_run(out_dir)
    (out_dir / "purge-dry-run.txt").write_text(purge_output, encoding="utf-8")
    purge_sql = (REPO_ROOT / "scripts" / "search" / "purge_search_events.sql").read_text(
        encoding="utf-8"
    )

    return {
        "session_rows": session_rows,
        "mapping_warnings": mapping_warnings,
        "chains": chains,
        "refused": refused,
        "final_events": {
            sid: (events[-1].get("event_type", "") if events else "")
            for sid, events in sessions_events.items()
        },
        "privacy": privacy,
        "gaps_phase5": gaps_phase5,
        "gaps_phase3": gaps_phase3,
        "spend": spend,
        "purge_output": purge_output,
        "purge_sql": purge_sql,
    }


def run_purge_dry_run(out_dir: Path) -> str:
    try:
        relative = out_dir.relative_to(REPO_ROOT)
    except ValueError:
        relative = out_dir
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "search" / "purge_search.py"),
            "--raw-dir",
            str(relative),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the checkpoint B review packet (read-only)."
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--day", default=SCOPE_DATE)
    parser.add_argument("--summaries-dir", default=str(REPO_ROOT / "evals" / "phase3_agent"))
    parser.add_argument("--raw-dir", default=str(REPO_ROOT / "data" / "phase3-live"))
    parser.add_argument(
        "--phase5-history", default=str(REPO_ROOT / "data" / "phase5-live" / "spend-history.json")
    )
    parser.add_argument(
        "--phase3-history", default=str(REPO_ROOT / "data" / "phase3-live" / "spend-history.json")
    )
    parser.add_argument(
        "--out-dir", default=str(REPO_ROOT / "data" / "phase5-search" / "checkpoint-b")
    )
    parser.add_argument(
        "--review-out", default=str(REPO_ROOT / "evals" / "phase5_search" / "REVIEW.md")
    )
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine

    from culinary_copilot.config import Settings

    url = args.database_url
    if not url:
        url = Settings().database_url.get_secret_value()
    engine = create_engine(url)
    generated_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    packet = build_packet(
        engine=engine,
        summaries_dir=Path(args.summaries_dir),
        raw_dir=Path(args.raw_dir),
        phase5_history=Path(args.phase5_history),
        phase3_history=Path(args.phase3_history),
        out_dir=Path(args.out_dir),
        day=args.day,
    )
    write_review(
        Path(args.review_out),
        session_rows=packet["session_rows"],
        mapping_warnings=packet["mapping_warnings"],
        chains=packet["chains"],
        refused=packet["refused"],
        final_events=packet["final_events"],
        privacy=packet["privacy"],
        gaps_phase5=packet["gaps_phase5"],
        gaps_phase3=packet["gaps_phase3"],
        spend=packet["spend"],
        purge_output=packet["purge_output"],
        purge_sql=packet["purge_sql"],
        generated_utc=generated_utc,
    )
    engine.dispose()
    n_chains = sum(len(v) for v in packet["chains"].values())
    n_refused = sum(len(v) for v in packet["refused"].values())
    print(f"sessions: {len(packet['session_rows'])}")
    print(f"dispatched searches: {n_chains}; refused claims: {n_refused}")
    print(
        f"privacy hits: {packet['privacy']['hit_total']}; "
        f"urls checked: {packet['privacy']['urls_checked']}"
    )
    print(f"gaps: phase5 {len(packet['gaps_phase5'])}, phase3-0930 {len(packet['gaps_phase3'])}")
    print(f"spend: {packet['spend']}")
    print(f"review: {args.review_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
