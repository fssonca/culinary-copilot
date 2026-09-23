"""Exact raw-text deduplication, with no normalization or semantic equivalence."""

import copy
import hashlib
from collections import defaultdict
from typing import Any


def deduplicate_exact_sources(
    recipes: list[dict[str, Any]], sources: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Group on actual text, not a parsed-content fingerprint or hash alone.
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for recipe in recipes:
        sid = recipe["source_id"]
        if sid in seen:
            raise ValueError(f"Repeated source ID: {sid}")
        seen.add(sid)
        raw = sources[sid]
        if not raw.strip():
            raise ValueError(f"Empty source text: {sid}")
        groups[raw].append(recipe)
    result, aliases = [], []
    for raw, members in groups.items():
        members.sort(key=lambda r: r["source_id"])
        canonical = copy.deepcopy(members[0])
        canonical["raw_source_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
        canonical["source_records"] = [
            {
                key: copy.deepcopy(r.get(key))
                for key in ("source_id", "row_number", "provenance", "capabilities")
            }
            for r in members
        ]
        # Preserve alternate structured interpretations, not just their IDs.
        canonical["duplicate_interpretations"] = copy.deepcopy(members[1:])
        result.append(canonical)
        for r in members:
            aliases.append(
                {
                    "source_id": r["source_id"],
                    "canonical_source_id": canonical["source_id"],
                    "raw_source_sha256": canonical["raw_source_sha256"],
                }
            )
    return sorted(result, key=lambda r: r["source_id"]), aliases
