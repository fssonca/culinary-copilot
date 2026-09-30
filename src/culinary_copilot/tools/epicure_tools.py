"""Epicure pairing and substitution tools (Milestone 3, Phase 2, reviewed).

``find_balanced_pairings`` uses the existing epicure-core adapter;
``find_conventional_pairings`` (cooc) and ``find_flavor_pairings``
(chem) sit behind the same adapter boundary. The tool path loads
cached assets only (``EpicureCore(cache_only=True)``): it never
downloads at query time, so with default settings (and generally) no
network call is possible from any Epicure tool. A missing local file
or revision mismatch returns the ``tool_not_configured``
(``contact_operator``) error, never an exception and never a fallback
to another sibling. Existing endpoints that use ``EpicureCore``
directly are unchanged (their default ``cache_only=False`` still
lazy-downloads).

``find_substitutions`` returns Epicure candidates each labelled
``"unverified"`` with an explicit disclaimer. It never makes a
dietary-compatibility claim: substitution safety, allergy safety and
nutrition stay unknown.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from culinary_copilot.domain.recommendations import (
    REASON_TOOL_INVALID_ARGUMENTS,
    REASON_TOOL_NOT_CONFIGURED,
    next_action_for,
)
from culinary_copilot.tools.registry import ToolContext, ToolDefinition

logger = logging.getLogger(__name__)


class PairingsArgs(BaseModel):
    """Arguments for the three Epicure pairing tools."""

    model_config = ConfigDict(extra="forbid")

    ingredient: str = Field(min_length=1, max_length=200)
    k: int = Field(default=5, ge=1, le=20)


class SubstitutionsArgs(BaseModel):
    """Arguments for ``find_substitutions``."""

    model_config = ConfigDict(extra="forbid")

    ingredient: str = Field(min_length=1, max_length=200)
    k: int = Field(default=5, ge=1, le=20)


def _invalid(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": "invalid_arguments",
        "reason": REASON_TOOL_INVALID_ARGUMENTS,
        "message": message,
        "next_action": next_action_for(REASON_TOOL_INVALID_ARGUMENTS),
    }


def _not_configured(message: str) -> dict[str, Any]:
    """Permanent configuration error (contact_operator; never retried)."""
    return {
        "ok": False,
        "error_type": "unavailable",
        "reason": REASON_TOOL_NOT_CONFIGURED,
        "message": message,
        "next_action": next_action_for(REASON_TOOL_NOT_CONFIGURED),
    }


def _core_for(context: ToolContext, variant: Literal["core", "cooc", "chem"]) -> Any | None:
    if variant == "core":
        return getattr(context, "epicure_core", None)
    if variant == "cooc":
        return getattr(context, "epicure_cooc", None)
    return getattr(context, "epicure_chem", None)


# --- ingredient normalization --------------------------------------------------


def _basic_form(ingredient: str) -> str:
    """Lowercase, trim, spaces and hyphens to underscores."""
    return "_".join(str(ingredient).strip().lower().replace("-", " ").split())


def _singularize(word: str) -> str:
    """Simple singular for one underscore token (small rule set)."""
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    for ending in ("ches", "shes", "sses", "xes", "zes", "oes"):
        if word.endswith(ending) and len(word) > len(ending):
            return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 2:
        return word[:-1]
    return word


def ingredient_candidates(ingredient: str) -> list[str]:
    """Ordered normalization candidates for one raw ingredient string.

    a. lowercase/trim with spaces and hyphens as underscores; b. the
    simple singular form; c. the last token, then the first token,
    each singularized. Duplicates dropped, order kept.
    """
    basic = _basic_form(ingredient)
    out: list[str] = []
    forms = [basic, "_".join(_singularize(t) for t in basic.split("_"))]
    tokens = basic.split("_")
    if tokens:
        forms.extend([_singularize(tokens[-1]), _singularize(tokens[0])])
    for form in forms:
        if form and form not in out:
            out.append(form)
    return out


def normalize_ingredient(ingredient: str, vocabulary: set[str] | None) -> str | None:
    """First normalization candidate found in the vocabulary.

    ``None`` vocabulary keeps the old behavior: the basic form is used
    unverified (cores without a vocabulary, e.g. test fakes, keep
    working exactly as before).
    """
    candidates = ingredient_candidates(ingredient)
    if vocabulary is None:
        return candidates[0] if candidates else None
    for candidate in candidates:
        if candidate in vocabulary:
            return candidate
    return None


def suggest_ingredients(ingredient: str, vocabulary: set[str], *, limit: int = 8) -> list[str]:
    """Up to ``limit`` vocabulary hints for a miss: difflib close
    matches first, then substring hits, alphabetically."""
    basic = _basic_form(ingredient)
    out = list(difflib.get_close_matches(basic, sorted(vocabulary), n=limit, cutoff=0.6))
    for name in sorted(vocabulary):
        if len(out) >= limit:
            break
        if name in out:
            continue
        if basic in name or (name and name in basic):
            out.append(name)
    return out[:limit]


def _core_vocabulary(core: Any) -> set[str] | None:
    """Vocabulary names for one core adapter (None when unavailable).

    Prefers a ``vocabulary()`` hook (the real adapter loads once,
    thread-safe); falls back to a ``_vocab`` mapping for older fakes.
    Loading raises on missing assets — the caller maps that exactly
    like a query-time load failure.
    """
    hook = getattr(core, "vocabulary", None)
    if callable(hook):
        names = hook()
        return set(names) if names is not None else None
    raw = getattr(core, "_vocab", None)
    if isinstance(raw, dict) and raw:
        return set(raw.keys())
    return None


def build_epicure_variants(settings: Any) -> dict[str, Any]:
    """Build cache-only core/cooc/chem ``EpicureCore`` instances.

    Each variant pins its own model id + revision from settings
    (``EPICURE_{COOC,CHEM}_{MODEL_ID,REVISION}``). ``cache_only=True``
    forces ``hf_hub_download(..., local_files_only=True)`` at query
    time: a missing file or revision never triggers a download and
    surfaces as ``tool_not_configured``. Construction itself makes no
    network call (loading stays lazy until the first query).
    """
    from culinary_copilot.tools.epicure import EpicureCore

    variants: dict[str, Any] = {}
    variants["core"] = EpicureCore(settings, cache_only=True)
    cooc_settings = _variant_settings(
        settings,
        getattr(settings, "epicure_cooc_model_id", "Kaikaku/epicure-cooc"),
        getattr(
            settings,
            "epicure_cooc_revision",
            "03edd311adde6e39a2eb6f9f3fa78f7396be6b53",
        ),
    )
    chem_settings = _variant_settings(
        settings,
        getattr(settings, "epicure_chem_model_id", "Kaikaku/epicure-chem"),
        getattr(
            settings,
            "epicure_chem_revision",
            "2461ef3fbafab36d2b1111187a3df98721146861",
        ),
    )
    variants["cooc"] = EpicureCore(cooc_settings, cache_only=True)
    variants["chem"] = EpicureCore(chem_settings, cache_only=True)
    return variants


def _variant_settings(settings: Any, model_id: str, revision: str) -> Any:
    """Shallow settings copy with an overridden Epicure model + revision."""

    class _View:
        def __init__(self, base: Any) -> None:
            self._base = base

        def __getattr__(self, name: str) -> Any:
            if name == "epicure_model_id":
                return model_id
            if name == "epicure_revision":
                return revision
            return getattr(self._base, name)

    return _View(settings)


def _classify_core_error(tool_name: str, variant: str, exc: Exception) -> dict[str, Any]:
    """Map an adapter failure to its typed result (never raises)."""
    kind = type(exc).__name__
    if "Disabled" in kind:
        return _not_configured(f"{tool_name} not configured: disabled")
    # Cache-only load failures (missing file / revision mismatch /
    # offline) never download; they are permanent configuration errors.
    if (
        "LocalEntryNotFound" in kind
        or "Offline" in kind
        or isinstance(exc, (OSError, FileNotFoundError, ValueError))
    ):
        return _not_configured(f"{tool_name} not configured: {variant} asset missing ({kind})")
    from culinary_copilot.domain.recommendations import (
        REASON_TOOL_UNAVAILABLE,
    )
    from culinary_copilot.domain.recommendations import (
        next_action_for as _naf,
    )

    return {
        "ok": False,
        "error_type": "unavailable",
        "reason": REASON_TOOL_UNAVAILABLE,
        "message": f"{tool_name} unavailable: {kind}",
        "next_action": _naf(REASON_TOOL_UNAVAILABLE),
    }


def _resolve_queried(
    tool_name: str, variant: str, requested: str, core: Any
) -> tuple[str | None, dict[str, Any] | None]:
    """Normalize ``requested`` against the adapter vocabulary.

    Returns ``(queried_as, None)`` on success, ``(None, error_result)``
    on a miss (``invalid_arguments`` with up to 8 vocabulary
    suggestions) or an asset-load failure (mapped exactly like a
    query-time failure).
    """
    try:
        vocabulary = _core_vocabulary(core)
    except Exception as exc:
        return None, _classify_core_error(tool_name, variant, exc)
    queried = normalize_ingredient(requested, vocabulary)
    if queried is None:
        assert vocabulary is not None
        suggestions = suggest_ingredients(requested, vocabulary)
        hint = f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
        return None, _invalid(
            f"{tool_name}: unknown ingredient {requested!r}{hint} "
            "(use one base ingredient, singular, e.g. chicken, lentil, olive_oil)"
        )
    return queried, None


async def _pairings_impl(
    args: PairingsArgs,
    context: ToolContext,
    *,
    variant: Literal["core", "cooc", "chem"],
    tool_name: str,
) -> dict[str, Any]:
    core = _core_for(context, variant)
    if core is None:
        return _not_configured(f"{tool_name} not configured: {variant} adapter missing")
    settings = getattr(core, "settings", None)
    if settings is not None and not bool(getattr(settings, "epicure_enabled", False)):
        return _not_configured(f"{tool_name} not configured: EPICURE_ENABLED=false")
    requested = args.ingredient
    queried, error = _resolve_queried(tool_name, variant, requested, core)
    if error is not None:
        return error
    assert queried is not None
    try:
        import asyncio as _asyncio

        pairs = await _asyncio.to_thread(core.find_balanced_pairings, queried, args.k)
    except Exception as exc:
        if "UnknownIngredient" in type(exc).__name__:
            # Safety net (the vocabulary listed it): same invalid shape.
            return _invalid(
                f"{tool_name}: unknown ingredient {requested!r} (queried as {queried!r}); "
                "use one base ingredient, singular, e.g. chicken, lentil, olive_oil"
            )
        return _classify_core_error(tool_name, variant, exc)
    return {
        "ok": True,
        "variant": variant,
        "ingredient": requested,
        "requested": requested,
        "queried_as": queried,
        "pairings": [{"ingredient": p.ingredient, "score": float(p.score)} for p in pairs],
    }


async def find_balanced_pairings_impl(args: PairingsArgs, context: ToolContext) -> dict[str, Any]:
    """Balanced pairings (epicure-core: chemistry + recipe context)."""
    return await _pairings_impl(args, context, variant="core", tool_name="find_balanced_pairings")


async def find_conventional_pairings_impl(
    args: PairingsArgs, context: ToolContext
) -> dict[str, Any]:
    """Conventional pairings (epicure-cooc: recipe context only)."""
    return await _pairings_impl(
        args, context, variant="cooc", tool_name="find_conventional_pairings"
    )


async def find_flavor_pairings_impl(args: PairingsArgs, context: ToolContext) -> dict[str, Any]:
    """Flavor pairings (epicure-chem: chemistry only)."""
    return await _pairings_impl(args, context, variant="chem", tool_name="find_flavor_pairings")


async def find_substitutions_impl(args: SubstitutionsArgs, context: ToolContext) -> dict[str, Any]:
    """Epicure substitution candidates, each labelled unverified.

    Candidates come from the balanced (core) adapter. Every candidate
    carries ``verification: "unverified"``; dietary compatibility,
    allergy safety and nutrition stay unknown and no dietary claim is
    ever made.
    """
    core = getattr(context, "epicure_core", None)
    if core is None:
        return _not_configured("find_substitutions not configured: core adapter missing")
    settings = getattr(core, "settings", None)
    if settings is not None and not bool(getattr(settings, "epicure_enabled", False)):
        return _not_configured("find_substitutions not configured: EPICURE_ENABLED=false")
    requested = args.ingredient
    queried, error = _resolve_queried("find_substitutions", "core", requested, core)
    if error is not None:
        return error
    assert queried is not None
    try:
        import asyncio as _asyncio

        pairs = await _asyncio.to_thread(core.find_balanced_pairings, queried, args.k)
    except Exception as exc:
        if "UnknownIngredient" in type(exc).__name__:
            return _invalid(
                f"find_substitutions: unknown ingredient {requested!r} "
                f"(queried as {queried!r}); use one base ingredient, singular, "
                "e.g. chicken, lentil, olive_oil"
            )
        return _classify_core_error("find_substitutions", "core", exc)
    candidates = [
        {
            "ingredient": p.ingredient,
            "score": float(p.score),
            "verification": "unverified",
        }
        for p in pairs
    ]
    return {
        "ok": True,
        "ingredient": requested,
        "requested": requested,
        "queried_as": queried,
        "candidates": candidates,
        "disclaimer": (
            "Substitution candidates are unverified Epicure neighbors; "
            "dietary compatibility, allergy safety and nutrition are unknown. "
            "No dietary claim is made."
        ),
    }


def tool_definitions(timeout_s: float = 10.0) -> list[ToolDefinition]:
    """Epicure-tool definitions (timeout server-set, never caller-set)."""
    return [
        ToolDefinition(
            name="find_balanced_pairings",
            description=(
                "Balanced ingredient pairings (epicure-core): one base ingredient, "
                "singular, e.g. chicken, lentil, olive_oil."
            ),
            args_model=PairingsArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
        ToolDefinition(
            name="find_conventional_pairings",
            description=(
                "Conventional pairings (epicure-cooc, recipe context only): one base "
                "ingredient, singular, e.g. chicken, lentil, olive_oil."
            ),
            args_model=PairingsArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
        ToolDefinition(
            name="find_flavor_pairings",
            description=(
                "Flavor pairings (epicure-chem, chemistry only): one base ingredient, "
                "singular, e.g. chicken, lentil, olive_oil."
            ),
            args_model=PairingsArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
        ToolDefinition(
            name="find_substitutions",
            description=(
                "Unverified Epicure substitution candidates; never a dietary claim. "
                "One base ingredient, singular, e.g. chicken, lentil, olive_oil."
            ),
            args_model=SubstitutionsArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
    ]


__all__ = [
    "PairingsArgs",
    "SubstitutionsArgs",
    "build_epicure_variants",
    "find_balanced_pairings_impl",
    "find_conventional_pairings_impl",
    "find_flavor_pairings_impl",
    "find_substitutions_impl",
    "ingredient_candidates",
    "normalize_ingredient",
    "suggest_ingredients",
    "tool_definitions",
]
