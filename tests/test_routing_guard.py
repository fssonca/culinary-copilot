"""No-network routing guard and explicit audit-mode regressions."""

import json
from pathlib import Path

import pytest
from test_llm_hybrid import CLEAN, _args, _settings, _write_csv

from culinary_copilot.recipes import llm_batch, routing
from culinary_copilot.recipes.adapters.foodie import normalize_foodie_text
from culinary_copilot.recipes.dataset_utils import sha256_file
from culinary_copilot.recipes.llm_contracts import numbered_source


def fixture(row):
    return json.loads(
        (Path(__file__).parent / "fixtures/llm_expected" / f"{row}.json").read_text()
    )["source"]["texts"]


def test_known_noop_and_single_line_stay_local():
    text = fixture(7453)
    recipe = normalize_foodie_text(text, 7453)
    result = routing.route_record(text, 7453, recipe=recipe)
    assert result["route"] == "deterministic_accept"
    assert result["reason_codes"] == ["no_recoverable_targets"]
    assert "name_contains_measure" in result["suppressed_reason_codes"]
    results = routing.route_pilot([{"texts": fixture(433)}], [1], audit_rate=1)
    assert results[0]["route"] == "quarantine"
    assert results[0]["reason_codes"] == ["single_line_requires_spans"]
    assert not results[0]["audit"]


@pytest.mark.parametrize("row", [173, 303, 3423])
def test_real_ambiguities_still_route(row):
    text = fixture(row)
    assert (
        routing.route_record(text, row, recipe=normalize_foodie_text(text, row))["route"]
        == "needs_llm"
    )


def test_servings_need_positive_source_evidence():
    recipe = normalize_foodie_text(CLEAN, 1)
    _, lines = numbered_source(CLEAN)
    route = routing.route_record(CLEAN, 1, recipe=recipe)
    assert route["route"] == "deterministic_accept"
    assert "servings" not in llm_batch.build_request_brief(CLEAN, route, recipe, lines)["missing"]
    text = CLEAN + "Serves 4.\n"
    route = routing.route_record(text, 1, recipe=recipe)
    assert route["route"] == "needs_llm"
    assert "servings_unstructured" in route["reason_codes"]
    assert not routing.servings_evidence("Makes 24 cookies. Serve hot.")


def test_audit_has_explicit_scope_and_does_not_change_fallback():
    rows = [{"texts": CLEAN}]
    assert routing.route_pilot(rows, [1], audit_rate=0)[0]["route"] == "deterministic_accept"
    result = routing.route_pilot(rows, [1], audit_rate=1)[0]
    recipe = normalize_foodie_text(CLEAN, 1)
    _, lines = numbered_source(CLEAN)
    brief = llm_batch.build_request_brief(CLEAN, result, recipe, lines)
    assert result["audit"] and result["reason_codes"] == ["audit_sample"]
    assert brief["mode"] == "audit"
    assert len(brief["requested_ingredient_line_ids"]) == 2


def test_structural_signal_is_not_silently_accepted():
    recipe = normalize_foodie_text(CLEAN, 1)
    recipe["line_coverage"] = {"dropped": ["unclassified ingredient"]}
    assert routing.route_record(CLEAN, 1, recipe=recipe)["route"] == "needs_llm"


def test_zero_request_run_finalizes_without_sdk(tmp_path, monkeypatch):
    path = tmp_path / "source.csv"
    _write_csv(path, [fixture(7453), fixture(433)])
    monkeypatch.setattr(llm_batch, "FOODIE_SHA256", sha256_file(path))

    def no_client(_):
        pytest.fail("No SDK client should be created")

    monkeypatch.setattr(llm_batch, "get_client", no_client)
    args = _args(tmp_path / "run", csv=str(path), size=2, audit_rate=0)
    settings = _settings()
    manifest = llm_batch.cmd_prepare(args, settings)
    assert manifest["requests"] == 0
    with pytest.raises(ValueError, match="No LLM requests"):
        llm_batch.cmd_submit(args, settings)
    result = llm_batch.cmd_finalize(args, settings)
    assert result["states"] == {"ready_to_load": 1, "quarantined": 1}
    quarantines = [
        json.loads(s)
        for s in (Path(args.run_dir) / "final-quarantine.jsonl").read_text().splitlines()
    ]
    assert quarantines[0]["raw"]["texts"] == fixture(433)
