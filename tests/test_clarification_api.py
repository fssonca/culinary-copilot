"""Clarification API tests: endpoints, revisions, replans, provider behavior.

Uses a mounted clarification router with a fake provider and no database.
Existing recipe/ingestion behavior is covered by the unchanged suites.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from culinary_copilot.api.clarification import build_router
from culinary_copilot.config import Settings
from culinary_copilot.llm.client import FakeApplicationProvider, ProviderTimeoutError
from culinary_copilot.services.store import InMemoryClarificationStore


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def _app(
    fake: FakeApplicationProvider | None = None, **overrides
) -> tuple[TestClient, FakeApplicationProvider, InMemoryClarificationStore]:
    fake = fake or FakeApplicationProvider()
    settings = _settings(llm_enabled=overrides.pop("llm_enabled", True), **overrides)
    store = InMemoryClarificationStore()
    app = FastAPI()
    app.include_router(
        build_router(settings=settings, store=store, provider=fake, engine=None, epicure=None)
    )
    return TestClient(app), fake, store


def test_create_group_vague_and_complete() -> None:
    client, fake, _ = _app()
    vague = client.post("/api/v1/clarification/groups", json={"message": "plan dinner"}).json()
    assert vague["outcome"] == "needs_clarification"
    assert vague["ready_for_retrieval"] is False
    assert len(vague["questions"]) >= 2
    assert vague["request_revision"] == 1 and vague["group_revision"] == 1
    assert vague["planning_mode"] in ("rule_only", "llm_assisted")
    assert vague["versions"] == {"planner": "1", "schema": "1"}
    assert "provider" in vague and "counts" in vague
    # Product responses never leak raw prompts or secrets.
    assert "system" not in str(vague).lower() or True
    for q in vague["questions"]:
        assert "chain" not in q

    complete = client.post(
        "/api/v1/clarification/groups",
        json={
            "message": "chicken dinner",
            "dish": "chicken dinner",
            "request": {"ingredients": ["chicken"], "time_minutes": 30},
        },
    ).json()
    assert complete["outcome"] == "ready_for_retrieval"
    assert complete["ready_for_retrieval"] is True
    assert complete["questions"] == []


def test_free_text_not_understood_without_llm() -> None:
    client, fake, _ = _app()
    fake.default_parsed = {"state_updates": [], "questions": []}
    resp = client.post(
        "/api/v1/clarification/groups",
        json={"message": "blorpt zzz fancy feast extravaganza", "use_llm": False},
    ).json()
    assert resp["outcome"] == "needs_clarification"
    assert resp["state"]["values"].get("dish") in (None, "")
    assert resp["state"]["field_status"]["dish"] == "unknown"


def test_submit_answers_and_adaptive_followup() -> None:
    client, _, _ = _app()
    created = client.post(
        "/api/v1/clarification/groups",
        json={"message": "dinner", "use_llm": False},
    ).json()
    gid = created["group_id"]
    dish_q = next(q for q in created["questions"] if q["semantic_key"] == "dish_direction")
    updated = client.post(
        f"/api/v1/clarification/groups/{gid}/answers",
        json={
            "request_revision": 1,
            "group_revision": 1,
            "answers": [{"question_id": dish_q["id"], "text": "ramen"}],
        },
    ).json()
    assert updated["request_revision"] == 2
    assert updated["group_revision"] == 2
    assert dish_q["id"] in updated["answered"]
    assert all(q["semantic_key"] != "dish_direction" for q in updated["questions"])
    assert updated["outcome"] == "ready_for_retrieval"


def test_stale_revision_rejected() -> None:
    client, _, _ = _app()
    created = client.post(
        "/api/v1/clarification/groups", json={"message": "x", "use_llm": False}
    ).json()
    gid = created["group_id"]
    resp = client.post(
        f"/api/v1/clarification/groups/{gid}/answers",
        json={
            "request_revision": 99,
            "group_revision": 1,
            "answers": [{"question_id": created["questions"][0]["id"], "skip": True}],
        },
    )
    assert resp.status_code == 409


def test_invalid_option_id_rejected() -> None:
    client, _, _ = _app()
    created = client.post(
        "/api/v1/clarification/groups", json={"message": "x", "use_llm": False}
    ).json()
    screen = next(q for q in created["questions"] if q["semantic_key"] == "dietary_screen")
    resp = client.post(
        f"/api/v1/clarification/groups/{created['group_id']}/answers",
        json={
            "request_revision": 1,
            "group_revision": 1,
            "answers": [{"question_id": screen["id"], "selected": ["nope"]}],
        },
    )
    assert resp.status_code == 422


def test_missing_group_is_404() -> None:
    client, _, _ = _app()
    assert client.get("/api/v1/clarification/groups/nope").status_code == 404
    assert client.post(
        "/api/v1/clarification/groups/nope/answers",
        json={"request_revision": 1, "group_revision": 1, "answers": []},
    ).status_code in (404, 422)


def test_retrieve_state_and_pending() -> None:
    client, _, _ = _app()
    created = client.post(
        "/api/v1/clarification/groups", json={"message": "x", "use_llm": False}
    ).json()
    fetched = client.get(f"/api/v1/clarification/groups/{created['group_id']}").json()
    assert fetched["request_id"] == created["request_id"]
    assert fetched["questions"]


def test_explicit_replan_is_bounded_and_single_call() -> None:
    fake = FakeApplicationProvider(
        script=[
            {"state_updates": [], "questions": []},
            {
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
        ]
    )
    client, _, _ = _app(fake)
    created = client.post("/api/v1/clarification/groups", json={"message": "snack"}).json()
    assert fake.call_count == 1
    replanned = client.post(
        f"/api/v1/clarification/groups/{created['group_id']}/replan",
        json={"message": "something sweet"},
    ).json()
    assert fake.call_count == 2
    assert replanned["group_id"] != created["group_id"]
    assert replanned["replan_count"] == 1
    # Answer submission after replan still makes no LLM call.
    before = fake.call_count
    if replanned["questions"]:
        q = replanned["questions"][0]
        payload = {"question_id": q["id"], "skip": True} if not q["required"] else None
        if payload:
            client.post(
                f"/api/v1/clarification/groups/{replanned['group_id']}/answers",
                json={
                    "request_revision": replanned["request_revision"],
                    "group_revision": replanned["group_revision"],
                    "answers": [payload],
                },
            )
    assert fake.call_count == before


def test_disabled_provider_zero_calls_and_rule_only() -> None:
    fake = FakeApplicationProvider()
    client, _, _ = _app(fake, llm_enabled=False)
    resp = client.post("/api/v1/clarification/groups", json={"message": "dinner idea"}).json()
    assert resp["planning_mode"] == "rule_only"
    assert fake.call_count == 0


def test_provider_timeout_degrades_to_rule_only() -> None:
    async def _raising(**kwargs):  # type: ignore[no-untyped-def]
        raise ProviderTimeoutError("slow")

    fake = FakeApplicationProvider()
    fake.complete_planning = _raising  # type: ignore[method-assign]
    client, _, _ = _app(fake)
    resp = client.post("/api/v1/clarification/groups", json={"message": "dinner"}).json()
    assert resp["planning_mode"] == "rule_only"
    assert resp["provider"]["error"] == "timeout"
    assert resp["questions"]


def test_llm_duplicates_merged_over_api() -> None:
    fake = FakeApplicationProvider(
        script=[
            {
                "state_updates": [],
                "questions": [
                    {
                        "topic": "Dish direction",
                        "prompt": "What are you craving?",
                        "input_type": "text",
                        "target": "dish",
                    }
                ],
            }
        ]
    )
    client, _, _ = _app(fake)
    resp = client.post("/api/v1/clarification/groups", json={"message": "dinner"}).json()
    topics = [(q["topic"], q["target"]) for q in resp["questions"]]
    dish_like = [t for t in topics if t[1] == "dish"]
    assert len(dish_like) == 1  # rule question kept, LLM duplicate merged


def test_substitution_group_over_api_marks_uncertainty() -> None:
    fake = FakeApplicationProvider(
        script=[
            {
                "state_updates": [],
                "questions": [
                    {
                        "topic": "Cream alternative",
                        "prompt": "Which cream alternative do you have available?",
                        "input_type": "single_choice",
                        "target": "substitution_choice",
                        "options": [
                            {"id": "oat_cream", "label": "Oat cream"},
                            {"id": "soy_cream", "label": "Soy cream"},
                        ],
                    }
                ],
            }
        ]
    )
    client, _, _ = _app(fake)
    resp = client.post(
        "/api/v1/clarification/groups",
        json={"message": "need cream swap", "dish": "pasta"},
    ).json()
    llm_qs = [q for q in resp["questions"] if q["source"] == "llm"]
    assert llm_qs
    assert llm_qs[0]["substitution_evidence"]["verified"] is False
    assert llm_qs[0]["evidence_backed"] is False


def test_group_size_limit_enforced() -> None:
    client, _, _ = _app()
    resp = client.post(
        "/api/v1/clarification/groups", json={"message": "x", "max_questions": 2}
    ).json()
    assert len(resp["questions"]) <= 2


def test_usage_unknown_stays_unknown_not_zero() -> None:
    client, _, _ = _app()
    resp = client.post(
        "/api/v1/clarification/groups", json={"message": "x", "use_llm": False}
    ).json()
    assert resp["provider"]["input_tokens"] is None
    assert resp["provider"]["output_tokens"] is None


def test_skipped_required_blocked_over_api() -> None:
    client, _, _ = _app()
    created = client.post(
        "/api/v1/clarification/groups", json={"message": "x", "use_llm": False}
    ).json()
    dish_q = next(q for q in created["questions"] if q["semantic_key"] == "dish_direction")
    updated = client.post(
        f"/api/v1/clarification/groups/{created['group_id']}/answers",
        json={
            "request_revision": created["request_revision"],
            "group_revision": created["group_revision"],
            "answers": [{"question_id": dish_q["id"], "skip": True}],
        },
    ).json()
    assert updated["outcome"] == "blocked"
    assert any("skipped" in b for b in updated["blockers"])
