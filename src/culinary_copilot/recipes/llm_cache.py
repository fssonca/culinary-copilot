"""Cache for validated LLM interpretations.

The cache key covers every input that can affect interpretation — not the
source hash alone. Re-running unchanged ingestion reuses validated results
instead of spending another model call; any changed input or version
invalidates the entry and routes the record back through preparation.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def cache_key(
    *,
    dataset_id: str,
    revision: str,
    source_id: str,
    content_hash: str,
    adapter_version: str,
    routing_version: str,
    prompt_version: str,
    prompt_hash: str,
    schema_version: str,
    model: str,
    segment: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    payload = "|".join(
        [
            dataset_id,
            revision,
            source_id,
            content_hash,
            adapter_version,
            routing_version,
            prompt_version,
            prompt_hash,
            schema_version,
            model,
            segment or "",
            reasoning_effort or "",
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def read(cache_dir: Path, key: str) -> dict[str, Any] | None:
    path = cache_dir / f"{key}.json"
    if not path.exists():
        return None
    try:
        entry: dict[str, Any] = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    return entry


def entry_is_fresh(entry: dict[str, Any] | None) -> bool:
    """A cached merge is reusable only under unchanged validation+merge logic.

    Legacy entries (written before logic versions were recorded) and
    entries from older logic are conservatively stale: callers must
    revalidate/remerge the saved raw response under current code instead
    of trusting the stored merged output. Never raises — unknown shapes
    are stale, not errors.
    """
    if not isinstance(entry, dict):
        return False
    try:
        from culinary_copilot.recipes.llm_validate import current_logic_versions
    except ImportError:
        return False
    current = current_logic_versions()
    return all(entry.get(key) == value for key, value in current.items())


def write(cache_dir: Path, key: str, entry: dict[str, Any]) -> Path:
    """Atomic write (tmp file + rename) so interruption never leaves halves."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key}.json"
    fd, tmp = tempfile.mkstemp(dir=str(cache_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(entry, stream, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
