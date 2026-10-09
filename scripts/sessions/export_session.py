#!/usr/bin/env python3
"""Export one agent session (row plus every event) for later analysis.

Read-only: runs inside a READ ONLY transaction against ``DATABASE_URL``
(or ``--database-url``) and writes files under ``data/session-exports/``
(gitignored), never into the repository.

Sessions created while ``AGENT_RECORD_TRAJECTORY`` was on (``make demo``)
also carry tool arguments, what each tool returned to the model, every
model directive (including rejected answers) and each run's result.
Older sessions export whatever they recorded.

Usage (repo root)::

    uv run python scripts/sessions/export_session.py --list
    uv run python scripts/sessions/export_session.py ses-c61d0af4a101
    uv run python scripts/sessions/export_session.py --latest --format md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "session-exports"


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", nargs="?", default="")
    parser.add_argument("--latest", action="store_true", help="export the newest session")
    parser.add_argument("--list", action="store_true", help="list the 20 newest sessions")
    parser.add_argument("--format", choices=("json", "md", "both"), default="both")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--database-url", default="")
    return parser.parse_args(argv)


def _engine(database_url: str) -> Any:
    from sqlalchemy import create_engine

    url = database_url
    if not url:
        from culinary_copilot.config import Settings

        raw = Settings().database_url
        url = raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw)
    return create_engine(url)


def _read_only(conn: Any) -> None:
    from sqlalchemy import text

    conn.execute(text("SET TRANSACTION READ ONLY"))


def list_sessions(engine: Any, limit: int = 20) -> list[dict[str, Any]]:
    from sqlalchemy import text

    with engine.connect() as conn:
        _read_only(conn)
        rows = conn.execute(
            text(
                "SELECT s.id, s.created_at, s.updated_at, s.current_phase, "
                "s.steps_remaining, s.tool_calls_remaining, "
                "(SELECT count(*) FROM session_events e WHERE e.session_id = s.id) AS events, "
                "(SELECT e.payload->>'text' FROM session_events e WHERE e.session_id = s.id "
                " AND e.event_type = 'user_message' ORDER BY e.seq LIMIT 1) AS first_message "
                "FROM sessions s ORDER BY s.updated_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        ).mappings()
        return [dict(row) for row in rows]


def load_session(engine: Any, session_id: str) -> dict[str, Any] | None:
    from sqlalchemy import text

    with engine.connect() as conn:
        _read_only(conn)
        if not session_id:
            session_id = str(
                conn.execute(
                    text("SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1")
                ).scalar_one_or_none()
                or ""
            )
        row = (
            conn.execute(text("SELECT * FROM sessions WHERE id = :sid"), {"sid": session_id})
            .mappings()
            .first()
        )
        if row is None:
            return None
        events = conn.execute(
            text(
                "SELECT seq, event_type, payload, created_at FROM session_events "
                "WHERE session_id = :sid ORDER BY seq"
            ),
            {"sid": session_id},
        ).mappings()
        return {"session": dict(row), "events": [dict(event) for event in events]}


def _short(value: Any, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + " …"


def _diagnostic_line(payload: dict[str, Any]) -> str:
    # H7: provider reasoning summaries travel only as a labelled,
    # bounded diagnostic. Show the label, never raw reasoning.
    diag = payload.get("reasoning_diagnostic")
    if not isinstance(diag, dict):
        return ""
    text = str(diag.get("text") or "")[:200]
    return f" diagnostic(provider_reasoning_summary): {_short(text, 200)}"


def _budgets_line(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    if payload.get("steps_remaining") is not None:
        parts.append(f"steps left {payload.get('steps_remaining')}")
    if payload.get("tool_calls_remaining") is not None:
        parts.append(f"tools left {payload.get('tool_calls_remaining')}")
    return "; ".join(parts)


def _event_line(event: dict[str, Any]) -> str:
    kind = str(event.get("event_type") or "")
    payload = event.get("payload") or {}
    if kind == "user_message":
        return f"**User:** {payload.get('text', '')}"
    if kind == "agent_answer":
        answer = payload.get("answer", "(not recorded)")
        return f"**User answered** `{payload.get('question_id')}`: {answer}"
    if kind == "agent_turn":
        # H7 decision log: tools offered and withheld per turn with the
        # reason, plus the remaining budgets. JSON carries the full
        # payload; the timeline shows the concise decision.
        offered = payload.get("offered") or []
        withheld = payload.get("withheld") or []
        withheld_txt = ", ".join(
            f"{w.get('tool')} ({w.get('reason')})" for w in withheld if isinstance(w, dict)
        )
        line = (
            f"**Turn {payload.get('turn')}** ({payload.get('phase')}): "
            f"offered [{', '.join(str(t) for t in offered)}]"
        )
        if withheld_txt:
            line += f"; withheld [{withheld_txt}]"
        if payload.get("final_turn"):
            line += " final"
        if payload.get("wrap_up"):
            line += " wrap-up"
        budgets = _budgets_line(payload)
        if budgets:
            line += f" ({budgets})"
        line += _diagnostic_line(payload)
        return line
    if kind == "agent_question":
        options = ", ".join(str(o) for o in payload.get("question_options") or [])
        line = f"**Agent asked:** {payload.get('question_text', '')} (options: {options})"
        budgets = _budgets_line(payload)
        if budgets:
            line += f" ({budgets})"
        line += _diagnostic_line(payload)
        return line
    if kind == "tool_call":
        args = payload.get("args", f"digest {payload.get('args_digest')}")
        facts = payload.get("result_facts")
        line = (
            f"**Tool** `{payload.get('tool')}` → {payload.get('outcome')}"
            f" ({payload.get('latency_ms')} ms, {payload.get('cost_class')})"
            f" args: {_short(args, 300)}"
        )
        if payload.get("reason"):
            line += f" reason: {payload.get('reason')}"
        if facts:
            line += f" facts: {_short(facts, 300)}"
        return line
    if kind == "trajectory_tool_outputs":
        parts = [
            f"  - `{o.get('tool')}`: {_short((o.get('output') or {}).get('text', ''), 800)}"
            for o in payload.get("outputs") or []
        ]
        return "**Tool outputs seen by the model:**\n" + "\n".join(parts)
    if kind == "trajectory_model_directive":
        directive = (payload.get("directive") or {}).get("text", "")
        return f"**Model directive:** {_short(directive, 1500)}"
    if kind == "agent_validation_reject":
        line = "**Rejected by validators:** " + "; ".join(
            str(e) for e in payload.get("errors") or []
        )
        budgets = _budgets_line(payload)
        if budgets:
            line += f" ({budgets})"
        line += _diagnostic_line(payload)
        return line
    if kind == "trajectory_run_result":
        final = (payload.get("final") or {}).get("text", "") if payload.get("final") else ""
        head = payload.get("stop_reason") or payload.get("reason")
        return f"**Run result:** {payload.get('status')} `{head}`" + (
            f"\n\n  final: {_short(final, 1500)}" if final else ""
        )
    if kind == "agent_step":
        line = (
            f"step: {payload.get('note')} (in {payload.get('input_tokens')} / "
            f"out {payload.get('output_tokens')} tokens; steps left "
            f"{payload.get('steps_remaining')}, tools left {payload.get('tool_calls_remaining')})"
        )
        if payload.get("repeated_tools"):
            line += f" repeated [{', '.join(str(t) for t in payload.get('repeated_tools') or [])}]"
        if payload.get("repeat_noted"):
            line += (
                f" repeat-noted [{', '.join(str(t) for t in payload.get('repeat_noted') or [])}]"
            )
        line += _diagnostic_line(payload)
        return line
    if kind == "agent_finished":
        line = f"**Finished:** {_short(payload.get('note', ''), 300)}"
        budgets = _budgets_line(payload)
        if budgets:
            line += f" ({budgets})"
        line += _diagnostic_line(payload)
        return line
    if kind == "agent_run_started":
        return (
            f"**Run started** ({payload.get('phase')}; steps "
            f"{payload.get('steps_remaining')}, tools {payload.get('tool_calls_remaining')})"
        )
    return f"`{kind}`: {_short(payload, 400)}"


def to_markdown(export: dict[str, Any]) -> str:
    session = export["session"]
    lines = [
        f"# Session {session.get('id')}",
        "",
        f"- Created: {session.get('created_at')}; updated: {session.get('updated_at')}",
        f"- Phase: {session.get('current_phase')}; constraints: "
        f"{json.dumps(session.get('constraints'), default=str)}",
        f"- Budgets left: {session.get('steps_remaining')} steps, "
        f"{session.get('tool_calls_remaining')} tool calls",
        f"- Internet search allowed: {session.get('internet_search_allowed')}",
        f"- Events: {len(export['events'])}",
        "",
        "## Timeline",
        "",
    ]
    for event in export["events"]:
        lines.append(f"{event.get('seq')}. {_event_line(event)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    engine = _engine(args.database_url)
    if args.list:
        for row in list_sessions(engine):
            first = str(row.get("first_message") or "")[:60]
            print(
                f"{row['id']}  {row['updated_at']:%Y-%m-%d %H:%M}  {row['current_phase']:<9} "
                f"events {row['events']:>3}  steps {row['steps_remaining']:>3}  {first}"
            )
        return 0
    if not args.session_id and not args.latest:
        print("give a session id, --latest or --list", file=sys.stderr)
        return 2
    export = load_session(engine, "" if args.latest else args.session_id)
    if export is None:
        print(f"session not found: {args.session_id}", file=sys.stderr)
        return 1
    session_id = str(export["session"].get("id"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if args.format in ("json", "both"):
        path = out_dir / f"{session_id}.json"
        path.write_text(json.dumps(export, indent=2, default=str), encoding="utf-8")
        written.append(path)
    if args.format in ("md", "both"):
        path = out_dir / f"{session_id}.md"
        path.write_text(to_markdown(export), encoding="utf-8")
        written.append(path)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
