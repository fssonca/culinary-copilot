#!/usr/bin/env python3
"""Search retention purge (Phase 5, part 2, offline, owner decision 8).

Owner-run only, never automatic. Deletes expired export/raw files
(documents backup copies) and prints the trigger-aware SQL procedure
for expired search events (90 days events, 30 days process logs are
the configured defaults). Tested on disposable databases only; the
append-only trigger rejects plain rewrites, so deletes use this
procedure.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Single source for the DB procedure: scripts/search/purge_search_events.sql.
# This script prints that file verbatim (never a second copy of the SQL).
PURGE_SQL_PATH = REPO_ROOT / "scripts" / "search" / "purge_search_events.sql"


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge expired search files (owner-run).")
    parser.add_argument("--raw-dir", default="data/phase5-search")
    parser.add_argument("--events-days", type=int, default=90)
    parser.add_argument("--logs-days", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="delete files (default: dry run)")
    args = parser.parse_args()

    raw_dir = REPO_ROOT / args.raw_dir
    now = datetime.now(timezone.utc)
    expired_exports = now - timedelta(days=args.events_days)
    expired_logs = now - timedelta(days=args.logs_days)
    removed: list[str] = []
    if raw_dir.exists():
        for path in sorted(raw_dir.iterdir()):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            limit = expired_logs if path.suffix == ".log" else expired_exports
            if mtime < limit:
                removed.append(str(path))
                if args.apply:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
    print(f"expired files: {len(removed)}")
    for item in removed:
        print(f"  {item}")
    if not args.apply:
        print("dry run: pass --apply to delete (backup copies first)")
    print("--- purge_search_events.sql (verbatim; run by the owner with a backup) ---")
    print(PURGE_SQL_PATH.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
