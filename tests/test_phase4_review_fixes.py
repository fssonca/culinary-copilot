"""Regression tests for the Phase 4 review findings (offline, fakes only).

Covers: reasoning tokens not double counted; per-turn telemetry that keeps
tool-mode turn latency and the usage of completed and failing turns;
exactly one telemetry event and one terminal stream event, including for
unexpected errors; client disconnect through the real endpoint on both
ASGI disconnect paths; entry checks shared with the JSON endpoint; and the
single-model registry (gpt-6-luna) for configuration and pricing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_phase4_streaming_telemetry import (
    _app,
    _patched_repo,
    _run,
    _selection,
    _settings,
    _sse_events,
    _store_with_group,
)

from culinary_copilot.config import Settings
from culinary_copilot.llm.client import (
    FakeApplicationProvider,
    NativeToolCall,
    NativeTurnResult,
    ProviderIncompleteError,
    ProviderOutcome,
    ProviderTimeoutError,
)
from culinary_copilot.llm.models import SUPPORTED_MODELS
from culinary_copilot.recommendations.epicure import FakeEpicureAdapter
from culinary_copilot.recommendations.pricing import estimate_cost_usd
from culinary_copilot.recommendations.service import RecommendationFailure, recommend_for_group

LUNA_IN = 0.10 / 1e6
LUNA_OUT = 0.50 / 1e6


@contextmanager
def _telemetry() -> Iterator[list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            event = getattr(record, "recommendation", None)
            if event is not None:
                records.append(event)

    handler = _Handler()
    logger = logging.getLogger("culinary_copilot.recommendations")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def _body(state: Any, group: Any) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }


def _recommend(provider: Any, *, tool_mode: bool = False, **settings: Any) -> dict[str, Any]:
    store, state, group = _store_with_group()
    with _patched_repo()[0], _patched_repo()[1]:
        result: dict[str, Any] = _run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=_settings(**settings),
                provider=provider,
                epicure=FakeEpicureAdapter(),
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
                tool_mode=tool_mode,
            )
        )
    return result


class _UsageProvider:
    """Default-path provider returning one selection with known usage."""

    def __init__(self, parsed: dict[str, Any], *, output: int, reasoning: int) -> None:
        self.parsed = parsed
        self.output = output
        self.reasoning = reasoning

    async def complete_recommendation(self, **_: Any) -> ProviderOutcome:
        return ProviderOutcome(
            ok=True,
            parsed=dict(self.parsed),
            model="gpt-6-luna",
            latency_ms=5,
            attempts=1,
            input_tokens=1000,
            output_tokens=self.output,
            reasoning_tokens=self.reasoning,
            response_id="resp_1",
        )


def _tool_turn_one() -> NativeTurnResult:
    call = {"type": "function_call", "call_id": "call_1", "name": "get_recipe"}
    return NativeTurnResult(
        tool_calls=[
            NativeToolCall(
                call_id="call_1", name="get_recipe", arguments='{"candidate_label": "1"}'
            )
        ],
        chain_items=[{**call, "arguments": '{"candidate_label": "1"}'}],
        model="gpt-6-luna",
        attempts=1,
        latency_ms=40,
        input_tokens=307,
        output_tokens=33,
        response_id="resp_turn1",
    )


class _TwoTurnProvider:
    def __init__(self, second: BaseException | NativeTurnResult) -> None:
        self.second = second
        self.calls = 0

    async def complete_native_tool_turn(self, **_: Any) -> NativeTurnResult:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.02)
            return _tool_turn_one()
        await asyncio.sleep(0.03)
        if isinstance(self.second, BaseException):
            raise self.second
        return self.second


# --- cost ---------------------------------------------------------------------


def test_reasoning_tokens_are_not_billed_twice() -> None:
    # output_tokens already includes reasoning tokens (Responses usage).
    with _telemetry() as events:
        result = _recommend(_UsageProvider(_selection(), output=150, reasoning=100))
    assert result["outcome"] == "recommendation"
    [event] = events
    assert event["output_tokens"] == 150 and event["reasoning_tokens"] == 100
    assert event["estimated_cost_usd"] == pytest.approx(1000 * LUNA_IN + 150 * LUNA_OUT)
    assert event["usage_complete"] is True


def test_pricing_comes_from_the_model_registry_only() -> None:
    assert estimate_cost_usd(1000, 100, "gpt-6-luna") == pytest.approx(
        1000 * LUNA_IN + 100 * LUNA_OUT
    )
    assert estimate_cost_usd(1000, 100, "gpt-5-nano") is None  # not supported
    assert estimate_cost_usd(None, 100, "gpt-6-luna") is None
    # No model-independent price override exists any more.
    assert not any("price" in name for name in Settings.model_fields if "rec" in name)


# --- per-turn telemetry -----------------------------------------------------------


def test_tool_mode_keeps_each_turn_latency_and_usage() -> None:
    second = NativeTurnResult(
        tool_calls=[],
        chain_items=[],
        parsed=_selection(),
        model="gpt-6-luna",
        attempts=1,
        latency_ms=60,
        input_tokens=1344,
        output_tokens=99,
        response_id="resp_turn2",
    )
    with _telemetry() as events:
        result = _recommend(_TwoTurnProvider(second), tool_mode=True)
    assert result["outcome"] == "recommendation"
    [event] = events
    timings = event["stage_timings_ms"]
    for key in ("provider_request_1", "provider_turn_1", "provider_request_2", "provider_turn_2"):
        assert key in timings
    assert timings["provider_turn_1"] >= 15 and timings["provider_turn_2"] >= 25
    assert [t["status"] for t in event["turns"]] == ["completed", "completed"]
    assert event["provider_turns"] == 2 and event["tool_calls"] == 1
    assert event["input_tokens"] == 307 + 1344 and event["output_tokens"] == 33 + 99
    assert event["response_ids"] == ["resp_turn1", "resp_turn2"]


def test_turn_two_timeout_keeps_turn_one_usage_in_telemetry() -> None:
    timeout = ProviderTimeoutError("t2", attempts=2, request_sent=True)
    with _telemetry() as events, pytest.raises(RecommendationFailure):
        _recommend(_TwoTurnProvider(timeout), tool_mode=True)
    [event] = events
    assert event["outcome"] == "error" and event["reason"] == "provider_timeout"
    assert event["provider_turns"] == 2
    assert event["attempts"] == 3
    first, second = event["turns"]
    assert (first["input_tokens"], first["output_tokens"]) == (307, 33)
    assert second["status"] == "failed" and second["input_tokens"] is None
    # Turn 2 was sent but its usage is unknown: no total, no estimate,
    # but turn 1's known cost is kept as a lower bound.
    assert event["usage_complete"] is False
    assert event["input_tokens"] is None and event["estimated_cost_usd"] is None
    assert event["known_cost_usd"] == pytest.approx(307 * LUNA_IN + 33 * LUNA_OUT)


def test_failing_turn_with_known_usage_is_billed() -> None:
    truncated = ProviderIncompleteError(
        "cut", attempts=1, request_sent=True, input_tokens=1400, output_tokens=6500
    )
    with _telemetry() as events, pytest.raises(RecommendationFailure):
        _recommend(_TwoTurnProvider(truncated), tool_mode=True)
    [event] = events
    assert event["reason"] == "truncated_incomplete_response"
    assert event["usage_complete"] is True
    assert event["estimated_cost_usd"] == pytest.approx(
        (307 + 1400) * LUNA_IN + (33 + 6500) * LUNA_OUT
    )


def test_validation_failure_keeps_the_completed_turn_usage() -> None:
    unknown = _selection(source_id="999999")
    with _telemetry() as events, pytest.raises(RecommendationFailure):
        _recommend(_UsageProvider(unknown, output=40, reasoning=0))
    [event] = events
    assert event["reason"] == "validation_rejected"
    assert event["input_tokens"] == 1000 and event["output_tokens"] == 40
    assert event["estimated_cost_usd"] == pytest.approx(1000 * LUNA_IN + 40 * LUNA_OUT)


# --- terminal events and unexpected errors ----------------------------------------


class _BrokenProvider:
    async def complete_recommendation(self, **_: Any) -> Any:
        raise RuntimeError("unexpected bug with internals")


def test_unexpected_error_ends_stream_with_one_internal_error() -> None:
    store, state, group = _store_with_group()
    app = _app(store, _settings(), _BrokenProvider())
    with _telemetry() as records, _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=_body(state, group))
    events = _sse_events(resp.text)
    kinds = [kind for kind, _ in events]
    assert kinds.count("error") == 1 and "final" not in kinds and kinds[-1] == "error"
    error = events[-1][1]
    assert error["status"] == 500 and error["reason"] == "internal_error"
    assert "internals" not in resp.text
    assert [s for s in (p.get("stage") for _, p in events) if s][-1] == "provider_request"
    [event] = records
    assert event["reason"] == "internal_error:RuntimeError"


def test_stream_duration_limit_cancels_provider_and_records_once() -> None:
    cancelled = asyncio.Event()

    class _Slow:
        async def complete_recommendation(self, **_: Any) -> Any:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    store, state, group = _store_with_group()
    app = _app(store, _settings(rec_stream_max_duration_s=0.3), _Slow())
    with _telemetry() as records, _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=_body(state, group))
    events = _sse_events(resp.text)
    assert [k for k, _ in events].count("error") == 1
    assert events[-1][1]["reason"] == "stream_duration_exceeded"
    assert cancelled.is_set()
    [event] = records
    assert event["cancelled"] is True and event["reason"] == "stream_duration_exceeded"


def test_stream_event_limit_is_one_terminal_within_budget() -> None:
    store, state, group = _store_with_group()
    app = _app(store, _settings(rec_stream_max_events=3), FakeApplicationProvider())
    with _telemetry() as records, _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=_body(state, group))
    events = _sse_events(resp.text)
    assert len(events) <= 3
    assert [k for k, _ in events].count("error") == 1 and events[-1][0] == "error"
    assert events[-1][1]["reason"] == "stream_event_limit_exceeded"
    assert [p["seq"] for _, p in events] == list(range(len(events)))
    [event] = records
    assert event["reason"] == "stream_event_limit_exceeded"


def test_stream_sets_no_buffering_headers() -> None:
    store, state, group = _store_with_group()
    app = _app(store, _settings(), FakeApplicationProvider(script=[_selection()]))
    with _patched_repo()[0], _patched_repo()[1]:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=_body(state, group))
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"


# --- entry checks shared with the JSON endpoint ------------------------------------


@pytest.mark.parametrize(
    ("settings", "mutate", "status"),
    [
        ({"llm_recommendation_enabled": False}, None, 503),
        ({}, "unknown_group", 404),
        ({}, "stale", 409),
        ({}, "bad_dataset", 422),
    ],
)
def test_pre_stream_errors_match_the_json_endpoint(
    settings: dict[str, Any], mutate: str | None, status: int
) -> None:
    store, state, group = _store_with_group()
    body: dict[str, Any] = _body(state, group)
    if mutate == "unknown_group":
        body["group_id"] = "grp-missing"
    elif mutate == "stale":
        body["request_revision"] = state.revision + 5
    elif mutate == "bad_dataset":
        body["dataset_id"] = "unknown/dataset"
    app = _app(store, _settings(**settings), FakeApplicationProvider())
    client = TestClient(app)
    with _telemetry() as records:
        plain = client.post("/api/v1/recommendations", json=body)
        streamed = client.post("/api/v1/recommendations/stream", json=body)
    assert plain.status_code == streamed.status_code == status
    assert plain.json() == streamed.json()
    assert [r["transport"] for r in records] == ["json", "sse"]


# --- client disconnect through the endpoint ----------------------------------------


async def _stream_then_disconnect(app: Any, body: dict[str, Any], spec: str) -> list[bytes]:
    payload = json.dumps(body).encode()
    first_chunk = asyncio.Event()
    delivered = False
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await first_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            if first_chunk.is_set() and spec >= "2.4":
                raise OSError("client gone")
            chunks.append(message["body"])
            first_chunk.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/recommendations/stream",
        "raw_path": b"/api/v1/recommendations/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("testclient", 1),
        "server": ("testserver", 80),
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=10)
    except Exception:  # noqa: S110 - a disconnect may surface as ClientDisconnect
        pass
    await asyncio.sleep(0.1)  # let the cancelled workflow record its telemetry
    return chunks


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
def test_client_disconnect_cancels_workflow_and_records_telemetry(spec: str) -> None:
    cancelled = {"hit": False}

    class _Slow:
        async def complete_recommendation(self, **_: Any) -> Any:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled["hit"] = True
                raise

    store, state, group = _store_with_group()
    app = _app(store, _settings(), _Slow())
    with _telemetry() as records, _patched_repo()[0], _patched_repo()[1]:
        chunks = _run(_stream_then_disconnect(app, _body(state, group), spec))
    text = b"".join(chunks).decode()
    assert "event: final" not in text and "event: error" not in text
    assert cancelled["hit"] is True
    [event] = records
    assert event["cancelled"] is True and event["transport"] == "sse"
    assert event["reason"] in {"client_disconnect", "stream_closed"}
    assert event["request_id"] == state.request_id


# --- model registry -------------------------------------------------------------


def test_only_gpt_6_luna_is_configured_and_minimal_effort_is_refused() -> None:
    assert list(SUPPORTED_MODELS) == ["gpt-6-luna"]
    settings = Settings(_env_file=None)
    for name in ("openai_model", "llm_extraction_model", "llm_app_model", "llm_rec_model"):
        assert getattr(settings, name) == "gpt-6-luna"
    assert settings.llm_rec_reasoning_effort == "none"
    with pytest.raises(ValueError, match="Unsupported model"):
        Settings(_env_file=None, llm_rec_model="gpt-5-nano")
    with pytest.raises(ValueError, match="not supported by gpt-6-luna"):
        Settings(_env_file=None, llm_rec_reasoning_effort="minimal")
    with pytest.raises(ValueError, match="not supported by gpt-6-luna"):
        Settings(_env_file=None, llm_reasoning_effort="minimal")
