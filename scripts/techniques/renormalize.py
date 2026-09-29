#!/usr/bin/env python3
"""Re-normalize technique docs from saved raw files (no refetch).

Reads ``data/technique-corpus/<doc_id>.raw.*`` for every id with
``status: ingested`` in the manifest, runs the current normalizer
(``fetch.html_to_text``), and rewrites ``<doc_id>.txt`` +
``<doc_id>.meta.json``. Updates ``sha256_normalized``, ``words``,
``bytes`` and ``normalizer_version``; ``retrieval_date_utc``,
``revision_id`` and ``sha256_raw`` stay unchanged (nothing refetched).

Dropped ids stay dropped: their would-be word counts are printed for
the report only, and no files are written for them.

``--dry-run`` prints per-doc before/after word counts and writes
nothing. No network, no model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "techniques"))

from fetch import (  # type: ignore[import-not-found]  # noqa: E402
    NORMALIZER_VERSION,
    _ensure_gitignored,
    html_to_text,
)

DEFAULT_OUT_DIR = REPO_ROOT / "data" / "technique-corpus"
DEFAULT_MANIFEST = REPO_ROOT / "evals" / "technique_corpus" / "manifest.json"


def _raw_html(doc_id: str, out_dir: Path) -> str:
    raws = sorted(out_dir.glob(f"{doc_id}.raw.*"))
    if not raws:
        raise ValueError(f"no raw file for {doc_id}")
    raw = raws[0].read_bytes()
    if raws[0].suffix == ".json":
        payload = json.loads(raw.decode("utf-8"))
        return str((payload.get("parse") or {}).get("text") or "")
    return raw.decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    docs = manifest["docs"]
    changed: list[tuple[str, int, int]] = []
    for doc_id in sorted(docs):
        record = docs[doc_id]
        try:
            html = _raw_html(doc_id, out_dir)
        except ValueError:
            print(f"{doc_id}: stays dropped ({record.get('reason')}); no raw file saved")
            continue
        _, text = html_to_text(html)
        words = len(text.split())
        if record.get("status") != "ingested":
            print(f"{doc_id}: stays dropped ({record.get('reason')}); renormalized words {words}")
            continue
        before = int(record.get("words") or 0)
        changed.append((doc_id, before, words))
        if args.dry_run:
            continue
        normalized = f"# {record['title']}\n\n{text}\n"
        norm_bytes = normalized.encode("utf-8")
        for path, data in ((out_dir / f"{doc_id}.txt", norm_bytes),):
            _ensure_gitignored(path)
            path.write_bytes(data)
        meta_path = out_dir / f"{doc_id}.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["sha256_normalized"] = hashlib.sha256(norm_bytes).hexdigest()
        meta["words"] = words
        meta["bytes"] = len(norm_bytes)
        meta["normalizer_version"] = NORMALIZER_VERSION
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        record["sha256_normalized"] = meta["sha256_normalized"]
        record["words"] = words
        record["bytes"] = len(norm_bytes)
        record["normalizer_version"] = NORMALIZER_VERSION
    manifest["generated_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest["normalizer_version"] = NORMALIZER_VERSION
    if not args.dry_run:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("doc table-content changes (before -> after words):")
    for doc_id, before, after in changed:
        flag = "  CHANGED" if before != after else ""
        print(f"  {doc_id}: {before} -> {after}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
