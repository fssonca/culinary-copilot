"""Hardening review tests: correction confirmation, readiness, concurrency.

Offline by default: fake providers and synthetic evidence only. No model
credentials, network, database writes, or downloads. Fake-provider tests
verify application behavior, not real-model interpretation quality.
"""

import asyncio
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from culinary_copilot.api.clarification import build_router
from culinary_copilot.config import Settings
from culinary_copilot.domain.clarification import (
    AnswerPayload,
    EvidenceStatus,
    FieldStatus,
)
from culinary_copilot.domain.rule_planner import plan_rule_questions
from culinary_copilot.llm.client import FakeApplicationProvider
from culinary_copilot.services.answers import apply_answers, init_state
from culinary_copilot.services.clarification_service import plan_group
from culinary_copilot.services.hybrid_planner import (
    EvidenceItem,
    LlmPlanProposal,
    apply_llm_proposal,
)
from culinary_copilot.services.store import InMemoryClarificationStore


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def _vague_state(**kwargs):
    return init_state(request_id=kwargs.pop("request_id", "req-1"), request={}, **kwargs)


def _correction_proposal(target: str, value: str, quote: str = "") -> LlmPlanProposal:
    return LlmPlanProposal.model_validate(
        {
            "state_updates": [
                {
                    "target": target,
                    "value": value,
                    "confidence": 0.95,
                    "is_correction": True,
                    "quote": quote,
                }
            ],
            "questions": [],
        }
    )


# --- area 1: explicit answers vs model corrections -------------------------


def test_model_correction_without_support_never_overwrites() -> None:
    state = init_state(request_id="c1", request={}, dish="ramen")
    rule_questions, _, _ = plan_rule_questions(state, max_questions=12)
    proposal = _correction_proposal("dish", "pizza", quote="the user loves pizza")
    accepted, report = apply_llm_proposal(
        state=state,
        proposal=proposal,
        rule_questions=rule_questions,
        evidence=[],
        evidence_status=EvidenceStatus.NOT_QUERIED,
        message="tell me about weeknight pasta",
    )
    assert state.dish == "ramen"
    assert state.values["dish"] == "ramen"
    # A confirmation question is produced instead of a silent overwrite.
    confirm = [q for q in accepted if q.semantic_key.startswith("confirm_")]
    assert len(confirm) == 1
    assert confirm[0].target == "dish"
    assert report.get("pending_confirmations")


def test_recipe_text_cannot_change_preferences() -> None:
    state = init_state(request_id="c2", request={"preferences": ["spicy"]}, dish="noodles")
    rule_questions, _, _ = plan_rule_questions(state, max_questions=12)
    proposal = _correction_proposal("preferences", "mild", quote="use mild sauce (recipe line 3)")
    accepted, _ = apply_llm_proposal(
        state=state,
        proposal=proposal,
        rule_questions=rule_questions,
        evidence=[
            EvidenceItem(
                dataset_id="odunola/foodie",
                source_id="x",
                title="T",
                snippet="use mild sauce",
            )
        ],
        evidence_status=EvidenceStatus.BACKED_BY_EVIDENCE,
        message="noodles please",
    )
    assert state.values["preferences"] == ["spicy"]
    assert any(q.semantic_key.startswith("confirm_") for q in accepted)


def test_typed_edit_still_applies() -> None:
    client, _, _ = _api()
    created = client.post(
        "/api/v1/clarification/groups", json={"message": "x", "use_llm": False}
    ).json()
    gid = created["group_id"]
    dish_q = next(q for q in created["questions"] if q["semantic_key"] == "dish_direction")
    first = client.post(
        f"/api/v1/clarification/groups/{gid}/answers",
        json={
            "request_revision": 1,
            "group_revision": 1,
            "answers": [{"question_id": dish_q["id"], "text": "ramen"}],
        },
    ).json()
    assert first["state"]["values"]["dish"] == "ramen"
    edited = client.post(
        f"/api/v1/clarification/groups/{gid}/answers",
        json={
            "request_revision": first["request_revision"],
            "group_revision": first["group_revision"],
            "answers": [{"question_id": dish_q["id"], "text": "pizza"}],
        },
    ).json()
    assert edited["state"]["values"]["dish"] == "pizza"
    assert edited["state"]["field_status"]["dish"] == "provided"


def test_free_text_correction_confirmation_flow() -> None:
    settings = _settings(llm_enabled=True)
    fake = FakeApplicationProvider(
        script=[
            {
                "state_updates": [
                    {
                        "target": "dish",
                        "value": "pizza",
                        "confidence": 0.9,
                        "is_correction": True,
                        "quote": "actually pizza",
                    }
                ],
                "questions": [],
            }
        ]
    )
    state = init_state(request_id="c3", request={}, dish="ramen")

    async def _run():
        return await plan_group(
            state=state,
            message="actually pizza tonight",
            settings=settings,
            provider=fake,
            max_questions=6,
        )

    questions, meta = asyncio.run(_run())
    assert meta["planning_mode"] == "llm_assisted"
    assert state.dish == "ramen"  # preserved until confirmation
    confirm = next(q for q in questions if q.semantic_key.startswith("confirm_"))
    # User confirms the change through the typed endpoint.
    state, _ = apply_answers(
        state, questions, [AnswerPayload(question_id=confirm.id, selected=["confirm_change"])]
    )
    assert state.dish == "pizza"
    assert state.field_status["dish"] == FieldStatus.PROVIDED
    # And the keep path preserves the original.
    state2 = init_state(request_id="c4", request={}, dish="ramen")
    rule2, _, _ = plan_rule_questions(state2, max_questions=12)
    accepted2, _ = apply_llm_proposal(
        state=state2,
        proposal=_correction_proposal("dish", "pizza", quote="actually pizza"),
        rule_questions=rule2,
        evidence=[],
        evidence_status=EvidenceStatus.NOT_QUERIED,
        message="actually pizza",
    )
    confirm2 = next(q for q in accepted2 if q.semantic_key.startswith("confirm_"))
    state2, _ = apply_answers(
        state2,
        rule2 + accepted2,
        [AnswerPayload(question_id=confirm2.id, selected=["keep_current"])],
    )
    assert state2.dish == "ramen"


def test_dietary_intact_until_authorized_correction() -> None:
    state = init_state(request_id="c5", request={"dietary_constraints": ["vegan"]}, dish="bowl")
    rule_questions, _, _ = plan_rule_questions(state, max_questions=12)
    proposal = _correction_proposal("dietary_constraints", "none", quote="no limits")
    accepted, _ = apply_llm_proposal(
        state=state,
        proposal=proposal,
        rule_questions=rule_questions,
        evidence=[],
        evidence_status=EvidenceStatus.NOT_QUERIED,
        message="no limits, anything goes",
    )
    assert state.values["dietary_constraints"] == ["vegan"]
    assert state.field_status["dietary_constraints"] == FieldStatus.PROVIDED
    confirm = [q for q in accepted if q.semantic_key.startswith("confirm_")]
    assert confirm and confirm[0].target == "dietary_constraints"


# --- area 2: readiness consistency ------------------------------------------


def test_ready_with_optionals_pending() -> None:
    state = _vague_state()
    questions, _, _ = plan_rule_questions(state, max_questions=12)
    dish = next(q for q in questions if q.semantic_key == "dish_direction")
    state, _ = apply_answers(state, questions, [AnswerPayload(question_id=dish.id, text="soup")])
    assert state.ready_for_retrieval is True
    assert state.outcome.value == "ready_for_retrieval"


def test_unknown_vs_no_restriction_vs_skipped() -> None:
    unknown = init_state(request_id="u", request={})
    assert unknown.field_status["dietary_constraints"] == FieldStatus.UNKNOWN
    explicit = init_state(request_id="e", request={}, no_preference_targets=["dietary_constraints"])
    assert explicit.field_status["dietary_constraints"] == FieldStatus.NO_PREFERENCE
    assert explicit.values["dietary_constraints"] == []
    assert (
        unknown.field_status["dietary_constraints"] != explicit.field_status["dietary_constraints"]
    )
    state = _vague_state(request_id="s")
    questions, _, _ = plan_rule_questions(state, max_questions=12)
    screen = next(q for q in questions if q.semantic_key == "dietary_screen")
    state, _ = apply_answers(state, questions, [AnswerPayload(question_id=screen.id, skip=True)])
    assert state.field_status["dietary_constraints"] == FieldStatus.SKIPPED


def test_ready_preserves_hard_constraints() -> None:
    client, _, _ = _api()
    resp = client.post(
        "/api/v1/clarification/groups",
        json={
            "message": "vegan dinner",
            "dish": "vegan curry",
            "request": {"dietary_constraints": ["vegan"], "ingredients": ["chickpeas"]},
            "use_llm": False,
        },
    ).json()
    assert resp["ready_for_retrieval"] is True
    assert resp["state"]["values"]["dietary_constraints"] == ["vegan"]
    assert resp["state"]["field_status"]["dietary_constraints"] == "provided"
    # Downstream structured request preserves the hard constraint.
    assert resp["state"]["request"]["dietary_constraints"] == ["vegan"]
    assert resp["state"]["request"]["ingredients"] == ["chickpeas"]


def test_unresolved_conflict_blocks_readiness_with_blocker() -> None:
    state = _vague_state()
    state.field_status["cuisine"] = FieldStatus.CONFLICTING
    state.values["cuisine"] = "Italian vs Mexican"
    questions, _, _ = plan_rule_questions(state, max_questions=12)
    dish = next(q for q in questions if q.semantic_key == "dish_direction")
    state, _ = apply_answers(state, questions, [AnswerPayload(question_id=dish.id, text="soup")])
    assert state.ready_for_retrieval is False
    assert state.outcome.value == "needs_clarification"
    assert any(b.startswith("conflicting_cuisine") for b in state.blockers)


def test_scaling_without_portions_not_ready() -> None:
    state = init_state(request_id="sc", request={}, dish="lasagna", task_scope="scaling")
    questions, _, outcome = plan_rule_questions(state)
    assert outcome.value == "needs_clarification"
    portions = [q for q in questions if q.target == "portions"]
    assert portions and portions[0].required is True


def test_edit_recomputes_readiness_on_conflict() -> None:
    state = init_state(
        request_id="ec",
        request={"dietary_constraints": ["vegan"]},
        dish=None,
    )
    questions, _, _ = plan_rule_questions(state, max_questions=12)
    dish_q = next(q for q in questions if q.semantic_key == "dish_direction")
    prefs = next(q for q in questions if q.semantic_key == "preferences")
    state, _ = apply_answers(
        state, questions, [AnswerPayload(question_id=dish_q.id, text="dinner")]
    )
    assert state.ready_for_retrieval is True
    state, _ = apply_answers(
        state, questions, [AnswerPayload(question_id=prefs.id, text="extra chicken")]
    )
    assert state.field_status["dietary_constraints"] == FieldStatus.CONFLICTING
    assert state.ready_for_retrieval is False


def test_skip_required_stable_blocked_no_repeat() -> None:
    client, _, _ = _api()
    created = client.post(
        "/api/v1/clarification/groups", json={"message": "x", "use_llm": False}
    ).json()
    gid = created["group_id"]
    dish_q = next(q for q in created["questions"] if q["semantic_key"] == "dish_direction")
    updated = client.post(
        f"/api/v1/clarification/groups/{gid}/answers",
        json={
            "request_revision": created["request_revision"],
            "group_revision": created["group_revision"],
            "answers": [{"question_id": dish_q["id"], "skip": True}],
        },
    ).json()
    assert updated["outcome"] == "blocked"
    replanned = client.post(
        f"/api/v1/clarification/groups/{gid}/replan", json={"message": "x", "use_llm": False}
    ).json()
    assert replanned["outcome"] == "blocked"
    assert all(q["id"] != "rule-dish-direction" for q in replanned["questions"])


def test_unenforced_constraints_surfaced() -> None:
    client, _, _ = _api()
    resp = client.post(
        "/api/v1/clarification/groups",
        json={
            "dish": "curry",
            "request": {"dietary_constraints": ["vegan"], "ingredients": ["chickpeas"]},
            "use_llm": False,
        },
    ).json()
    assert resp["ready_for_retrieval"] is True
    assert "dietary_constraints" in resp["unenforced_constraints"]
    assert "dietary compatibility" in resp["readiness_note"]


# --- area 3: concurrency -----------------------------------------------------


def _api(
    fake: FakeApplicationProvider | None = None, **overrides
) -> tuple[TestClient, FakeApplicationProvider, InMemoryClarificationStore]:
    fake = fake or FakeApplicationProvider()
    settings = _settings(llm_enabled=overrides.pop("llm_enabled", False), **overrides)
    store = InMemoryClarificationStore()
    app = FastAPI()
    app.include_router(
        build_router(settings=settings, store=store, provider=fake, engine=None, epicure=None)
    )
    return TestClient(app), fake, store


def test_concurrent_submissions_one_wins() -> None:
    client, _, store = _api()
    created = client.post(
        "/api/v1/clarification/groups", json={"message": "x", "use_llm": False}
    ).json()
    gid = created["group_id"]
    dish_q = next(q for q in created["questions"] if q["semantic_key"] == "dish_direction")
    barrier = threading.Barrier(2)
    orig_snapshot = store.get_snapshot

    def _gated(group_id: str):  # type: ignore[no-untyped-def]
        found = orig_snapshot(group_id)
        barrier.wait(timeout=10)
        return found

    store.get_snapshot = _gated  # type: ignore[method-assign]
    results: list[int] = []

    def _submit(text: str) -> None:
        with TestClient(client.app) as thread_client:
            resp = thread_client.post(
                f"/api/v1/clarification/groups/{gid}/answers",
                json={
                    "request_revision": 1,
                    "group_revision": 1,
                    "answers": [{"question_id": dish_q["id"], "text": text}],
                },
            )
            results.append(resp.status_code)

    threads = [threading.Thread(target=_submit, args=(t,)) for t in ("ramen", "pizza")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == [200, 409]
    found = orig_snapshot(gid)
    assert found is not None
    state, _ = found
    assert state.revision == 2
    assert len([a for a in state.answers if a.status == "answered"]) == 1


def test_answer_during_replan_not_lost() -> None:
    release = threading.Event()

    class _Gated(FakeApplicationProvider):
        async def complete_planning(self, **kwargs):  # type: ignore[no-untyped-def]
            from culinary_copilot.llm.client import ProviderOutcome

            self.calls.append({"system": "", "user": ""})
            assert release.wait(timeout=30)
            return ProviderOutcome(
                ok=True,
                parsed={
                    "state_updates": [],
                    "questions": [
                        {
                            "topic": "Snack context",
                            "prompt": "Sweet or savory?",
                            "input_type": "single_choice",
                            "target": "preferences",
                            "options": [
                                {"id": "sweet", "label": "Sweet"},
                                {"id": "savory", "label": "Savory"},
                            ],
                        }
                    ],
                },
                model="fake",
                latency_ms=1,
                attempts=1,
            )

    fake = _Gated()
    client, _, store = _api(fake, llm_enabled=True)
    # Initial group is rule-only so the provider gate applies solely to the
    # replan call below.
    created = client.post(
        "/api/v1/clarification/groups", json={"message": "hi", "use_llm": False}
    ).json()
    gid = created["group_id"]
    rev = created["request_revision"]
    replans: list[int] = []
    snapshot_taken = threading.Event()
    orig_snapshot = store.get_snapshot

    def _signalled(group_id: str):  # type: ignore[no-untyped-def]
        found = orig_snapshot(group_id)
        snapshot_taken.set()
        return found

    store.get_snapshot = _signalled  # type: ignore[method-assign]

    def _replan() -> None:
        with TestClient(client.app) as thread_client:
            resp = thread_client.post(
                f"/api/v1/clarification/groups/{gid}/replan", json={"message": "snack"}
            )
            replans.append(resp.status_code)

    worker = threading.Thread(target=_replan)
    worker.start()
    # Deterministic ordering: the replan snapshot lands first, then the
    # provider call starts, then the answer commits, then planning resolves.
    assert snapshot_taken.wait(timeout=30)
    for _ in range(200):
        if fake.calls:
            break
        release.wait(timeout=0.05)
    dish_q = next(q for q in created["questions"] if q["semantic_key"] == "dish_direction")
    answered = client.post(
        f"/api/v1/clarification/groups/{gid}/answers",
        json={
            "request_revision": rev,
            "group_revision": created["group_revision"],
            "answers": [{"question_id": dish_q["id"], "text": "ramen"}],
        },
    )
    assert answered.status_code == 200
    release.set()
    worker.join(timeout=30)
    assert replans == [409]
    stored = store.get_state(created["request_id"])
    assert stored is not None
    assert stored.dish == "ramen"
    assert stored.revision == rev + 1


def test_retrieved_state_mutation_isolated() -> None:
    store = InMemoryClarificationStore()
    state = init_state(request_id="iso", request={}, dish="ramen")
    from culinary_copilot.services.clarification_service import make_group

    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    found = store.get_snapshot(group.group_id)
    assert found is not None
    copy_state, _ = found
    copy_state.dish = "MUTATED"
    copy_state.values["dish"] = "MUTATED"
    refetch = store.get_snapshot(group.group_id)
    assert refetch is not None
    assert refetch[0].dish == "ramen"


def test_stale_replan_commit_rejected() -> None:
    store = InMemoryClarificationStore()
    state = init_state(request_id="sr", request={}, dish="ramen")
    from culinary_copilot.services.clarification_service import make_group

    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    snap = store.get_snapshot(group.group_id)
    assert snap is not None
    snap_state, snap_group = snap
    # A newer write lands first (revision 1 -> 2).
    from culinary_copilot.services.answers import apply_answers as _apply

    advanced, _ = _apply(snap_state, [], [])
    assert advanced.revision == 2
    assert (
        store.commit_answers(
            state.request_id,
            expected_state_rev=1,
            expected_group_rev=1,
            new_state=advanced,
            new_group=snap_group,
        )
        is True
    )
    # Stale replan against revision 1 must not overwrite.
    from culinary_copilot.services.clarification_service import make_group as _mk

    stale_group = _mk(state=snap_state, questions=[], created_by="rule_only")
    assert (
        store.commit_replan(
            state.request_id,
            expected_state_rev=1,
            new_state=snap_state,
            new_group=stale_group,
        )
        is False
    )
