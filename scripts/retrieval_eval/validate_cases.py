"""Validate the Phase 1 retrieval case file and record its provenance.

Usage:
    uv run python scripts/retrieval_eval/validate_cases.py \
        evals/cases/phase1_retrieval.json
"""

from __future__ import annotations

import hashlib
import json
import sys

from case_schema import check_case_file, load_case_file


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "evals/cases/phase1_retrieval.json"
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    data = load_case_file(path)
    problems = check_case_file(data)
    dev = sum(1 for c in data.cases if c.split == "development")
    held = sum(1 for c in data.cases if c.split == "held_out")
    kinds: dict[str, int] = {}
    for case in data.cases:
        kinds[case.kind] = kinds.get(case.kind, 0) + 1
    print(
        json.dumps(
            {
                "file": path,
                "sha256": digest,
                "version": data.version,
                "cases": len(data.cases),
                "development": dev,
                "held_out": held,
                "kinds": kinds,
                "problems": problems,
            },
            indent=2,
        )
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
