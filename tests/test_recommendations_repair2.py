"""Repair-pass-2 regression tests (offline, from phase3_live_run1 findings).

SDK-shaped fixtures (real openai exception classes, duck-typed responses),
fake providers, patched repository, temporary output dirs. No model calls,
no downloads, no application DB use.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from openai import BadRequestError, NotFoundError

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "recommendations_live"))

from runner import execute_live_case, load_cases, main  # noqa: E402

from culinary_copilot.config import Settings  # noqa: E402
from culinary_copilot.domain.recommendations import SelectionProposal  # noqa: E402
from culinary_copilot.llm.client import (  # noqa: E402
    FakeApplicationProvider,
    OpenAIApplicationProvider,
    ProviderIncompleteError,
    ProviderRateLimitError,
)
from culinary_copilot.recommendations.service import (  # noqa: E402
    RecommendationFailure,
    recommend_for_group,
)

CASES = Path(__file__).parent.parent / "evals" / "cases" / "phase3_live_cases.json"
FOODCOM = "AkashPS11/recipes_data_food.com"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_enabled": False,
        "llm_recommendation_enabled": True,
        "epicure_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Repair2 Curry",
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
        ],
        "instructions": ["Cook the chicken."],
        "provenance": {"dataset_id": FOODCOM, "source_id": "000159"},
        "flags": [],
        "available_fields": {"servings": True},
        "quality_issues": [],
        "description": "A curry.",
    }
    base.update(overrides)
    return base


def _repo_patches():
    rows = [{"dataset_id": FOODCOM, "source_id": "000159", "title": "T"}]
    return (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.search_recipes", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()),
    )


def _repo_patches_empty():
    return (
        patch("culinary_copilot.recipes.repository.search_all", return_value=[]),
        patch("culinary_copilot.recipes.repository.search_recipes", return_value=[]),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=None),
    )


def _sdk_error(exc_cls: Any, status: int) -> Any:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status, request=request, headers={"x-request-id": "req-test-1"})
    return exc_cls("test error", response=response, body={"error": {"message": "test"}})


class _StubResponses:
    """Stands in for client.responses: scripted results or SDK exceptions."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.kwargs: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _provider_with(script: list[Any]) -> tuple[OpenAIApplicationProvider, _StubResponses]:
    provider = OpenAIApplicationProvider(_settings())
    stub = _StubResponses(script)
    provider._client = SimpleNamespace(responses=stub)
    return provider, stub


def _incomplete_response() -> Any:
    return SimpleNamespace(
        id="resp-incomplete-1",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        usage=SimpleNamespace(
            input_tokens=6016,
            output_tokens=800,
            output_tokens_details=SimpleNamespace(reasoning_tokens=700),
        ),
        output=[],
        output_parsed=None,
    )


def _ok_response(parsed: Any) -> Any:
    return SimpleNamespace(
        id="resp-ok-1",
        status="completed",
        incomplete_details=None,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            output_tokens_details=SimpleNamespace(reasoning_tokens=10),
        ),
        output=[],
        output_parsed=parsed,
    )


# --- finding 1: truncation root cause ---------------------------------------


def test_reasoning_effort_is_sent_explicitly() -> None:
    provider, stub = _provider_with([_ok_response(SimpleNamespace(model_dump=lambda: {}))])
    asyncio.run(
        provider.complete_recommendation(system="s", user="u", response_model=SelectionProposal)
    )
    assert stub.kwargs, "expected one provider call"
    assert stub.kwargs[0].get("reasoning") == {"effort": "none"}


def test_default_reasoning_effort_is_supported_by_the_model() -> None:
    # gpt-6-luna documents none/low/medium/high/xhigh/max, not "minimal".
    assert _settings().llm_rec_reasoning_effort == "none"


def test_output_cap_covers_measured_max_plus_reasoning_allowance() -> None:
    # Measured 5414-char worst-case valid label selection (server-issued
    # label + schema maxima + 200-char free text + evidence-bounded refs)
    # + 1000-token reasoning allowance, rounded up: 6500.
    assert _settings().llm_rec_max_output_tokens == 6500


def test_free_text_fields_rejected_by_schema() -> None:
    # The free-text contract is retired: selection_reasons/needs are
    # forbidden extras, and typed refs accept only ing-N references.
    with pytest.raises(Exception):
        SelectionProposal.model_validate(
            {"dataset_id": "d", "source_id": "s", "selection_reasons": ["r"]}
        )
    with pytest.raises(Exception):
        SelectionProposal.model_validate({"dataset_id": "d", "source_id": "s", "needs": ["n"]})
    with pytest.raises(Exception):
        SelectionProposal.model_validate(
            {
                "dataset_id": "d",
                "source_id": "s",
                "reasons": [{"type": "uses_listed_ingredients", "ingredient_refs": ["r" * 201]}],
            }
        )
    with pytest.raises(Exception):
        SelectionProposal.model_validate(
            {"dataset_id": "d", "source_id": "s", "reasons": [{"type": "dish_named_in_title"}] * 7}
        )


def test_incomplete_carries_usage_and_reason() -> None:
    provider, _ = _provider_with([_incomplete_response()])
    with pytest.raises(ProviderIncompleteError) as exc_info:
        asyncio.run(
            provider.complete_recommendation(system="s", user="u", response_model=SelectionProposal)
        )
    err = exc_info.value
    assert err.incomplete_reason == "max_output_tokens"
    assert err.input_tokens == 6016
    assert err.output_tokens == 800
    assert err.reasoning_tokens == 700
    assert err.response_id == "resp-incomplete-1"


# --- finding 2: failure metadata --------------------------------------------


def test_bad_request_maps_to_distinct_controlled_reason() -> None:
    from culinary_copilot.llm.client import ProviderBadRequestError

    provider, _ = _provider_with([_sdk_error(BadRequestError, 400)])
    with pytest.raises(ProviderBadRequestError):
        asyncio.run(
            provider.complete_recommendation(system="s", user="u", response_model=SelectionProposal)
        )


def test_bad_request_is_not_retried_and_not_unavailable() -> None:
    from culinary_copilot.llm.client import ProviderUnavailableError

    provider, stub = _provider_with([_sdk_error(BadRequestError, 400)])
    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            provider.complete_recommendation(system="s", user="u", response_model=SelectionProposal)
        )
    assert not isinstance(exc_info.value, ProviderUnavailableError)
    assert len(stub.kwargs) == 1  # no retry on 4xx


def test_rate_limit_surfaces_as_controlled_service_failure() -> None:
    from culinary_copilot.services.answers import init_state
    from culinary_copilot.services.clarification_service import make_group
    from culinary_copilot.services.store import InMemoryClarificationStore

    state = init_state(
        request_id="req-ratelimit",
        request={"ingredients": ["chicken"]},
        dish="chicken curry",
    )
    store = InMemoryClarificationStore()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    fake = FakeApplicationProvider(script=[ProviderRateLimitError("rate limited by provider")])
    search_all, search_recipes, get_recipe = _repo_patches()
    with search_all, search_recipes, get_recipe:
        with pytest.raises(RecommendationFailure) as exc_info:
            asyncio.run(
                recommend_for_group(
                    store=store,
                    engine=object(),
                    settings=_settings(),
                    provider=fake,
                    epicure=None,
                    group_id=group.group_id,
                    expected_request_revision=state.revision,
                    expected_group_revision=group.revision,
                )
            )
    assert exc_info.value.http_status == 503
    assert exc_info.value.reason == "provider_rate_limited"


def test_sdk_error_metadata_persisted_per_attempt() -> None:
    provider, _ = _provider_with([_sdk_error(NotFoundError, 404)])
    try:
        asyncio.run(
            provider.complete_recommendation(system="s", user="u", response_model=SelectionProposal)
        )
    except Exception as exc:
        details = getattr(exc, "attempt_details", None)
        assert details, "expected per-attempt metadata on the raised error"
        first = details[0]
        assert first["sdk_error"] == "NotFoundError"
        assert first["http_status"] == 404
        assert first["request_id"] == "req-test-1"
    else:
        raise AssertionError("expected an error")


# --- finding 3: native chain -------------------------------------------------


def test_native_chain_replays_reasoning_items() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs-1",
        "summary": [],
        "encrypted_content": "enc-abc",
    }
    call_item = {
        "type": "function_call",
        "id": "fc-1",
        "call_id": "call-1",
        "name": "get_recipe",
        "arguments": '{"dataset_id": "d", "source_id": "s"}',
    }

    class _Item:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload
            self.type = payload["type"]

        def __getattr__(self, name: str) -> Any:
            return self._payload.get(name)

        def model_dump(self) -> dict[str, Any]:
            return dict(self._payload)

    response = SimpleNamespace(
        id="resp-tool-1",
        status="completed",
        incomplete_details=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        output=[_Item(reasoning_item), _Item(call_item)],
        output_parsed=None,
    )
    provider, _ = _provider_with([response])
    result = asyncio.run(
        provider.complete_native_tool_turn(
            input_items=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "name": "get_recipe"}],
            tool_choice={"type": "function", "name": "get_recipe"},
            response_model=None,
        )
    )
    assert len(result.tool_calls) == 1
    types = [item.get("type") for item in result.chain_items]
    assert "reasoning" in types, f"reasoning items must chain, got {types}"
    assert "function_call" in types
    # Order preserved: reasoning before the call it supports.
    assert types.index("reasoning") < types.index("function_call")


# --- finding 4: runner accounting --------------------------------------------


def test_no_provider_call_costs_zero_and_releases_reservation(tmp_path: Path) -> None:
    cases = load_cases(CASES)
    case = next(c for c in cases if c.case_id == "LIVE-03")
    from culinary_copilot.llm.client import FakeApplicationProvider

    fake = FakeApplicationProvider()
    # Empty corpus match: the service abstains pre-provider (no_candidates),
    # so the fake must never be called.
    search_all, search_recipes, get_recipe = _repo_patches_empty()
    from runner import RunState

    state = RunState(tmp_path / "state.json")
    state.load()
    with search_all, search_recipes, get_recipe:
        record = execute_live_case(
            case=case,
            settings=_settings(),
            engine=object(),
            provider=fake,
            out_dir=tmp_path,
            state=state,
            prices={"input": 0.05, "output": 0.40},
            dry_payload_bytes=0,
        )
    assert record["paid"] is False
    assert record["spent_usd"] == 0.0
    assert record["provider_reached"] is False
    assert fake.call_count == 0


def test_summary_written_on_ceiling_stop(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key-for-gate-test")
    search_all, search_recipes, get_recipe = _repo_patches()
    with search_all, search_recipes, get_recipe:
        rc = main(
            [
                "--cases",
                str(CASES),
                "--out",
                str(tmp_path),
                "--live",
                "--ceiling-usd",
                "0.0000001",
                "--price-input-per-1m",
                "0.05",
                "--price-output-per-1m",
                "0.40",
            ]
        )
    assert rc == 3
    summary_path = tmp_path / "summary.json"
    assert summary_path.exists(), "summary.json must exist on every exit path"
    summary = json.loads(summary_path.read_text())
    assert summary.get("stop") == "ceiling"
