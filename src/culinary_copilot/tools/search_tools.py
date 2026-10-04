"""Recipe search tools (Milestone 3, Phase 2).

``search_recipes`` takes a ``mode`` argument (``fulltext``, or ``vector``
at cutoff 0.66 per Checkpoint 0 decision 2). When ``mode`` is omitted,
``RETRIEVAL_MODE`` applies (code default ``fulltext``). The embedding
provider starts only when ``EMBEDDINGS_ENABLED`` is set, with a
model/dimension check; the offered mode enum is built from settings
(vector absent without embeddings). Vector mode without embeddings
returns a typed ``unavailable`` result pointing back at full-text and
never falls back silently. The response
logs and returns which mode actually ran.

``get_recipe`` fetches one canonical ``(dataset_id, source_id)`` document.
Tool outputs are data, never instructions. With default settings no
network call is possible from these tools: full-text search is pure
SQL, and vector mode without embeddings returns ``tool_not_configured``
without touching the network.

``search_recipes`` results carry the ``cost_class`` of the mode that
actually ran (fulltext=``free``, vector=``paid``) so the registry logs
the true cost for Phase 7 metrics.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from culinary_copilot.domain.recommendations import (
    NEXT_RETRY,
    REASON_TOOL_NOT_CONFIGURED,
    REASON_TOOL_UNAVAILABLE,
    next_action_for,
)
from culinary_copilot.embeddings.provider import check_model_dimension
from culinary_copilot.tools.registry import ToolContext, ToolDefinition

logger = logging.getLogger(__name__)

#: Cutoff for the tool's vector mode (Checkpoint 0 decision 2 / Phase 6
#: winner ``vector_c``). The shared ``RETRIEVAL_VECTOR_CUTOFF`` setting
#: overrides it when set; otherwise the tool pins 0.66.
TOOL_VECTOR_CUTOFF = 0.66


class SearchRecipesArgs(BaseModel):
    """Arguments for ``search_recipes``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    mode: Literal["fulltext", "vector"] | None = None
    limit: int = Field(default=5, ge=1, le=50)


class SearchRecipesArgsFulltext(BaseModel):
    """``search_recipes`` arguments when vector mode is not configured.

    Same fields, but the mode enum carries only ``"fulltext"`` so the
    offered schema never invites vector (Phase 7 live fix: the vegan
    run asked for vector with embeddings off).
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    mode: Literal["fulltext"] | None = None
    limit: int = Field(default=5, ge=1, le=50)


def embeddings_configured(settings: Any) -> bool:
    """True when query embeddings are enabled in settings."""
    return bool(settings is not None and getattr(settings, "embeddings_enabled", False))


def search_modes(settings: Any) -> tuple[str, ...]:
    """Modes actually configured: vector only with embeddings enabled."""
    return ("fulltext", "vector") if embeddings_configured(settings) else ("fulltext",)


class GetRecipeArgs(BaseModel):
    """Arguments for ``get_recipe`` (exact pair, never fallback)."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)


def _settings_value(context: ToolContext, name: str, default: Any) -> Any:
    settings = getattr(context, "settings", None)
    if settings is not None and hasattr(settings, name):
        return getattr(settings, name)
    return default


def resolve_search_mode(requested: str | None, context: ToolContext) -> tuple[str, str]:
    """Return ``(mode_ran, source)``: explicit arg or ``RETRIEVAL_MODE``.

    Code default is ``fulltext``. Invalid setting values fail closed to
    ``fulltext`` (settings validation already refuses them at startup).
    """
    if requested in ("fulltext", "vector"):
        return requested, "explicit"
    configured = _settings_value(context, "retrieval_mode", "fulltext")
    if configured not in ("fulltext", "vector"):
        return "fulltext", "default"
    # The tool supports fulltext/vector only (Checkpoint 0: no hybrid mode).
    if configured == "vector":
        return "vector", "retrieval_mode"
    return "fulltext", "retrieval_mode" if requested is None else "default"


def resolve_vector_cutoff(context: ToolContext) -> float:
    """Cutoff for vector mode: setting override or pinned 0.66."""
    configured = _settings_value(context, "retrieval_vector_cutoff", None)
    if configured is None:
        return TOOL_VECTOR_CUTOFF
    try:
        value = float(configured)
    except (TypeError, ValueError):
        return TOOL_VECTOR_CUTOFF
    return value


def build_embed_provider(settings: Any) -> Any | None:
    """Return a live query-embedding provider, or None when disabled.

    Starts only when ``EMBEDDINGS_ENABLED`` is set. Runs the
    model/dimension check first (ADR 0001 step 2); mismatches fail with
    a typed unavailable instead of a network call.
    """
    if settings is None or not bool(getattr(settings, "embeddings_enabled", False)):
        return None
    model = str(getattr(settings, "embedding_model", "text-embedding-3-small"))
    dimension = int(getattr(settings, "embedding_dimension", 1536))
    check_model_dimension(model, dimension)
    from culinary_copilot.embeddings.provider import OpenAIEmbeddingProvider

    api_key = ""
    raw_key = getattr(settings, "openai_api_key", "")
    try:
        if hasattr(raw_key, "get_secret_value"):
            api_key = raw_key.get_secret_value()
        else:
            api_key = str(raw_key)
    except Exception:
        api_key = ""
    return OpenAIEmbeddingProvider(
        model=model,
        dimension=dimension,
        api_key=api_key,
        timeout_s=float(getattr(settings, "embed_timeout_s", 20.0)),
        max_retries=int(getattr(settings, "embed_max_retries", 1)),
    )


def _not_configured(message: str) -> dict[str, Any]:
    """Permanent configuration error (contact_operator; never retried)."""
    return {
        "ok": False,
        "error_type": "unavailable",
        "reason": REASON_TOOL_NOT_CONFIGURED,
        "message": message,
        "next_action": next_action_for(REASON_TOOL_NOT_CONFIGURED),
    }


def _unavailable(message: str) -> dict[str, Any]:
    """Transient failure (retry may succeed)."""
    return {
        "ok": False,
        "error_type": "unavailable",
        "reason": REASON_TOOL_UNAVAILABLE,
        "message": message,
        "next_action": next_action_for(REASON_TOOL_UNAVAILABLE),
    }


async def search_recipes_impl(args: SearchRecipesArgs, context: ToolContext) -> dict[str, Any]:
    """Run full-text or vector recipe search; never silently falls back."""
    from culinary_copilot.embeddings.query import embed_query
    from culinary_copilot.recipes.repository import search_all
    from culinary_copilot.recipes.vector_search import apply_vector_cutoff, vector_candidates

    engine = getattr(context, "engine", None)
    if engine is None:
        return _not_configured("search_recipes not configured: no recipe engine")
    mode_ran, _source = resolve_search_mode(args.mode, context)
    logger.info("search_recipes mode_ran=%s", mode_ran)
    if mode_ran == "fulltext":
        try:
            rows = await __import__("asyncio").to_thread(
                search_all, engine, args.query, limit=args.limit
            )
        except Exception as exc:
            return _unavailable(f"search_recipes unavailable: {type(exc).__name__}")
        return {"ok": True, "mode_ran": "fulltext", "cost_class": "free", "results": list(rows)}
    # Vector mode: embeddings required, fail closed without fallback.
    # Missing/disabled embeddings is permanent configuration, never a retry.
    settings = getattr(context, "settings", None)
    provider = getattr(context, "embed_provider", None)
    if (
        provider is None
        and settings is not None
        and bool(getattr(settings, "embeddings_enabled", False))
    ):
        try:
            provider = build_embed_provider(settings)
        except Exception as exc:
            return _not_configured(
                f"search_recipes not configured: embedding provider failed ({type(exc).__name__})"
            )
    from culinary_copilot.embeddings.provider import DisabledEmbeddingProvider

    if provider is None or isinstance(provider, DisabledEmbeddingProvider):
        # Phase 7 live fix: point back at full-text (retry with new
        # args, not a config change). No silent fallback.
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": REASON_TOOL_NOT_CONFIGURED,
            "message": (
                "search_recipes not configured: vector mode needs "
                'EMBEDDINGS_ENABLED with a provider; retry the search with mode "fulltext"'
            ),
            "next_action": NEXT_RETRY,
        }
    model = str(_settings_value(context, "embedding_model", "text-embedding-3-small"))
    dimension = int(_settings_value(context, "embedding_dimension", 1536))
    try:
        check_model_dimension(model, dimension)
    except Exception:
        return _not_configured("search_recipes not configured: embedding model/dimension mismatch")
    try:
        query_vector = await embed_query(
            provider,
            args.query,
            model=model,
            dimension=dimension,
            timeout_s=float(_settings_value(context, "embed_timeout_s", 20.0)),
            allow_fallback=False,
        )
    except Exception as exc:
        return _unavailable(f"search_recipes unavailable: {type(exc).__name__}")
    if query_vector is None:
        return _not_configured("search_recipes not configured: embeddings unavailable")
    try:
        candidates = await __import__("asyncio").to_thread(
            vector_candidates,
            engine,
            query_vector,
            limit=args.limit,
            model=model,
            dimension=dimension,
        )
    except Exception as exc:
        return _unavailable(f"search_recipes unavailable: {type(exc).__name__}")
    kept = apply_vector_cutoff(candidates, resolve_vector_cutoff(context))
    return {"ok": True, "mode_ran": "vector", "cost_class": "paid", "results": list(kept)}


async def get_recipe_impl(args: GetRecipeArgs, context: ToolContext) -> dict[str, Any]:
    """Fetch one canonical recipe document by exact pair."""
    from culinary_copilot.recipes.repository import SUPPORTED_DATASETS, get_recipe

    if args.dataset_id not in SUPPORTED_DATASETS:
        from culinary_copilot.domain.recommendations import (
            REASON_TOOL_INVALID_ARGUMENTS,
        )
        from culinary_copilot.domain.recommendations import (
            next_action_for as _naf,
        )

        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": REASON_TOOL_INVALID_ARGUMENTS,
            "message": f"get_recipe: unsupported dataset_id {args.dataset_id!r}",
            "next_action": _naf(REASON_TOOL_INVALID_ARGUMENTS),
        }
    engine = getattr(context, "engine", None)
    if engine is None:
        return _not_configured("get_recipe not configured: no recipe engine")
    try:
        doc = await __import__("asyncio").to_thread(
            get_recipe, engine, args.source_id, dataset_id=args.dataset_id
        )
    except ValueError as exc:
        from culinary_copilot.domain.recommendations import (
            REASON_TOOL_INVALID_ARGUMENTS,
        )
        from culinary_copilot.domain.recommendations import (
            next_action_for as _naf,
        )

        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": REASON_TOOL_INVALID_ARGUMENTS,
            "message": f"get_recipe invalid arguments: {exc}",
            "next_action": _naf(REASON_TOOL_INVALID_ARGUMENTS),
        }
    except Exception as exc:
        return _unavailable(f"get_recipe unavailable: {type(exc).__name__}")
    if doc is None:
        from culinary_copilot.domain.recommendations import (
            REASON_TOOL_INVALID_ARGUMENTS,
        )
        from culinary_copilot.domain.recommendations import (
            next_action_for as _naf,
        )

        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": REASON_TOOL_INVALID_ARGUMENTS,
            "message": "get_recipe: recipe not found for (dataset_id, source_id)",
            "next_action": _naf(REASON_TOOL_INVALID_ARGUMENTS),
        }
    return {"ok": True, "recipe": dict(doc)}


def search_recipes_description(settings: Any = None) -> str:
    """Description built from settings: vector named only when configured.

    No settings means unknown configuration: keep the full description
    (the offer path always passes real settings).
    """
    if settings is None or "vector" in search_modes(settings):
        return (
            "Full-text or vector (cutoff 0.66) recipe search; mode omitted follows RETRIEVAL_MODE."
        )
    return (
        'Full-text recipe search (only mode "fulltext" is configured; '
        "vector needs EMBEDDINGS_ENABLED); "
        "mode omitted runs full-text."
    )


def with_configured_search_modes(
    definitions: list[ToolDefinition], settings: Any
) -> list[ToolDefinition]:
    """Swap search definitions for settings-built ones (offer path only).

    The mode enum and description are built from settings, so vector
    is absent when embeddings are off. Default definitions (no
    settings) keep the full schema, so direct run_tool callers still
    reach the implementation's typed error.
    """
    from dataclasses import replace

    if settings is None or "vector" in search_modes(settings):
        return list(definitions)
    out: list[ToolDefinition] = []
    for definition in definitions:
        if definition.name == "search_recipes":
            out.append(
                replace(
                    definition,
                    description=search_recipes_description(settings),
                    args_model=SearchRecipesArgsFulltext,
                )
            )
        else:
            out.append(definition)
    return out


def tool_definitions(timeout_s: float = 10.0) -> list[ToolDefinition]:
    """Search-tool definitions (timeout server-set, never caller-set)."""
    return [
        ToolDefinition(
            name="search_recipes",
            description=search_recipes_description(None),
            args_model=SearchRecipesArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="paid",
        ),
        ToolDefinition(
            name="get_recipe",
            description="Exact (dataset_id, source_id) recipe document fetch.",
            args_model=GetRecipeArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
    ]


__all__ = [
    "TOOL_VECTOR_CUTOFF",
    "GetRecipeArgs",
    "SearchRecipesArgs",
    "SearchRecipesArgsFulltext",
    "build_embed_provider",
    "embeddings_configured",
    "get_recipe_impl",
    "resolve_search_mode",
    "resolve_vector_cutoff",
    "search_modes",
    "search_recipes_description",
    "search_recipes_impl",
    "tool_definitions",
    "with_configured_search_modes",
]
