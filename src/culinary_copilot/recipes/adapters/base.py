"""Shared adapter types and deterministic identity helpers."""

import hashlib
import json
from typing import Any, TypedDict


class Provenance(TypedDict, total=False):
    dataset_id: str
    revision: str
    file_path: str
    file_sha256: str
    row_number: int
    source_id: str
    normalizer_version: str
    adapter: str
    adapter_version: str
    language: str
    source_url: str | None
    license_declared: str | None
    provenance_status: str


class AdapterResult(TypedDict):
    recipe: dict[str, Any] | None
    quarantine: dict[str, Any] | None


def fingerprint(title: str, ingredients: list[str], instructions: list[str]) -> str:
    """Normalized title/ingredient/instruction fingerprint (dedup key)."""
    from culinary_copilot.recipes.normalize import canonical

    payload = json.dumps(
        [canonical(title), [canonical(i) for i in ingredients], instructions],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def foodie_source_id(row_number: int) -> str:
    """Deterministic ID for odunola/foodie rows (no natural source ID).

    ``foodie-{row:06d}`` is stable for a pinned revision+checksum. If upstream
    reorders rows, IDs remap (documented limitation); content fingerprints in
    the manifest detect content changes independently of order.
    """
    return f"foodie-{row_number:06d}"
