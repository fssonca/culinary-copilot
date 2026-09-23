"""Versioned search-document renderer (Workstream 1A).

Bug fixed: ``*" ".join(keywords)`` inside a list expands the joined string
into individual characters. This renderer preserves complete keyword phrases.
"""

from typing import Any, Mapping

SEARCH_DOCUMENT_VERSION = "1"


def render_search_document(
    *,
    title: str | None,
    description: str | None,
    category: str | None,
    keywords: list[str] | tuple[str, ...] | None,
    ingredient_names: list[str] | tuple[str, ...] | None,
    instructions: list[str] | tuple[str, ...] | None,
) -> str:
    """Build a deterministic Postgres FTS document string.

    - Keyword phrases are preserved (joined, never starred).
    - Missing optional metadata contributes nothing (no "None" tokens).
    - No field is duplicated: callers pass each value once.
    """

    def clean(value: str | None) -> str:
        return " ".join(value.split()) if value else ""

    parts: list[str] = []
    for value in (clean(title), clean(description), clean(category)):
        if value:
            parts.append(value)
    if keywords:
        joined = " ".join(clean(k) for k in keywords if k and clean(k))
        if joined:
            parts.append(joined)
    if ingredient_names:
        joined = " ".join(clean(i) for i in ingredient_names if i and clean(i))
        if joined:
            parts.append(joined)
    if instructions:
        joined = " ".join(clean(s) for s in instructions if s and clean(s))
        if joined:
            parts.append(joined)
    return " ".join(parts)


def render_from_recipe(recipe: Mapping[str, Any]) -> str:
    """Render from a normalized recipe dict (adapter-agnostic)."""
    meta = recipe.get("meta") or {}
    ingredients = recipe.get("ingredients") or []
    names = [i.get("canonical") or i.get("name") or "" for i in ingredients]
    return render_search_document(
        title=recipe.get("title"),
        description=recipe.get("description"),
        category=meta.get("category"),
        keywords=meta.get("keywords"),
        ingredient_names=names,
        instructions=recipe.get("instructions"),
    )
