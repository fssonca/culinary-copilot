"""Deterministic (rule-based) question planner.

Pure and offline-testable: evaluates structured
:class:`CookingRequestState` and proposes relevant questions. No I/O, no
network, no settings access.

Planning principles (see docs/clarification.md):

- Required questions first, then priority order. Bounded group size
  (default 6, configurable); never a universal two-question limit.
- Never re-ask answered fields (``provided`` / ``no_preference``), retained
  values (explicit time limit), or skipped required questions (those become
  documented ``blocked`` outcomes, not repetition loops).
- Discovery readiness needs dish direction OR available ingredients plus no
  conflicts. Portions are required only for scaling; a full preference
  questionnaire is never required for retrieval.
- Conflicting fields produce clarification questions, never silent choices.
"""

from culinary_copilot.domain.clarification import (
    ALLOWED_TARGETS,
    BLOCKER_CONFLICT_PREFIX,
    BLOCKER_DISH_DIRECTION_SKIPPED,
    BLOCKER_PORTIONS_SKIPPED,
    CookingRequestState,
    FieldStatus,
    PlanningOutcome,
    Question,
    QuestionDependency,
    QuestionInputType,
    QuestionOption,
    QuestionSource,
    is_blocked,
)

DEFAULT_MAX_QUESTIONS = 6
MAX_GROUP_QUESTIONS = 12

PRIORITY_CONFLICT = 100
PRIORITY_DISH = 90
PRIORITY_PORTIONS_SCALING = 90
PRIORITY_INGREDIENTS = 85
PRIORITY_TASK_SCOPE = 70
PRIORITY_TIME = 60
PRIORITY_DIETARY_SCREEN = 55
PRIORITY_DIETARY_DETAIL = 45
PRIORITY_CUISINE = 40
PRIORITY_PORTIONS_OPTIONAL = 35
PRIORITY_EQUIPMENT = 30
PRIORITY_PREFERENCES = 30


def _status(state: CookingRequestState, target: str) -> FieldStatus:
    status = state.field_status.get(target, FieldStatus.UNKNOWN)
    return status


def _answered_semantics(state: CookingRequestState) -> set[str]:
    return {a.semantic_key for a in state.answers if a.status == "answered"}


def _skipped_ids(state: CookingRequestState) -> set[str]:
    return set(state.skipped_questions)


def _ingredients(state: CookingRequestState) -> list[str]:
    raw = state.request.get("ingredients", [])
    vals = state.values.get("ingredients", raw)
    if isinstance(vals, list):
        return [v for v in vals if isinstance(v, str) and v.strip()]
    return []


def _conflict_question(field: str) -> Question:
    return Question(
        id=f"rule-conflict-{field.replace('_', '-')}",
        semantic_key=f"conflict_{field}",
        topic=f"Conflicting {field} requirements",
        prompt=(f"Your {field.replace('_', ' ')} requirements conflict. Which one should I keep?"),
        input_type=QuestionInputType.TEXT,
        required=True,
        priority=PRIORITY_CONFLICT,
        source=QuestionSource.RULE,
        target=field,
        max_length=500,
    )


def _catalog(state: CookingRequestState) -> list[Question]:
    """Full candidate catalog before suppression/ordering."""
    task = (state.task_scope or state.values.get("task_scope") or "").strip().lower()
    is_scaling = task == "scaling"
    candidates: list[Question] = []

    for field in sorted(state.field_status):
        if state.field_status[field] == FieldStatus.CONFLICTING and field in ALLOWED_TARGETS:
            candidates.append(_conflict_question(field))

    candidates.append(
        Question(
            id="rule-dish-direction",
            semantic_key="dish_direction",
            topic="Dish direction",
            prompt="What would you like to cook? (a dish, craving, or meal idea)",
            input_type=QuestionInputType.TEXT,
            required=True,
            priority=PRIORITY_DISH,
            source=QuestionSource.RULE,
            target="dish",
            max_length=300,
        )
    )
    candidates.append(
        Question(
            id="rule-available-ingredients",
            semantic_key="available_ingredients",
            topic="Available ingredients",
            prompt="What ingredients do you have available? (comma-separated)",
            input_type=QuestionInputType.TEXT,
            required=False,
            priority=PRIORITY_INGREDIENTS,
            source=QuestionSource.RULE,
            target="ingredients",
            max_length=1000,
        )
    )
    candidates.append(
        Question(
            id="rule-portions",
            semantic_key="portions",
            topic="Portions",
            prompt="How many people are you cooking for?",
            input_type=QuestionInputType.NUMBER,
            required=is_scaling,
            priority=PRIORITY_PORTIONS_SCALING if is_scaling else PRIORITY_PORTIONS_OPTIONAL,
            source=QuestionSource.RULE,
            target="portions",
            min_value=1,
            max_value=100,
        )
    )
    candidates.append(
        Question(
            id="rule-time-limit",
            semantic_key="time_limit",
            topic="Time limit",
            prompt="Do you have a time limit in minutes? (leave blank if none)",
            input_type=QuestionInputType.NUMBER,
            required=False,
            priority=PRIORITY_TIME,
            source=QuestionSource.RULE,
            target="time_minutes",
            min_value=1,
            max_value=1440,
        )
    )
    candidates.append(
        Question(
            id="rule-dietary-screen",
            semantic_key="dietary_screen",
            topic="Dietary restrictions",
            prompt="Do you have any dietary restrictions?",
            input_type=QuestionInputType.SINGLE_CHOICE,
            options=[
                QuestionOption(id="none", label="No restrictions"),
                QuestionOption(id="vegetarian", label="Vegetarian"),
                QuestionOption(id="vegan", label="Vegan"),
                QuestionOption(id="gluten_free", label="Gluten-free"),
                QuestionOption(id="other", label="Other (describe)"),
            ],
            allow_custom_text=True,
            required=False,
            priority=PRIORITY_DIETARY_SCREEN,
            source=QuestionSource.RULE,
            target="dietary_constraints",
        )
    )
    candidates.append(
        Question(
            id="rule-dietary-detail",
            semantic_key="dietary_detail",
            topic="Dietary details",
            prompt="Please describe your dietary restriction in detail.",
            input_type=QuestionInputType.TEXT,
            required=False,
            priority=PRIORITY_DIETARY_DETAIL,
            depends_on=QuestionDependency(
                depends_on="rule-dietary-screen",
                required_options=["vegetarian", "vegan", "gluten_free", "other"],
            ),
            source=QuestionSource.RULE,
            target="dietary_constraints",
            max_length=300,
        )
    )
    candidates.append(
        Question(
            id="rule-cuisine",
            semantic_key="cuisine",
            topic="Cuisine",
            prompt="Any cuisine preference?",
            input_type=QuestionInputType.SINGLE_CHOICE,
            options=[
                QuestionOption(id="none", label="No preference"),
                QuestionOption(id="italian", label="Italian"),
                QuestionOption(id="mexican", label="Mexican"),
                QuestionOption(id="indian", label="Indian"),
                QuestionOption(id="chinese", label="Chinese"),
                QuestionOption(id="other", label="Other (describe)"),
            ],
            allow_custom_text=True,
            required=False,
            priority=PRIORITY_CUISINE,
            source=QuestionSource.RULE,
            target="cuisine",
        )
    )
    candidates.append(
        Question(
            id="rule-equipment",
            semantic_key="equipment",
            topic="Equipment",
            prompt="What cooking equipment do you have? (comma-separated)",
            input_type=QuestionInputType.TEXT,
            required=False,
            priority=PRIORITY_EQUIPMENT,
            source=QuestionSource.RULE,
            target="equipment",
            max_length=500,
        )
    )
    candidates.append(
        Question(
            id="rule-preferences",
            semantic_key="preferences",
            topic="Preferences",
            prompt="Any other preferences? (comma-separated)",
            input_type=QuestionInputType.TEXT,
            required=False,
            priority=PRIORITY_PREFERENCES,
            source=QuestionSource.RULE,
            target="preferences",
            max_length=500,
        )
    )
    if _status(state, "task_scope") == FieldStatus.UNKNOWN:
        candidates.append(
            Question(
                id="rule-task-scope",
                semantic_key="task_scope",
                topic="Task",
                prompt="What are you trying to do?",
                input_type=QuestionInputType.SINGLE_CHOICE,
                options=[
                    QuestionOption(id="discovery", label="Find a recipe"),
                    QuestionOption(id="meal_planning", label="Plan a meal"),
                    QuestionOption(id="scaling", label="Scale a recipe"),
                ],
                required=False,
                priority=PRIORITY_TASK_SCOPE,
                source=QuestionSource.RULE,
                target="task_scope",
            )
        )
    return candidates


def _suppress(candidates: list[Question], state: CookingRequestState) -> list[Question]:
    answered = _answered_semantics(state)
    skipped = _skipped_ids(state)
    out: list[Question] = []
    for q in candidates:
        if q.id in skipped:
            continue  # skipped required questions become blockers, never loops
        if q.semantic_key in answered:
            continue
        st = _status(state, q.target)
        if st in (FieldStatus.PROVIDED, FieldStatus.NO_PREFERENCE):
            # Explicit answers (including explicit no-preference) suppress.
            # Conflict questions target conflicting fields, which fall through
            # because their status is CONFLICTING, not PROVIDED.
            if not q.semantic_key.startswith("conflict_"):
                continue
        if q.semantic_key == "dietary_detail" and st == FieldStatus.PROVIDED:
            continue
        out.append(q)
    return out


def _order(questions: list[Question]) -> list[Question]:
    return sorted(questions, key=lambda q: (not q.required, -q.priority, q.id))


def discovery_ready(state: CookingRequestState) -> bool:
    """Sufficient search information for recipe discovery.

    Dish direction OR available ingredients, with no conflicting fields.
    Portions and the full preference questionnaire are never required.
    Task-essential information (e.g. portions for scaling) is checked
    separately by :func:`retrieval_ready`.
    """
    for field, status in state.field_status.items():
        if status == FieldStatus.CONFLICTING:
            return False
    dish = (state.dish or state.values.get("dish") or "").strip()
    if dish:
        return True
    return len(_ingredients(state)) > 0


def task_essentials_met(state: CookingRequestState) -> bool:
    """Task-essential information beyond bare discovery search keys.

    Scaling requires portions; all other scopes have no extra essentials.
    """
    task = (state.task_scope or state.values.get("task_scope") or "").strip().lower()
    if task == "scaling":
        portions = state.values.get("portions")
        return isinstance(portions, (int, float)) and portions is not None
    return True


def retrieval_ready(state: CookingRequestState) -> bool:
    """Authoritative readiness gate: discovery keys plus task essentials."""
    return discovery_ready(state) and task_essentials_met(state)


def plan_rule_questions(
    state: CookingRequestState, *, max_questions: int = DEFAULT_MAX_QUESTIONS
) -> tuple[list[Question], list[str], PlanningOutcome]:
    """Plan rule questions for ``state``.

    Returns ``(questions, blockers, outcome)``. Pure: no I/O.
    """
    if not 1 <= max_questions <= MAX_GROUP_QUESTIONS:
        raise ValueError(f"max_questions must be between 1 and {MAX_GROUP_QUESTIONS}")
    candidates = _suppress(_catalog(state), state)
    task = (state.task_scope or state.values.get("task_scope") or "").strip().lower()
    is_scaling = task == "scaling"
    ready = retrieval_ready(state)

    blockers: list[str] = []
    for field, status in state.field_status.items():
        if status == FieldStatus.CONFLICTING:
            blockers.append(
                f"{BLOCKER_CONFLICT_PREFIX}{field}: conflicting requirements need clarification"
            )

    # Skipped required essentials become documented blockers.
    dish_status = _status(state, "dish")
    has_ingredients = len(_ingredients(state)) > 0
    if (
        dish_status == FieldStatus.SKIPPED
        and not has_ingredients
        and "rule-dish-direction" in _skipped_ids(state)
    ):
        blockers.append(
            f"{BLOCKER_DISH_DIRECTION_SKIPPED}: required dish direction was skipped and no "
            "ingredients are known; change task scope or provide a dish/ingredients"
        )
    portions_status = _status(state, "portions")
    if (
        is_scaling
        and portions_status == FieldStatus.SKIPPED
        and "rule-portions" in _skipped_ids(state)
    ):
        blockers.append(
            f"{BLOCKER_PORTIONS_SKIPPED}: scaling requires portions; provide portions or "
            "change task scope away from scaling"
        )

    if ready and not blockers:
        outcome = PlanningOutcome.READY_FOR_RETRIEVAL
        # Sufficient discovery info: no questionnaire. Keep only conflict
        # questions (none here, since conflicts imply not ready).
        questions: list[Question] = [q for q in candidates if q.required and q.priority >= 100]
        return _order(questions)[:max_questions], blockers, outcome

    if blockers and is_blocked(blockers):
        outcome = PlanningOutcome.BLOCKED
        # Blocked: return remaining applicable questions without the skipped ones.
        return _order(candidates)[:max_questions], blockers, outcome

    # Not ready: prioritize search-critical questions (dish, ingredients,
    # portions-when-scaling, task scope) plus a bounded set of high-value
    # optional filters (time, dietary screen). Drop low-value questionnaire
    # tail (cuisine detail, equipment, preferences, dietary detail) unless
    # room remains within the bound.
    if not ready:
        critical = {
            "dish_direction",
            "available_ingredients",
            "portions",
            "task_scope",
            "time_limit",
            "dietary_screen",
        }
        conflicts = [q for q in candidates if q.semantic_key.startswith("conflict_")]
        rest = [q for q in candidates if q in candidates and q not in conflicts]
        preferred = [q for q in rest if q.semantic_key in critical]
        tail = [q for q in rest if q.semantic_key not in critical]
        ordered = _order(conflicts + preferred) + _order(tail)
        # Deduplicate while preserving order.
        seen: set[str] = set()
        deduped: list[Question] = []
        for q in ordered:
            if q.id not in seen:
                seen.add(q.id)
                deduped.append(q)
        outcome = PlanningOutcome.NEEDS_CLARIFICATION
        return deduped[:max_questions], blockers, outcome

    outcome = PlanningOutcome.NEEDS_CLARIFICATION
    return _order(candidates)[:max_questions], blockers, outcome
