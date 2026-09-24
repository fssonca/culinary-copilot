"""Chained input-item field tests (offline; run5 LIVE-10 400 finding).

The server rejects undocumented input fields (observed: status on a
replayed function_call → 400 unknown_parameter). Chain construction uses
only documented input fields; the pre-send guard and the strict
rehearsal stub enforce the same set. No model calls, no network beyond
localhost loopback in one test.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "recommendations_live"))

from culinary_copilot.config import Settings  # noqa: E402
from culinary_copilot.llm.client import (  # noqa: E402
    OpenAIApplicationProvider,
    ProviderInternalError,
    _chain_fields,
    validate_chained_input,
)

FULL_DUMP = {
    "type": "function_call",
    "id": "fc-1",
    "call_id": "call-1",
    "name": "get_recipe",
    "arguments": '{"candidate_label": "1"}',
    "status": "completed",
    "async_": False,
    "caller": {"type": "direct"},
    "namespace": "default",
    "parsed_arguments": {"candidate_label": "1"},
}

REASONING_DUMP = {
    "type": "reasoning",
    "id": "rs-1",
    "summary": [{"type": "summary_text", "text": "s"}],
    "content": [{"type": "reasoning_text", "text": "t"}],
    "encrypted_content": "enc-abc",
    "status": "completed",
}


def test_full_dump_chained_to_documented_fields_only() -> None:
    chained = _chain_fields(dict(FULL_DUMP), ("type", "id", "call_id", "name", "arguments"))
    assert chained == {
        "type": "function_call",
        "id": "fc-1",
        "call_id": "call-1",
        "name": "get_recipe",
        "arguments": '{"candidate_label": "1"}',
    }
    assert "status" not in chained


def test_reasoning_chained_with_and_without_optionals() -> None:
    from culinary_copilot.llm.client import CHAIN_REASONING_FIELDS

    chained = _chain_fields(dict(REASONING_DUMP), CHAIN_REASONING_FIELDS)
    assert chained == {
        "type": "reasoning",
        "id": "rs-1",
        "summary": [{"type": "summary_text", "text": "s"}],
        "content": [{"type": "reasoning_text", "text": "t"}],
        "encrypted_content": "enc-abc",
    }
    minimal = {"type": "reasoning", "id": "rs-2", "summary": []}
    assert _chain_fields(dict(minimal), CHAIN_REASONING_FIELDS) == minimal


def test_sdk_object_chained_without_dump() -> None:
    class _Item:
        type = "function_call"

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def __getattr__(self, name: str) -> Any:
            return self._payload.get(name)

        def model_dump(self) -> dict[str, Any]:
            return dict(self._payload)

    from culinary_copilot.llm.client import CHAIN_FUNCTION_CALL_FIELDS

    chained = _chain_fields(_Item(dict(FULL_DUMP)), CHAIN_FUNCTION_CALL_FIELDS)
    assert sorted(chained) == ["arguments", "call_id", "id", "name", "type"]


def test_guard_fires_on_unknown_key() -> None:
    settings = Settings(_env_file=None, llm_enabled=False, llm_recommendation_enabled=True)
    provider = OpenAIApplicationProvider(settings)
    provider._client = SimpleNamespace(responses=SimpleNamespace())
    chained = [dict(FULL_DUMP)]  # status + SDK extras must never be sent
    with pytest.raises(ProviderInternalError) as exc_info:
        asyncio.run(
            provider.complete_native_tool_turn(
                input_items=[
                    {"role": "user", "content": "x"},
                    *chained,
                    {"type": "function_call_output", "call_id": "call-1", "output": "{}"},
                ],
                tools=None,
                tool_choice=None,
                response_model=None,
            )
        )
    assert exc_info.value.request_sent is False
    assert "status" in str(exc_info.value)


def test_guard_accepts_documented_chain() -> None:
    from culinary_copilot.llm.client import CHAIN_FUNCTION_CALL_FIELDS

    validate_chained_input(
        [
            {"role": "user", "content": "x"},
            _chain_fields(dict(FULL_DUMP), CHAIN_FUNCTION_CALL_FIELDS),
            {"type": "function_call_output", "call_id": "call-1", "output": "{}"},
            _chain_fields(
                dict(REASONING_DUMP),
                ("type", "id", "summary", "content", "encrypted_content"),
            ),
        ]
    )


def test_strict_stub_rejects_old_chain_accepts_new() -> None:
    from runner import _validate_stub_input

    ok, message, param = _validate_stub_input([dict(FULL_DUMP)])
    assert ok is False
    assert param == "input[0].status"
    assert "Unknown parameter" in message
    from culinary_copilot.llm.client import CHAIN_FUNCTION_CALL_FIELDS

    ok, _, _ = _validate_stub_input(
        [
            {"role": "user", "content": "x"},
            _chain_fields(dict(FULL_DUMP), CHAIN_FUNCTION_CALL_FIELDS),
            {"type": "function_call_output", "call_id": "call-1", "output": "{}"},
        ]
    )
    assert ok is True


def test_turn1_usage_kept_when_turn2_fails() -> None:
    """Prior turns' usage/ids survive a later-turn failure in the detail."""
    from unittest.mock import patch

    from culinary_copilot.llm.client import FakeApplicationProvider, ProviderUnavailableError
    from culinary_copilot.recommendations.service import RecommendationFailure, recommend_for_group
    from culinary_copilot.services.answers import init_state
    from culinary_copilot.services.clarification_service import make_group
    from culinary_copilot.services.store import InMemoryClarificationStore

    foodcom = "AkashPS11/recipes_data_food.com"
    doc = {
        "title": "T",
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
        "provenance": {"dataset_id": foodcom, "source_id": "000250"},
        "flags": [],
        "available_fields": {"servings": True},
        "quality_issues": [],
        "description": "T",
    }
    rows = [{"dataset_id": foodcom, "source_id": "000250", "title": "T"}]
    script = [
        {
            "native_tool_calls": [
                {
                    "call_id": "call-1",
                    "name": "get_recipe",
                    "arguments": json.dumps({"candidate_label": "1"}),
                }
            ]
        },
        ProviderUnavailableError("boom"),
    ]
    state = init_state(
        request_id="req-chain", request={"ingredients": ["chicken"]}, dish="thai chicken curry"
    )
    store = InMemoryClarificationStore()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    search_all = patch("culinary_copilot.recipes.repository.search_all", return_value=rows)
    search_recipes = patch("culinary_copilot.recipes.repository.search_recipes", return_value=rows)
    get_recipe = patch("culinary_copilot.recipes.repository.get_recipe", return_value=doc)
    with search_all, search_recipes, get_recipe:
        with pytest.raises(RecommendationFailure) as exc_info:
            asyncio.run(
                recommend_for_group(
                    store=store,
                    engine=object(),
                    settings=Settings(
                        _env_file=None,
                        llm_enabled=False,
                        llm_recommendation_enabled=True,
                        epicure_enabled=False,
                    ),
                    provider=FakeApplicationProvider(script=script),
                    epicure=None,
                    group_id=group.group_id,
                    expected_request_revision=state.revision,
                    expected_group_revision=group.revision,
                    tool_mode=True,
                )
            )
    detail = exc_info.value.detail
    assert "prior_turns" in detail
    assert detail["prior_turns"]["attempts"] >= 1
