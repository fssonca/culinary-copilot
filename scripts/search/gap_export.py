#!/usr/bin/env python3
"""Gap-candidate export (Phase 5, part 2, offline, owner decision 9).

Derives investigation candidates from existing session_events on a
disposable database (or a review export) and writes a git-ignored JSON
file under data/phase5-search/. Nothing is imported automatically;
only review confirms a cause. Triggers produce candidates, never
confirmed causes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


def main() -> int:
    # Deferred imports (review fix 1): minimizers resolve at call time
    # so tests can prove fail-closed behavior by patching them.
    from culinary_copilot.search.gaps import gap_candidates_for_session
    from culinary_copilot.search.minimize import minimize_query

    parser = argparse.ArgumentParser(description="Export web-search gap candidates.")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--events-json", required=True, help="session_events export (list)")
    parser.add_argument("--query", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    raw = json.loads(Path(args.events_json).read_text(encoding="utf-8"))
    # Fail-closed (review fix 1): minimize BEFORE building anything,
    # and write the output file only after everything succeeds — a
    # minimizer failure exits non-zero with no file written.
    try:
        minimized = minimize_query(args.query)
    except Exception as exc:
        print(f"refusing export: query minimization failed ({type(exc).__name__})")
        return 1
    events: list[object] = []
    for index, payload in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(payload, dict):
            continue
        events.append(
            type(
                "E",
                (),
                {
                    "event_type": payload.get("event_type", ""),
                    "payload": payload.get("payload", {}),
                },
            )()
        )
    candidates = gap_candidates_for_session(
        session_id=args.session_id,
        events=events,
        minimized_query=minimized,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"wrote {len(candidates)} candidates to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
