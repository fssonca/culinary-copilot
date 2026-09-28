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
    try:
        import asyncio as _asyncio

        pairs = await _asyncio.to_thread(core.find_balanced_pairings, args.ingredient, args.k)
    except Exception as exc:
        kind = type(exc).__name__
        if "UnknownIngredient" in kind:
            return _invalid(f"{tool_name}: unknown canonical ingredient {args.ingredient!r}")
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
    return {
        "ok": True,
        "variant": variant,
        "ingredient": args.ingredient,
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
    try:
        import asyncio as _asyncio

        pairs = await _asyncio.to_thread(core.find_balanced_pairings, args.ingredient, args.k)
    except Exception as exc:
        kind = type(exc).__name__
        if "UnknownIngredient" in kind:
            return _invalid(f"find_substitutions: unknown canonical ingredient {args.ingredient!r}")
        if (
            "Disabled" in kind
            or "LocalEntryNotFound" in kind
            or "Offline" in kind
            or isinstance(exc, (OSError, FileNotFoundError, ValueError))
        ):
            return _not_configured(
                f"find_substitutions not configured: core asset missing ({kind})"
            )
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
            "message": f"find_substitutions unavailable: {kind}",
            "next_action": _naf(REASON_TOOL_UNAVAILABLE),
        }
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
        "ingredient": args.ingredient,
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
            description="Balanced ingredient pairings (epicure-core).",
            args_model=PairingsArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
        ToolDefinition(
            name="find_conventional_pairings",
            description="Conventional pairings (epicure-cooc, recipe context only).",
            args_model=PairingsArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
        ToolDefinition(
            name="find_flavor_pairings",
            description="Flavor pairings (epicure-chem, chemistry only).",
            args_model=PairingsArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
        ToolDefinition(
            name="find_substitutions",
            description="Unverified Epicure substitution candidates; never a dietary claim.",
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
    "tool_definitions",
]
