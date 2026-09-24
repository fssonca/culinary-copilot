"""Clarification API: groups, answers, retrieval, explicit replans."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from culinary_copilot.config import Settings
from culinary_copilot.domain.clarification import (
    CLARIFICATION_SCHEMA_VERSION,
    AnswerPayload,
    ConfirmationStatus,
    PlanningMode,
)
from culinary_copilot.domain.requests import CookingRequest
from culinary_copilot.services.answers import apply_answers, pending_questions
from culinary_copilot.services.clarification_service import (
    PLANNER_VERSION,
    _clamp_max,
    build_initial_state,
    group_response,
    make_group,
    plan_group,
)
from culinary_copilot.services.store import new_id


class CreateGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, max_length=2000)
    request: CookingRequest | None = None
    dish: str | None = Field(default=None, max_length=200)
    task_scope: str | None = Field(default=None, max_length=100)
    no_preference_targets: list[str] = Field(default_factory=list, max_length=20)
    max_questions: int | None = Field(default=None, ge=1, le=12)
    use_llm: bool = True


class SubmitAnswersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_revision: int = Field(ge=1)
    group_revision: int = Field(ge=1)
    answers: list[AnswerPayload] = Field(min_length=1, max_length=20)


def build_router(
    *,
    settings: Settings,
    store: Any,
    provider: Any,
    engine: Any = None,
    epicure: Any = None,
) -> APIRouter:
    bound = APIRouter(prefix="/api/v1/clarification", tags=["clarification"])

    @bound.post("/groups")
    async def create_group_bound(body: CreateGroupRequest) -> dict[str, Any]:
        try:
            max_q = _clamp_max(body.max_questions)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        request_id = new_id("req")
        try:
            state = build_initial_state(
                request_id=request_id,
                request=body.request,
                dish=body.dish,
                task_scope=body.task_scope,
                no_preference_targets=body.no_preference_targets,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        questions, meta = await plan_group(
            state=state,
            message=body.message or "",
            settings=settings,
            provider=provider,
            engine=engine,
            epicure=epicure,
            max_questions=max_q,
            use_llm=body.use_llm,
        )
        group = make_group(
            state=state,
            questions=questions,
            created_by=meta.get("planning_mode", PlanningMode.RULE_ONLY.value),
        )
        store.create(state, group)
        _emit_group_telemetry(
            state=state, group_id=group.group_id, meta=meta, message=body.message or ""
        )
        return group_response(state=state, group=group, meta=meta)

    @bound.get("/groups/{group_id}")
    async def get_group_bound(group_id: str) -> dict[str, Any]:
        found = store.get_snapshot(group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="clarification group not found")
        state, group = found
        return group_response(state=state, group=group)

    @bound.post("/groups/{group_id}/answers")
    async def submit_answers_bound(group_id: str, body: SubmitAnswersRequest) -> dict[str, Any]:
        found = store.get_snapshot(group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="clarification group not found")
        state, group = found
        if body.request_revision != state.revision or body.group_revision != group.revision:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"stale revision: have request={state.revision} group={group.revision}; "
                    f"got request={body.request_revision} group={body.group_revision}"
                ),
            )
        # Validate against every question ever issued (edits to earlier groups
        # stay supported after the pending group is filtered).
        known = store.known_questions(state.request_id)
        try:
            state, _ = apply_answers(state, known, list(body.answers))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        # Deterministic update: filter the pending group, bump its revision.
        # No LLM call here by design. The commit is compare-and-swap: a
        # concurrent writer wins once and the loser gets 409.
        remaining = pending_questions(known, state)
        group.revision += 1
        group.questions = remaining
        committed = store.commit_answers(
            state.request_id,
            expected_state_rev=body.request_revision,
            expected_group_rev=body.group_revision,
            new_state=state,
            new_group=group,
        )
        if not committed:
            raise HTTPException(
                status_code=409,
                detail=(
                    "stale revision: state changed while submitting; refetch the group and resubmit"
                ),
            )
        return group_response(state=state, group=group)

    @bound.post("/groups/{group_id}/replan")
    async def replan_bound(group_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        found = store.get_snapshot(group_id)
        if found is None:
            raise HTTPException(status_code=404, detail="clarification group not found")
        state, _ = found
        base_state_rev = state.revision
        payload = body or {}
        message = str(payload.get("message", "") or "")[:2000]
        use_llm = bool(payload.get("use_llm", True))
        max_q = payload.get("max_questions")
        try:
            max_questions = _clamp_max(max_q)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if state.replan_count >= 5:
            raise HTTPException(status_code=422, detail="replan budget exhausted (max 5)")
        state.replan_count += 1
        # Planning runs on the snapshot outside any store lock (never held
        # across the provider call). The commit below fails closed if answers
        # landed meanwhile, so a stale replan can never overwrite them.
        questions, meta = await plan_group(
            state=state,
            message=message,
            settings=settings,
            provider=provider,
            engine=engine,
            epicure=epicure,
            max_questions=max_questions,
            use_llm=use_llm,
        )
        # Suppress already-answered/skipped/invalidated questions in the
        # follow-up group, but always carry unresolved confirmation questions
        # so pending confirmations stay answerable.
        known = store.known_questions(state.request_id) + questions
        followup_pending = pending_questions(known, state)
        wanted_ids = {q.id for q in questions}
        pending_ids = {
            c.question_id
            for c in state.pending_confirmations
            if c.status == ConfirmationStatus.PENDING
        }
        followup = [q for q in followup_pending if q.id in wanted_ids or q.id in pending_ids][
            :max_questions
        ]
        group = make_group(
            state=state,
            questions=followup,
            created_by=meta.get("planning_mode", PlanningMode.RULE_ONLY.value),
        )
        committed = store.commit_replan(
            state.request_id,
            expected_state_rev=base_state_rev,
            new_state=state,
            new_group=group,
        )
        if not committed:
            raise HTTPException(
                status_code=409,
                detail=(
                    "replan_stale: answers were submitted while planning; "
                    "refetch the group and replan again if still needed"
                ),
            )
        fresh = store.get_snapshot(group.group_id)
        if fresh is None:  # pragma: no cover - commit just succeeded
            raise HTTPException(status_code=404, detail="clarification group not found")
        fresh_state, fresh_group = fresh
        _emit_group_telemetry(
            state=fresh_state, group_id=fresh_group.group_id, meta=meta, message=message
        )
        resp = group_response(state=fresh_state, group=fresh_group, meta=meta)
        resp["replan_count"] = fresh_state.replan_count
        return resp

    return bound


def _emit_group_telemetry(*, state: Any, group_id: str, meta: dict[str, Any], message: str) -> None:
    """Emit one planning event with the real group id for every path.

    Covers rule-only, provider-error, and provider-failure exits that
    previously bypassed emission, and replaces the ``pending``
    placeholder. Privacy: message length only, never text.
    """

    try:
        from culinary_copilot.obs.clarification import emit_planning_event

        emit_planning_event(
            request_id=state.request_id,
            group_id=group_id,
            planner_version=PLANNER_VERSION,
            schema_version=CLARIFICATION_SCHEMA_VERSION,
            rule_count=int(meta.get("rule_count") or 0),
            llm_count=int(meta.get("llm_count") or 0),
            planning_mode=str(meta.get("planning_mode") or PlanningMode.RULE_ONLY.value),
            outcome=state.outcome.value,
            model=meta.get("provider_model"),
            input_tokens=meta.get("input_tokens"),
            output_tokens=meta.get("output_tokens"),
            latency_ms=meta.get("latency_ms"),
            attempts=int(meta.get("attempts") or 1),
            provider_error=meta.get("provider_error"),
            replan_count=int(getattr(state, "replan_count", 0) or 0),
            message_chars=len(message or ""),
            blockers=len(getattr(state, "blockers", []) or []),
        )
    except Exception:
        pass
