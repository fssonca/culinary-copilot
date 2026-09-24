"""Deterministic answer validation and state updates. No LLM calls here."""

from __future__ import annotations

import math
import re
from typing import Any

from culinary_copilot.domain.clarification import (
    ALLOWED_TARGETS,
    BLOCKER_CONFLICT_PREFIX,
    BLOCKER_DISH_DIRECTION_SKIPPED,
    BLOCKER_PORTIONS_SKIPPED,
    AnswerPayload,
    AnswerRecord,
    ConfirmationStatus,
    CookingRequestState,
    FieldStatus,
    PlanningOutcome,
    Question,
    QuestionInputType,
    blocker_code,
    is_blocked,
)

LIST_TARGETS = {"ingredients", "equipment", "preferences", "dietary_constraints"}
MEAT_TOKENS = {"chicken", "beef", "pork", "fish", "bacon", "meat", "turkey", "lamb"}
TASK_SCOPES = {"discovery", "meal_planning", "scaling"}

CONFIRM_KEEP = "keep_current"
CONFIRM_CHANGE = "confirm_change"


def init_state(
    *,
    request_id: str,
    request: dict[str, Any],
    dish: str | None = None,
    task_scope: str | None = None,
    no_preference_targets: list[str] | None = None,
) -> CookingRequestState:
    """Build initial state. Empty/missing means UNKNOWN, never no-preference."""
    req = dict(request or {})
    values: dict[str, Any] = {}
    status: dict[str, FieldStatus] = {}
    no_pref = {t for t in (no_preference_targets or [])}
    unknown_targets = set(no_pref) - ALLOWED_TARGETS
    if unknown_targets:
        raise ValueError(f"Unsupported no-preference targets: {sorted(unknown_targets)}")

    def set_value(target: str, value: Any, present: bool) -> None:
        values[target] = value
        if target in no_pref:
            status[target] = FieldStatus.NO_PREFERENCE
        elif present:
            status[target] = FieldStatus.PROVIDED
        else:
            status[target] = FieldStatus.UNKNOWN

    ingredients = [str(v) for v in (req.get("ingredients") or []) if str(v).strip()]
    set_value("ingredients", ingredients, bool(ingredients))
    portions = req.get("portions")
    set_value("portions", portions, portions is not None)
    time_minutes = req.get("time_minutes")
    set_value("time_minutes", time_minutes, time_minutes is not None)
    cuisine = req.get("cuisine")
    set_value("cuisine", cuisine, bool(str(cuisine or "").strip()))
    preferences = [str(v) for v in (req.get("preferences") or []) if str(v).strip()]
    set_value("preferences", preferences, bool(preferences))
    dietary = [str(v) for v in (req.get("dietary_constraints") or []) if str(v).strip()]
    set_value("dietary_constraints", dietary, bool(dietary))
    equipment = [str(v) for v in (req.get("equipment") or []) if str(v).strip()]
    set_value("equipment", equipment, bool(equipment))
    set_value("dish", (dish or "").strip() or None, bool((dish or "").strip()))
    set_value("task_scope", (task_scope or "").strip() or None, bool((task_scope or "").strip()))
    set_value("substitution_choice", None, False)
    if "substitution_choice" in no_pref:
        status["substitution_choice"] = FieldStatus.NO_PREFERENCE

    return CookingRequestState(
        request_id=request_id,
        revision=1,
        request=req,
        dish=(dish or "").strip() or None,
        task_scope=(task_scope or "").strip() or None,
        field_status=status,
        values=values,
    )


def _split_list(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;\n]+", text) if p.strip()][:100]


def validate_answer(question: Question, payload: AnswerPayload) -> AnswerPayload:
    if payload.question_id != question.id:
        raise ValueError(f"payload targets {payload.question_id}, not {question.id}")
    if payload.skip:
        return payload
    if payload.no_preference:
        return payload
    if question.input_type == QuestionInputType.TEXT:
        if payload.text is None and payload.custom_text is None:
            raise ValueError("text answer is required")
        text = payload.text if payload.text is not None else payload.custom_text
        assert text is not None
        if len(text.strip()) > (question.max_length or 2000):
            raise ValueError("text answer too long")
        return payload
    if question.input_type == QuestionInputType.NUMBER:
        if payload.number is None:
            raise ValueError("number answer is required")
        if payload.custom_text is not None:
            raise ValueError("custom text is not allowed for this question")
        if not math.isfinite(payload.number):
            raise ValueError("number must be finite")
        if question.min_value is not None and payload.number < question.min_value:
            raise ValueError(f"number below minimum {question.min_value}")
        if question.max_value is not None and payload.number > question.max_value:
            raise ValueError(f"number above maximum {question.max_value}")
        return payload
    # Choice types.
    if payload.selected is None or not payload.selected:
        if payload.custom_text is not None and question.allow_custom_text:
            return payload  # custom-only answer where allowed
        raise ValueError("selection is required")
    valid_ids = {o.id for o in question.options}
    for oid in payload.selected:
        if oid not in valid_ids:
            raise ValueError(f"invalid option id {oid!r}")
    if question.input_type == QuestionInputType.SINGLE_CHOICE and len(payload.selected) != 1:
        raise ValueError("single-choice questions take exactly one option")
    if question.input_type == QuestionInputType.MULTIPLE_CHOICE:
        lo = question.min_selections or 1
        hi = question.max_selections or len(question.options)
        if not lo <= len(payload.selected) <= hi:
            raise ValueError(f"select between {lo} and {hi} options")
    if payload.custom_text is not None and not question.allow_custom_text:
        raise ValueError("custom text is not allowed for this question")
    return payload


def _label_map(question: Question) -> dict[str, str]:
    return {o.id: o.label for o in question.options}


def is_confirmation_question(question: Question) -> bool:
    return question.semantic_key.startswith("confirm_")


def set_typed_value(state: CookingRequestState, target: str, raw: str) -> None:
    """Set a target from a model-proposed raw string. Raises ValueError on bad shape.

    Shared by hybrid unknown-field updates and user-confirmed corrections so
    both paths enforce identical bounds and enums.
    """
    if target not in ALLOWED_TARGETS:
        raise ValueError(f"Unsupported target {target!r}")
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"Empty value for {target}")
    if target == "portions":
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"Portions must be numeric, got {raw!r}") from None
        if not math.isfinite(number) or not 1 <= number <= 100:
            raise ValueError("Portions must be between 1 and 100")
        state.values[target] = int(number) if float(number).is_integer() else number
    elif target == "time_minutes":
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"Time must be numeric, got {raw!r}") from None
        if not math.isfinite(number) or not 1 <= number <= 1440:
            raise ValueError("Time must be between 1 and 1440 minutes")
        state.values[target] = int(number) if float(number).is_integer() else number
    elif target == "dish":
        state.dish = text[:200]
        state.values["dish"] = state.dish
    elif target == "task_scope":
        if text.strip().lower() not in TASK_SCOPES:
            raise ValueError(f"Unsupported task scope {raw!r}")
        state.task_scope = text.strip().lower()[:100]
        state.values["task_scope"] = state.task_scope
    elif target == "cuisine":
        state.values["cuisine"] = text[:200]
    elif target in LIST_TARGETS:
        items = _split_list(text)
        if not items:
            raise ValueError(f"Empty value for {target}")
        state.values[target] = items
    else:  # substitution_choice
        state.values[target] = text[:500]
    state.field_status[target] = FieldStatus.PROVIDED
    _sync_request(state)


def _apply_confirmation(
    state: CookingRequestState, question: Question, payload: AnswerPayload
) -> AnswerRecord:
    record = AnswerRecord(
        question_id=question.id,
        semantic_key=question.semantic_key,
        target=question.target,
        status="answered",
        request_revision=state.revision + 1,
    )
    pending = next(
        (
            c
            for c in state.pending_confirmations
            if c.question_id == question.id and c.status == ConfirmationStatus.PENDING
        ),
        None,
    )
    if pending is None:
        raise ValueError(f"no pending confirmation for {question.id!r}")
    if payload.skip:
        record.status = "skipped"
        if question.id not in state.skipped_questions:
            state.skipped_questions.append(question.id)
        return record
    if payload.no_preference:
        raise ValueError("confirmation requires an explicit keep or change choice")
    selected = list(payload.selected or [])
    if selected == [CONFIRM_KEEP]:
        record.selected = selected
        pending.status = ConfirmationStatus.RESOLVED
        return record
    if selected == [CONFIRM_CHANGE]:
        record.selected = selected
        set_typed_value(state, pending.target, pending.proposed)
        pending.status = ConfirmationStatus.RESOLVED
        return record
    raise ValueError(f"unknown confirmation choice {selected!r}")


def _apply_one(
    state: CookingRequestState, question: Question, payload: AnswerPayload
) -> AnswerRecord:
    if is_confirmation_question(question):
        return _apply_confirmation(state, question, payload)
    record = AnswerRecord(
        question_id=question.id,
        semantic_key=question.semantic_key,
        target=question.target,
        status="answered",
        request_revision=state.revision + 1,
    )
    target = question.target
    if payload.skip:
        record.status = "skipped"
        if target not in state.values or state.values.get(target) in (None, [], ""):
            state.field_status[target] = FieldStatus.SKIPPED
        if question.id not in state.skipped_questions:
            state.skipped_questions.append(question.id)
        return record
    if payload.no_preference:
        record.no_preference = True
        state.field_status[target] = FieldStatus.NO_PREFERENCE
        if target in LIST_TARGETS:
            state.values[target] = []
        elif target in ("dish", "cuisine", "task_scope"):
            state.values[target] = None
            if target == "dish":
                state.dish = None
            if target == "task_scope":
                state.task_scope = None
        else:
            state.values[target] = None
        _sync_request(state)
        return record

    state.field_status[target] = FieldStatus.PROVIDED
    if question.input_type == QuestionInputType.TEXT:
        text = (payload.text if payload.text is not None else payload.custom_text or "").strip()
        record.text = text[:2000]
        _store_text(state, target, text)
    elif question.input_type == QuestionInputType.NUMBER:
        assert payload.number is not None
        record.number = payload.number
        _store_number(state, target, payload.number)
    else:
        selected = list(payload.selected or [])
        record.selected = selected
        if payload.custom_text is not None:
            # Documented rule: custom text SUPPLEMENTS selections.
            record.custom_text = payload.custom_text.strip()[:500]
        _store_selection(state, target, question, selected, record.custom_text)
    _sync_request(state)
    return record


def _store_text(state: CookingRequestState, target: str, text: str) -> None:
    if target == "dish":
        state.dish = text[:200]
        state.values["dish"] = state.dish
    elif target == "task_scope":
        state.task_scope = text[:100]
        state.values["task_scope"] = state.task_scope
    elif target == "cuisine":
        state.values["cuisine"] = text[:200]
    elif target in LIST_TARGETS:
        state.values[target] = _split_list(text)
    else:
        state.values[target] = text


def _store_number(state: CookingRequestState, target: str, number: float) -> None:
    value: Any = int(number) if float(number).is_integer() else number
    state.values[target] = value


def _store_selection(
    state: CookingRequestState,
    target: str,
    question: Question,
    selected: list[str],
    custom_text: str | None,
) -> None:
    labels = _label_map(question)
    if target == "task_scope":
        state.task_scope = selected[0]
        state.values["task_scope"] = selected[0]
        return
    if target == "cuisine":
        if selected == ["none"]:
            state.field_status[target] = FieldStatus.NO_PREFERENCE
            state.values[target] = None
        else:
            picked = [labels[s] for s in selected if s in labels]
            if custom_text:
                picked.append(custom_text)
            state.values[target] = picked[0] if picked else None
        return
    # List targets keep labels; custom text supplements selections.
    picked = [labels[s] if s in labels else s for s in selected if s != "none"]
    if "none" in selected:
        # Model-level validator guarantees exclusivity; double-check here.
        picked = []
        if custom_text:
            picked = [custom_text]
            state.field_status[target] = FieldStatus.PROVIDED
        else:
            state.field_status[target] = FieldStatus.NO_PREFERENCE
        state.values[target] = picked
        return
    if custom_text:
        picked.append(custom_text)
    if target in LIST_TARGETS:
        existing = state.values.get(target)
        if question.semantic_key == "dietary_detail" and isinstance(existing, list):
            state.values[target] = (existing + picked)[:100]
        else:
            state.values[target] = picked[:100]
    else:
        state.values[target] = picked


def _sync_request(state: CookingRequestState) -> None:
    req = dict(state.request)
    for key in (
        "ingredients",
        "portions",
        "time_minutes",
        "cuisine",
        "preferences",
        "dietary_constraints",
        "equipment",
    ):
        if key in state.values and state.values[key] is not None:
            req[key] = state.values[key]
    state.request = req


def _detect_conflicts(state: CookingRequestState) -> list[str]:
    blockers: list[str] = []
    dietary = [str(v).lower() for v in (state.values.get("dietary_constraints") or [])]
    ingredients = [str(v).lower() for v in (state.values.get("ingredients") or [])]
    preferences = [str(v).lower() for v in (state.values.get("preferences") or [])]
    wants_veg = any("vegan" in d or "vegetarian" in d for d in dietary)
    if wants_veg:
        hay = " ".join(ingredients + preferences)
        tokens = set(re.findall(r"[a-z]+", hay))
        hit = sorted(MEAT_TOKENS & tokens)
        if hit:
            state.field_status["dietary_constraints"] = FieldStatus.CONFLICTING
            blockers.append(
                f"conflicting_dietary_constraints: {', '.join(hit)} conflicts with "
                f"{', '.join(dietary)}; clarify which to keep"
            )
    return blockers


def _dependency_satisfied(question: Question, state: CookingRequestState) -> bool:
    dep = question.depends_on
    if dep is None:
        return True
    parent_answers = [
        a for a in state.answers if a.question_id == dep.depends_on and a.status == "answered"
    ]
    if not parent_answers:
        return False
    if not dep.required_options:
        return True
    latest = parent_answers[-1]
    return any(o in dep.required_options for o in latest.selected)


def pending_questions(all_questions: list[Question], state: CookingRequestState) -> list[Question]:
    answered_ids = {a.question_id for a in state.answers if a.status == "answered"}
    invalidated = set(state.invalidated_questions)
    skipped = set(state.skipped_questions)
    out: list[Question] = []
    for q in all_questions:
        if q.id in answered_ids or q.id in invalidated or q.id in skipped:
            continue
        if not _dependency_satisfied(q, state):
            continue
        out.append(q)
    return out


def apply_answers(
    state: CookingRequestState,
    group_questions: list[Question],
    payloads: list[AnswerPayload],
) -> tuple[CookingRequestState, list[AnswerRecord]]:
    """Validate + apply a batch of answers. Deterministic; no LLM calls."""
    by_id = {q.id: q for q in group_questions}
    # Also allow editing answers to questions from earlier groups when the
    # caller supplies their definitions; unknown ids are rejected clearly.
    records: list[AnswerRecord] = []
    for payload in payloads:
        question = by_id.get(payload.question_id)
        if question is None:
            raise ValueError(f"unknown question id {payload.question_id!r}")
        validate_answer(question, payload)
    for payload in payloads:
        question = by_id[payload.question_id]
        records.append(_apply_one(state, question, payload))
    state.answers.extend(records)
    state.revision += 1
    for r in records:
        r.request_revision = state.revision
    _invalidate_dependents(state, group_questions)
    conflict_blockers = _detect_conflicts(state)
    state.blockers = _recompute_blockers(state, conflict_blockers)
    state.outcome, state.ready_for_retrieval = _recompute_outcome(state)
    return state, records


def _invalidate_dependents(state: CookingRequestState, group_questions: list[Question]) -> None:
    by_id = {q.id: q for q in group_questions}
    for answer in list(state.answers):
        if answer.status != "answered":
            continue
        # Find questions depending on this answer's question.
        for q in group_questions:
            dep = q.depends_on
            if dep is None or dep.depends_on != answer.question_id:
                continue
            if not _dependency_satisfied(q, state):
                # Invalidate any answers given to the now-inapplicable question.
                for other in state.answers:
                    if other.question_id == q.id and other.status == "answered":
                        other.status = "invalidated"
                        if q.id not in state.invalidated_questions:
                            state.invalidated_questions.append(q.id)
    # Unknown group context: still invalidate dietary detail when screen says none.
    screen = [
        a for a in state.answers if a.semantic_key == "dietary_screen" and a.status == "answered"
    ]
    if screen and screen[-1].selected == ["none"]:
        for other in state.answers:
            if other.semantic_key == "dietary_detail" and other.status == "answered":
                other.status = "invalidated"
                if other.question_id not in state.invalidated_questions:
                    state.invalidated_questions.append(other.question_id)
    _ = by_id


def _recompute_blockers(state: CookingRequestState, conflict_blockers: list[str]) -> list[str]:
    blockers = list(conflict_blockers)
    # Derive conflict blockers from state so pre-existing CONFLICTING fields
    # keep their documented blocker across answer round-trips.
    for field, status in state.field_status.items():
        if status == FieldStatus.CONFLICTING and not any(
            blocker_code(b) == f"{BLOCKER_CONFLICT_PREFIX}{field}" for b in blockers
        ):
            blockers.append(
                f"{BLOCKER_CONFLICT_PREFIX}{field}: conflicting requirements need clarification"
            )
    dish_skipped = (
        state.field_status.get("dish") == FieldStatus.SKIPPED
        and not [v for v in (state.values.get("ingredients") or [])]
        and "rule-dish-direction" in state.skipped_questions
    )
    if dish_skipped:
        blockers.append(
            f"{BLOCKER_DISH_DIRECTION_SKIPPED}: required dish direction was skipped and no "
            "ingredients are known; change task scope or provide a dish/ingredients"
        )
    task = (state.task_scope or state.values.get("task_scope") or "").strip().lower()
    if (
        task == "scaling"
        and state.field_status.get("portions") == FieldStatus.SKIPPED
        and "rule-portions" in state.skipped_questions
    ):
        blockers.append(
            f"{BLOCKER_PORTIONS_SKIPPED}: scaling requires portions; provide portions or "
            "change task scope away from scaling"
        )
    return blockers[:50]


def _recompute_outcome(state: CookingRequestState) -> tuple[PlanningOutcome, bool]:
    from culinary_copilot.domain.rule_planner import retrieval_ready

    if any(v == FieldStatus.CONFLICTING for v in state.field_status.values()):
        return PlanningOutcome.NEEDS_CLARIFICATION, False
    if is_blocked(state.blockers):
        return PlanningOutcome.BLOCKED, False
    if retrieval_ready(state):
        return PlanningOutcome.READY_FOR_RETRIEVAL, True
    return PlanningOutcome.NEEDS_CLARIFICATION, False
