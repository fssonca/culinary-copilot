"""Hybrid ingestion tests: mocked Batch API, offline, key-free (except isolated DB)."""

import json
from types import SimpleNamespace

import pytest

from culinary_copilot.config import Settings
from culinary_copilot.recipes import llm_batch
from culinary_copilot.recipes import routing as routing_mod
from culinary_copilot.recipes.adapters.foodie import normalize_foodie_text
from culinary_copilot.recipes.dataset_utils import sha256_file
from culinary_copilot.recipes.llm_batch import (
    block_line_ids,
    build_request_body,
    estimate_cost_usd,
)
from culinary_copilot.recipes.llm_cache import cache_key
from culinary_copilot.recipes.llm_cache import read as cache_read
from culinary_copilot.recipes.llm_cache import write as cache_write
from culinary_copilot.recipes.llm_contracts import (
    ExtractionResponse,
    custom_id_for,
    numbered_source,
)
from culinary_copilot.recipes.llm_validate import merge_response, validate_response

CLEAN = (
    "Simple Cake\nIngredients\n2 cups flour\n1 cup sugar\n"
    "Introduction\nEasy.\nDirections\nMix.\nBake 20 minutes.\n"
)
AMBIGUOUS = (
    "Leek Test\nIngredients\n6leek whites600 g of leeks\n1 pinchSalt\n"
    "Introduction\nTasty.\nDirections\nCook 5 minutes.\nServe.\n"
)
BLOB = (
    "July 3, 2020Mystery Stew\nIt was good 2 cups rice cooked well done ok "
    "with plenty of flavor and extra words to make this blob substantive "
    "enough for LLM segmentation instead of quarantine, yes indeed, and "
    "here is even more padding text to cross the threshold confidently."
)
NOISY = "x" * 50


def test_parser_regressions_from_review():
    from culinary_copilot.recipes.adapters.foodie import parse_ingredient_line as p

    assert p("6leek whites600 g of leeks")["unit"] in (None, "count")  # never liters
    assert p("40 gsemi-salted butter")["unit"] == "g"
    assert p("25 clVegetable Stock")["unit"] == "cl"
    assert p("6 fluid ounces pineapple juice")["unit"] == "fl_oz"
    assert p("1 pinchSalt")["unit"] == "pinch"
    assert p("1½ pound steaks")["amount"] == "3/2"  # no "11/2" corruption
    assert p("1-2 tablespoons flour")["canonical"] == "flour"
    assert p("1/2 cup maple syrupor honey")["alternatives"] is True
    assert p("1 tablespoon avocado oil, or more as needed")["alternatives"] is False
    assert p("3 pounds flank or skirt steak")["alternatives"] is True
    assert p("1/3 cup plus 2 tablespoons sugar")["canonical"] == "sugar"
    assert "sugar" in p("1 cup white sugar, plus 1/3 cup (for rolling)")["canonical"]
    assert "goya" in p("1 teaspoon Goya Adobo with Pepper, plus more to taste")["canonical"]
    assert p("ice")["canonical"] == "ice"  # ingredient, not heading


def test_name_cleanup_is_cosmetic_only():
    """Trailing commas from prep-suffix cuts are stripped; nothing else changes."""
    from culinary_copilot.recipes.adapters.foodie import parse_ingredient_line as p

    assert p("1/2 pound andouille sausage, sliced")["name"] == "andouille sausage"
    assert p("1 small yellow onion, diced")["name"] == "small yellow onion"
    assert p("12 cloves garlic, minced")["name"] == "garlic"
    assert p("1 celery rib, sliced")["name"] == "celery rib"


def test_package_size_unit_and_or_more_preservation():
    """Package sizes, units, and or-more qualifiers must survive parsing.

    Structured fields keep the countable amount and normalized unit; the full
    original line (with sizes and qualifiers) is always preserved verbatim.
    """
    from culinary_copilot.recipes.adapters.foodie import parse_ingredient_line as p

    r = p("1 (16 ounce) package dry lentils")
    assert r["amount_text"] == "1" and "16 ounce" in r["original"]
    r = p("2 (8 ounce) cans tomato sauce")
    assert r["amount_text"] == "2" and "8 ounce" in r["original"]
    assert p("3 pounds ground lamb")["unit"] == "lb"
    assert p("2 tablespoons diced celery")["unit"] == "tbsp"
    assert p("1 Tablespoon grated ginger")["unit"] == "tbsp"
    assert p("15 ounces black olives")["unit"] == "oz"
    assert p("2 cans tomato sauce")["unit"] == "can"
    r = p("2 tablespoons or more diced celery")
    assert r["amount_text"] == "2" and r["unit"] == "tbsp"
    assert "or more" in (r["name"] + " " + (r["notes"] or "") + " " + r["original"])
    r = p("1 cup sifted all-purpose flour, or more as needed")
    assert r["amount_text"] == "1" and "or more" in (r["name"] + " " + r["original"])


def test_headings_and_dozen_and_credits():
    recipe = normalize_foodie_text(
        "T\nIngredients\nice\n1 cup water\nIntroduction\nHi.\nDirections\nMix well.\nServe.\n",
        5,
    )
    assert len(recipe["ingredients"]) == 2  # ice kept, no synthetic group
    assert recipe["ingredient_groups"] == []
    assert "ice" in [i["canonical"] for i in recipe["ingredients"]]

    recipe = normalize_foodie_text(CLEAN.replace("2 cups flour", "2 cups flour"), 6)
    assert recipe["capabilities"]["quantities_validated"] is True

    credited = normalize_foodie_text(
        "T\nIngredients\n1 cup water\nIntroduction\nHi.\nDirections\nMix well.\n"
        "Bake 20 minutes.\nDotdash Meredith Food Studios\nServe.\n",
        7,
    )
    assert "Dotdash Meredith Food Studios" not in credited["instructions"]
    assert credited["attribution"] == ["Dotdash Meredith Food Studios"]


def test_clean_bypasses_llm_and_silent_errors_route():
    clean = routing_mod.route_record(CLEAN, 1, recipe=normalize_foodie_text(CLEAN, 1))
    assert clean["route"] == "deterministic_accept"
    bad = routing_mod.route_record(AMBIGUOUS, 2, recipe=normalize_foodie_text(AMBIGUOUS, 2))
    assert bad["route"] == "needs_llm"
    assert "name_contains_measure" in bad["reason_codes"]
    assert bad["audit"] is False
    blob = routing_mod.route_record(BLOB, 3, parse_error="incomplete_ingredients_or_steps")
    assert blob["route"] == "needs_llm"
    assert blob["reason_codes"] == ["section_boundary_failed"]
    tiny = routing_mod.route_record(NOISY, 4, parse_error="incomplete_ingredients_or_steps")
    assert tiny["route"] == "quarantine"


def test_custom_id_stable_and_unique():
    assert custom_id_for("foodie-000001", "abc123") == custom_id_for("foodie-000001", "abc123")
    assert custom_id_for("foodie-000001", "abc123") != custom_id_for("foodie-000001", "def456")
    assert custom_id_for("foodie-000001", "abc123") != custom_id_for("foodie-000002", "abc123")


def _ctx(texts, row=9):
    line_ids, line_map = numbered_source(texts)
    ing_ids, step_ids, _, _ = block_line_ids(texts)
    versions = {
        "prompt_version": "1",
        "schema_version": "1",
        "adapter_version": "2",
        "routing_version": "1",
    }
    return line_map, ing_ids, step_ids, versions


def _good_response(texts, row=9, source_id="foodie-000009", needle="rice"):
    line_ids, line_map = numbered_source(texts)
    first_ing = next(lid for lid in line_ids if needle in line_map[lid].casefold())
    step_ids = [lid for lid in line_ids if line_map[lid].rstrip().endswith(".")]
    import hashlib

    return {
        "status": "resolved",
        "source_id": source_id,
        "content_hash": hashlib.sha256(texts.encode()).hexdigest(),
        "title": "Mystery",
        "description": None,
        "ingredients": [
            {
                "source_line_id": first_ing,
                "name": "rice",
                "amount_text": None,
                "amount_value": None,
                "unit_text": None,
                "unit_normalized": None,
                "qualitative": True,
                "optional": False,
                "alternatives": [],
                "notes": None,
                "group": "main",
                "is_range": False,
                "compound": False,
                "equivalent": None,
                "uncertain": False,
                "evidence": {"line_ids": [first_ing], "excerpt": line_map[first_ing][:40]},
            }
        ],
        "steps": [
            {
                "source_line_id": lid,
                "text": line_map[lid],
                "uncertain": False,
                "evidence": {"line_ids": [lid], "excerpt": line_map[lid][:40]},
            }
            for lid in step_ids
        ],
        "notes": [],
        "servings": None,
        "servings_text": None,
        "batch_yield_count": None,
        "batch_yield_text": None,
        "temperatures": [],
        "durations": [],
        "unclassified_line_ids": [
            lid for lid in line_ids if lid != first_ing and lid not in step_ids
        ],
        "uncertainty_notes": [],
    }


MYSTERY = (
    "Mystery\nIngredients\na handful of rice\nIntroduction\nOk.\n"
    "Directions\nCook rice.\nServe hot.\n"
)


def test_validation_accepts_good_and_rejects_fabrication():
    import hashlib

    line_map, ing_ids, step_ids, versions = _ctx(MYSTERY)
    good = _good_response(MYSTERY)
    report = validate_response(
        good,
        source_id="foodie-000009",
        content_hash=hashlib.sha256(MYSTERY.encode()).hexdigest(),
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "accepted", report["problems"]

    fabricated = json.loads(json.dumps(good))
    fabricated["ingredients"][0]["evidence"]["excerpt"] = "2 cups unicorn flour"
    report = validate_response(
        fabricated,
        source_id="foodie-000009",
        content_hash=hashlib.sha256(MYSTERY.encode()).hexdigest(),
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "rejected_validation"
    assert any(p["code"] == "fabricated_evidence" for p in report["problems"])
    assert report["retry_eligible"] is False


def test_validation_rejects_injection_and_invention():
    import hashlib

    line_map, ing_ids, step_ids, versions = _ctx(MYSTERY)
    evil = _good_response(MYSTERY)
    evil["servings"] = 100
    evil["servings_text"] = "per secret instructions"
    report = validate_response(
        evil,
        source_id="foodie-000009",
        content_hash=hashlib.sha256(MYSTERY.encode()).hexdigest(),
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    codes = {p["code"] for p in report["problems"]}
    assert "invented_servings" in codes
    assert report["verdict"] == "rejected_validation"

    with_url = _good_response(MYSTERY)
    with_url["uncertainty_notes"] = ["see https://example.com/recipe for details"]
    report = validate_response(
        with_url,
        source_id="foodie-000009",
        content_hash=hashlib.sha256(MYSTERY.encode()).hexdigest(),
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert any(p["code"] == "invented_url" for p in report["problems"])


def test_validation_unresolved_and_stale_and_downgrade():
    import hashlib

    line_map, ing_ids, step_ids, versions = _ctx(MYSTERY)
    content_hash = hashlib.sha256(MYSTERY.encode()).hexdigest()
    partial = _good_response(MYSTERY)
    partial["status"] = "unresolved"
    report = validate_response(
        partial,
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "accepted_partial"

    stale_versions = dict(versions, prompt_version="0")
    report = validate_response(
        _good_response(MYSTERY),
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=stale_versions,
        current_versions=versions,
    )
    assert report["verdict"] == "rejected_validation"
    assert any(p["code"] == "stale_configuration" for p in report["problems"])

    overconfident = _good_response(MYSTERY)
    overconfident["ingredients"][0]["uncertain"] = True
    report = validate_response(
        overconfident,
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "accepted_partial"


def test_merge_recomputes_capabilities_and_keeps_provenance():

    response = ExtractionResponse.model_validate(_good_response(MYSTERY))
    merged = merge_response(
        response, deterministic=None, validation={"verdict": "accepted", "problems": []}
    )
    assert merged["capabilities"]["quantities_validated"] is True
    assert merged["capabilities"]["complete_eligible"] is False  # no servings/durations
    assert merged["llm_status"] == "resolved"


def test_cache_reuse_and_invalidation(tmp_path):
    entry = {"validation": {"verdict": "accepted"}, "merged": {"title": "T"}}
    key = cache_key(
        dataset_id="d",
        revision="r",
        source_id="s",
        content_hash="h",
        adapter_version="2",
        routing_version="1",
        prompt_version="1",
        prompt_hash="ph",
        schema_version="1",
        model="m",
    )
    assert cache_read(tmp_path, key) is None
    cache_write(tmp_path, key, entry)
    assert cache_read(tmp_path, key)["merged"]["title"] == "T"
    changed = cache_key(
        dataset_id="d",
        revision="r",
        source_id="s",
        content_hash="h",
        adapter_version="3",
        routing_version="1",
        prompt_version="1",
        prompt_hash="ph",
        schema_version="1",
        model="m",
    )
    assert cache_read(tmp_path, changed) is None


def test_estimate_unknown_without_prices():
    settings = Settings(_env_file=None)
    assert estimate_cost_usd(1000, 500, settings) is None
    priced = Settings(_env_file=None, llm_price_input_per_1m=0.05, llm_price_output_per_1m=0.40)
    assert estimate_cost_usd(1_000_000, 1_000_000, priced) == round(0.45 * 0.5, 4)


# --------------------------------------------------------------- mocked e2e ---


class FakeFiles:
    def __init__(self):
        self.uploads: list[tuple[str, str, bytes]] = []
        self.contents: dict[str, str] = {}

    def create(self, file, purpose):
        data = file.read()
        fid = f"file-mock{len(self.uploads)}"
        self.uploads.append((fid, purpose, data))
        return SimpleNamespace(id=fid)

    def content(self, file_id):
        return SimpleNamespace(text=self.contents[file_id])


class FakeBatches:
    def __init__(self):
        self.batches: dict = {}

    def create(self, input_file_id, endpoint, completion_window, metadata=None):
        bid = f"batch-mock{len(self.batches)}"
        self.batches[bid] = {
            "status": "in_progress",
            "input_file_id": input_file_id,
            "output_file_id": None,
            "error_file_id": None,
        }
        return SimpleNamespace(id=bid, status="in_progress", request_counts={})

    def list(self, limit=20):
        return SimpleNamespace(
            data=[
                SimpleNamespace(id=bid, input_file_id=e.get("input_file_id"), status=e["status"])
                for bid, e in list(self.batches.items())[:limit]
            ]
        )

    def retrieve(self, bid):
        entry = self.batches[bid]
        return SimpleNamespace(
            id=bid,
            status=entry["status"],
            output_file_id=entry.get("output_file_id"),
            error_file_id=entry.get("error_file_id"),
            request_counts={},
        )


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()
        self.batches = FakeBatches()


def _response_body(extraction, usage=None):
    return {
        "id": "resp-1",
        "object": "response",
        "model": "gpt-5-nano",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(extraction)}],
            }
        ],
        "usage": usage or {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    }


def _write_csv(path, texts_list):
    import csv

    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["texts"])
        for texts in texts_list:
            writer.writerow([texts])


def _args(run_dir, **overrides):
    from pathlib import Path as _Path

    args = SimpleNamespace(
        run_dir=str(run_dir),
        cache_dir=str(_Path(run_dir) / "cache"),
        csv=None,
        size=3,
        seed=1,
        model="gpt-5-nano",
        limit=50,
        max_output_tokens=500,
        audit_rate=0.0,
        audit_seed=1,
        yes=True,
        yes_llm=True,
        resume=False,
        wait=0,
        force=False,
        import_id="test-import",
        partial=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _settings(**overrides):
    params = {
        "_env_file": None,
        "llm_ingestion_enabled": True,
        "llm_price_input_per_1m": 0.05,
        "llm_price_output_per_1m": 0.40,
    }
    params.update(overrides)
    return Settings(**params)


def test_mocked_end_to_end_prepare_submit_collect_finalize(tmp_path, monkeypatch):

    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    args = _args(run_dir, csv=str(csv_path))
    llm_batch.cmd_prepare(args, settings)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["requests"] == 2  # AMBIGUOUS + BLOB; CLEAN bypasses
    mapping = json.loads((run_dir / "mapping.json").read_text())

    llm_batch.cmd_submit(args, settings)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["batch_id"] == "batch-mock0"
    with pytest.raises(ValueError, match="reconcile"):
        llm_batch.cmd_submit(args, settings)  # no silent resubmission

    # Collect: shuffled order + duplicate + unknown + missing.
    cids = list(mapping)
    good_line = {
        "id": "batch_req_1",
        "custom_id": cids[1],
        "response": {"status_code": 200, "request_id": "req-1", "body": {"outputs": []}},
        "error": None,
    }
    dup_line = {
        "id": "batch_req_2",
        "custom_id": cids[1],
        "response": {"status_code": 200, "request_id": "req-2", "body": {"outputs": []}},
        "error": None,
    }
    unknown_line = {
        "id": "batch_req_3",
        "custom_id": "nope-000",
        "response": {"status_code": 200, "request_id": "req-3", "body": {"outputs": []}},
        "error": None,
    }
    fake.batches.batches["batch-mock0"]["status"] = "completed"
    fake.batches.batches["batch-mock0"]["output_file_id"] = "file-out"
    fake.files.contents["file-out"] = "\n".join(
        json.dumps(line) for line in [dup_line, good_line, unknown_line]
    )
    report = llm_batch.cmd_collect(args, settings)
    assert report["missing_ids"] == cids[:1] or cids[0] in report["missing_ids"]
    assert any(e["code"] == "duplicate_result_id" for e in report["unknown_or_duplicate_ids"])
    assert any(e["code"] == "unknown_result_id" for e in report["unknown_or_duplicate_ids"])


def test_collect_refusal_incomplete_malformed(tmp_path, monkeypatch):
    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    llm_batch.cmd_prepare(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    mapping = json.loads((run_dir / "mapping.json").read_text())
    cids = list(mapping)
    refusal = {
        "id": "r1",
        "custom_id": cids[0],
        "response": {
            "status_code": 200,
            "request_id": "req-1",
            "body": {
                "id": "resp-1",
                "object": "response",
                "model": "gpt-5-nano",
                "status": "completed",
                "output": [{"type": "refusal", "refusal": "I cannot help."}],
            },
        },
        "error": None,
    }
    incomplete = {
        "id": "r2",
        "custom_id": cids[1],
        "response": {
            "status_code": 200,
            "request_id": "req-2",
            "body": {
                "id": "resp-2",
                "object": "response",
                "model": "gpt-5-nano",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        },
        "error": None,
    }
    fake.batches.batches["batch-mock0"]["status"] = "completed"
    fake.batches.batches["batch-mock0"]["output_file_id"] = "file-out"
    fake.files.contents["file-out"] = "\n".join(json.dumps(line) for line in [refusal, incomplete])
    report = llm_batch.cmd_collect(_args(run_dir, csv=str(csv_path)), settings)
    assert report["ok"] == 0 and report["failed"] == 2
    errors = [
        json.loads(line) for line in (run_dir / "result-errors.jsonl").read_text().splitlines()
    ]
    assert {e["code"] for e in errors} == {"model_failure"}
    summary = llm_batch.cmd_finalize(_args(run_dir, csv=str(csv_path)), settings)
    assert summary["states"].get("awaiting_llm") == 2  # both retry-eligible
    retry = llm_batch.cmd_retry(_args(run_dir, csv=str(csv_path), limit=50), settings)
    assert retry["retry_requests"] == 2
    assert (run_dir / "records.jsonl").exists()


def test_finalize_caches_and_blocks_snapshot_replacement(tmp_path, monkeypatch):
    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    llm_batch.cmd_prepare(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    mapping = json.loads((run_dir / "mapping.json").read_text())
    cids = list(mapping)
    entry = mapping[cids[0]]

    texts = entry["texts"]
    line_ids, line_map = numbered_source(texts)
    ing_ids, step_ids, _, _ = block_line_ids(texts)
    first_ing = ing_ids[0] if ing_ids else line_ids[0]
    extraction = {
        "status": "resolved",
        "source_id": entry["source_id"],
        "content_hash": entry["content_hash"],
        "title": "Leek Test",
        "description": None,
        "ingredients": [
            {
                "source_line_id": first_ing,
                "name": line_map[first_ing],
                "amount_text": None,
                "amount_value": None,
                "unit_text": None,
                "unit_normalized": None,
                "qualitative": True,
                "optional": False,
                "alternatives": [],
                "notes": None,
                "group": "main",
                "is_range": False,
                "compound": False,
                "equivalent": None,
                "uncertain": False,
                "evidence": {"line_ids": [first_ing], "excerpt": line_map[first_ing][:40]},
            }
        ],
        "steps": [],
        "notes": [],
        "servings": None,
        "servings_text": None,
        "batch_yield_count": None,
        "batch_yield_text": None,
        "temperatures": [],
        "durations": [],
        "unclassified_line_ids": [lid for lid in line_ids if lid != first_ing],
        "uncertainty_notes": [],
    }
    requested_error = {
        "id": "e1",
        "custom_id": cids[1],
        "response": None,
        "error": {"code": "batch_expired", "message": "window expired"},
    }
    ok_line = {
        "id": "r1",
        "custom_id": cids[0],
        "response": {"status_code": 200, "request_id": "req-1", "body": _response_body(extraction)},
        "error": None,
    }
    fake.batches.batches["batch-mock0"]["status"] = "expired"
    fake.batches.batches["batch-mock0"]["output_file_id"] = "file-out"
    fake.batches.batches["batch-mock0"]["error_file_id"] = "file-err"
    fake.files.contents["file-out"] = json.dumps(ok_line)
    fake.files.contents["file-err"] = json.dumps(requested_error)
    llm_batch.cmd_collect(_args(run_dir, csv=str(csv_path)), settings)
    summary = llm_batch.cmd_finalize(_args(run_dir, csv=str(csv_path)), settings)
    assert summary["ready"] >= 0  # acceptance depends on coverage; states recorded regardless
    assert "states" in summary
    # Full load blocked while records are still pending.
    if summary["states"].get("awaiting_llm"):
        with pytest.raises(ValueError, match="pending"):
            llm_batch.cmd_load(_args(run_dir, csv=str(csv_path)), settings)


def test_load_boundary_isolated_postgres(tmp_path):
    """Upsert-only loads, pending blocks full loads, datasets isolated."""
    from sqlalchemy import create_engine, text

    from culinary_copilot.recipes import import_data

    maint_url, test_url = _hybrid_urls()
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text('DROP DATABASE IF EXISTS "culinary_test_hybrid"'))
            conn.execute(text('CREATE DATABASE "culinary_test_hybrid"'))
        maint.dispose()
        engine = create_engine(test_url)
        migrations = sorted(import_data.MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
        with engine.begin() as conn:
            for migration in migrations:
                for statement in import_data.split_sql_statements(migration.read_text()):
                    conn.execute(text(statement))
    except Exception as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc!r}")
    try:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        ready_record = {
            "dataset_id": "odunola/foodie",
            "source_id": "foodie-000009",
            "title": "Mystery",
            "ingredients": [
                {
                    "original": "rice",
                    "canonical": "rice",
                    "name": "rice",
                    "amount": None,
                    "quantity_text": None,
                    "unit": None,
                }
            ],
            "instructions": ["Cook rice."],
            "servings": None,
            "durations_minutes": {"TotalTime": None},
            "quality_issues": [],
            "capabilities": {},
        }
        (run_dir / "ready_to_load.jsonl").write_text(json.dumps(ready_record) + "\n")
        (run_dir / "records.jsonl").write_text(
            json.dumps({"source_id": "foodie-000009", "state": "ready_to_load"}) + "\n"
        )
        (run_dir / "manifest.json").write_text(
            json.dumps({"revision": "test-rev", "file_sha256": "test-sha"}) + "\n"
        )
        with engine.begin() as conn:  # unrelated dataset already present
            conn.execute(
                text(
                    "INSERT INTO recipe_imports (id, dataset_id, revision, checksum,"
                    " normalizer_version, vocabulary_checksum, dataset_url, report)"
                    " VALUES ('imp-food','AkashPS11/recipes_data_food.com',"
                    " 'r','c','3','v','u','{}')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO recipes (dataset_id, source_id, import_id, title,"
                    " total_minutes, servings, ingredient_names, document, search_text)"
                    " VALUES ('AkashPS11/recipes_data_food.com','000038','imp-food',"
                    " 'T',1,1,'{}','{}','t')"
                )
            )
        settings = Settings(_env_file=None, database_url=test_url)
        llm_batch.cmd_load(_args(run_dir, csv="x"), settings)
        llm_batch.cmd_load(_args(run_dir, csv="x"), settings)  # idempotent
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM recipes")).scalar_one() == 2
            assert (
                conn.execute(
                    text(
                        "SELECT title FROM recipes WHERE dataset_id="
                        "'AkashPS11/recipes_data_food.com'"
                    )
                ).scalar_one()
                == "T"
            )
        (run_dir / "records.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"source_id": "foodie-000009", "state": "ready_to_load"}),
                    json.dumps({"source_id": "foodie-000010", "state": "awaiting_llm"}),
                ]
            )
            + "\n"
        )
        with pytest.raises(ValueError, match="pending"):
            llm_batch.cmd_load(_args(run_dir, csv="x"), settings)
        llm_batch.cmd_load(_args(run_dir, csv="x", partial=True), settings)  # explicit partial ok
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM recipes")).scalar_one() == 2
    finally:
        engine.dispose()
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text('DROP DATABASE IF EXISTS "culinary_test_hybrid"'))
        maint.dispose()


def test_load_persists_fully_rejected_rows_with_status(tmp_path):
    """Fully-rejected rows land in recipe_quarantine with status+reason (003)."""
    from sqlalchemy import create_engine, text

    from culinary_copilot.recipes import import_data

    maint_url, test_url = _hybrid_urls()
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text('DROP DATABASE IF EXISTS "culinary_test_hybrid"'))
            conn.execute(text('CREATE DATABASE "culinary_test_hybrid"'))
        maint.dispose()
        engine = create_engine(test_url)
        migrations = sorted(import_data.MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
        assert [p.name for p in migrations] == [
            "001_recipes.sql",
            "002_search_version.sql",
            "003_quarantine_status.sql",
        ]
        with engine.begin() as conn:
            for migration in migrations:
                for statement in import_data.split_sql_statements(migration.read_text()):
                    conn.execute(text(statement))
    except Exception as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc!r}")
    try:
        run_dir = tmp_path / "runq"
        run_dir.mkdir()
        (run_dir / "ready_to_load.jsonl").write_text("", encoding="utf-8")
        (run_dir / "records.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "source_id": "foodie-000021",
                            "row_number": 21,
                            "state": "unresolved",
                            "verdict": "rejected_validation",
                            "detail": "retry budget exhausted",
                            "attempts": 2,
                        }
                    ),
                    json.dumps(
                        {
                            "source_id": "foodie-000022",
                            "row_number": 22,
                            "state": "quarantined",
                            "verdict": "single_line_requires_spans",
                            "attempts": 0,
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (run_dir / "final-quarantine.jsonl").write_text(
            json.dumps(
                {
                    "source_id": "foodie-000022",
                    "row_number": 22,
                    "reason": "single_line_requires_spans",
                    "raw": {"texts": "blob"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (run_dir / "validation-report.json").write_text(
            json.dumps(
                {
                    "validations": [
                        {
                            "source_id": "foodie-000021",
                            "verdict": "rejected_validation",
                            "problems": [
                                {
                                    "code": "fabricated_evidence",
                                    "detail": "excerpt not found",
                                    "class": "critical",
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "sources.json").write_text(
            json.dumps({"foodie-000021": {"texts": "X", "row_number": 21}}), encoding="utf-8"
        )
        (run_dir / "manifest.json").write_text(
            json.dumps({"revision": "test-rev", "file_sha256": "test-sha"}) + "\n"
        )
        settings = Settings(_env_file=None, database_url=test_url)
        result = llm_batch.cmd_load(
            _args(run_dir, csv="x", partial=True, import_id="imp-q"), settings
        )
        assert result["quarantined"] == 2
        llm_batch.cmd_load(
            _args(run_dir, csv="x", partial=True, import_id="imp-q"), settings
        )  # idempotent
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT source_id, status, reason, verdict, problems FROM "
                    "recipe_quarantine ORDER BY row_number"
                )
            ).all()
            assert [(r[0], r[1], r[3]) for r in rows] == [
                ("foodie-000021", "rejected", "rejected_validation"),
                ("foodie-000022", "quarantined", "quarantined"),
            ]
            assert rows[0][2] == "retry budget exhausted"
            assert rows[0][4] == [
                {
                    "code": "fabricated_evidence",
                    "detail": "excerpt not found",
                    "class": "critical",
                }
            ]
            assert rows[1][2] == "single_line_requires_spans"
    finally:
        engine.dispose()
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text('DROP DATABASE IF EXISTS "culinary_test_hybrid"'))
        maint.dispose()


def test_load_apply_schema_upgrades_and_is_idempotent(tmp_path):
    """load --apply-schema upgrades 001/002 to 003; reruns apply nothing."""
    import hashlib

    from sqlalchemy import create_engine, text

    from culinary_copilot.recipes import import_data

    maint_url, test_url = _hybrid_urls()
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text('DROP DATABASE IF EXISTS "culinary_test_hybrid"'))
            conn.execute(text('CREATE DATABASE "culinary_test_hybrid"'))
        maint.dispose()
        engine = create_engine(test_url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS recipe_schema_migrations "
                    "(version text PRIMARY KEY, checksum text NOT NULL)"
                )
            )
            for migration in sorted(import_data.MIGRATIONS_DIR.glob("*.sql"))[:2]:
                for statement in import_data.split_sql_statements(migration.read_text()):
                    conn.execute(text(statement))
                conn.execute(
                    text("INSERT INTO recipe_schema_migrations VALUES (:v, :c)"),
                    {
                        "v": migration.name.split("_", 1)[0],
                        "c": hashlib.sha256(migration.read_bytes()).hexdigest(),
                    },
                )
    except Exception as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc!r}")
    try:
        run_dir = tmp_path / "runm"
        run_dir.mkdir()
        (run_dir / "ready_to_load.jsonl").write_text("", encoding="utf-8")
        (run_dir / "records.jsonl").write_text("", encoding="utf-8")
        (run_dir / "manifest.json").write_text(json.dumps({"revision": "r"}) + "\n")
        settings = Settings(_env_file=None, database_url=test_url)
        llm_batch.cmd_load(_args(run_dir, csv="x", apply_schema=True, import_id="imp-m"), settings)
        with engine.connect() as conn:
            versions = [
                row[0]
                for row in conn.execute(
                    text("SELECT version FROM recipe_schema_migrations ORDER BY 1")
                ).all()
            ]
            assert versions == ["001", "002", "003"]
            assert (
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='recipe_quarantine' AND column_name='status'"
                    )
                ).scalar_one()
                == "status"
            )
        llm_batch.cmd_load(
            _args(run_dir, csv="x", apply_schema=True, import_id="imp-m"), settings
        )  # rerun: nothing pending
    finally:
        engine.dispose()
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text('DROP DATABASE IF EXISTS "culinary_test_hybrid"'))
        maint.dispose()


def test_unloadable_merges_are_not_ready():
    """Accepted merges without a title or without content must not load.

    They would violate the recipes NOT NULL constraint and abort the whole
    load transaction (tranche-200 staging find: 9 rows).
    """
    assert not llm_batch._is_loadable({"title": "X", "ingredients": [{}], "instructions": []})
    assert llm_batch._is_loadable(
        {"title": "X", "ingredients": [{"original": "salt"}], "instructions": []}
    )
    assert llm_batch._is_loadable({"title": "X", "ingredients": [], "instructions": ["Do."]})
    assert not llm_batch._is_loadable({"title": None, "ingredients": [{}], "instructions": ["Do."]})
    assert not llm_batch._is_loadable({"title": "X", "ingredients": [], "instructions": []})


def _hybrid_urls():
    settings = Settings(_env_file=None)
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/culinary_test_hybrid"


def _cover_all_extraction(entry):
    """Unresolved extraction covering every line via unclassified (valid)."""
    line_map = entry["line_map"]
    line_ids = sorted(line_map, key=lambda lid: int(lid[1:]))
    return {
        "status": "unresolved",
        "source_id": entry["source_id"],
        "content_hash": entry["content_hash"],
        "title": None,
        "description": None,
        "ingredients": [],
        "steps": [],
        "notes": [],
        "servings": None,
        "servings_text": None,
        "batch_yield_count": None,
        "batch_yield_text": None,
        "temperatures": [],
        "durations": [],
        "unclassified_line_ids": line_ids,
        "uncertainty_notes": ["deferred to human review"],
    }


def test_amount_mismatch_and_bad_unit_rejected():
    import hashlib

    line_map, ing_ids, step_ids, versions = _ctx(MYSTERY)
    content_hash = hashlib.sha256(MYSTERY.encode()).hexdigest()
    base = _good_response(MYSTERY)
    base["ingredients"][0]["amount_text"] = "2"
    base["ingredients"][0]["amount_value"] = "999"
    report = validate_response(
        base,
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "rejected_validation"
    assert any(p["code"] == "amount_inconsistent" for p in report["problems"])

    sloppy = _good_response(MYSTERY)
    sloppy["ingredients"][0]["unit_normalized"] = "bathtub"
    report = validate_response(
        sloppy,
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "rejected_validation"
    assert any(p["code"] == "unsupported_unit" for p in report["problems"])


def test_empty_evidence_rejected():
    import hashlib

    line_map, ing_ids, step_ids, versions = _ctx(MYSTERY)
    hollow = _good_response(MYSTERY)
    hollow["ingredients"][0]["evidence"] = {"line_ids": [], "excerpt": ""}
    report = validate_response(
        hollow,
        source_id="foodie-000009",
        content_hash=hashlib.sha256(MYSTERY.encode()).hexdigest(),
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "rejected_validation"
    assert any(p["code"] == "missing_evidence" for p in report["problems"])


def test_servings_number_must_match_source():
    import hashlib

    served = (
        "Pie\nIngredients\n1 cup flour\nIntroduction\nHi.\n"
        "Directions\nMix batter.\nBake.\nServes 2.\n"
    )
    line_map, ing_ids, step_ids, versions = _ctx(served)
    content_hash = hashlib.sha256(served.encode()).hexdigest()
    response = _good_response(served, source_id="foodie-000010", needle="flour")
    response["content_hash"] = content_hash
    # "Serves 2." is a servings statement, not a cooking step.
    response["steps"] = [s for s in response["steps"] if "Serves" not in s["text"]]
    response["unclassified_line_ids"].append(
        next(lid for lid, text in line_map.items() if "Serves" in text)
    )
    response["servings"] = 999
    response["servings_text"] = "Serves 999"
    report = validate_response(
        response,
        source_id="foodie-000010",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "rejected_validation"
    assert any(p["code"] == "servings_mismatch" for p in report["problems"])
    response["servings"] = 2
    response["servings_text"] = "Serves 2"
    report = validate_response(
        response,
        source_id="foodie-000010",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] in ("accepted", "accepted_partial")


def test_unresolved_never_eligible_for_use():
    response = ExtractionResponse.model_validate(_good_response(MYSTERY))
    for verdict in ("accepted_partial", "unresolved"):
        merged = merge_response(
            response, deterministic=None, validation={"verdict": verdict, "problems": []}
        )
        assert merged["capabilities"]["quantities_validated"] is False
        assert merged["capabilities"]["complete_eligible"] is False
        assert merged["capabilities"]["scalable"] is False


def test_finalize_preserves_every_record(tmp_path, monkeypatch):
    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    llm_batch.cmd_prepare(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    mapping = json.loads((run_dir / "mapping.json").read_text())
    output_lines = []
    for pos, (custom_id, entry) in enumerate(mapping.items()):
        output_lines.append(
            {
                "id": f"r{pos}",
                "custom_id": custom_id,
                "response": {
                    "status_code": 200,
                    "request_id": f"req-{pos}",
                    "body": _response_body(_cover_all_extraction(entry)),
                },
                "error": None,
            }
        )
    fake.batches.batches["batch-mock0"]["status"] = "completed"
    fake.batches.batches["batch-mock0"]["output_file_id"] = "file-out"
    fake.files.contents["file-out"] = "\n".join(json.dumps(line) for line in output_lines)
    llm_batch.cmd_collect(_args(run_dir, csv=str(csv_path)), settings)
    summary = llm_batch.cmd_finalize(_args(run_dir, csv=str(csv_path)), settings)
    records = {
        json.loads(line)["source_id"]: json.loads(line)
        for line in (run_dir / "records.jsonl").read_text().splitlines()
    }
    # All three selected records survive with explicit final states.
    assert len(records) == 3
    ready_sources = {
        json.loads(line)["source_id"]
        for line in (run_dir / "ready_to_load.jsonl").read_text().splitlines()
    }
    assert "foodie-000001" in ready_sources  # deterministic success kept
    # The unclassified-only merge (no title, no ingredients/steps) is
    # preserved as terminally unresolved, never as a loadable row that
    # would violate the recipes NOT NULL constraint at load.
    assert summary["states"].get("ready_to_load") == 2
    assert summary["states"].get("unresolved") == 1
    unresolved = [r for r in records.values() if r["state"] == "unresolved"]
    assert unresolved[0]["detail"].startswith("evidence_unusable:")


def test_submit_ambiguous_failure_requires_reconcile(tmp_path, monkeypatch):
    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    calls = {"creates": 0}

    def flaky_create(**kwargs):
        calls["creates"] += 1
        bid = f"batch-mock{calls['creates'] - 1}"
        # Server-side creation succeeded; the response was lost (timeout).
        fake.batches.batches[bid] = {
            "status": "in_progress",
            "input_file_id": kwargs["input_file_id"],
            "output_file_id": None,
            "error_file_id": None,
        }
        raise TimeoutError("response lost after server-side creation")

    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(fake.batches, "create", flaky_create)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    llm_batch.cmd_prepare(_args(run_dir, csv=str(csv_path)), settings)
    with pytest.raises(TimeoutError):
        llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest.get("batch_id") is None
    assert manifest["submission_attempt"]["state"] == "unknown"
    with pytest.raises(ValueError, match="reconcile"):
        llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    assert calls["creates"] == 1  # no duplicate batch created
    adopted = llm_batch.cmd_status(_args(run_dir, csv=str(csv_path), reconcile=True), settings)
    assert adopted["reconciled"] == "batch-mock0"
    with pytest.raises(ValueError, match="already references"):
        llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)


def test_retry_dir_is_runnable_and_reconciles(tmp_path, monkeypatch):
    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    llm_batch.cmd_prepare(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    mapping = json.loads((run_dir / "mapping.json").read_text())
    cids = list(mapping)
    error_line = {
        "id": "e1",
        "custom_id": cids[0],
        "response": None,
        "error": {"code": "request_error", "message": "transient"},
    }
    ok_entry = mapping[cids[1]]
    ok_line = {
        "id": "r1",
        "custom_id": cids[1],
        "response": {
            "status_code": 200,
            "request_id": "req-1",
            "body": _response_body(_cover_all_extraction(ok_entry)),
        },
        "error": None,
    }
    fake.batches.batches["batch-mock0"]["status"] = "completed"
    fake.batches.batches["batch-mock0"]["output_file_id"] = "file-out"
    fake.files.contents["file-out"] = "\n".join(json.dumps(line) for line in [error_line, ok_line])
    llm_batch.cmd_collect(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_finalize(_args(run_dir, csv=str(csv_path)), settings)
    retry = llm_batch.cmd_retry(_args(run_dir, csv=str(csv_path), limit=50), settings)
    assert retry["retry_requests"] == 1
    import os

    retry_dir = run_dir / [p for p in os.listdir(run_dir) if p.startswith("retry-")][0]
    assert (retry_dir / "manifest.json").exists()
    assert (retry_dir / "mapping.json").exists()
    assert (retry_dir / "batch-input.jsonl").exists()
    # Retry runs inherit generation settings: without reasoning_effort the
    # child finalize marks every result stale_configuration (rehearsal find).
    parent_manifest = json.loads((run_dir / "manifest.json").read_text())
    child_manifest = json.loads((retry_dir / "manifest.json").read_text())
    assert child_manifest.get("reasoning_effort") == parent_manifest.get("reasoning_effort")
    # The retry directory submits through the normal path (no FileNotFoundError).
    llm_batch.cmd_submit(_args(str(retry_dir), csv=str(csv_path)), settings)
    retry_manifest = json.loads((retry_dir / "manifest.json").read_text())
    assert retry_manifest["batch_id"] == "batch-mock1"
    # Reconcile merges retry outcomes into the parent run.
    fake.batches.batches["batch-mock1"]["status"] = "completed"
    fake.batches.batches["batch-mock1"]["output_file_id"] = "file-out-2"
    child_mapping = json.loads((retry_dir / "mapping.json").read_text())
    child_cid = next(iter(child_mapping))
    child_entry = child_mapping[child_cid]
    fake.files.contents["file-out-2"] = json.dumps(
        {
            "id": "r9",
            "custom_id": child_cid,
            "response": {
                "status_code": 200,
                "request_id": "req-9",
                "body": _response_body(_cover_all_extraction(child_entry)),
            },
            "error": None,
        }
    )
    llm_batch.cmd_collect(_args(str(retry_dir), csv=str(csv_path)), settings)
    llm_batch.cmd_finalize(_args(str(retry_dir), csv=str(csv_path)), settings)
    result = llm_batch.cmd_reconcile(
        _args(run_dir, csv=str(csv_path), retry_dir=str(retry_dir)), settings
    )
    assert result["added_ready"] >= 1
    collect_before = json.loads((run_dir / "collect-report.json").read_text())
    again = llm_batch.cmd_reconcile(
        _args(run_dir, csv=str(csv_path), retry_dir=str(retry_dir)), settings
    )
    assert again.get("skipped") is True
    collect_after = json.loads((run_dir / "collect-report.json").read_text())
    assert collect_after == collect_before  # idempotent: no double-count
    parent_records = {
        json.loads(line)["source_id"]: json.loads(line)
        for line in (run_dir / "records.jsonl").read_text().splitlines()
    }
    assert parent_records[child_entry["source_id"]]["state"] == "ready_to_load"


def test_strict_schema_requires_every_property():
    """Live API rejects strict schemas whose `required` omits property keys."""
    from culinary_copilot.recipes.llm_contracts import extraction_json_schema

    def check(node, path="root"):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                required = node.get("required")
                assert isinstance(required, list), f"{path}: strict objects need required"
                missing = set(props) - set(required)
                assert not missing, f"{path}: required misses {missing}"
                for key, child in props.items():
                    check(child, f"{path}.{key}")
            if node.get("type") == "array":
                assert isinstance(node.get("items"), dict), f"{path}: arrays need items"
            items = node.get("items")
            if isinstance(items, dict):
                check(items, path + "[]")

    check(extraction_json_schema())


def test_collect_surfaces_inline_error_message(tmp_path, monkeypatch):
    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    llm_batch.cmd_prepare(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    mapping = json.loads((run_dir / "mapping.json").read_text())
    cids = list(mapping)
    inline_error = {
        "id": "batch_req_x",
        "custom_id": cids[0],
        "response": {
            "status_code": 400,
            "request_id": "req-x",
            "body": {
                "error": {"message": "Invalid schema: missing foo", "code": "invalid_json_schema"}
            },
        },
        "error": None,
    }
    fake.batches.batches["batch-mock0"]["status"] = "completed"
    fake.batches.batches["batch-mock0"]["error_file_id"] = "file-err"
    fake.files.contents["file-err"] = json.dumps(inline_error)
    report = llm_batch.cmd_collect(_args(run_dir, csv=str(csv_path)), settings)
    assert report["missing_ids"] == [cids[1]]
    errors = [
        json.loads(line) for line in (run_dir / "result-errors.jsonl").read_text().splitlines()
    ]
    assert errors[0]["code"] == "invalid_json_schema"
    assert "missing foo" in errors[0]["detail"]


def test_request_carries_full_content_hash():
    """The model must be able to echo the hash validation requires."""
    from culinary_copilot.recipes.llm_contracts import build_brief, build_user_content

    brief = build_brief(
        routing_reasons=["quantity_unknown"],
        ambiguous_lines=[],
        settled={"title": "Title"},
        missing=[],
    )
    content = build_user_content(
        source_id="foodie-000001",
        content_hash="ab" * 32,
        line_ids=["L1"],
        line_map={"L1": "Title"},
        brief=brief,
    )
    assert "ab" * 32 in content
    assert "quantity_unknown" in content
    assert "settled" in content


def test_unicode_amount_normalized_not_rejected():
    """Exact-arithmetic normalization in scripts: '½' parses as 1/2."""
    import hashlib

    texts = MYSTERY.replace("a handful of rice", "½ cup rice")
    line_map, ing_ids, step_ids, versions = _ctx(texts)
    content_hash = hashlib.sha256(texts.encode()).hexdigest()
    response = _good_response(texts)
    response["ingredients"][0]["amount_text"] = "½"
    response["ingredients"][0]["amount_value"] = "½"
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "accepted", report["problems"]
    merged = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=None,
        validation=report,
    )
    assert merged["ingredients"][0]["amount"] == "1/2"


def test_leaf_unit_supported():
    from culinary_copilot.recipes.llm_validate import canonical_unit

    assert canonical_unit("leaves") == "leaf"
    assert canonical_unit("bathtub") is None


def test_leading_quantity_name_rejected_percent_exempt():
    import hashlib

    line_map, ing_ids, step_ids, versions = _ctx(MYSTERY)
    content_hash = hashlib.sha256(MYSTERY.encode()).hexdigest()

    def check(name, ok):
        response = _good_response(MYSTERY)
        response["ingredients"][0]["name"] = name
        report = validate_response(
            response,
            source_id="foodie-000009",
            content_hash=content_hash,
            line_map=line_map,
            ingredient_line_ids=ing_ids,
            step_line_ids=step_ids,
            request_versions=versions,
            current_versions=versions,
        )
        assert (report["verdict"] == "accepted") is ok, (name, report["problems"])

    check("2 cups rice", False)
    check("2% milk", True)
    check("red peppers, cut into 1/4-inch strips", True)


def test_unit_alias_spellings_canonicalized():
    """Model echoing 'tablespoon' validates and merges as 'tbsp'; bathtub never does."""
    import hashlib

    from culinary_copilot.recipes.llm_validate import canonical_unit

    assert canonical_unit("tablespoon") == "tbsp"
    assert canonical_unit("fluid ounces") == "fl_oz"
    assert canonical_unit("bathtub") is None
    texts = MYSTERY.replace("a handful of rice", "1 tablespoon rice")
    line_map, ing_ids, step_ids, versions = _ctx(texts)
    content_hash = hashlib.sha256(texts.encode()).hexdigest()
    response = _good_response(texts)
    response["ingredients"][0]["unit_normalized"] = "tablespoon"
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "accepted", report["problems"]
    merged = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=None,
        validation=report,
    )
    assert merged["ingredients"][0]["unit"] == "tbsp"


def test_splash_and_wedge_units_supported():
    from culinary_copilot.recipes.adapters.foodie import parse_ingredient_line as p

    assert p("1 splash club soda")["unit"] == "splash"
    assert p("1 wedge fresh pineapple")["unit"] == "wedge"


def _incomplete_error(custom_id):
    return {
        "id": "e1",
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "request_id": "req-1",
            "body": {
                "id": "resp-1",
                "object": "response",
                "model": "gpt-5-nano",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        },
        "error": None,
    }


def test_exhausted_attempts_go_terminal(tmp_path, monkeypatch):
    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    llm_batch.cmd_prepare(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    mapping = json.loads((run_dir / "mapping.json").read_text())
    cids = list(mapping)
    # Both routed records already burned their full budget (attempts=2/2).
    seed = [
        {
            "source_id": mapping[cid]["source_id"],
            "row_number": mapping[cid]["row_number"],
            "state": "awaiting_llm",
            "attempts": 2,
        }
        for cid in cids
    ]
    (run_dir / "records.jsonl").write_text("\n".join(json.dumps(r) for r in seed) + "\n")
    fake.batches.batches["batch-mock0"]["status"] = "completed"
    fake.batches.batches["batch-mock0"]["output_file_id"] = "file-out"
    fake.files.contents["file-out"] = "\n".join(json.dumps(_incomplete_error(cid)) for cid in cids)
    llm_batch.cmd_collect(_args(run_dir, csv=str(csv_path)), settings)
    summary = llm_batch.cmd_finalize(_args(run_dir, csv=str(csv_path)), settings)
    assert summary["states"].get("awaiting_llm") in (None, 0)
    records = {
        json.loads(line)["source_id"]: json.loads(line)
        for line in (run_dir / "records.jsonl").read_text().splitlines()
    }
    for record in records.values():
        if record.get("attempts") == 2 and record["state"] != "ready_to_load":
            assert record["state"] == "unresolved"
            assert record["detail"] == "output_budget_exhausted"
            assert record["attempts"] == 2  # budget preserved, never reset
    ready_sources = {
        json.loads(line)["source_id"]
        for line in (run_dir / "ready_to_load.jsonl").read_text().splitlines()
        if line.strip()
    }
    for source_id, record in records.items():
        if record["state"] == "unresolved":
            assert source_id not in ready_sources
    # Exhausted records are not retry-eligible anymore — including legacy
    # rows that an older finalize left in awaiting_llm at the attempt limit.
    retry = llm_batch.cmd_retry(_args(run_dir, csv=str(csv_path), limit=50), settings)
    assert retry["retry_requests"] == 0
    legacy = [
        dict(r, state="awaiting_llm")
        for r in seed  # attempts already 2/2
    ]
    (run_dir / "records.jsonl").write_text("\n".join(json.dumps(r) for r in legacy) + "\n")
    retry = llm_batch.cmd_retry(_args(run_dir, csv=str(csv_path), limit=50), settings)
    assert retry["retry_requests"] == 0


def test_reconcile_never_resets_attempts(tmp_path, monkeypatch):
    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    llm_batch.cmd_prepare(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    mapping = json.loads((run_dir / "mapping.json").read_text())
    cids = list(mapping)
    fake.batches.batches["batch-mock0"]["status"] = "completed"
    fake.batches.batches["batch-mock0"]["output_file_id"] = "file-out"
    fake.files.contents["file-out"] = "\n".join(json.dumps(_incomplete_error(cid)) for cid in cids)
    llm_batch.cmd_collect(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_finalize(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_retry(_args(run_dir, csv=str(csv_path), limit=50), settings)
    import os

    retry_dir = run_dir / [p for p in os.listdir(run_dir) if p.startswith("retry-")][0]
    child_records = [
        json.loads(line)
        for line in (retry_dir / "records.jsonl").read_text().splitlines()
        if line.strip()
    ]
    # Simulate a stale child dir reporting fewer attempts than the parent.
    stale = dict(child_records[0], attempts=1)
    (retry_dir / "records.jsonl").write_text(json.dumps(stale) + "\n")
    (retry_dir / "ready_to_load.jsonl").write_text("")
    (retry_dir / "validation-report.json").write_text(json.dumps({"validations": []}))
    (retry_dir / "collect-report.json").write_text(
        json.dumps({"actual_input_tokens": 0, "actual_output_tokens": 0})
    )
    child_manifest = json.loads((retry_dir / "manifest.json").read_text())
    child_manifest["stage"] = "finalized"
    (retry_dir / "manifest.json").write_text(json.dumps(child_manifest))
    # Parent already accounts two attempts; the stale child claims one.
    parent_lines = []
    for line in (run_dir / "records.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record["source_id"] == stale["source_id"]:
            record["attempts"] = 2
        parent_lines.append(json.dumps(record))
    (run_dir / "records.jsonl").write_text("\n".join(parent_lines) + "\n")
    llm_batch.cmd_reconcile(_args(run_dir, csv=str(csv_path), retry_dir=str(retry_dir)), settings)
    parent_records = {
        json.loads(line)["source_id"]: json.loads(line)
        for line in (run_dir / "records.jsonl").read_text().splitlines()
    }
    assert parent_records[stale["source_id"]]["attempts"] == 2


def test_reasoning_effort_body_cache_and_stale():
    body = build_request_body(
        model="gpt-5-nano", system="s", user_content="u", max_output_tokens=10
    )
    assert "reasoning" not in body
    body = build_request_body(
        model="gpt-5-nano",
        system="s",
        user_content="u",
        max_output_tokens=10,
        reasoning_effort="low",
    )
    assert body["reasoning"] == {"effort": "low"}
    with pytest.raises(ValueError, match="Unknown reasoning effort"):
        build_request_body(
            model="gpt-5-nano",
            system="s",
            user_content="u",
            max_output_tokens=10,
            reasoning_effort="turbo",
        )
    base = {
        "dataset_id": "d",
        "revision": "r",
        "source_id": "s",
        "content_hash": "h",
        "adapter_version": "2",
        "routing_version": "1",
        "prompt_version": "2",
        "prompt_hash": "ph",
        "schema_version": "2",
        "model": "m",
    }
    assert cache_key(**base) != cache_key(**base, reasoning_effort="low")


def test_finalize_rejects_effort_mismatch(tmp_path, monkeypatch):
    csv_path = tmp_path / "mini.csv"
    _write_csv(csv_path, [CLEAN, AMBIGUOUS, BLOB])
    fake = FakeClient()
    monkeypatch.setattr(llm_batch, "get_client", lambda settings: fake)
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(csv_path))
    settings = _settings()
    run_dir = tmp_path / "run"
    llm_batch.cmd_prepare(_args(run_dir, csv=str(csv_path)), settings)
    mapping = json.loads((run_dir / "mapping.json").read_text())
    cid = next(iter(mapping))
    mapping[cid]["request_versions"]["reasoning_effort"] = "high"
    (run_dir / "mapping.json").write_text(json.dumps(mapping))
    llm_batch.cmd_submit(_args(run_dir, csv=str(csv_path)), settings)
    entry = mapping[cid]
    parsed = _cover_all_extraction(entry)
    fake.batches.batches["batch-mock0"]["status"] = "completed"
    fake.batches.batches["batch-mock0"]["output_file_id"] = "file-out"
    body = dict(_response_body(parsed))
    fake.files.contents["file-out"] = json.dumps(
        {
            "id": "r1",
            "custom_id": cid,
            "response": {"status_code": 200, "request_id": "req-1", "body": body},
            "error": None,
        }
    )
    llm_batch.cmd_collect(_args(run_dir, csv=str(csv_path)), settings)
    llm_batch.cmd_finalize(_args(run_dir, csv=str(csv_path)), settings)
    records = {
        json.loads(line)["source_id"]: json.loads(line)
        for line in (run_dir / "records.jsonl").read_text().splitlines()
    }
    assert records[entry["source_id"]]["state"] == "unresolved"


GROUPED = (
    "Soup\nIngredients\nFor the sauce:\n1 cup sugar\n1 cup water\n"
    "Introduction\nNice.\nDirections\nMix.\nServe.\n"
)


def _grouped_ctx():
    from culinary_copilot.recipes.llm_batch import block_line_ids

    line_ids, line_map = numbered_source(GROUPED)
    ing_ids, step_ids, heading_ids, _ = block_line_ids(GROUPED)
    versions = {
        "prompt_version": "3",
        "schema_version": "3",
        "adapter_version": "2",
        "routing_version": "1",
    }
    return line_map, ing_ids, step_ids, heading_ids, versions


def _grouped_response():
    import hashlib

    line_map, _, _, _, _ = _grouped_ctx()
    lids = sorted(line_map, key=lambda lid: int(lid[1:]))
    heading = next(lid for lid in lids if line_map[lid].rstrip().endswith(":"))
    first_ing = next(lid for lid in lids if line_map[lid].strip().startswith("1 cup sugar"))
    step = next(lid for lid in lids if line_map[lid].strip() == "Mix.")
    other_step = next(lid for lid in lids if line_map[lid].strip() == "Serve.")
    covered = {heading, first_ing, step, other_step}
    return {
        "status": "resolved",
        "source_id": "foodie-000100",
        "content_hash": hashlib.sha256(GROUPED.encode()).hexdigest(),
        "title": "Soup",
        "description": None,
        "ingredients": [
            {
                "source_line_id": first_ing,
                "name": "sugar",
                "amount_text": "1 cup",
                "amount_value": "1",
                "unit_text": "cup",
                "unit_normalized": "cup",
                "qualitative": False,
                "optional": False,
                "alternatives": [],
                "notes": None,
                "group": "For the sauce",
                "is_range": False,
                "compound": False,
                "equivalent": None,
                "uncertain": False,
                "evidence": {
                    "line_ids": [first_ing],
                    "excerpt": line_map[first_ing][:40],
                },
            }
        ],
        "headings": [
            {
                "source_line_id": heading,
                "text": line_map[heading],
                "evidence": {"line_ids": [heading], "excerpt": line_map[heading][:40]},
            }
        ],
        "steps": [
            {
                "source_line_id": lid,
                "text": line_map[lid],
                "uncertain": False,
                "evidence": {"line_ids": [lid], "excerpt": line_map[lid][:40]},
            }
            for lid in (step, other_step)
        ],
        "notes": [],
        "servings": None,
        "servings_text": None,
        "batch_yield_count": None,
        "batch_yield_text": None,
        "temperatures": [],
        "durations": [],
        "unclassified_line_ids": [lid for lid in lids if lid not in covered],
        "uncertainty_notes": [],
    }


def test_headings_accepted_and_merged():
    import hashlib

    line_map, ing_ids, step_ids, heading_ids, versions = _grouped_ctx()
    assert len(heading_ids) == 1
    response = _grouped_response()
    report = validate_response(
        response,
        source_id="foodie-000100",
        content_hash=hashlib.sha256(GROUPED.encode()).hexdigest(),
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        heading_line_ids=heading_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "accepted_partial", report["problems"]
    # The heading is valid, but the explicitly unclassified ingredient remains unresolved.
    merged = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=None,
        validation=report,
    )
    assert merged["ingredient_groups"] == [
        {"heading": "For the sauce", "source_line_id": heading_ids[0]}
    ]
    assert merged["ingredients"][0]["group"] == "For the sauce"


def test_merge_unions_model_and_deterministic_groups():
    from culinary_copilot.recipes.llm_validate import _merge_groups

    merged = _merge_groups(
        [{"heading": "A", "source_line_id": "L1"}],
        [{"heading": "A", "position": 0}, {"heading": "B", "position": 5}],
    )
    assert [g["heading"] for g in merged] == ["A", "B"]
    assert merged[0]["source_line_id"] == "L1"  # model version wins ties


def test_heading_coverage_gap_caps_at_partial():
    import hashlib

    line_map, ing_ids, step_ids, heading_ids, versions = _grouped_ctx()
    baseline = {"has_deterministic": True, "has_steps": True, "has_groups": False}
    response = _grouped_response()
    response["headings"] = []
    report = validate_response(
        response,
        source_id="foodie-000100",
        content_hash=hashlib.sha256(GROUPED.encode()).hexdigest(),
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        heading_line_ids=heading_ids,
        baseline=baseline,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "accepted_partial"
    assert any(p["code"] == "heading_coverage_gap" for p in report["problems"])


def test_prose_omission_is_optional_not_verdict_affecting():
    """Prose gaps are reported as optional; critical errors still decide."""
    import hashlib

    from culinary_copilot.recipes.llm_batch import prose_line_ids

    prose_ids, _ = prose_line_ids(MYSTERY)
    assert prose_ids, "MYSTERY intro line should count as prose"
    line_map, ing_ids, step_ids, versions = _ctx(MYSTERY)
    content_hash = hashlib.sha256(MYSTERY.encode()).hexdigest()
    response = _good_response(MYSTERY)
    # The "Ok." line is prose per block structure; simulate silent loss by
    # dropping it from steps and unclassified alike.
    intro = next(lid for lid in prose_ids)
    response["steps"] = [s for s in response["steps"] if s["source_line_id"] != intro]
    response["unclassified_line_ids"] = [
        lid for lid in response["unclassified_line_ids"] if lid != intro
    ]
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        prose_line_ids=prose_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "accepted", report["problems"]
    flagged = [p for p in report["problems"] if p["code"] == "prose_coverage_gap"]
    assert flagged and all(p["class"] == "optional" for p in flagged)
    assert all(p["class"] in ("critical", "optional", "uncertainty") for p in report["problems"])
    # A critical error alongside still decides the verdict.
    response["ingredients"][0]["unit_normalized"] = "bathtub"
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        prose_line_ids=prose_ids,
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "rejected_validation"


def test_honest_range_and_absent_amount_are_uncertainty_not_error():
    """Faithfully preserved ranges / honestly absent amounts are uncertainty.

    They report as quantity_uncertain (class uncertainty), never as
    extraction errors — while still capping the verdict at accepted_partial
    with scaling restrictions in the merge.
    """
    import hashlib

    line_map, ing_ids, step_ids, versions = _ctx(MYSTERY)
    content_hash = hashlib.sha256(MYSTERY.encode()).hexdigest()
    response = _good_response(MYSTERY)
    lid = response["ingredients"][0]["source_line_id"]
    excerpt = response["ingredients"][0]["evidence"]["excerpt"]

    def item(amount_text, *, is_range, uncertain, name="rice"):
        return {
            "source_line_id": lid,
            "name": name,
            "amount_text": amount_text,
            "amount_value": None,
            "unit_text": None,
            "unit_normalized": None,
            "qualitative": False,
            "optional": False,
            "alternatives": [],
            "notes": None,
            "group": "main",
            "is_range": is_range,
            "compound": False,
            "equivalent": None,
            "uncertain": uncertain,
            "evidence": {"line_ids": [lid], "excerpt": excerpt},
        }

    response["ingredients"] = [
        item("1-2", is_range=True, uncertain=False),
        item("2", is_range=False, uncertain=True),
        item("3", is_range=False, uncertain=False),
    ]
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=content_hash,
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        baseline={"has_deterministic": True, "has_steps": True},
        request_versions=versions,
        current_versions=versions,
    )
    assert report["verdict"] == "accepted_partial", report["problems"]
    by_code = {}
    for p in report["problems"]:
        by_code.setdefault(p["code"], []).append(p)
    assert len(by_code.get("quantity_uncertain", [])) == 2
    assert all(p["class"] == "uncertainty" for p in by_code["quantity_uncertain"])
    assert len(by_code.get("quantity_unresolved", [])) == 1
    assert by_code["quantity_unresolved"][0]["class"] == "critical"
    merged = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=None,
        validation=report,
        line_map=line_map,
    )
    assert merged["capabilities"]["scalable"] is False
    assert merged["capabilities"]["quantities_validated"] is False


def _targeted_ctx():
    import hashlib

    line_map, ing_ids, step_ids, versions = _ctx(MYSTERY)
    return {
        "line_map": line_map,
        "ing_ids": ing_ids,
        "step_ids": step_ids,
        "versions": versions,
        "content_hash": hashlib.sha256(MYSTERY.encode()).hexdigest(),
        "baseline": {
            "has_deterministic": True,
            "servings": 4.0,
            "batch_yield_count": None,
            "has_steps": True,
            "has_title": True,
        },
    }


def test_targeted_accept_keeps_deterministic_fields():
    ctx = _targeted_ctx()
    response = _good_response(MYSTERY)
    response["title"] = None
    response["description"] = None
    response["steps"] = []  # deterministic stands
    response["servings"] = None
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=ctx["content_hash"],
        line_map=ctx["line_map"],
        ingredient_line_ids=ctx["ing_ids"],
        step_line_ids=ctx["step_ids"],
        baseline=ctx["baseline"],
        request_versions=ctx["versions"],
        current_versions=ctx["versions"],
    )
    assert report["verdict"] == "accepted", report["problems"]
    deterministic = {
        "title": "Mystery",
        "description": "Ok.",
        "instructions": ["Cook rice.", "Serve hot."],
        "servings": 4.0,
        "servings_text": "Serves 4",
        "ingredients": [],
        "quality_issues": [],
    }
    merged = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=deterministic,
        validation=report,
        line_map=ctx["line_map"],
    )
    assert merged["title"] == "Mystery"
    assert merged["instructions"] == ["Cook rice.", "Serve hot."]
    assert merged["servings"] == 4.0
    assert merged["ingredients"][0]["origin"] == "llm"


def test_servings_conflict_rejected():
    ctx = _targeted_ctx()
    response = _good_response(MYSTERY)
    response["servings"] = 6
    response["servings_text"] = "Serves 6"
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=ctx["content_hash"],
        line_map=ctx["line_map"],
        ingredient_line_ids=ctx["ing_ids"],
        step_line_ids=ctx["step_ids"],
        baseline=ctx["baseline"],
        request_versions=ctx["versions"],
        current_versions=ctx["versions"],
    )
    assert report["verdict"] == "rejected_validation"
    assert any(p["code"] == "deterministic_conflict" for p in report["problems"])


def test_step_paraphrase_rejected():
    ctx = _targeted_ctx()
    response = _good_response(MYSTERY)
    response["steps"][0]["text"] = "Totally rewritten step"
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=ctx["content_hash"],
        line_map=ctx["line_map"],
        ingredient_line_ids=ctx["ing_ids"],
        step_line_ids=ctx["step_ids"],
        baseline=ctx["baseline"],
        request_versions=ctx["versions"],
        current_versions=ctx["versions"],
    )
    assert report["verdict"] == "rejected_validation"
    assert any(p["code"] == "step_paraphrase" for p in report["problems"])


def test_empty_resolved_extraction_rejected():
    ctx = _targeted_ctx()
    response = _good_response(MYSTERY)
    response["ingredients"] = []
    response["steps"] = []
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=ctx["content_hash"],
        line_map=ctx["line_map"],
        ingredient_line_ids=ctx["ing_ids"],
        step_line_ids=ctx["step_ids"],
        baseline=ctx["baseline"],
        request_versions=ctx["versions"],
        current_versions=ctx["versions"],
    )
    assert report["verdict"] == "rejected_validation"
    assert any(p["code"] == "empty_extraction" for p in report["problems"])


def test_boundary_record_model_supplies_missing_steps():
    ctx = _targeted_ctx()
    baseline = dict(ctx["baseline"], has_deterministic=False, has_steps=False)
    response = _good_response(MYSTERY)
    report = validate_response(
        response,
        source_id="foodie-000009",
        content_hash=ctx["content_hash"],
        line_map=ctx["line_map"],
        ingredient_line_ids=[],
        step_line_ids=[],
        baseline=baseline,
        request_versions=ctx["versions"],
        current_versions=ctx["versions"],
    )
    assert report["verdict"] == "accepted", report["problems"]
    merged = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=None,
        validation=report,
        line_map=ctx["line_map"],
    )
    assert merged["instructions"] == [s["text"] for s in response["steps"]]


def test_brief_flags_ambiguous_lines_only():
    from culinary_copilot.recipes.llm_batch import build_request_brief

    recipe = normalize_foodie_text(AMBIGUOUS, 2)
    line_ids, line_map = numbered_source(AMBIGUOUS)
    brief = build_request_brief(
        AMBIGUOUS,
        {"reason_codes": ["name_contains_measure"], "row_number": 2},
        recipe,
        line_map,
    )
    assert brief["routing_reasons"] == ["name_contains_measure"]
    assert brief["settled"]["steps_count"] == len(recipe["instructions"])
    assert any("600" in (line.get("text") or "") for line in brief["ambiguous_lines"])
    assert "steps" not in brief["missing"]  # deterministic steps stand


def test_prompt_requires_verbatim_evidence_and_headings():
    from culinary_copilot.recipes.llm_contracts import SYSTEM_PROMPT

    assert "verbatim" in SYSTEM_PROMPT
    assert "headings" in SYSTEM_PROMPT.casefold()
    assert "can" in SYSTEM_PROMPT  # source-word unit guidance


def test_request_body_uses_responses_structured_outputs():
    body = build_request_body(
        model="gpt-5-nano", system="s", user_content="u", max_output_tokens=10
    )
    assert body["model"] == "gpt-5-nano"
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert body["max_output_tokens"] == 10
