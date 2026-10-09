"""Typed tool layer (Milestone 3, Phase 2).

Registry + implementations. Tool outputs are data, never instructions.
See docs/tools.md for the tool table and docs/adr/0001-retrieval-default.md
for the retrieval wiring status.
"""

from __future__ import annotations

from typing import Any, Callable

from culinary_copilot.tools import (
    epicure_tools,
    measure_tools,
    search_tools,
    stub_tools,
    technique_tools,
)
from culinary_copilot.tools.registry import ToolContext, ToolDefinition, run_tool

ToolImpl = Callable[..., Any]


def all_tool_definitions(timeout_s: float = 10.0) -> list[ToolDefinition]:
    """Every Phase 2 tool definition (server-set timeout)."""
    return [
        *search_tools.tool_definitions(timeout_s),
        *epicure_tools.tool_definitions(timeout_s),
        *measure_tools.tool_definitions(timeout_s),
        *technique_tools.tool_definitions(timeout_s),
        *stub_tools.tool_definitions(timeout_s),
    ]


def all_tool_impls() -> dict[str, ToolImpl]:
    """Map tool name -> async implementation."""
    return {
        "search_recipes": search_tools.search_recipes_impl,
        "get_recipe": search_tools.get_recipe_impl,
        "find_balanced_pairings": epicure_tools.find_balanced_pairings_impl,
        "find_conventional_pairings": epicure_tools.find_conventional_pairings_impl,
        "find_flavor_pairings": epicure_tools.find_flavor_pairings_impl,
        "find_substitutions": epicure_tools.find_substitutions_impl,
        "scale_recipe": measure_tools.scale_recipe_impl,
        "convert_units": measure_tools.convert_units_impl,
        "search_techniques": technique_tools.search_techniques_impl,
        "search_web": stub_tools.search_web_impl,
    }


def build_tool_context(
    *,
    settings: Any | None = None,
    engine: Any | None = None,
    session_store: Any | None = None,
    embed_provider: Any | None = None,
    epicure_core: Any | None = None,
    epicure_cooc: Any | None = None,
    epicure_chem: Any | None = None,
    bound_session_id: str | None = None,
    search_provider: Any | None = None,
) -> ToolContext:
    """Assemble a ``ToolContext`` (fakes injected in tests)."""
    timeout = float(getattr(settings, "tool_timeout_s", 10.0)) if settings else 10.0
    _ = timeout
    if (
        embed_provider is None
        and settings is not None
        and bool(getattr(settings, "embeddings_enabled", False))
    ):
        try:
            embed_provider = search_tools.build_embed_provider(settings)
        except Exception:
            embed_provider = None
    if settings is not None and (
        epicure_core is None or epicure_cooc is None or epicure_chem is None
    ):
        try:
            variants = epicure_tools.build_epicure_variants(settings)
            epicure_core = epicure_core if epicure_core is not None else variants.get("core")
            epicure_cooc = epicure_cooc if epicure_cooc is not None else variants.get("cooc")
            epicure_chem = epicure_chem if epicure_chem is not None else variants.get("chem")
        except Exception:
            pass
    return ToolContext(
        settings=settings,
        engine=engine,
        session_store=session_store,
        embed_provider=embed_provider,
        epicure_core=epicure_core,
        epicure_cooc=epicure_cooc,
        epicure_chem=epicure_chem,
        bound_session_id=bound_session_id,
        search_provider=search_provider,
        # Bounded, minimized args in tool_call events only when full
        # trajectory recording is on (default stays digest-only).
        record_tool_args=bool(getattr(settings, "agent_record_trajectory", False)),
    )


__all__ = [
    "ToolContext",
    "ToolDefinition",
    "all_tool_definitions",
    "all_tool_impls",
    "build_tool_context",
    "run_tool",
]
