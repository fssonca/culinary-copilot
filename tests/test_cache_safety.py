"""Automatic stale-cache protection.

Regression suite: cached merges are reusable only under unchanged
validation + merge logic. Legacy entries (no recorded logic versions)
and entries from older logic force full revalidation/remerging of the
saved raw response — a stale merged defect (e.g. 19380's inherited 'lb'
units) can never reach ready storage through the cache.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from culinary_copilot.config import Settings
from culinary_copilot.recipes import llm_batch
from culinary_copilot.recipes.adapters.foodie import FOODIE_ADAPTER_VERSION
from culinary_copilot.recipes.llm_cache import cache_key, entry_is_fresh
from culinary_copilot.recipes.llm_cache import read as cache_read
from culinary_copilot.recipes.llm_cache import write as cache_write
from culinary_copilot.recipes.llm_contracts import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    numbered_source,
    prompt_hash,
)
from culinary_copilot.recipes.llm_validate import (
    MERGE_VERSION,
    VALIDATOR_VERSION,
    current_logic_versions,
)
from culinary_copilot.recipes.routing import ROUTING_VERSION

FIX = Path(__file__).parent / "fixtures" / "amount_unit"


def _fresh_entry():
    return {
        "validation": {"verdict": "accepted", "problems": [], "retry_eligible": False},
        "merged": {"title": "T"},
        "source_id": "s",
        **current_logic_versions(),
    }


def test_fresh_entry_is_reusable():
    assert entry_is_fresh(_fresh_entry()) is True


def test_legacy_entry_without_versions_is_stale():
    legacy = {"validation": {"verdict": "accepted"}, "merged": {"title": "T"}, "source_id": "s"}
    assert entry_is_fresh(legacy) is False
    assert entry_is_fresh(None) is False
    assert entry_is_fresh({}) is False


def test_bumped_validator_version_invalidates():
    assert entry_is_fresh({**_fresh_entry(), "validator_version": "older"}) is False


def test_bumped_merge_version_invalidates():
    assert entry_is_fresh({**_fresh_entry(), "merge_version": "older"}) is False


def _run_dir_for_19380(run_dir: Path) -> str:
    """Minimal finalize-ready run dir around the verbatim 19380 artifacts."""
    texts = (FIX / "19380-source.txt").read_text()
    parsed = json.loads((FIX / "19380-parsed.json").read_text())
    _, line_map = numbered_source(texts)
    content_hash = hashlib.sha256(texts.encode()).hexdigest()
    assert parsed["content_hash"] == content_hash
    key = cache_key(
        dataset_id="odunola/foodie",
        revision="test-rev",
        source_id="foodie-019380",
        content_hash=content_hash,
        adapter_version=FOODIE_ADAPTER_VERSION,
        routing_version=ROUTING_VERSION,
        prompt_version=PROMPT_VERSION,
        prompt_hash=prompt_hash(),
        schema_version=SCHEMA_VERSION,
        model="gpt-6-luna",
    )
    custom_id = "foodie-019380:test:p6s5"
    mapping = {
        custom_id: {
            "source_id": "foodie-019380",
            "row_number": 19380,
            "texts": texts,
            "content_hash": content_hash,
            "segment": None,
            "request_versions": {
                "prompt_version": PROMPT_VERSION,
                "prompt_hash": prompt_hash(),
                "schema_version": SCHEMA_VERSION,
                "adapter_version": FOODIE_ADAPTER_VERSION,
                "routing_version": ROUTING_VERSION,
                "model": "gpt-6-luna",
                "reasoning_effort": "low",
            },
            "cache_key": key,
            "line_map": line_map,
            "ingredient_line_ids": ["L4"],
            "step_line_ids": ["L6"],
            "heading_line_ids": [],
            "prose_line_ids": ["L2"],
            "requested_ingredient_line_ids": ["L4"],
            "reason_codes": ["quantity_unknown"],
            "audit": {},
        }
    }
    (run_dir / "mapping.json").write_text(json.dumps(mapping))
    (run_dir / "sources.json").write_text(
        json.dumps(
            {
                "foodie-019380": {
                    "row_number": 19380,
                    "texts": texts,
                    "content_hash": content_hash,
                    "route": "needs_llm",
                    "reason_codes": ["quantity_unknown"],
                    "audit": {},
                }
            }
        )
    )
    (run_dir / "results.jsonl").write_text(
        json.dumps(
            {
                "source_id": "foodie-019380",
                "custom_id": custom_id,
                "parsed": parsed,
                "model": "gpt-6-luna",
                "api_request_id": "req-test",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        )
        + "\n"
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "stage": "collected",
                "model": "gpt-6-luna",
                "reasoning_effort": "low",
                "prompt_version": PROMPT_VERSION,
                "prompt_hash": prompt_hash(),
                "schema_version": SCHEMA_VERSION,
                "adapter_version": FOODIE_ADAPTER_VERSION,
                "routing_version": ROUTING_VERSION,
                "batch_id": None,
            }
        )
    )
    return key


def _args(run_dir: Path, cache_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(run_dir=str(run_dir), cache_dir=str(cache_dir))


def _settings() -> Settings:
    return Settings(_env_file=None, llm_ingestion_enabled=True)


def _ready(cache_dir: Path, run_dir: Path):
    summary = llm_batch.cmd_finalize(_args(run_dir, cache_dir), _settings())
    ready = [
        json.loads(line)
        for line in (run_dir / "ready_to_load.jsonl").read_text().splitlines()
        if line.strip()
    ]
    records = [
        json.loads(line)
        for line in (run_dir / "records.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return summary, ready, records


def _units_by_name(ready_row):
    return {i["name"]: i.get("unit") for i in ready_row["ingredients"]}


def test_unchanged_logic_reuses_valid_results(tmp_path):
    run_dir, cache_dir = tmp_path / "run", tmp_path / "cache"
    run_dir.mkdir()
    _run_dir_for_19380(run_dir)
    first, ready1, records1 = _ready(cache_dir, run_dir)
    assert first["ready"] == 1 and first["from_cache"] == 0
    assert records1[0].get("from_cache") is not True
    # Second finalize reuses the freshly written entry: no revalidation.
    second, ready2, records2 = _ready(cache_dir, run_dir)
    assert second["ready"] == 1 and second["from_cache"] == 1
    assert records2[0]["from_cache"] is True
    assert _units_by_name(ready2[0]) == _units_by_name(ready1[0])


def test_changed_validator_logic_cannot_reuse_stale_merge(tmp_path, monkeypatch):
    import culinary_copilot.recipes.llm_validate as validate_mod

    run_dir, cache_dir = tmp_path / "run", tmp_path / "cache"
    run_dir.mkdir()
    _run_dir_for_19380(run_dir)
    llm_batch.cmd_finalize(_args(run_dir, cache_dir), _settings())
    monkeypatch.setattr(validate_mod, "VALIDATOR_VERSION", "changed")
    summary, _, records = _ready(cache_dir, run_dir)
    assert summary["ready"] == 1 and summary["from_cache"] == 0
    assert records[0].get("from_cache") is not True


def test_changed_merge_logic_cannot_reuse_stale_merge(tmp_path, monkeypatch):
    import culinary_copilot.recipes.llm_validate as validate_mod

    run_dir, cache_dir = tmp_path / "run", tmp_path / "cache"
    run_dir.mkdir()
    _run_dir_for_19380(run_dir)
    llm_batch.cmd_finalize(_args(run_dir, cache_dir), _settings())
    monkeypatch.setattr(validate_mod, "MERGE_VERSION", "changed")
    summary, _, records = _ready(cache_dir, run_dir)
    assert summary["ready"] == 1 and summary["from_cache"] == 0
    assert records[0].get("from_cache") is not True


def test_legacy_19380_defect_cannot_reach_ready(tmp_path):
    """A legacy accepted entry carrying the inherited-'lb' defect is
    ignored; the fresh merge keeps chicken's own lb and nothing else."""
    run_dir, cache_dir = tmp_path / "run", tmp_path / "cache"
    run_dir.mkdir()
    key = _run_dir_for_19380(run_dir)
    cache_dir.mkdir()
    legacy_defect = {
        "validation": {"verdict": "accepted", "problems": [], "retry_eligible": False},
        "merged": {
            "title": "Chicken Yassa (legacy defect)",
            "ingredients": [
                {"name": "chicken thighs and drumsticks", "unit": "lb"},
                {"name": "lemon, juiced", "unit": "lb"},
                {"name": "salt", "unit": "lb"},
                {"name": "hard-boiled eggs, peeled", "unit": "lb"},
            ],
            "instructions": ["Cook."],
        },
        "source_id": "foodie-019380",
    }
    cache_write(cache_dir, key, legacy_defect)
    assert entry_is_fresh(cache_read(cache_dir, key)) is False
    summary, ready, records = _ready(cache_dir, run_dir)
    assert summary["ready"] == 1 and summary["from_cache"] == 0
    assert records[0].get("from_cache") is not True
    units = _units_by_name(ready[0])
    assert units["chicken thighs and drumsticks"] == "lb"
    assert units["lemon, juiced"] is None
    assert units["salt"] is None
    assert units["hard-boiled eggs, peeled"] is None
    assert VALIDATOR_VERSION and MERGE_VERSION
    assert entry_is_fresh(cache_read(cache_dir, key))
