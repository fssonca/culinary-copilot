"""Native-tool continuation, budgeting and evidence snapshots (repair pass 4).

Regression for the confirmed defect: ``_tool_mode_selection`` built
final-selection instructions but never sent them (turn 2 kept the turn-1
"reply with ONLY one native get_recipe function call" system item), budgeted
that unused payload instead of the continuation actually sent, and validated
the earlier candidate while the model saw a refetched one. Every assertion
here inspects the exact provider input. Offline: no model calls, no
downloads, repository functions patched.
"""

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "recommendations_live"))

from runner import _bill_failure, turn_bounds_for_case  # noqa: E402

from culinary_copilot.config import Settings  # noqa: E402
from culinary_copilot.llm.client import (  # noqa: E402
    NativeToolCall,
    NativeTurnResult,
    ProviderTimeoutError,
    request_size,
)
from culinary_copilot.recommendations.evidence import (  # noqa: E402
    build_candidate,
    evidence_fingerprint,
    render_recipe,
)
from culinary_copilot.recommendations.prompts import (  # noqa: E402
    TOOL_FINAL_SELECTION_SYSTEM_PROMPT,
    TOOL_METADATA_SYSTEM_PROMPT,
)
from culinary_copilot.recommendations.service import (  # noqa: E402
    RecommendationFailure,
    recommend_for_group,
)

FOODCOM = "AkashPS11/recipes_data_food.com"
FOODIE = "odunola/foodie"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_enabled": False,
        "llm_recommendation_enabled": True,
        "epicure_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _ingredient(canonical: str) -> dict[str, Any]:
    return {
        "canonical": canonical,
        "original": f"1 {canonical}",
        "amount": 1.0,
        "amount_text": "1",
        "quantity_text": "1",
        "unit": None,
        "unit_text": None,
        "notes": "",
        "optional": False,
    }


def _doc(sid: str = "000250", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Thai Chicken Curry",
        "servings": 4.0,
        "durations_minutes": {"TotalTime": 33.0},
        "ingredients": [_ingredient("coconut milk"), _ingredient("boneless chicken")],
        "instructions": ["Simmer the coconut milk 15 minutes.", "Add chicken; cook 8 minutes."],
        "provenance": {"dataset_id": FOODCOM, "source_id": sid},
        "flags": [],
        "quality_issues": [],
    }
    base.update(overrides)
    return base


class RecordingProvider:
    """Native-turn provider that records exact inputs and reports usage."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def complete_native_tool_turn(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
        response_model: Any,
    ) -> NativeTurnResult:
        self.calls.append(
            {
                "input_items": copy.deepcopy(input_items),
                "tools": copy.deepcopy(tools),
                "tool_choice": copy.deepcopy(tool_choice),
                "response_model": response_model,
            }
        )
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    async def complete_recommendation(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("tool mode must not use the single-call path")


REASONING = {"type": "reasoning", "id": "rs_turn1", "summary": [], "encrypted_content": "enc=="}


def _turn1(label: str = "1", call_id: str = "call_live") -> NativeTurnResult:
    call = NativeToolCall(
        call_id=call_id, name="get_recipe", arguments=json.dumps({"candidate_label": label})
    )
    return NativeTurnResult(
        tool_calls=[call],
        chain_items=[
            dict(REASONING),
            {
                "type": "function_call",
                "id": "fc_turn1",
                "call_id": call_id,
                "name": "get_recipe",
                "arguments": call.arguments,
            },
        ],
        model="gpt-5-nano",
        latency_ms=5,
        attempts=1,
        input_tokens=307,
        output_tokens=33,
        reasoning_tokens=0,
        response_id="resp_turn1",
    )


def _turn2(label: str = "1", **overrides: Any) -> NativeTurnResult:
    parsed: dict[str, Any] = {
        "candidate_label": label,
        "ingredient_refs": ["ing-0", "ing-1"],
        "step_refs": ["step-0", "step-1"],
        "reasons": [{"type": "reported_time_within_limit", "ingredient_refs": []}],
        "questions": [],
    }
    parsed.update(overrides)
    return NativeTurnResult(
        parsed=parsed,
        model="gpt-5-nano",
        latency_ms=5,
        attempts=1,
        input_tokens=718,
        output_tokens=121,
        reasoning_tokens=0,
        response_id="resp_turn2",
    )


def _run(
    provider: RecordingProvider,
    *,
    fetches: list[dict[str, Any]],
    rows: list[dict[str, Any]] | None = None,
    request: dict[str, Any] | None = None,
    **settings: Any,
) -> dict[str, Any]:
    from culinary_copilot.services.answers import init_state
    from culinary_copilot.services.clarification_service import make_group
    from culinary_copilot.services.store import InMemoryClarificationStore

    state = init_state(
        request_id="req-cont",
        request=request or {"ingredients": ["chicken"], "time_minutes": 40},
        dish="thai chicken curry",
    )
    store = InMemoryClarificationStore()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    rows = rows or [{"dataset_id": FOODCOM, "source_id": "000250", "title": "T"}]
    queue = list(fetches)
    with (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.search_recipes", return_value=rows),
        patch(
            "culinary_copilot.recipes.repository.get_recipe",
            side_effect=lambda *a, **k: copy.deepcopy(queue.pop(0)),
        ),
    ):
        return asyncio.run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=_settings(**settings),
                provider=provider,
                epicure=None,
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
                tool_mode=True,
            )
        )


def _fail(provider: RecordingProvider, **kwargs: Any) -> RecommendationFailure:
    with pytest.raises(RecommendationFailure) as info:
        _run(provider, **kwargs)
    return info.value


# --- turn 2 carries the final task, not the tool-only instruction ------------


def test_final_selection_instructions_are_sent_in_turn_2() -> None:
    provider = RecordingProvider([_turn1(), _turn2()])
    result = _run(provider, fetches=[_doc(), _doc()])
    assert result["outcome"] == "recommendation"
    turn2 = provider.calls[1]["input_items"]
    system = turn2[0]
    assert system["role"] == "system"
    assert system["content"].startswith(TOOL_FINAL_SELECTION_SYSTEM_PROMPT)
    # Authoritative request constraints travel into the final selection.
    assert "time_ceiling=40.0" in system["content"]
    assert "time_limit=supported" in system["content"]
    assert "fields_not_provided=['portions', 'dietary_constraints']" in system["content"]
    # The final schema carries typed propositions and only the fetched label.
    schema = provider.calls[1]["response_model"].model_json_schema()
    label_schema = schema["properties"]["candidate_label"]
    assert label_schema.get("enum", [label_schema.get("const")]) == ["1"]
    assert {"reasons", "questions"} <= set(schema["properties"])
    assert "selection_reasons" not in schema["properties"]


def test_no_tool_only_instruction_governs_turn_2() -> None:
    provider = RecordingProvider([_turn1(), _turn2()])
    _run(provider, fetches=[_doc(), _doc()])
    turn1, turn2 = provider.calls
    assert turn1["input_items"][0]["content"] == TOOL_METADATA_SYSTEM_PROMPT
    assert turn1["tool_choice"] == {"type": "function", "name": "get_recipe"}
    serialized = json.dumps(turn2["input_items"])
    assert TOOL_METADATA_SYSTEM_PROMPT not in serialized
    assert "ONLY one native get_recipe function call" not in serialized
    assert turn2["tools"] is None and turn2["tool_choice"] is None
    assert sum(1 for i in turn2["input_items"] if i.get("role") == "system") == 1


def test_continuation_items_and_call_ids_survive_in_order() -> None:
    provider = RecordingProvider([_turn1(call_id="call_abc"), _turn2()])
    _run(provider, fetches=[_doc(), _doc()])
    turn1, turn2 = provider.calls
    items = turn2["input_items"]
    # Retained turn-1 user message, byte-identical.
    assert items[1] == turn1["input_items"][1]
    assert items[2] == REASONING
    assert items[3] == {
        "type": "function_call",
        "id": "fc_turn1",
        "call_id": "call_abc",
        "name": "get_recipe",
        "arguments": json.dumps({"candidate_label": "1"}),
    }
    assert items[4]["type"] == "function_call_output"
    assert items[4]["call_id"] == "call_abc"
    assert len(items) == 5  # no user turn after the tool output


def test_missing_function_call_continuation_item_fails_closed() -> None:
    first = _turn1()
    first.chain_items = [dict(REASONING)]
    provider = RecordingProvider([first])
    failure = _fail(provider, fetches=[_doc()])
    assert failure.reason == "invalid_tool_call"
    assert failure.detail["arguments_error"] == "missing_continuation_item"
    assert len(provider.calls) == 1


# --- budgeting the actual continuation --------------------------------------


def test_budget_measures_the_exact_serialized_continuation() -> None:
    provider = RecordingProvider([_turn1(), _turn2()])
    result = _run(provider, fetches=[_doc(), _doc()])
    turn1, turn2 = provider.calls
    sizes = result["evidence"]["request_chars_per_turn"]
    assert (
        sizes[0]
        == request_size(
            input_items=turn1["input_items"], tools=turn1["tools"], tool_choice=turn1["tool_choice"]
        )["chars"]
    )
    assert (
        sizes[1]
        == request_size(input_items=turn2["input_items"], response_model=turn2["response_model"])[
            "chars"
        ]
    )
    # Characters vs bytes are reported separately (bytes >= chars).
    assert all(
        b >= c
        for b, c in zip(result["evidence"]["request_utf8_bytes_per_turn"], sizes, strict=True)
    )


def test_fitting_tool_block_cannot_bypass_full_continuation_overflow() -> None:
    probe = RecordingProvider([_turn1(), _turn2()])
    sizes = _run(probe, fetches=[_doc(), _doc()])["evidence"]["request_chars_per_turn"]
    limit = sizes[1] - 1  # turn 1 and the tool block fit; the continuation does not
    assert sizes[0] <= limit
    provider = RecordingProvider([_turn1()])
    failure = _fail(provider, fetches=[_doc(), _doc()], llm_rec_max_input_chars=limit)
    assert failure.reason == "input_budget_exceeded"
    assert failure.detail["turn"] == 2
    assert failure.detail["required_chars"] == sizes[1]
    assert failure.detail["limit_chars"] == limit
    assert failure.detail["tool_result_chars"] <= failure.detail["evidence_limit_chars"]
    assert failure.detail["tool_result_chars"] < limit
    assert len(provider.calls) == 1  # turn 2 never sent
    # Turn-1 accounting preserved; the unsent turn has zero attempts.
    assert failure.detail["prior_turns"]["input_tokens"] == 307
    assert failure.detail["prior_response_ids"] == ["resp_turn1"]
    assert failure.detail["attempts"] == 0
    assert failure.detail["provider_reached"] is True


def test_unsent_turn_2_bills_turn_1_only_and_reservation_bounds_continuation() -> None:
    prices = {"input": 0.05, "output": 0.40}
    bounds = turn_bounds_for_case(
        tool_mode=True,
        dry_payload_bytes=1700,
        max_input_chars=12000,
        evidence_max_chars=6000,
        max_output_tokens=6500,
    )
    assert bounds[1] == 12000 * 4 + 6500
    reservation = {"turn_bounds": bounds, "max_output_tokens": 6500, "cost_usd": 1.0}
    inner = {
        "attempts": 0,
        "provider_reached": True,
        "prior_turns": {
            "attempts": 1,
            "turns": [{"attempts": 1, "input_tokens": 307, "output_tokens": 33}],
        },
    }
    charged, sent, note = _bill_failure(inner, reservation, prices)
    assert charged == pytest.approx((307 * 0.05 + 33 * 0.40) / 1_000_000)
    assert sent == 1
    assert "never sent" in note


# --- evidence snapshot consistency ------------------------------------------


def test_changed_source_with_identical_counts_fails_closed() -> None:
    changed = _doc(instructions=["Deep-fry the chicken 3 hours.", "Serve cold."])
    assert len(changed["instructions"]) == len(_doc()["instructions"])
    provider = RecordingProvider([_turn1()])
    failure = _fail(provider, fetches=[_doc(), changed])
    assert failure.reason == "validation_rejected"
    assert failure.detail["validation_reason"] == "evidence_changed"
    assert len(provider.calls) == 1  # the changed evidence never reached the model
    assert failure.detail["prior_turns"]["output_tokens"] == 33


def test_changed_ingredient_quantity_with_same_names_fails_closed() -> None:
    changed = _doc()
    changed["ingredients"][1]["original"] = "99 buckets boneless chicken"
    provider = RecordingProvider([_turn1()])
    failure = _fail(provider, fetches=[_doc(), changed])
    assert failure.detail["validation_reason"] == "evidence_changed"


def test_success_validates_and_renders_the_evidence_supplied_to_the_model() -> None:
    provider = RecordingProvider([_turn1(), _turn2()])
    result = _run(provider, fetches=[_doc(), _doc()])
    tool_output = provider.calls[1]["input_items"][4]["output"]
    for step in result["recipe"]["instructions"]:
        assert f"{step['ref']}: {step['text']}" in tool_output
    expected = build_candidate(dataset_id=FOODCOM, source_id="000250", title=None, doc=_doc())
    assert result["recipe"] == render_recipe(expected)
    assert result["evidence"]["evidence_fingerprint"] == evidence_fingerprint(expected)
    assert result["selection_reasons"] == [
        "The source reports a total time of 33 minutes, within your 40-minute limit. This is "
        "the source's own figure, not a promise of how long it will take you."
    ]


def test_final_selection_validated_only_against_the_fetched_snapshot() -> None:
    # Two candidates offered; the model fetched "1". A legacy-form final
    # answer naming the other (unfetched) candidate is rejected.
    rows = [
        {"dataset_id": FOODCOM, "source_id": "000250", "title": "A"},
        {"dataset_id": FOODIE, "source_id": "foodie-1", "title": "B"},
    ]
    other = _doc(sid="foodie-1", title="Thai Chicken Curry Two")
    final = _turn2()
    final.parsed = {
        "dataset_id": FOODIE,
        "source_id": "foodie-1",
        "ingredient_refs": ["ing-0", "ing-1"],
        "step_refs": ["step-0", "step-1"],
        "reasons": [],
        "questions": [],
    }
    provider = RecordingProvider([_turn1(), final])
    failure = _fail(provider, fetches=[_doc(), other, _doc()], rows=rows)
    assert failure.detail["validation_reason"] == "unknown_identity"
    # Both turns completed: their usage is kept, nothing pending.
    assert [t["response_id"] for t in failure.detail["prior_turns"]["turns"]] == [
        "resp_turn1",
        "resp_turn2",
    ]
    assert failure.detail["attempts"] == 0


# --- turn-1 usage survives second-turn failures -----------------------------


def test_turn_1_usage_survives_turn_2_provider_failure() -> None:
    timeout = ProviderTimeoutError("timed out", attempts=2, attempt_details=[], request_sent=True)
    provider = RecordingProvider([_turn1(), timeout])
    failure = _fail(provider, fetches=[_doc(), _doc()])
    assert failure.http_status == 504
    assert failure.detail["prior_turns"]["input_tokens"] == 307
    assert failure.detail["prior_turns"]["output_tokens"] == 33
    assert failure.detail["prior_response_ids"] == ["resp_turn1"]
    assert failure.detail["attempts"] == 2
    assert failure.detail["provider_reached"] is True


def test_turn_1_usage_survives_turn_2_schema_failure() -> None:
    bad = _turn2(selection_reasons=["Guaranteed allergen-free."])
    provider = RecordingProvider([_turn1(), bad])
    failure = _fail(provider, fetches=[_doc(), _doc()])
    assert failure.reason == "schema_failure"
    assert "Guaranteed" not in json.dumps(failure.detail)
    assert failure.detail["prior_turns"]["input_tokens"] == 307 + 718


# --- diagnostics recorded for live review -----------------------------------


def test_success_records_offered_candidates_envelope_without_reasoning_content() -> None:
    provider = RecordingProvider([_turn1(call_id="call_diag"), _turn2()])
    result = _run(provider, fetches=[_doc(), _doc()])
    offered = result["evidence"]["offered_candidates"]
    expected_fp = evidence_fingerprint(
        build_candidate(dataset_id=FOODCOM, source_id="000250", title=None, doc=_doc())
    )
    assert offered == [
        {
            "candidate_label": "1",
            "dataset_id": FOODCOM,
            "source_id": "000250",
            "title": "Thai Chicken Curry",
            "evidence_fingerprint": expected_fp,
        }
    ]
    trace = result["evidence"]["tool_continuation"]
    assert trace["call_id"] == "call_diag"
    assert trace["fetched"]["fetched_matches_snapshot"] is True
    assert trace["fetched"]["snapshot_fingerprint"] == expected_fp
    assert [i["kind"] for i in trace["turn2_input_items"]] == [
        "system",
        "user",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert trace["turn2_input_items"][4]["call_id"] == "call_diag"
    assert trace["turn2_system_is_final_selection_prompt"] is True
    assert trace["turn2_request_chars"] == result["evidence"]["request_chars_per_turn"][1]
    turn1 = result["usage"]["turns"][0]
    assert turn1["tool_call_envelope"] == [
        {"call_id": "call_diag", "name": "get_recipe", "arguments": {"candidate_label": "1"}}
    ]
    assert turn1["continuation_items"][0] == {"type": "reasoning", "id": "rs_turn1"}
    # Reasoning content/encrypted content is never recorded anywhere.
    body = json.dumps(result)
    assert "enc==" not in body and "encrypted_content" not in body


def test_failure_detail_keeps_offered_candidates_and_fingerprints() -> None:
    changed = _doc(instructions=["Deep-fry the chicken 3 hours.", "Serve cold."])
    provider = RecordingProvider([_turn1()])
    failure = _fail(provider, fetches=[_doc(), changed])
    assert failure.detail["offered_candidates"][0]["source_id"] == "000250"
    assert failure.detail["snapshot_fingerprint"] != failure.detail["fetched_fingerprint"]
    assert failure.detail["tool_continuation"]["turn1_offered_labels"] == ["1"]


def test_runner_claim_check_is_structural() -> None:
    from runner import check_unsupported_claim

    provider = RecordingProvider([_turn1(), _turn2()])
    result = _run(provider, fetches=[_doc(), _doc()])
    assert check_unsupported_claim(result) is None
    tampered = copy.deepcopy(result)
    tampered["selection_reasons"].append("Guaranteed allergen-free.")
    assert "differ" in str(check_unsupported_claim(tampered))
    leaked = copy.deepcopy(result)
    leaked["rejected_propositions"] = [
        {"kind": "reason", "index": 0, "type": "x", "code": "y", "text": "leak"}
    ]
    assert "beyond" in str(check_unsupported_claim(leaked))
