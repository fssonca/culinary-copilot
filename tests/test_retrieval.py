"""Phase 1 retrieval tests: mapping, revisions, filters, evidence (offline).

No database, network, model, or ingestion use. Repository functions are
patched so no connection is opened; the fake engine object is never
touched directly.
"""

import asyncio
import math
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from culinary_copilot.api.retrieval import build_router
from culinary_copilot.domain.clarification import FieldStatus
from culinary_copilot.recipes.durations import DurationStatus, classify_total, satisfies_ceiling
from culinary_copilot.retrieval.query import MATCH_DISH, MATCH_PANTRY_OVERLAP, map_request_to_query
from culinary_copilot.retrieval.service import (
    build_evidence,
    evaluate_readiness,
    retrieve_for_group,
)
from culinary_copilot.services.answers import init_state
from culinary_copilot.services.clarification_service import make_group
from culinary_copilot.services.store import InMemoryClarificationStore

FOODCOM = "AkashPS11/recipes_data_food.com"
FOODIE = "odunola/foodie"


def _ready_state(**overrides: Any):
    kwargs: dict[str, Any] = {
        "request_id": "req-r1",
        "request": {"ingredients": ["chicken"], "time_minutes": 30},
        "dish": "chicken curry",
        "task_scope": "discovery",
    }
    kwargs.update(overrides)
    return init_state(**kwargs)


def _store_with_group(state=None):
    store = InMemoryClarificationStore()
    state = state or _ready_state()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    return store, state, group


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Chicken Curry",
        "servings": 4.0,
        "durations_minutes": {"TotalTime": 107.0},
        "ingredients": [{"canonical": "chicken"}, {"canonical": "curry powder"}],
        "instructions": ["Cook the chicken.", "Serve hot."],
        "provenance": {"dataset_id": FOODCOM, "source_id": "000159"},
        "flags": ["units_unknown"],
        "capabilities": {"searchable": True, "scalable": False},
        "available_fields": {"servings": True},
        "quality_issues": [],
        "nutrition": {"calories": 10.0},
        "description": "A curry.",
        "source_url": None,
    }
    base.update(overrides)
    return base


# --- mapping -------------------------------------------------------------


def test_dish_decides_eligibility_pantry_ranks_only() -> None:
    state = init_state(
        request_id="m1",
        request={"ingredients": ["chicken", "garlic", "rice"]},
        dish="chicken curry",
    )
    query = map_request_to_query(state)
    assert query.match_mode == MATCH_DISH
    assert query.query_text == "chicken curry"  # pantry never enters eligibility text
    assert query.match_any_ingredients is None
    assert query.rank_pantry_terms == ["chicken", "garlic", "rice"]
    assert query.required_ingredients == []
    assert query.applied_filters == []  # no time ceiling set
    assert "ranking-only" in query.explanation


def test_dish_match_with_zero_pantry_overlap_stays_eligible() -> None:
    state = init_state(
        request_id="m1b",
        request={"ingredients": ["tofu", "miso"]},
        dish="chicken curry",
    )
    query = map_request_to_query(state)
    assert query.match_mode == MATCH_DISH
    assert query.query_text == "chicken curry"
    assert query.has_search_keys is True


def test_dish_terms_preserved_without_stopword_removal() -> None:
    state = init_state(request_id="m1c", request={}, dish="chicken and not fish stew")
    query = map_request_to_query(state)
    assert query.query_text == "chicken and not fish stew"
    assert query.dish_input == "chicken and not fish stew"
    assert "no-stopword-removal" in query.transformations


def test_time_ceiling_applied_without_relaxation() -> None:
    state = _ready_state()
    query = map_request_to_query(state)
    assert query.max_minutes == 30
    assert query.applied_filters == ["max_minutes"]


def test_time_ceiling_rejects_bool_and_nonfinite() -> None:
    # Note: the string "30" is valid input (request validation coerces it).
    for bad in (True, float("nan"), float("inf"), 0, 2000):
        state = init_state(request_id="mt", request={"time_minutes": bad}, dish="soup")
        assert map_request_to_query(state).max_minutes is None


def test_unsupported_constraints_surfaced_unchanged() -> None:
    state = init_state(
        request_id="m2",
        request={"dietary_constraints": ["vegan"], "equipment": ["wok"]},
        dish="pasta",
    )
    query = map_request_to_query(state)
    assert query.unsupported_constraints == ["dietary_constraints", "equipment"]


def test_pantry_terms_bounded_to_five() -> None:
    state = init_state(
        request_id="m3",
        request={"ingredients": ["a", "b", "c", "d", "e", "f", "g"]},
        dish="soup",
    )
    query = map_request_to_query(state)
    assert query.pantry_hint_terms == ["a", "b", "c", "d", "e"]
    assert query.rank_pantry_terms == ["a", "b", "c", "d", "e"]
    assert any("capped-at-5" in t for t in query.transformations)


def test_pantry_only_uses_overlap_eligibility() -> None:
    state = init_state(request_id="m4", request={"ingredients": ["tofu", "olive oil"]})
    query = map_request_to_query(state)
    assert query.match_mode == MATCH_PANTRY_OVERLAP
    assert query.has_search_keys is True
    # Multiword meaning preserved: the term is never shredded into words.
    assert query.match_any_ingredients == ["tofu", "olive oil"]
    assert query.rank_pantry_terms == []
    assert "may still be needed" in query.explanation


# --- duration policy -----------------------------------------------------


def test_classify_total_values() -> None:
    assert classify_total(20.0) == (DurationStatus.REPORTED_POSITIVE, 20.0)
    assert classify_total(7) == (DurationStatus.REPORTED_POSITIVE, 7.0)
    assert classify_total(0) == (DurationStatus.REPORTED_ZERO_UNVERIFIED, None)
    assert classify_total(0.0) == (DurationStatus.REPORTED_ZERO_UNVERIFIED, None)
    assert classify_total(None) == (DurationStatus.MISSING, None)
    assert classify_total(-5.0) == (DurationStatus.MISSING, None)
    assert classify_total(float("nan")) == (DurationStatus.MISSING, None)
    assert classify_total(float("inf")) == (DurationStatus.MISSING, None)
    # Invalid JSON numeric types are never coerced.
    assert classify_total("20") == (DurationStatus.MISSING, None)
    assert classify_total(True) == (DurationStatus.MISSING, None)
    assert classify_total({"TotalTime": 20}) == (DurationStatus.MISSING, None)


def test_satisfies_ceiling_only_for_positive_totals() -> None:
    assert satisfies_ceiling(20.0, 30) is True
    assert satisfies_ceiling(30.0, 30) is True
    assert satisfies_ceiling(31.0, 30) is False
    assert satisfies_ceiling(0, 30) is False
    assert satisfies_ceiling(None, 30) is False
    assert satisfies_ceiling(-5.0, 30) is False
    assert satisfies_ceiling(float("nan"), 30) is False
    assert satisfies_ceiling("20", 30) is False


# --- readiness -----------------------------------------------------------


def test_evaluate_readiness_reuses_conflict_and_blockers() -> None:
    vague = init_state(request_id="v", request={})
    ready, reason, _ = evaluate_readiness(vague)
    assert (ready, reason) == (False, "needs_clarification")
    conflict = init_state(request_id="c", request={}, dish="pasta")
    conflict.field_status["dietary_constraints"] = FieldStatus.CONFLICTING
    ready, reason, blockers = evaluate_readiness(conflict)
    assert ready is False and reason == "conflicting"
    assert any("conflicting" in b for b in blockers)


def test_blocked_matches_stable_code_not_substring() -> None:
    state = _ready_state()
    state.blockers = ["dish_direction_skipped: required dish direction was skipped"]
    ready, reason, _ = evaluate_readiness(state)
    assert (ready, reason) == (False, "blocked")
    # A human message that merely contains the word "skipped" is not a blocker.
    decoy = _ready_state()
    decoy.blockers = ["custom_note: the chef skipped lunch"]
    ready, _, _ = evaluate_readiness(decoy)
    assert ready is True


def test_not_ready_returns_controlled_outcome_without_db() -> None:
    store, state, group = _store_with_group(init_state(request_id="nr", request={}))
    with patch(
        "culinary_copilot.recipes.repository.search_all",
        side_effect=AssertionError("must not search when not ready"),
    ):
        result = asyncio.run(
            retrieve_for_group(
                store=store,
                engine=object(),
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
            )
        )
    assert result["outcome"] == "not_ready"
    assert result["results"] == []
    assert "readiness_note" in result
    assert result["request_revision"] == state.revision


# --- revisions -----------------------------------------------------------


def test_stale_revision_rejected_before_io() -> None:
    store, state, group = _store_with_group()
    with pytest.raises(Exception, match="stale revision"):
        asyncio.run(
            retrieve_for_group(
                store=store,
                engine=object(),
                group_id=group.group_id,
                expected_request_revision=99,
                expected_group_revision=group.revision,
            )
        )


def test_intervening_edit_detected_after_io() -> None:
    from culinary_copilot.retrieval import service as svc

    store, state, group = _store_with_group()
    real_to_thread = asyncio.to_thread

    async def _mutating_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = await real_to_thread(func, *args, **kwargs)
        if func is svc._search_sync:
            # Simulate an answer landing while DB I/O was in flight.
            current = store.get_state(state.request_id)
            assert current is not None
            current.revision += 1
            store.save_state(current)
        return result

    rows = [{"dataset_id": FOODCOM, "source_id": "000159", "title": "Chicken Curry"}]
    with (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()),
        patch("asyncio.to_thread", side_effect=_mutating_to_thread),
    ):
        with pytest.raises(Exception, match="state changed while retrieving"):
            asyncio.run(
                retrieve_for_group(
                    store=store,
                    engine=object(),
                    group_id=group.group_id,
                    expected_request_revision=state.revision,
                    expected_group_revision=group.revision,
                )
            )


def test_superseded_group_rejected_with_refresh_direction() -> None:
    store, state, group = _store_with_group()
    followup = make_group(state=state, questions=[], created_by="rule_only")
    assert store.commit_replan(
        state.request_id,
        expected_state_rev=state.revision,
        new_state=state,
        new_group=followup,
    )
    # The replan advanced the stored request revision; call with the current
    # revisions so the failure is supersession, not staleness.
    fresh = store.get_snapshot(group.group_id)
    assert fresh is not None
    fresh_state, fresh_group = fresh
    assert fresh_state.revision == state.revision + 1
    with pytest.raises(Exception, match="superseded group"):
        asyncio.run(
            retrieve_for_group(
                store=store,
                engine=object(),
                group_id=group.group_id,
                expected_request_revision=fresh_state.revision,
                expected_group_revision=fresh_group.revision,
            )
        )


# --- filters, routing, evidence ------------------------------------------


def test_dataset_slice_routes_with_ranking_boost() -> None:
    store, state, group = _store_with_group()
    rows = [{"dataset_id": FOODIE, "source_id": "foodie-1", "title": "T"}]
    with (
        patch("culinary_copilot.recipes.repository.search_recipes", return_value=rows) as single,
        patch("culinary_copilot.recipes.repository.search_all") as combined,
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()),
    ):
        result = asyncio.run(
            retrieve_for_group(
                store=store,
                engine=object(),
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
                dataset_id=FOODIE,
            )
        )
    combined.assert_not_called()
    _, kwargs = single.call_args
    assert kwargs["dataset_id"] == FOODIE
    assert kwargs["ingredients"] is None  # pantry never a mandatory filter
    assert kwargs["match_any_ingredients"] is None  # dish mode: no overlap gate
    assert kwargs["rank_pantry_terms"] == ["chicken"]
    assert kwargs["max_minutes"] == 30
    assert result["result_count"] == 1
    assert result["datasets_searched"] == [FOODIE]
    assert result["datasets_in_results"] == [FOODIE]


def test_pantry_only_routes_to_overlap_matching() -> None:
    state = init_state(request_id="po", request={"ingredients": ["tofu", "olive oil"]})
    store, _, group = _store_with_group(state)
    snap_state, _ = store.get_snapshot(group.group_id) or (None, None)
    assert snap_state is not None
    rows = [{"dataset_id": FOODIE, "source_id": "foodie-9", "title": "T"}]
    with (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows) as search,
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()),
    ):
        result = asyncio.run(
            retrieve_for_group(
                store=store,
                engine=object(),
                group_id=group.group_id,
                expected_request_revision=snap_state.revision,
                expected_group_revision=group.revision,
            )
        )
    _, kwargs = search.call_args
    assert kwargs["match_any_ingredients"] == ["tofu", "olive oil"]
    assert kwargs["rank_pantry_terms"] == []
    assert result["result_count"] == 1


def test_time_ceiling_forwarded_and_unknowns_explicit() -> None:
    store, state, group = _store_with_group()
    rows = [{"dataset_id": FOODCOM, "source_id": "000159", "title": "Chicken Curry"}]
    doc = _doc(servings=None, nutrition={"calories": None})
    with (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=doc),
    ):
        result = asyncio.run(
            retrieve_for_group(
                store=store,
                engine=object(),
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
            )
        )
    evidence = result["results"][0]
    assert evidence["dataset_id"] == FOODCOM and evidence["source_id"] == "000159"
    assert evidence["servings_known"] is False
    assert "servings" in evidence["unknowns"]
    assert "nutrition" in evidence["unknowns"]  # verification always unknown
    assert evidence["nutrition_values_present"] is False
    assert result["constraints_not_verified"]
    assert "usable reported total duration" in result["explanation"]


def test_reported_zero_total_is_unknown_with_raw_preserved() -> None:
    evidence = build_evidence(
        dataset_id=FOODCOM,
        source_id="z",
        title="Z",
        doc=_doc(durations_minutes={"TotalTime": 0.0}),
    )
    assert evidence["duration_status"] == "reported_zero_unverified"
    assert evidence["total_minutes"] is None
    assert evidence["total_minutes_reported"] == 0.0
    assert evidence["total_minutes_known"] is False
    assert "total_minutes" in evidence["unknowns"]


def test_invalid_numeric_metadata_stays_unknown() -> None:
    evidence = build_evidence(
        dataset_id=FOODCOM,
        source_id="s",
        title="S",
        doc=_doc(servings=True, durations_minutes={"TotalTime": "20"}),
    )
    assert evidence["servings_known"] is False
    assert evidence["servings"] is None
    assert evidence["duration_status"] == "missing"
    nan_evidence = build_evidence(
        dataset_id=FOODCOM,
        source_id="n",
        title="N",
        doc=_doc(servings=math.nan, durations_minutes={"TotalTime": math.inf}),
    )
    assert nan_evidence["servings_known"] is False
    assert nan_evidence["duration_status"] == "missing"


def test_exact_pair_lookup_never_falls_back() -> None:
    store, state, group = _store_with_group()
    rows = [{"dataset_id": FOODCOM, "source_id": "shared-001", "title": "Shared"}]
    with (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()) as lookup,
    ):
        asyncio.run(
            retrieve_for_group(
                store=store,
                engine=object(),
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
            )
        )
    _, kwargs = lookup.call_args
    assert kwargs == {"dataset_id": FOODCOM}


def test_no_match_explains_without_relaxation() -> None:
    store, state, group = _store_with_group()
    with patch("culinary_copilot.recipes.repository.search_all", return_value=[]):
        result = asyncio.run(
            retrieve_for_group(
                store=store,
                engine=object(),
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
            )
        )
    assert result["outcome"] == "ready"
    assert result["result_count"] == 0
    assert "No recipes matched" in result["explanation"]
    assert "No constraints were dropped" in result["explanation"]
    assert result["datasets_searched"] == [FOODCOM, FOODIE]
    assert result["datasets_in_results"] == []


def test_old_document_missing_capability_fields_stays_unknown() -> None:
    evidence = build_evidence(dataset_id=FOODCOM, source_id="000159", title="T", doc={"title": "T"})
    assert evidence["capabilities_unknown"] is True
    assert evidence["available_fields_unknown"] is True
    assert "capabilities" in evidence["unknowns"]
    assert evidence["nutrition_values_present"] is False
    assert evidence["evidence_type"] == "bounded_summary"


def test_missing_document_marked_without_claims() -> None:
    evidence = build_evidence(dataset_id=FOODCOM, source_id="x", title="T", doc=None)
    assert evidence["document_available"] is False
    assert evidence["unknowns"] == ["full_document"]


# --- API -----------------------------------------------------------------


def _api_client(store, engine) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(store=store, engine=engine))
    return TestClient(app)


def test_api_not_ready_and_stale_and_bad_dataset() -> None:
    vague_store, vague_state, vague_group = _store_with_group(
        init_state(request_id="api-nr", request={})
    )
    client = _api_client(vague_store, object())
    resp = client.post(
        "/api/v1/retrieval/search",
        json={
            "group_id": vague_group.group_id,
            "request_revision": vague_state.revision,
            "group_revision": vague_group.revision,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "not_ready"

    ready_store, ready_state, ready_group = _store_with_group()
    client = _api_client(ready_store, object())
    stale = client.post(
        "/api/v1/retrieval/search",
        json={
            "group_id": ready_group.group_id,
            "request_revision": 99,
            "group_revision": ready_group.revision,
        },
    )
    assert stale.status_code == 409
    assert (
        client.post(
            "/api/v1/retrieval/search",
            json={
                "group_id": "does-not-exist",
                "request_revision": 1,
                "group_revision": 1,
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/retrieval/search",
            json={
                "group_id": ready_group.group_id,
                "request_revision": ready_state.revision,
                "group_revision": ready_group.revision,
                "dataset_id": "nope/dataset",
            },
        ).status_code
        == 422
    )


def test_api_superseded_group_is_409_with_refresh() -> None:
    store, state, group = _store_with_group()
    followup = make_group(state=state, questions=[], created_by="rule_only")
    assert store.commit_replan(
        state.request_id,
        expected_state_rev=state.revision,
        new_state=state,
        new_group=followup,
    )
    fresh = store.get_snapshot(group.group_id)
    assert fresh is not None
    fresh_state, fresh_group = fresh
    client = _api_client(store, object())
    resp = client.post(
        "/api/v1/retrieval/search",
        json={
            "group_id": group.group_id,
            "request_revision": fresh_state.revision,
            "group_revision": fresh_group.revision,
        },
    )
    assert resp.status_code == 409
    assert "current group" in resp.json()["detail"]


def test_api_ready_returns_bounded_summaries_with_revisions() -> None:
    store, state, group = _store_with_group()
    client = _api_client(store, object())
    rows = [{"dataset_id": FOODCOM, "source_id": "000159", "title": "Chicken Curry"}]
    with (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()),
    ):
        resp = client.post(
            "/api/v1/retrieval/search",
            json={
                "group_id": group.group_id,
                "request_revision": state.revision,
                "group_revision": group.revision,
                "limit": 5,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "ready"
    assert body["request_revision"] == state.revision
    assert body["group_revision"] == group.revision
    assert body["result_count"] == 1
    assert body["results"][0]["source_id"] == "000159"
    assert body["results"][0]["evidence_type"] == "bounded_summary"
    assert body["evidence_limits"]["excerpt_chars"] == 600


def test_api_limit_bounds_are_422() -> None:
    store, state, group = _store_with_group()
    client = _api_client(store, object())
    for bad in (0, 11):
        resp = client.post(
            "/api/v1/retrieval/search",
            json={
                "group_id": group.group_id,
                "request_revision": state.revision,
                "group_revision": group.revision,
                "limit": bad,
            },
        )
        assert resp.status_code == 422
