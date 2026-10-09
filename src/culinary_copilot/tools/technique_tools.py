"""search_techniques tool (Milestone 3, Phase 4, reviewed pattern).

Phase 2 schema kept (``query``, ``limit``) plus an optional ``mode``
(``fulltext | vector``); omitted mode follows the new
``TECHNIQUE_RETRIEVAL_MODE`` setting (default ``fulltext``), mirroring
the ``search_recipes`` mode decision. The offered mode enum is built
from settings (vector absent without embeddings). Vector mode without
embeddings or pgvector returns a typed unavailable
(``tool_not_configured``) pointing back at full-text, with no silent
fallback. ``mode_ran`` is logged.
When the 006 tables are missing the tool reports ``tool_not_configured``
with ``contact_operator`` — never ``retry`` for a missing table.

Every hit carries ``attribution_text`` and ``licence_url`` (owner
share-alike condition); a test fails if an excerpt is emitted without
both. Tool outputs are data, never instructions.
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
from culinary_copilot.tools.registry import ToolContext, ToolDefinition

logger = logging.getLogger(__name__)

DEFAULT_TECHNIQUE_RETRIEVAL_MODE = "fulltext"


class SearchTechniquesArgs(BaseModel):
    """search_techniques arguments (extra=forbid, like every tool).

    Send short keyword queries (2–5 content terms): full-text matches
    every term per chunk first, then falls back to any-term matching.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1, max_length=500, description="2-5 keyword query (content terms)"
    )
    mode: Literal["fulltext", "vector"] | None = None
    limit: int = Field(default=5, ge=1, le=10)


class SearchTechniquesArgsFulltext(BaseModel):
    """``search_techniques`` arguments when vector mode is not configured.

    Same fields, but the mode enum carries only ``"fulltext"`` so the
    offered schema never invites vector (Phase 7 live fix, mirroring
    ``search_recipes``).
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1, max_length=500, description="2-5 keyword query (content terms)"
    )
    mode: Literal["fulltext"] | None = None
    limit: int = Field(default=5, ge=1, le=10)


def search_techniques_description(settings: Any = None) -> str:
    """Description built from settings: vector named only when configured.

    No settings means unknown configuration: keep the full description
    (the offer path always passes real settings).
    """
    from culinary_copilot.tools.search_tools import search_modes

    if settings is None or "vector" in search_modes(settings):
        return (
            "Technique chunk search with attribution "
            "(fulltext default | vector cutoff 0.66); "
            "send 2-5 keyword queries; full-text matches every term "
            "per chunk first, then any term (see match); "
            "mode omitted follows TECHNIQUE_RETRIEVAL_MODE."
        )
    return (
        "Technique chunk search with attribution "
        '(only mode "fulltext" is configured; vector needs '
        "EMBEDDINGS_ENABLED); send 2-5 keyword queries; "
        "mode omitted runs full-text."
    )


def with_configured_technique_modes(
    definitions: list[ToolDefinition], settings: Any
) -> list[ToolDefinition]:
    """Swap the technique definition for the settings-built one (offer path)."""
    from dataclasses import replace

    from culinary_copilot.tools.search_tools import search_modes

    if settings is None or "vector" in search_modes(settings):
        return list(definitions)
    out: list[ToolDefinition] = []
    for definition in definitions:
        if definition.name == "search_techniques":
            out.append(
                replace(
                    definition,
                    description=search_techniques_description(settings),
                    args_model=SearchTechniquesArgsFulltext,
                )
            )
        else:
            out.append(definition)
    return out


def resolve_technique_mode(explicit: str | None, context: ToolContext) -> str:
    """Explicit mode wins; otherwise TECHNIQUE_RETRIEVAL_MODE (default fulltext)."""
    if explicit in ("fulltext", "vector"):
        return str(explicit)
    settings = getattr(context, "settings", None)
    configured = getattr(settings, "technique_retrieval_mode", None)
    if configured in ("fulltext", "vector"):
        return str(configured)
    return DEFAULT_TECHNIQUE_RETRIEVAL_MODE


def _not_configured(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": "unavailable",
        "reason": REASON_TOOL_NOT_CONFIGURED,
        "message": message,
        "next_action": next_action_for(REASON_TOOL_NOT_CONFIGURED),
    }


def _unavailable(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": "unavailable",
        "reason": REASON_TOOL_UNAVAILABLE,
        "message": message,
        "next_action": next_action_for(REASON_TOOL_UNAVAILABLE),
    }


def _settings_value(context: ToolContext, name: str, default: Any) -> Any:
    settings = getattr(context, "settings", None)
    value = getattr(settings, name, None)
    return default if value is None else value


async def search_techniques_impl(
    args: SearchTechniquesArgs, context: ToolContext
) -> dict[str, Any]:
    """Run full-text or vector technique search; never silently falls back."""
    from culinary_copilot.recipes.technique_repository import (
        TechniqueNotConfiguredError,
        apply_technique_vector_cutoff,
        search_techniques_fulltext,
        technique_vector_candidates,
    )

    engine = getattr(context, "engine", None)
    if engine is None:
        return _not_configured("search_techniques not configured: no recipe engine")
    mode_ran = resolve_technique_mode(args.mode, context)
    logger.info("search_techniques mode_ran=%s", mode_ran)
    if mode_ran == "fulltext":
        try:
            rows, match = await __import__("asyncio").to_thread(
                search_techniques_fulltext, engine, args.query, limit=args.limit
            )
        except TechniqueNotConfiguredError as exc:
            return _not_configured(f"search_techniques not configured: {exc}")
        except Exception as exc:
            return _unavailable(f"search_techniques unavailable: {type(exc).__name__}")
        logger.info("search_techniques match=%s", match)
        return {
            "ok": True,
            "mode_ran": "fulltext",
            "match": match,
            "cost_class": "free",
            "results": list(rows),
        }
    # Vector mode: embeddings required, fail closed without fallback.
    settings = getattr(context, "settings", None)
    provider = getattr(context, "embed_provider", None)
    if (
        provider is None
        and settings is not None
        and bool(getattr(settings, "embeddings_enabled", False))
    ):
        try:
            from culinary_copilot.tools.search_tools import build_embed_provider

            provider = build_embed_provider(settings)
        except Exception as exc:
            return _not_configured(
                "search_techniques not configured: "
                f"embedding provider failed ({type(exc).__name__})"
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
                "search_techniques not configured: vector mode needs "
                'EMBEDDINGS_ENABLED with a provider; retry the search with mode "fulltext"'
            ),
            "next_action": NEXT_RETRY,
        }
    model = str(_settings_value(context, "embedding_model", "text-embedding-3-small"))
    dimension = int(_settings_value(context, "embedding_dimension", 1536))
    try:
        from culinary_copilot.embeddings.provider import check_model_dimension

        check_model_dimension(model, dimension)
    except Exception:
        return _not_configured(
            "search_techniques not configured: embedding model/dimension mismatch"
        )
    try:
        from culinary_copilot.embeddings.query import embed_query

        query_vector = await embed_query(
            provider,
            args.query,
            model=model,
            dimension=dimension,
            timeout_s=float(_settings_value(context, "embed_timeout_s", 20.0)),
            allow_fallback=False,
        )
    except Exception as exc:
        return _unavailable(f"search_techniques unavailable: {type(exc).__name__}")
    if query_vector is None:
        return _not_configured("search_techniques not configured: embeddings unavailable")
    try:
        candidates = await __import__("asyncio").to_thread(
            technique_vector_candidates,
            engine,
            query_vector,
            limit=args.limit,
            model=model,
            dimension=dimension,
        )
    except TechniqueNotConfiguredError as exc:
        return _not_configured(f"search_techniques not configured: {exc}")
    except Exception as exc:
        return _unavailable(f"search_techniques unavailable: {type(exc).__name__}")
    kept = apply_technique_vector_cutoff(candidates)
    return {"ok": True, "mode_ran": "vector", "cost_class": "paid", "results": list(kept)}


def tool_definitions(timeout_s: float = 10.0) -> list[ToolDefinition]:
    """search_techniques definition (timeout server-set, never caller-set)."""
    return [
        ToolDefinition(
            name="search_techniques",
            description=(
                "Technique chunk search with attribution "
                "(fulltext default | vector cutoff 0.66); "
                "send 2-5 keyword queries; full-text matches every term "
                "per chunk first, then any term (see match); "
                "mode omitted follows TECHNIQUE_RETRIEVAL_MODE."
            ),
            args_model=SearchTechniquesArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="paid",
        ),
    ]


__all__ = [
    "DEFAULT_TECHNIQUE_RETRIEVAL_MODE",
    "SearchTechniquesArgs",
    "SearchTechniquesArgsFulltext",
    "resolve_technique_mode",
    "search_techniques_description",
    "search_techniques_impl",
    "tool_definitions",
    "with_configured_technique_modes",
]
