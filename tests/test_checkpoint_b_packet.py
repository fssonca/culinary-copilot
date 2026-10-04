"""Checkpoint B packet tests (disposable DB, fake sessions only).

No paid calls, no live searches, no application-database writes. The
packet script itself is read-only; these tests prove it on a
disposable database.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_SPEC = _ilu.spec_from_file_location("checkpoint_b_packet", "scripts/search/checkpoint_b_packet.py")
assert _SPEC is not None and _SPEC.loader is not None
_packet = _ilu.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_packet)


def _pg_urls() -> tuple[str, str]:
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/culinary_test_checkpoint_b"


def _make_db(db_name: str) -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError

    from culinary_copilot.recipes import import_data

    maint_url, _ = _pg_urls()
    head = maint_url.rsplit("/", 1)[0]
    test_url = f"{head}/{db_name}"
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            from sqlalchemy import text as _text

            conn.execute(_text(f'DROP DATABASE IF EXISTS "{db_name}"'))
            conn.execute(_text(f'CREATE DATABASE "{db_name}"'))
        maint.dispose()
        engine = create_engine(test_url)
        with engine.begin() as conn:
            import_data.apply_migrations(conn)
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for packet tests: {exc!r}")
    return engine


def _drop_db(db_name: str, engine: Any) -> None:
    from sqlalchemy import create_engine

    try:
        engine.dispose()
    except Exception:
        pass
    try:
        maint_url, _ = _pg_urls()
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            from sqlalchemy import text as _text

            conn.execute(_text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        maint.dispose()
    except Exception:
        pass


def _seed_session(store: Any, session_id: str, full: bool) -> None:
    from culinary_copilot.domain.sessions import SessionState

    store.create(SessionState(id=session_id, internet_search_allowed=True))
    call_id = f"call-{session_id[-6:]}"
    store.append_event(
        session_id,
        "search_slot_claimed",
        {"call_id": call_id, "slots_used": 1, "slots_max": 3},
    )
    store.append_event(
        session_id,
        "search_requested",
        {"call_id": call_id, "permission": True, "minimized_query": "fake dish query"},
    )
    store.append_event(
        session_id,
        "tool_call",
        {
            "call_id": call_id,
            "tool": "search_web",
            "outcome": "ok",
            "args": '{"query": "fake dish query"}',
            "latency_ms": 4100.0,
        },
    )
    if full:
        store.append_event(
            session_id,
            "search_results_retrieved",
            {
                "call_id": call_id,
                "urls": ["https://example.com/fake"],
                "retrieved_at": "2026-10-03T00:00:00Z",
            },
        )
        store.append_event(
            session_id,
            "evidence_evaluated",
            {
                "call_id": call_id,
                "evaluations": [
                    {
                        "url": "https://example.com/fake",
                        "decision": "kept",
                        "classification": "unclassified",
                    }
                ],
            },
        )
        store.append_event(
            session_id,
            "search_outcome",
            {"call_id": call_id, "outcome": "ok"},
        )
        store.append_event(
            session_id,
            "search_operations",
            {"call_id": call_id, "latency_ms": 4100.0, "input_tokens": 100, "output_tokens": 50},
        )


def _write_summary(path: Path, name: str, pool: str, utc: str) -> None:
    body = {
        "generated_utc": utc,
        "budget_pool": pool,
        "scenarios": [
            {
                "key": "live-search-missing-dish",
                "status": "completed: answered",
                "stop_reason": "agent_sufficient_evidence",
                "searches_dispatched": 1,
                "grades": {"task_completion": True, "web_answer": True},
            }
        ],
        "spend": {
            "entries": [
                {
                    "label": "search-1",
                    "kind": "search",
                    "decision": "reconciled",
                    "used_usd": 0.011,
                    "usd": 0.011,
                },
            ]
        },
    }
    (path / name).write_text(json.dumps(body), encoding="utf-8")


def _fixture_dirs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    for name in _packet.SUMMARY_FILES:
        pool = "phase3" if "p3l13" in name else "phase5"
        _write_summary(summaries, name, pool, utc)
    raw = tmp_path / "raw"
    raw.mkdir()
    for history, ceiling in (("phase5-history.json", 0.13), ("phase3-history.json", 0.15)):
        (tmp_path / history).write_text(
            json.dumps({"runs": [], "ceiling_usd": ceiling}), encoding="utf-8"
        )
    return summaries, raw, tmp_path / "phase5-history.json", tmp_path / "phase3-history.json"


def test_completeness_detects_missing_events() -> None:
    """A chain with slot + request but no results/evaluation/outcome/
    operations is flagged per missing event (the timeout shape)."""
    db_name = "culinary_test_checkpoint_b1"
    engine = _make_db(db_name)
    try:
        from culinary_copilot.services.session_store import PostgresSessionStore

        store = PostgresSessionStore(engine)
        sid = "ses-live-live-search-fake01"
        _seed_session(store, sid, full=False)
        refused_id = "call-refused-1"
        store.append_event(
            sid,
            "tool_call",
            {
                "call_id": refused_id,
                "tool": "search_web",
                "outcome": "error",
                "reason": "search_budget_exhausted",
                "args": '{"query": "second attempt"}',
            },
        )
        with _packet.read_only_connection(engine) as conn:
            events = _packet.fetch_events(conn, sid)
        chains, refused = _packet.chain_rows(events)
        assert len(chains) == 1
        assert chains[0]["missing"] == [
            "search_results_retrieved",
            "evidence_evaluated",
            "search_outcome",
            "search_operations",
        ]
        assert len(refused) == 1
        assert refused[0]["call_id"] == refused_id
    finally:
        _drop_db(db_name, engine)


def test_privacy_detector_finds_planted_email_and_phone() -> None:
    records = [
        {
            "origin": "db:ses-x:tool_call",
            "field": "args",
            "text": '{"query": "contact chef@example.com about dinner"}',
        },
        {
            "origin": "db:ses-x:user_message",
            "field": "content",
            "text": "call me at 415-555-0132 please",
        },
        {
            "origin": "db:ses-x:search_requested",
            "field": "minimized_query",
            "text": "fake dish query",
        },
    ]
    report = _packet.privacy_scan(records)
    by_pattern = {h["pattern"] for h in report["hits"]}
    assert "email" in by_pattern
    assert "phone" in by_pattern
    assert report["hit_total"] >= 2


def test_privacy_groups_and_digest_exclusion() -> None:
    """args_digest (truncated sha256) never appears in free-text lists;
    minimized fields (args, minimized_query, urls) group as minimized;
    the phone-hit disposition fires only for the verified hit set."""
    records = [
        {"origin": "db:ses-x:tool_call", "field": "args_digest", "text": "a1b2c3d4e5f60718"},
        {
            "origin": "db:ses-x:tool_call",
            "field": "args",
            "text": '{"query": "fake dish query with enough length"}',
        },
        {
            "origin": "db:ses-x:user_message",
            "field": "text",
            "text": "a user message with enough length here",
        },
    ]
    report = _packet.privacy_scan(records)
    assert report["raw_query_fields"] == [
        "db:ses-x:tool_call :: args",
        "db:ses-x:user_message :: text",
    ]
    assert all("digest" not in f for f in report["raw_query_fields"])
    groups = {(g["source"], g["minimized"]): g for g in report["free_text_groups"]}
    assert groups[("db event tool_call", True)]["fields"] == 1
    assert groups[("db event user_message", False)]["fields"] == 1
    assert "verified 2026-10-03" in _packet.phone_hit_disposition(
        [
            {
                "pattern": "phone",
                "origin": "file:data/phase3-live/live-technique-question.json",
                "field": "(whole file)",
                "count": 4,
            }
        ]
    )
    assert "fresh manual disposition" in _packet.phone_hit_disposition(
        [{"pattern": "phone", "origin": "file:other.json", "field": "f", "count": 1}]
    )


def test_packet_never_writes_to_db() -> None:
    """The full packet run issues no INSERT/UPDATE/DELETE and leaves
    every row count unchanged (read-only transaction + SELECT guard)."""
    from sqlalchemy import event, text

    db_name = "culinary_test_checkpoint_b2"
    engine = _make_db(db_name)
    try:
        from culinary_copilot.services.session_store import PostgresSessionStore

        store = PostgresSessionStore(engine)
        _seed_session(store, "ses-live-live-search-fake02", full=True)
        _seed_session(store, "ses-live-live-ask-res-fake02", full=False)

        def _counts() -> dict[str, int]:
            with engine.connect() as conn:
                out = {}
                for table in ("sessions", "session_events"):
                    out[table] = int(
                        conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0
                    )
                return out

        before = _counts()
        seen: list[str] = []
        event.listen(
            engine,
            "before_cursor_execute",
            lambda conn, cursor, statement, params, context, executemany: seen.append(statement),
        )
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with _packet.read_only_connection(engine) as conn:
            sessions = _packet.discover_sessions(conn, day)
            assert {s["session_id"] for s in sessions} == {
                "ses-live-live-search-fake02",
                "ses-live-live-ask-res-fake02",
            }
            for sid in ("ses-live-live-search-fake02", "ses-live-live-ask-res-fake02"):
                _packet.fetch_events(conn, sid)
        writes = [
            s
            for s in seen
            if re.match(r"\s*(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE)\b", s, re.IGNORECASE)
        ]
        assert writes == []
        assert _counts() == before
    finally:
        _drop_db(db_name, engine)


def test_packet_is_rerunnable_and_deterministic(tmp_path: Path) -> None:
    """Two runs over the same disposable DB produce byte-identical exports."""
    db_name = "culinary_test_checkpoint_b3"
    engine = _make_db(db_name)
    try:
        from culinary_copilot.services.session_store import PostgresSessionStore

        store = PostgresSessionStore(engine)
        _seed_session(store, "ses-live-live-search-fake03", full=True)
        summaries, raw, h5, h3 = _fixture_dirs(tmp_path)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        kwargs: dict[str, Any] = {
            "engine": engine,
            "summaries_dir": summaries,
            "raw_dir": raw,
            "phase5_history": h5,
            "phase3_history": h3,
            "day": day,
        }
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        _packet.build_packet(out_dir=out1, **kwargs)
        _packet.build_packet(out_dir=out2, **kwargs)
        files1 = sorted(p.name for p in out1.glob("*.json"))
        files2 = sorted(p.name for p in out2.glob("*.json"))
        assert files1 and files1 == files2
        for name in files1:
            first = (out1 / name).read_text(encoding="utf-8")
            second = (out2 / name).read_text(encoding="utf-8")
            if name.startswith("events-"):
                continue  # event ids only grow; chains/privacy/gaps must match
            assert first == second, name
    finally:
        _drop_db(db_name, engine)


def test_session_outcome_labels_derive_from_stop_reason() -> None:
    """Checkpoint B condition 6: agent_needs_user_input is 'asked the
    user (awaiting input)', never 'completed: answered'."""
    label = _packet.session_outcome_label
    assert (
        label(
            {"stop_reason": "agent_needs_user_input", "grades": {"asked": True}}, "agent_question"
        )
        == "asked the user (awaiting input)"
    )
    web = label(
        {"stop_reason": "agent_sufficient_evidence", "grades": {"web_answer": True}},
        "agent_finished",
    )
    assert "one demonstrated discovery answer" in web and "not a reliability result" in web
    assert (
        label({"stop_reason": "agent_token_budget_exhausted", "grades": {}}, "agent_step")
        == "stopped: token budget exhausted (no answer)"
    )
    assert (
        label({"stop_reason": "agent_no_progress", "grades": {}}, "agent_step")
        == "stopped: no progress (no answer)"
    )
    mismatch = label(
        {"stop_reason": "agent_needs_user_input", "grades": {"asked": True}}, "agent_step"
    )
    assert "final event agent_step" in mismatch


def test_provenance_text_prefers_prefixed_missing() -> None:
    assert _packet.provenance_text({}) == "provenance: not recorded (pre-fix)"
    assert _packet.provenance_text({"provider_url_count": 0, "unverified_dropped": 0}) == (
        "provenance: 0 provider URLs, 0 dropped"
    )
    assert _packet.provenance_text({"provider_url_count": 3, "unverified_dropped": 1}) == (
        "provenance: 3 provider URLs, 1 dropped"
    )
