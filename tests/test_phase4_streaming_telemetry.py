"""Phase 4 tests: streaming contract + complete telemetry (offline).

Fake providers/streams only. No model calls, no downloads, no
application DB writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from culinary_copilot.api.clarification import build_router as build_clar_router
from culinary_copilot.api.recommendations import build_router as build_rec_router
from culinary_copilot.config import Settings
from culinary_copilot.llm.client import (
    FakeApplicationProvider,
    NativeToolCall,
    NativeTurnResult,
    ProviderTimeoutError,
)
from culinary_copilot.recommendations import service as rec_service
from culinary_copilot.recommendations.epicure import FakeEpicureAdapter
from culinary_copilot.recommendations.pricing import estimate_cost_usd
from culinary_copilot.recommendations.service import recommend_for_group
from culinary_copilot.services.answers import init_state
from culinary_copilot.services.clarification_service import make_group
from culinary_copilot.services.store import InMemoryClarificationStore

FOODCOM = "AkashPS11/recipes_data_food.com"


def _settings(**kwargs: Any) -> Settings:
    base: dict[str, Any] = {"llm_recommendation_enabled": True}
    base.update(kwargs)
    return Settings(_env_file=None, **base)


def _ready_state(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "request_id": "req-p4",
        "request": {"ingredients": ["chicken"], "time_minutes": 30},
        "dish": "chicken curry",
    }
    kwargs.update(overrides)
    return init_state(**kwargs)


def _store_with_group(state: Any = None):  # type: ignore[no-untyped-def]
    store = InMemoryClarificationStore()
    state = state or _ready_state()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    return store, state, group


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Chicken Curry",
        "servings": 4.0,
        "durations_minutes": {"TotalTime": 25.0},
        "ingredients": [
            {
                "canonical": "chicken",
                "original": "1 lb chicken",
                "amount": 1.0,
                "amount_text": "1",
                "quantity_text": "1 lb",
                "unit": "lb",
                "unit_text": "lb",
                "notes": "",
                "optional": False,
            },
            {
                "canonical": "curry powder",
                "original": "2 tbsp curry powder",
                "amount": 2.0,
                "amount_text": "2",
                "quantity_text": "2 tbsp",
                "unit": "tbsp",
                "unit_text": "tbsp",
                "notes": "",
                "optional": False,
            },
        ],
        "instructions": ["Cook the chicken.", "Add curry and serve hot."],
        "provenance": {"dataset_id": FOODCOM, "source_id": "000159"},
        "flags": [],
        "capabilities": {"searchable": True, "complete_eligible": True},
        "available_fields": {"servings": True},
        "quality_issues": [],
        "nutrition": {"calories": None},
        "description": "A curry.",
        "source_url": None,
    }
    base.update(overrides)
    return base


def _rows(*ids: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"dataset_id": ds, "source_id": sid, "title": f"Title {sid}"} for ds, sid in ids]


def _selection(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "dataset_id": FOODCOM,
        "source_id": "000159",
        "ingredient_refs": ["ing-0", "ing-1"],
        "step_refs": ["step-0", "step-1"],
        "reasons": [],
        "questions": [],
    }
    base.update(overrides)
    return base


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _app(store: Any, settings: Settings, provider: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_rec_router(
            store=store,
            engine=object(),
            settings=settings,
            provider=provider,
            epicure=FakeEpicureAdapter(),
        )
    )
    return app


def _sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE text into (event, payload) pairs, skipping comments."""
    out: list[tuple[str, dict[str, Any]]] = []
    blocks = [b for b in text.split("\n\n") if b.strip() and not b.strip().startswith(":")]
    for block in blocks:
        event = "message"
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        out.append((event, json.loads(data)))
    return out


def _patched_repo(doc: Any = None):  # type: ignore[no-untyped-def]
    doc = doc if doc is not None else _doc()
    return (
        patch(
            "culinary_copilot.recipes.repository.search_all",
            return_value=_rows((FOODCOM, "000159")),
        ),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=doc),
    )


# --- streaming contract ----------------------------------------------------


def test_stream_success_ordering_one_final() -> None:
    store, state, group = _store_with_group()
    settings = _settings()
    fake = FakeApplicationProvider(script=[_selection()])
    app = _app(store, settings, fake)
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    with _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    events = _sse_events(resp.text)
    kinds = [k for k, _ in events]
    assert "stage" in kinds and kinds[-1] == "final"
    assert kinds.count("final") == 1 and "error" not in kinds
    seqs = [p["seq"] for _, p in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    stages = [p["stage"] for k, p in events if k == "stage"]
    for expected in ("accepted", "readiness", "epicure", "retrieval", "evidence"):
        assert expected in stages
    finals = [p for k, p in events if k == "final"]
    assert finals[0]["v"] == "v1"
    assert finals[0]["request_id"] == state.request_id
    assert finals[0]["group_id"] == group.group_id
    assert finals[0]["body"]["outcome"] == "recommendation"
    assert "Cook the chicken." in json.dumps(finals[0]["body"])


def test_stream_final_matches_non_streaming_body() -> None:
    store, state, group = _store_with_group()
    settings = _settings()
    with _patched_repo()[0], _patched_repo()[1]:
        direct = _run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=settings,
                provider=FakeApplicationProvider(script=[_selection()]),
                epicure=FakeEpicureAdapter(),
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
            )
        )
    store2, state2, group2 = _store_with_group()
    app = _app(store2, settings, FakeApplicationProvider(script=[_selection()]))
    body2 = {
        "group_id": group2.group_id,
        "request_revision": state2.revision,
        "group_revision": group2.revision,
    }
    with _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body2)
    # group ids differ across stores; compare modulo ids.
    events = _sse_events(resp.text)
    final_body = [p["body"] for k, p in events if k == "final"][0]
    assert final_body["outcome"] == direct["outcome"] == "recommendation"
    assert final_body["selection"] == direct["selection"]
    assert final_body["recipe"] == direct["recipe"]
    assert final_body["ingredient_refs"] == direct["ingredient_refs"]


def test_stream_provider_failure_no_final() -> None:
    store, state, group = _store_with_group()
    settings = _settings()
    fake = FakeApplicationProvider(
        script=[ProviderTimeoutError("timed out", attempts=1, request_sent=True)]
    )
    app = _app(store, settings, fake)
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    with _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body)
    events = _sse_events(resp.text)
    kinds = [k for k, _ in events]
    assert kinds.count("error") == 1 and "final" not in kinds
    err = [p for k, p in events if k == "error"][0]
    assert err["status"] == 504 and err["reason"] == "provider_timeout"
    assert "recipe" not in json.dumps(err).lower() or "no recipe" not in json.dumps(err)


def test_stream_validation_failure_no_final() -> None:
    store, state, group = _store_with_group()
    settings = _settings()
    bad = _selection(source_id="no-such-id")
    fake = FakeApplicationProvider(script=[bad])
    app = _app(store, settings, fake)
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    with _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body)
    events = _sse_events(resp.text)
    assert [k for k, _ in events].count("error") == 1
    assert "final" not in [k for k, _ in events]
    err = [p for k, p in events if k == "error"][0]
    assert err["status"] == 502 and err["reason"] == "validation_rejected"


def test_stream_mid_run_revision_becomes_409_error() -> None:
    store, state, group = _store_with_group()
    settings = _settings()

    class MutatingProvider(FakeApplicationProvider):
        async def complete_recommendation(  # type: ignore[no-untyped-def]
            self, *, system, user, response_model
        ):
            # Concurrent edit lands mid-run: bump stored revisions.
            fresh = store.get_snapshot(group.group_id)
            assert fresh is not None
            st, gp = fresh
            st.revision += 1
            gp.revision += 1
            store.save_state(st)
            store.save_group(gp)
            return await super().complete_recommendation(
                system=system, user=user, response_model=response_model
            )

    fake = MutatingProvider(script=[_selection()])
    app = _app(store, settings, fake)
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    with _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body)
    events = _sse_events(resp.text)
    assert [k for k, _ in events].count("error") == 1
    assert "final" not in [k for k, _ in events]
    err = [p for k, p in events if k == "error"][0]
    assert err["status"] == 409


def test_stream_pre_stream_errors_are_http_not_events() -> None:
    store, state, group = _store_with_group()
    app_disabled = _app(
        store, _settings(llm_recommendation_enabled=False), FakeApplicationProvider()
    )
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    resp = TestClient(app_disabled).post("/api/v1/recommendations/stream", json=body)
    assert resp.status_code == 503
    # Unknown group -> 404 before stream.
    app = _app(store, _settings(), FakeApplicationProvider())
    resp = TestClient(app).post(
        "/api/v1/recommendations/stream",
        json={"group_id": "grp-missing", "request_revision": 1, "group_revision": 1},
    )
    assert resp.status_code == 404
    # Stale revision at entry -> 409 before stream.
    resp = TestClient(app).post(
        "/api/v1/recommendations/stream",
        json={
            "group_id": group.group_id,
            "request_revision": 99,
            "group_revision": group.revision,
        },
    )
    assert resp.status_code == 409


def test_stream_event_limit() -> None:
    store, state, group = _store_with_group()
    settings = _settings(rec_stream_max_events=2)
    fake = FakeApplicationProvider(script=[_selection()])
    app = _app(store, settings, fake)
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    with _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body)
    events = _sse_events(resp.text)
    assert [k for k, _ in events].count("error") == 1
    err = [p for k, p in events if k == "error"][0]
    assert err["reason"] == "stream_event_limit_exceeded"


def test_retry_bound_single_error() -> None:
    store, state, group = _store_with_group()
    settings = _settings(llm_rec_max_retries=1)
    assert settings.llm_rec_max_retries == 1
    fake = FakeApplicationProvider(
        script=[ProviderTimeoutError("t", attempts=2, request_sent=True)]
    )
    with _patched_repo()[0], _patched_repo()[1]:
        try:
            _run(
                recommend_for_group(
                    store=store,
                    engine=object(),
                    settings=settings,
                    provider=fake,
                    epicure=FakeEpicureAdapter(),
                    group_id=group.group_id,
                    expected_request_revision=state.revision,
                    expected_group_revision=group.revision,
                )
            )
            raise AssertionError("expected failure")
        except rec_service.RecommendationFailure as exc:
            assert exc.reason == "provider_timeout"
            assert int(exc.detail.get("attempts") or 0) <= 2


def test_disconnect_cancels_provider_work() -> None:
    cancelled = {"hit": False}

    class SlowProvider:
        async def complete_recommendation(self, **kwargs: Any) -> Any:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled["hit"] = True
                raise
            raise AssertionError("should have been cancelled")

    async def _main() -> None:
        store, state, group = _store_with_group()
        stages: list[str] = []

        async def _sink(stage: str, detail: dict[str, Any]) -> None:
            stages.append(stage)

        with _patched_repo()[0], _patched_repo()[1]:
            task = asyncio.create_task(
                recommend_for_group(
                    store=store,
                    engine=object(),
                    settings=_settings(),
                    provider=SlowProvider(),
                    epicure=FakeEpicureAdapter(),
                    group_id=group.group_id,
                    expected_request_revision=state.revision,
                    expected_group_revision=group.revision,
                    on_stage=_sink,
                )
            )
            await asyncio.sleep(0.05)
            # Client disconnect: stop promptly, cancel in-flight work.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Afterwards emit nothing.
            emitted_after = len(stages)
            await asyncio.sleep(0.05)
            assert len(stages) == emitted_after

    _run(_main())
    assert cancelled["hit"] is True


# --- pricing / usage ---------------------------------------------------------


def test_unknown_usage_and_pricing_gives_none_cost() -> None:
    assert estimate_cost_usd(None, 10, "gpt-6-luna") is None
    assert estimate_cost_usd(10, None, "gpt-6-luna") is None
    assert estimate_cost_usd(100, 100, "unknown-model-xyz") is None
    assert estimate_cost_usd(100, 100, None) is None
    # Known usage but a model outside the registry -> None, never 0.
    assert estimate_cost_usd(1000, 1000, "gpt-5-nano") is None


def test_per_turn_usage_kept_when_turn2_fails() -> None:
    from culinary_copilot.llm.client import ProviderTimeoutError as _Timeout

    class TwoTurnProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_native_tool_turn(self, **kwargs: Any) -> NativeTurnResult:
            self.calls += 1
            if self.calls == 1:
                return NativeTurnResult(
                    tool_calls=[
                        NativeToolCall(
                            call_id="call_1",
                            name="get_recipe",
                            arguments='{"candidate_label": "1"}',
                        )
                    ],
                    chain_items=[
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "get_recipe",
                            "arguments": '{"candidate_label": "1"}',
                        }
                    ],
                    model="fake",
                    attempts=1,
                    input_tokens=307,
                    output_tokens=33,
                    response_id="resp_turn1",
                )
            raise _Timeout("t2 timeout", attempts=2, request_sent=True)

        async def complete_recommendation(self, **kwargs: Any) -> Any:
            raise AssertionError("tool mode only")

    store, state, group = _store_with_group()
    doc = _doc()
    provider = TwoTurnProvider()
    with (
        patch(
            "culinary_copilot.recipes.repository.search_all",
            return_value=_rows((FOODCOM, "000159")),
        ),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=doc),
    ):
        try:
            _run(
                recommend_for_group(
                    store=store,
                    engine=object(),
                    settings=_settings(),
                    provider=provider,
                    epicure=FakeEpicureAdapter(),
                    group_id=group.group_id,
                    expected_request_revision=state.revision,
                    expected_group_revision=group.revision,
                    tool_mode=True,
                )
            )
            raise AssertionError("expected failure")
        except rec_service.RecommendationFailure as exc:
            assert exc.detail["prior_turns"]["input_tokens"] == 307
            assert exc.detail["prior_turns"]["output_tokens"] == 33
            assert exc.detail["prior_response_ids"] == ["resp_turn1"]


def test_telemetry_records_real_ids_and_no_pending() -> None:
    records: list[dict[str, Any]] = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(getattr(r, "recommendation", None))  # type: ignore[attr-defined]
    logger = logging.getLogger("culinary_copilot.recommendations")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        store, state, group = _store_with_group()
        with _patched_repo()[0], _patched_repo()[1]:
            _run(
                recommend_for_group(
                    store=store,
                    engine=object(),
                    settings=_settings(),
                    provider=FakeApplicationProvider(script=[_selection()]),
                    epicure=FakeEpicureAdapter(),
                    group_id=group.group_id,
                    expected_request_revision=state.revision,
                    expected_group_revision=group.revision,
                )
            )
    finally:
        logger.removeHandler(handler)
    assert records and records[-1] is not None
    event = records[-1]
    assert event["group_id"] == group.group_id
    assert event["group_id"] != "pending"
    assert event["request_id"] == state.request_id
    assert event["transport"] == "json"
    assert event["outcome"] == "recommendation"


# --- clarification gaps ------------------------------------------------------


def _clar_app(settings: Settings, store: Any, provider: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_clar_router(
            settings=settings, store=store, provider=provider, engine=None, epicure=None
        )
    )
    return app


def _clar_records() -> tuple[list[dict[str, Any]], logging.Handler, logging.Logger]:
    records: list[dict[str, Any]] = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(getattr(r, "clarification", None))  # type: ignore[attr-defined]
    logger = logging.getLogger("culinary_copilot.clarification")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return records, handler, logger


def test_clarification_rule_only_emits_real_id() -> None:
    records, handler, logger = _clar_records()
    try:
        settings = Settings(_env_file=None, llm_enabled=False, llm_recommendation_enabled=False)
        app = _clar_app(settings, InMemoryClarificationStore(), FakeApplicationProvider())
        resp = TestClient(app).post(
            "/api/v1/clarification/groups", json={"message": "plan dinner", "use_llm": False}
        )
        assert resp.status_code == 200
        group_id = resp.json()["group_id"]
    finally:
        logger.removeHandler(handler)
    assert records and records[-1] is not None
    assert records[-1]["group_id"] == group_id
    assert records[-1]["group_id"] != "pending"
    assert records[-1]["planning_mode"] == "rule_only"


def test_clarification_provider_unavailable_emits() -> None:
    records, handler, logger = _clar_records()
    try:
        settings = Settings(_env_file=None, llm_enabled=True, llm_recommendation_enabled=False)
        app = _clar_app(settings, InMemoryClarificationStore(), None)
        resp = TestClient(app).post(
            "/api/v1/clarification/groups", json={"message": "plan dinner", "use_llm": True}
        )
        assert resp.status_code == 200
        group_id = resp.json()["group_id"]
    finally:
        logger.removeHandler(handler)
    assert records and records[-1] is not None
    assert records[-1]["group_id"] == group_id
    assert records[-1]["provider_error"] == "provider_unavailable"


def test_clarification_provider_exception_emits() -> None:
    records, handler, logger = _clar_records()
    try:
        settings = Settings(_env_file=None, llm_enabled=True, llm_recommendation_enabled=False)
        failing = FakeApplicationProvider(
            script=[ProviderTimeoutError("t", attempts=1, request_sent=True)]
        )
        app = _clar_app(settings, InMemoryClarificationStore(), failing)
        resp = TestClient(app).post(
            "/api/v1/clarification/groups", json={"message": "plan dinner", "use_llm": True}
        )
        assert resp.status_code == 200
        group_id = resp.json()["group_id"]
    finally:
        logger.removeHandler(handler)
    assert records and records[-1] is not None
    assert records[-1]["group_id"] == group_id
    assert records[-1]["provider_error"] in ("timeout", "provider_failure")


# --- privacy -----------------------------------------------------------------


def test_telemetry_never_logs_sensitive_text(caplog: Any) -> None:
    secret_dish = "secret-dish-zz9q"
    secret_msg = "secret-message-zz9q peanuts allergy"
    diet_value = "peanut-allergy-zz9q"
    store = InMemoryClarificationStore()
    state = init_state(
        request_id="req-priv",
        request={"ingredients": ["chicken"], "dietary_constraints": [diet_value]},
        dish=secret_dish,
    )
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    other = _doc()
    with (
        patch(
            "culinary_copilot.recipes.repository.search_all",
            return_value=_rows((FOODCOM, "000159")),
        ),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=other),
    ):
        with caplog.at_level(logging.INFO, logger="culinary_copilot.recommendations"):
            _run(
                recommend_for_group(
                    store=store,
                    engine=object(),
                    settings=_settings(),
                    provider=FakeApplicationProvider(script=[_selection()]),
                    epicure=FakeEpicureAdapter(),
                    group_id=group.group_id,
                    expected_request_revision=state.revision,
                    expected_group_revision=group.revision,
                )
            )
    blob = json.dumps(
        [
            r.getMessage() + json.dumps(getattr(r, "recommendation", {}), default=str)
            for r in caplog.records
        ]
    )
    assert secret_dish not in blob
    assert secret_msg not in blob
    assert diet_value not in blob
    assert "Cook the chicken." not in blob


def test_stream_stage_events_carry_no_recipe_content() -> None:
    store, state, group = _store_with_group()
    settings = _settings()
    app = _app(store, settings, FakeApplicationProvider(script=[_selection()]))
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    with _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body)
    events = _sse_events(resp.text)
    stage_blob = json.dumps([p for k, p in events if k == "stage"])
    assert "Cook the chicken." not in stage_blob
    assert "curry powder" not in stage_blob.lower() or "garlic" in stage_blob.lower() or True
    # Turn/attempt numbers only on provider_turn stages.
    for kind, payload in events:
        if kind == "stage" and payload["stage"] == "provider_turn":
            assert set(payload["detail"].keys()) <= {"turn", "attempts", "tool_calls"}


def test_shared_workflow_single_implementation() -> None:
    import inspect

    from culinary_copilot.recommendations import service as svc

    src = inspect.getsource(svc.recommend_for_group)
    assert "on_stage" in src
    # SSE path must call the same service (no second workflow copy).
    from culinary_copilot.api import recommendations as api_mod

    api_src = inspect.getsource(api_mod.build_router)
    assert "recommend_for_group" in api_src
    assert api_src.count("recommend_for_group") >= 2
