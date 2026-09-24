"""Repair-pass-3 regression tests (offline, from phase3_live_run2 findings).

Covers: local-vs-transport failure split with request_sent billing,
server-issued candidate labels, synthetic source-injection fixture
loading, and SDK-validated request construction. No model calls, no
downloads, no application DB use.
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

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "recommendations_live"))

from runner import RunState, execute_live_case, load_cases  # noqa: E402

from culinary_copilot.config import Settings  # noqa: E402
from culinary_copilot.llm.client import (  # noqa: E402
    FakeApplicationProvider,
    OpenAIApplicationProvider,
    ProviderUnavailableError,
)
from culinary_copilot.recommendations.prompts import (  # noqa: E402
    get_recipe_function,
    selection_model_for,
)
from culinary_copilot.recommendations.service import (  # noqa: E402
    RecommendationFailure,
    recommend_for_group,
    resolve_candidate_label,
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
        "title": "Repair3 Curry",
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
        "provenance": {"dataset_id": FOODCOM, "source_id": "000250"},
        "flags": [],
        "available_fields": {"servings": True},
        "quality_issues": [],
        "description": "A curry.",
    }
    base.update(overrides)
    return base


def _repo_patches(doc: dict[str, Any] | None = None):
    rows = [{"dataset_id": FOODCOM, "source_id": "000250", "title": "T"}]
    return (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.search_recipes", return_value=rows),
        patch(
            "culinary_copilot.recipes.repository.get_recipe",
            return_value=doc if doc is not None else _doc(),
        ),
    )


def _ready():
    from culinary_copilot.services.answers import init_state
    from culinary_copilot.services.clarification_service import make_group
    from culinary_copilot.services.store import InMemoryClarificationStore

    state = init_state(
        request_id="req-repair3",
        request={"ingredients": ["chicken"]},
        dish="thai chicken curry",
    )
    store = InMemoryClarificationStore()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    return store, state, group


def _recommend(store, state, group, provider, **overrides):
    search_all, search_recipes, get_recipe = _repo_patches()
    with search_all, search_recipes, get_recipe:
        return asyncio.run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=_settings(),
                provider=provider,
                epicure=None,
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
                **overrides,
            )
        )


def _label_selection(label="1"):
    return {
        "candidate_label": label,
        "ingredient_refs": ["ing-0"],
        "step_refs": ["step-0"],
        "reasons": [],
        "questions": [],
    }


# --- item 2: local vs transport split ---------------------------------------


def test_local_exception_becomes_internal_not_unavailable() -> None:
    provider = OpenAIApplicationProvider(_settings())

    class _Stub:
        async def parse(self, **kwargs: Any) -> Any:
            raise ValueError("local construction bug")

    provider._client = SimpleNamespace(responses=_Stub())
    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            provider.complete_recommendation(
                system="s",
                user="u",
                response_model=__import__(
                    "culinary_copilot.domain.recommendations",
                    fromlist=["SelectionProposal"],
                ).SelectionProposal,
            )
        )
    err = exc_info.value
    assert type(err).__name__ == "ProviderInternalError"
    assert not isinstance(err, ProviderUnavailableError)
    assert err.request_sent is False
    assert err.attempt_details and err.attempt_details[0]["request_sent"] is False


def test_unmapped_sdk_exception_stays_unavailable_and_reached() -> None:
    import openai

    provider = OpenAIApplicationProvider(_settings())

    class _Stub:
        async def parse(self, **kwargs: Any) -> Any:
            request = httpx.Request("POST", "https://api.openai.com/v1/responses")
            raise openai.APIError("unmapped sdk failure", request=request, body=None)

    provider._client = SimpleNamespace(responses=_Stub())
    with pytest.raises(ProviderUnavailableError) as exc_info:
        asyncio.run(
            provider.complete_recommendation(
                system="s",
                user="u",
                response_model=__import__(
                    "culinary_copilot.domain.recommendations",
                    fromlist=["SelectionProposal"],
                ).SelectionProposal,
            )
        )
    assert exc_info.value.request_sent is False  # generic APIError proves no exchange


def test_transport_failure_retried_reached_and_paid(tmp_path: Path) -> None:
    import openai

    provider = OpenAIApplicationProvider(_settings())

    class _Stub:
        def __init__(self):
            self.calls = 0

        async def parse(self, **kwargs: Any) -> Any:
            self.calls += 1
            request = httpx.Request("POST", "https://api.openai.com/v1/responses")
            raise openai.APIConnectionError(request=request)

    stub = _Stub()
    provider._client = SimpleNamespace(responses=stub)
    with pytest.raises(ProviderUnavailableError) as exc_info:
        asyncio.run(
            provider.complete_recommendation(
                system="s",
                user="u",
                response_model=__import__(
                    "culinary_copilot.domain.recommendations",
                    fromlist=["SelectionProposal"],
                ).SelectionProposal,
            )
        )
    assert stub.calls == 2  # initial + one bounded retry
    assert exc_info.value.request_sent is True
    assert len(exc_info.value.attempt_details) == 2


def test_runner_internal_error_costs_zero_and_stops(tmp_path: Path) -> None:
    cases = load_cases(CASES)
    case = next(c for c in cases if c.case_id == "LIVE-02")

    class _Stub:
        async def parse(self, **kwargs: Any) -> Any:
            raise ValueError("local bug during call")

    provider = OpenAIApplicationProvider(_settings())
    provider._client = SimpleNamespace(responses=_Stub())

    search_all, search_recipes, get_recipe = _repo_patches()
    state = RunState(tmp_path / "state.json")
    state.load()
    with search_all, search_recipes, get_recipe:
        record = execute_live_case(
            case=case,
            settings=_settings(),
            engine=object(),
            provider=provider,
            out_dir=tmp_path,
            state=state,
            prices={"input": 0.05, "output": 0.40},
            dry_payload_bytes=6138,
        )
    assert record["status_code"] == 502
    assert record["error_reason"] == "provider_internal_error"
    assert record["provider_reached"] is False
    assert record["paid"] is False
    assert record["spent_usd"] == 0.0
    assert record["stop"] is True


def test_request_construction_valid_through_real_sdk() -> None:
    """Captured default-path kwargs validate through the real SDK (mock
    transport, zero network): no pre-boundary exception exists."""
    from openai import AsyncOpenAI

    selection_text = json.dumps(
        {
            "candidate_label": "1",
            "ingredient_refs": ["ing-0"],
            "step_refs": ["step-0"],
            "reasons": [],
            "questions": [],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-mock-1",
                "object": "response",
                "created_at": 1758720000,
                "model": "gpt-6-luna",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg-1",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": selection_text,
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 2000,
                    "output_tokens": 224,
                    "total_tokens": 2224,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = AsyncOpenAI(api_key="test-key", http_client=httpx.AsyncClient(transport=transport))
    provider = OpenAIApplicationProvider(_settings())
    provider._client = client
    outcome = asyncio.run(
        provider.complete_recommendation(
            system="s", user="u", response_model=selection_model_for(["1"])
        )
    )
    assert outcome.ok is True
    assert outcome.reasoning_effort == "none"
    assert outcome.parsed and outcome.parsed.get("candidate_label") == "1"


# --- item 3: server-issued labels -------------------------------------------


def test_label_schema_is_enum_restricted() -> None:
    model = selection_model_for(["1", "2", "3"])
    schema = model.model_json_schema()
    assert schema["properties"]["candidate_label"] == {
        "enum": ["1", "2", "3"],
        "title": "Candidate Label",
        "type": "string",
        "description": "Server-issued label of exactly one offered candidate.",
    }
    tool = get_recipe_function(["1", "2"])
    assert tool["strict"] is True
    assert tool["parameters"]["properties"]["candidate_label"]["enum"] == ["1", "2"]
    assert tool["parameters"]["required"] == ["candidate_label"]
    assert tool["parameters"]["additionalProperties"] is False


def test_leading_zero_id_resolves_through_label() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(script=[_label_selection("1")])
    result = _recommend(store, state, group, fake)
    assert result["outcome"] == "recommendation"
    assert result["selection"]["source_id"] == "000250"
    assert result["selection"]["dataset_id"] == FOODCOM


def test_stripped_id_rejected_with_proposed_and_offered() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(
        script=[
            {
                "dataset_id": FOODCOM,
                "source_id": "250",  # stripped leading zeros
                "ingredient_refs": ["ing-0"],
                "step_refs": ["step-0"],
                "reasons": [],
                "questions": [],
            }
        ]
    )
    with pytest.raises(RecommendationFailure) as exc_info:
        _recommend(store, state, group, fake)
    assert exc_info.value.reason == "validation_rejected"
    detail = exc_info.value.detail
    assert detail["validation_reason"] == "unknown_identity"
    assert detail["proposed"]["source_id"] == "250"
    assert detail["offered"][0]["source_id"] == "000250"
    assert detail["offered"][0]["candidate_label"] == "1"
    assert detail["attempts"] >= 1
    assert detail["provider_reached"] is True


def test_unoffered_label_rejected() -> None:
    offered = [
        {"label": "1", "dataset_id": FOODCOM, "source_id": "000250"},
    ]
    with pytest.raises(RecommendationFailure) as exc_info:
        resolve_candidate_label("9", offered)
    assert exc_info.value.reason == "validation_rejected"
    assert exc_info.value.detail["proposed"] == {"candidate_label": "9"}
    assert exc_info.value.detail["offered"][0]["source_id"] == "000250"


def test_label_from_another_request_rejected() -> None:
    offered_a = [{"label": "1", "dataset_id": "A", "source_id": "s1"}]
    with pytest.raises(RecommendationFailure):
        resolve_candidate_label("2", offered_a)


def _tool_recommend(store, state, group, fake):
    search_all, search_recipes, get_recipe = _repo_patches()
    with search_all, search_recipes, get_recipe:
        return asyncio.run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=_settings(),
                provider=fake,
                epicure=None,
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
                tool_mode=True,
            )
        )


def _native_call(args: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {
        "native_tool_calls": [
            {"call_id": call_id, "name": "get_recipe", "arguments": json.dumps(args)}
        ]
    }


def _native_parsed(payload: dict[str, Any]) -> dict[str, Any]:
    return {"native_parsed": payload}


def test_tool_mode_label_arguments_resolve() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(
        script=[_native_call({"candidate_label": "1"}), _native_parsed(_label_selection("1"))]
    )
    result = _tool_recommend(store, state, group, fake)
    assert result["outcome"] == "recommendation"
    assert result["selection"]["source_id"] == "000250"


def test_tool_mode_unoffered_label_rejected() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(script=[_native_call({"candidate_label": "7"})])
    with pytest.raises(RecommendationFailure) as exc_info:
        _tool_recommend(store, state, group, fake)
    assert exc_info.value.reason == "validation_rejected"
    assert exc_info.value.detail["proposed"] == {"candidate_label": "7"}


# --- item 4: synthetic fixture ----------------------------------------------


def test_fixture_case_loads_and_validates() -> None:
    cases = load_cases(CASES)
    assert len(cases) == 10
    live01 = next(c for c in cases if c.case_id == "LIVE-01")
    fixture = live01.synthetic_fixture
    assert fixture and fixture.get("synthetic") is True
    assert "SYNTHETIC" in fixture["doc"]["title"]
    assert all("SYNTHETIC" in r["dataset_id"] for r in fixture["rows"])
    assert not any(c.synthetic_fixture for c in cases if c.case_id != "LIVE-01")
    # LIVE-10 tool parity and LIVE-09 Epicure isolation preserved.
    live10 = next(c for c in cases if c.case_id == "LIVE-10")
    assert live10.tool_mode is True
    live09 = next(c for c in cases if c.case_id == "LIVE-09")
    assert live09.config_override.get("epicure_enabled") is False


def test_fixture_validation_rejects_unlabeled() -> None:
    import tempfile

    bad = {
        "cases": [
            {
                "id": "LIVE-X",
                "clarification": {"message": "x", "dish": "x", "request": {}},
                "answer_steps": [],
                "recommendation_request": {"limit": 3},
                "expected_status": 200,
                "expected_outcome": "recommendation",
                "synthetic_fixture": {"rows": [], "doc": {}},
            }
        ]
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(bad, fh)
        path = Path(fh.name)
    with pytest.raises(ValueError, match="synthetic"):
        load_cases(path)


def _sdk_4xx(exc_cls: Any, status: int, body: dict[str, Any]) -> Any:
    import httpx

    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status, request=request)
    return exc_cls("rehearsal 4xx", response=response, body=body)


def test_bad_request_persists_redacted_message_code_param() -> None:
    import asyncio
    from types import SimpleNamespace

    from openai import BadRequestError

    from culinary_copilot.llm.client import OpenAIApplicationProvider
    from culinary_copilot.recommendations.service import RecommendationFailure
    from culinary_copilot.services.answers import init_state
    from culinary_copilot.services.clarification_service import make_group
    from culinary_copilot.services.store import InMemoryClarificationStore

    body = {
        "error": {
            "message": "Invalid tool schema, got Bearer abcdef-secret key sk-live-SECRET123",
            "code": "invalid_tool_schema",
            "param": "tools[0].parameters",
        }
    }

    class _Stub:
        async def parse(self, **kwargs: Any) -> Any:
            raise _sdk_4xx(BadRequestError, 400, body)

    provider = OpenAIApplicationProvider(_settings())
    provider._client = SimpleNamespace(responses=_Stub())
    state = init_state(
        request_id="req-4xx", request={"ingredients": ["chicken"]}, dish="thai chicken curry"
    )
    store = InMemoryClarificationStore()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    search_all, search_recipes, get_recipe = _repo_patches()
    with search_all, search_recipes, get_recipe:
        with pytest.raises(RecommendationFailure) as exc_info:
            asyncio.run(
                recommend_for_group(
                    store=store,
                    engine=object(),
                    settings=_settings(),
                    provider=provider,
                    epicure=None,
                    group_id=group.group_id,
                    expected_request_revision=state.revision,
                    expected_group_revision=group.revision,
                    tool_mode=True,
                )
            )
    assert exc_info.value.http_status == 502
    assert exc_info.value.reason == "provider_bad_request"
    detail = exc_info.value.detail
    assert detail["error_code"] == "invalid_tool_schema"
    assert detail["error_param"] == "tools[0].parameters"
    assert len(detail["error_message"]) <= 500
    assert "SECRET123" not in detail["error_message"]
    assert "abcdef-secret" not in detail["error_message"]
