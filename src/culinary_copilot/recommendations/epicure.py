"""Epicure consultation boundary for recommendations (Phase 3).

Stage A exposes the boundary with a fake adapter; Stage B connects the
real cached adapter. Outcomes are recorded distinctly:

- ``consulted``: real neighbor suggestions returned for canonical
  ingredients and available for later assessment.
- ``simple_technique_skip``: explicit documented policy skip with reason.
- ``disabled``: Epicure master switch off (never a consultation).
- ``unavailable``: enabled but load/query failed (never a consultation).
- ``unmapped``: queried ingredient has no canonical mapping.
- ``insufficient_ingredient_context``: no usable ingredient names to query.

Disabled/unavailable tools never appear as a successful consultation or a
simple-task skip. Suggestions are unverified pairing notes: they may
explain pairing considerations or be deferred as future adaptations. They
do not establish substitutions, dietary safety, nutrition, or chemistry,
and are never silently incorporated into the rendered recipe.
"""

from __future__ import annotations

from typing import Any, Protocol

from culinary_copilot.domain.recommendations import EpicureOutcome

EPICURE_TOOL_NAME = "epicure_pairings"


class EpicureAdapter(Protocol):
    def consult(
        self, ingredients: list[str], *, suggestion_count: int
    ) -> tuple[EpicureOutcome, list[dict[str, Any]], str]:
        """Return ``(outcome, suggestions, note)`` synchronously.

        ``suggestions`` are ``{"ingredient": str, "score": float,
        "for": str}`` records. ``note`` is a short human-readable reason.
        """
        ...


class FakeEpicureAdapter:
    """Offline fake: scripted (outcome, suggestions, note) per consultation."""

    def __init__(
        self,
        script: list[tuple[EpicureOutcome, list[dict[str, Any]], str]] | None = None,
    ) -> None:
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def consult(
        self, ingredients: list[str], *, suggestion_count: int
    ) -> tuple[EpicureOutcome, list[dict[str, Any]], str]:
        self.calls.append({"ingredients": list(ingredients), "k": suggestion_count})
        if self.script:
            outcome, suggestions, note = self.script.pop(0)
            return outcome, list(suggestions), note
        return (
            EpicureOutcome.CONSULTED,
            [{"ingredient": "garlic", "score": 0.5, "for": ingredients[0] if ingredients else ""}]
            if ingredients
            else [],
            "fake consultation",
        )


class CachedEpicureAdapter:
    """Real adapter over cached Epicure assets only (Stage B).

    Wraps ``EpicureCore``; never downloads (no network from this path in
    tests: the core raises when assets are missing and that maps to
    ``unavailable``). Canonical ingredient names come from the caller.
    """

    def __init__(self, core: Any) -> None:
        self.core = core

    def consult(
        self, ingredients: list[str], *, suggestion_count: int
    ) -> tuple[EpicureOutcome, list[dict[str, Any]], str]:
        settings = getattr(self.core, "settings", None)
        if settings is not None and not bool(getattr(settings, "epicure_enabled", False)):
            return EpicureOutcome.DISABLED, [], "Epicure is disabled (EPICURE_ENABLED=false)"
        if not ingredients:
            return (
                EpicureOutcome.INSUFFICIENT_INGREDIENT_CONTEXT,
                [],
                "No canonical ingredient names available for consultation",
            )
        suggestions: list[dict[str, Any]] = []
        unmapped: list[str] = []
        for name in ingredients:
            try:
                pairs = self.core.find_balanced_pairings(name, suggestion_count)
            except Exception as exc:
                kind = type(exc).__name__
                if "UnknownIngredient" in kind:
                    unmapped.append(name)
                    continue
                if "Disabled" in kind:
                    return EpicureOutcome.DISABLED, [], str(exc)[:200]
                return EpicureOutcome.UNAVAILABLE, [], f"Epicure query failed: {kind}"
            for pair in pairs:
                ingredient = getattr(pair, "ingredient", None)
                score = getattr(pair, "score", None)
                suggestions.append(
                    {
                        "ingredient": str(ingredient),
                        "score": float(score) if score is not None else 0.0,
                        "for": name,
                    }
                )
        suggestions = suggestions[: max(0, suggestion_count * len(ingredients))]
        if not suggestions and unmapped:
            return (
                EpicureOutcome.UNMAPPED,
                [],
                f"No canonical mapping for: {', '.join(unmapped[:5])}",
            )
        return (
            EpicureOutcome.CONSULTED,
            suggestions,
            f"Consulted for {len(ingredients)} ingredient(s)",
        )
