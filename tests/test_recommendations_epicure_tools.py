"""Phase 3 Stage B tests: Epicure outcomes and bounded tool calling (offline).

Fake provider + stub Epicure core + patched repository only. No model
calls, no downloads, no application DB writes.
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from culinary_copilot.config import Settings
from culinary_copilot.domain.recommendations import EpicureOutcome
from culinary_copilot.llm.client import FakeApplicationProvider
from culinary_copilot.recommendations.epicure import (
    CachedEpicureAdapter,
    FakeEpicureAdapter,
)
from culinary_copilot.recommendations.service import (
    RecommendationFailure,
    recommend_for_group,
)
from culinary_copilot.services.answers import init_state
from culinary_copilot.services.clarification_service import make_group
from culinary_copilot.services.store import InMemoryClarificationStore

FOODCOM = "AkashPS11/recipes_data_food.com"


def _settings(**kwargs: Any) -> Settings:
    base: dict[str, Any] = {"llm_recommendation_enabled": True}
    base.update(kwargs)
    return Settings(_env_file=None, **base)


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
                "canonical": "garlic",
                "original": "2 cloves garlic",
                "amount": 2.0,
                "amount_text": "2",
                "quantity_text": "2 cloves",
                "unit": None,
                "unit_text": "",
                "notes": "",
                "optional": False,
            },
        ],
        "instructions": ["Cook the chicken.", "Add garlic and serve."],
        "provenance": {"dataset_id": FOODCOM, "source_id": "000159"},
        "flags": [],
        "capabilities": {"searchable": True},
        "available_fields": {"servings": True},
        "quality_issues": [],
        "nutrition": {},
        "description": "A curry.",
        "source_url": None,
    }
    base.update(overrides)
    return base


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


def _ready(**overrides: Any):
    kwargs: dict[str, Any] = {
        "request_id": "req-epi",
        "request": {"ingredients": ["chicken", "garlic"], "time_minutes": 30},
        "dish": "chicken curry",
    }
    kwargs.update(overrides)
    state = init_state(**kwargs)
    store = InMemoryClarificationStore()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    return store, state, group


def _run(coro):
    return asyncio.run(coro)


def _recommend(store, state, group, fake, epicure, **kwargs):
    settings = kwargs.pop("settings", _settings())
    with (
        patch(
            "culinary_copilot.recipes.repository.search_all",
            return_value=[{"dataset_id": FOODCOM, "source_id": "000159", "title": "T"}],
        ),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()),
    ):
        return _run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=settings,
                provider=fake,
                epicure=epicure,
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
                **kwargs,
            )
        )


# --- Epicure outcomes ----------------------------------------------------


def test_epicure_consulted_early_with_canonical_names() -> None:
    store, state, group = _ready()
    epicure = FakeEpicureAdapter(
        script=[
            (
                EpicureOutcome.CONSULTED,
                [{"ingredient": "garlic", "score": 0.7, "for": "chicken"}],
                "neighbors found",
            )
        ]
    )
    fake = FakeApplicationProvider(script=[_selection()])
    result = _recommend(store, state, group, fake, epicure)
    assert result["outcome"] == "recommendation"
    # Early consultation used real canonical ingredient names.
    assert epicure.calls[0]["ingredients"] == ["chicken", "garlic"]
    assert result["epicure"]["outcome"] == "consulted"
    assert result["epicure"]["degraded"] is False
    # Later assessment: garlic is in the source -> used as pairing note;
    # nothing enters the rendered recipe.
    assessment = result["epicure"]["assessment"]
    assert [s["ingredient"] for s in assessment["used"]] == ["garlic"]
    assert assessment["rejected"] == []
    assert "garlic" in str(result["recipe"])


def test_epicure_suggestion_absent_from_source_is_deferred() -> None:
    store, state, group = _ready()
    epicure = FakeEpicureAdapter(
        script=[
            (
                EpicureOutcome.CONSULTED,
                [{"ingredient": "truffle", "score": 0.9, "for": "chicken"}],
                "neighbors found",
            )
        ]
    )
    fake = FakeApplicationProvider(script=[_selection()])
    result = _recommend(store, state, group, fake, epicure)
    assessment = result["epicure"]["assessment"]
    assert assessment["used"] == []
    assert assessment["rejected"][0]["use"] == "deferred"
    assert "truffle" not in str(result["recipe"])


def test_simple_technique_skip_policy_unit() -> None:
    from culinary_copilot.recommendations.service import _simple_technique_skip

    toast = init_state(request_id="s1", request={}, dish="toast")
    assert _simple_technique_skip(toast) is not None
    curry = init_state(request_id="s2", request={"ingredients": ["chicken"]}, dish="chicken curry")
    assert _simple_technique_skip(curry) is None
    toast_with_pantry = init_state(
        request_id="s3", request={"ingredients": ["bread"]}, dish="toast"
    )
    assert _simple_technique_skip(toast_with_pantry) is None


def test_epicure_skip_never_claims_consultation() -> None:
    store, state, group = _ready(request_id="req-skip", request={"ingredients": []}, dish="toast")
    # Dish-only toast is not retrieval-ready (no dish AND no ingredients
    # means... dish IS set, so ready). Pantry empty -> skip policy applies.
    epicure = FakeEpicureAdapter()
    fake = FakeApplicationProvider(script=[_selection()])
    with (
        patch(
            "culinary_copilot.recipes.repository.search_all",
            return_value=[{"dataset_id": FOODCOM, "source_id": "000159", "title": "T"}],
        ),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()),
    ):
        result = _run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=_settings(),
                provider=fake,
                epicure=epicure,
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
            )
        )
    assert result["epicure"]["outcome"] == "simple_technique_skip"
    assert epicure.call_count == 0
    assert "Simple-technique skip" in result["epicure"]["note"]


def test_epicure_disabled_and_unavailable_proceed_degraded() -> None:
    for epicure, outcome in [
        (None, "disabled"),
        (
            FakeEpicureAdapter(script=[(EpicureOutcome.UNAVAILABLE, [], "vectors missing")]),
            "unavailable",
        ),
    ]:
        store, state, group = _ready()
        fake = FakeApplicationProvider(script=[_selection()])
        result = _recommend(store, state, group, fake, epicure)
        assert result["outcome"] == "recommendation"
        assert result["epicure"]["outcome"] == outcome
        assert result["epicure"]["degraded"] is True
        assert "gates retained" in result["epicure"]["degradation_note"]
        # Grounding gates retained: rendered content still source-exact.
        assert result["recipe"]["ingredients"][0]["amount"] == 1.0


def test_epicure_unmapped_and_insufficient_context_recorded() -> None:
    for script_outcome in (
        (EpicureOutcome.UNMAPPED, [], "no mapping"),
        (EpicureOutcome.INSUFFICIENT_INGREDIENT_CONTEXT, [], "no ingredients"),
    ):
        store, state, group = _ready()
        epicure = FakeEpicureAdapter(script=[script_outcome])
        fake = FakeApplicationProvider(script=[_selection()])
        result = _recommend(store, state, group, fake, epicure)
        assert result["outcome"] == "recommendation"
        assert result["epicure"]["outcome"] == script_outcome[0].value
        assert result["epicure"]["degraded"] is False


def test_epicure_bounded_calls_single_consultation() -> None:
    store, state, group = _ready()
    epicure = FakeEpicureAdapter()
    fake = FakeApplicationProvider(script=[_selection()])
    _recommend(store, state, group, fake, epicure)
    assert epicure.call_count == 1
    assert epicure.calls[0]["k"] == _settings().rec_epicure_suggestion_count
    assert len(epicure.calls[0]["ingredients"]) <= _settings().rec_epicure_max_ingredients


# --- CachedEpicureAdapter (no downloads) ---------------------------------


def _stub_core(
    *,
    enabled: bool,
    mapping: dict[str, list[tuple[str, float]]] | None = None,
    fail: Exception | None = None,
):
    from culinary_copilot.tools.epicure import UnknownIngredientError

    mapping = mapping or {}

    class _Stub:
        def __init__(self) -> None:
            self.settings = SimpleNamespace(epicure_enabled=enabled)
            self.calls: list[tuple[str, int]] = []

        def find_balanced_pairings(self, ingredient: str, k: int):
            self.calls.append((ingredient, k))
            if fail is not None:
                raise fail
            if ingredient not in mapping:
                raise UnknownIngredientError(ingredient)
            from culinary_copilot.tools.epicure import Pairing

            return [Pairing(ingredient=n, score=s) for n, s in mapping[ingredient][:k]]

    return _Stub()


def test_cached_adapter_disabled_unmapped_unavailable() -> None:
    adapter = CachedEpicureAdapter(_stub_core(enabled=False))
    outcome, suggestions, _ = adapter.consult(["chicken"], suggestion_count=5)
    assert outcome == EpicureOutcome.DISABLED and suggestions == []

    adapter = CachedEpicureAdapter(_stub_core(enabled=True, mapping={}))
    outcome, _, note = adapter.consult(["mysteryfruit"], suggestion_count=5)
    assert outcome == EpicureOutcome.UNMAPPED
    assert "mysteryfruit" in note

    adapter = CachedEpicureAdapter(_stub_core(enabled=True, mapping={}, fail=OSError("disk")))
    outcome, suggestions, _ = adapter.consult(["chicken"], suggestion_count=5)
    assert outcome == EpicureOutcome.UNAVAILABLE and suggestions == []

    adapter = CachedEpicureAdapter(_stub_core(enabled=True, mapping={}))
    outcome, _, _ = adapter.consult([], suggestion_count=5)
    assert outcome == EpicureOutcome.INSUFFICIENT_INGREDIENT_CONTEXT


def test_cached_adapter_consulted_and_bounded() -> None:
    core = _stub_core(enabled=True, mapping={"chicken": [("garlic", 0.8), ("onion", 0.6)]})
    adapter = CachedEpicureAdapter(core)
    outcome, suggestions, _ = adapter.consult(["chicken"], suggestion_count=1)
    assert outcome == EpicureOutcome.CONSULTED
    assert suggestions == [{"ingredient": "garlic", "score": 0.8, "for": "chicken"}]
    assert core.calls == [("chicken", 1)]


# --- tool-calling mode ---------------------------------------------------


def _tool_recommend(store, state, group, fake, epicure=None, **kwargs):
    settings = kwargs.pop("settings", _settings())
    with (
        patch(
            "culinary_copilot.recipes.repository.search_all",
            return_value=[{"dataset_id": FOODCOM, "source_id": "000159", "title": "T"}],
        ),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()),
    ):
        return _run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=settings,
                provider=fake,
                epicure=epicure or FakeEpicureAdapter(),
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
                tool_mode=True,
                **kwargs,
            )
        )


def _native_call(
    name: str = "get_recipe",
    args: dict[str, Any] | None = None,
    raw_arguments: str | None = None,
    call_id: str = "call-1",
) -> dict[str, Any]:
    """Fake native function-call envelope for one get_recipe turn."""
    import json as _json

    arguments = raw_arguments
    if arguments is None:
        arguments = _json.dumps(
            args if args is not None else {"dataset_id": FOODCOM, "source_id": "000159"}
        )
    return {"native_tool_calls": [{"call_id": call_id, "name": name, "arguments": arguments}]}


def _native_parsed(payload: dict[str, Any]) -> dict[str, Any]:
    return {"native_parsed": payload}


def test_tool_mode_success_uses_same_validation_and_rendering() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(script=[_native_call(), _native_parsed(_selection())])
    result = _tool_recommend(store, state, group, fake)
    assert result["outcome"] == "recommendation"
    assert result["recipe"]["ingredients"][1]["name"] == "garlic"
    assert [s["text"] for s in result["recipe"]["instructions"]] == [
        "Cook the chicken.",
        "Add garlic and serve.",
    ]
    assert result["usage"]["turns"] is not None and len(result["usage"]["turns"]) == 2
    assert result["usage"]["turns"][0]["tool_calls"] == 1
    native_calls = [c for c in fake.calls if c.get("kind") == "native_tool"]
    assert len(native_calls) == 2
    assert "get_recipe" in native_calls[0]["tools"]
    # First turn saw metadata only: no full documents before fetching.
    first_input = native_calls[0]["input"]
    assert "000159" in first_input
    assert "Cook the chicken." not in first_input
    assert "Add garlic and serve." not in first_input
    # The tool result re-enters with the provider's call identifier.
    second_input = str(native_calls[1]["input_items"])
    assert "call-1" in second_input
    assert "function_call_output" in second_input
    assert "Cook the chicken." in second_input


def test_tool_mode_default_off_single_call() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(script=[_selection()])
    result = _recommend(store, state, group, fake, FakeEpicureAdapter())
    assert result["outcome"] == "recommendation"
    assert fake.call_count == 1
    assert "Cook the chicken." in fake.calls[0]["user"]


def test_tool_mode_invalid_tool_name_rejected() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(script=[_native_call(name="search_web")])
    with pytest.raises(RecommendationFailure) as exc_info:
        _tool_recommend(store, state, group, fake)
    assert exc_info.value.http_status == 502
    assert exc_info.value.reason == "invalid_tool_call"


def test_tool_mode_malformed_arguments_rejected() -> None:
    cases = [
        "not json at all",
        '["dataset_id", "source_id"]',
        '{"dataset_id": "d"}',
        '{"dataset_id": "d", "source_id": 5}',
        '{"dataset_id": "d", "source_id": "s", "extra": true}',
        '{"dataset_id": "", "source_id": "s"}',
    ]
    for raw in cases:
        store, state, group = _ready()
        fake = FakeApplicationProvider(script=[_native_call(raw_arguments=raw)])
        with pytest.raises(RecommendationFailure) as exc_info:
            _tool_recommend(store, state, group, fake)
        assert exc_info.value.http_status == 502
        assert exc_info.value.reason == "invalid_tool_call"


def test_tool_mode_absent_and_multiple_calls_rejected() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(script=[_native_parsed(_selection())])
    with pytest.raises(RecommendationFailure) as exc_info:
        _tool_recommend(store, state, group, fake)
    assert exc_info.value.reason == "invalid_tool_call"
    assert exc_info.value.detail.get("arguments_error") == "absent_call"

    store, state, group = _ready()
    fake = FakeApplicationProvider(
        script=[
            {
                "native_tool_calls": [
                    {
                        "call_id": "call-1",
                        "name": "get_recipe",
                        "arguments": ('{"dataset_id": "%s", "source_id": "000159"}' % FOODCOM),
                    },
                    {
                        "call_id": "call-2",
                        "name": "get_recipe",
                        "arguments": ('{"dataset_id": "%s", "source_id": "000159"}' % FOODCOM),
                    },
                ]
            }
        ]
    )
    with pytest.raises(RecommendationFailure) as exc_info:
        _tool_recommend(store, state, group, fake)
    assert exc_info.value.reason == "invalid_tool_call"
    assert exc_info.value.detail.get("arguments_error") == "multiple_calls"


def test_tool_mode_unknown_identity_rejected() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(
        script=[_native_call(args={"dataset_id": FOODCOM, "source_id": "nope"})]
    )
    with pytest.raises(RecommendationFailure) as exc_info:
        _tool_recommend(store, state, group, fake)
    assert exc_info.value.http_status == 502
    assert exc_info.value.reason == "validation_rejected"
    assert exc_info.value.detail.get("validation_reason") == "unknown_identity"


def test_tool_mode_second_tool_call_hits_turn_limit() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(script=[_native_call(), _native_call(call_id="call-2")])
    with pytest.raises(RecommendationFailure) as exc_info:
        _tool_recommend(store, state, group, fake)
    # No agent loop: the final turn must be a selection, not another call.
    assert exc_info.value.http_status == 502
    assert exc_info.value.reason == "turn_limit_exceeded"


def test_tool_mode_final_selection_validated_like_default() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(
        script=[_native_call(), _native_parsed(_selection(step_refs=["step-1", "step-0"]))]
    )
    with pytest.raises(RecommendationFailure) as exc_info:
        _tool_recommend(store, state, group, fake)
    assert exc_info.value.http_status == 502
    assert exc_info.value.reason == "validation_rejected"
    assert exc_info.value.detail.get("validation_reason") == "step_coverage"


def test_tool_mode_turn_ceiling_enforced() -> None:
    store, state, group = _ready()
    fake = FakeApplicationProvider(script=[_native_call(), _native_parsed(_selection())])
    with pytest.raises(RecommendationFailure) as exc_info:
        _tool_recommend(store, state, group, fake, settings=_settings(rec_max_provider_turns=1))
    assert exc_info.value.http_status == 502
    assert exc_info.value.reason == "turn_limit_exceeded"
