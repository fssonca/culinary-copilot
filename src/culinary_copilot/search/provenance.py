"""Web provenance model (Phase 5, part 2, owner decisions 6–7).

Three provenance classes stay separate in the data model:

- provider citation metadata: url_citation (url/title/indices) and
  action.sources (type/url) — copied verbatim, never edited;
- model-generated text: summary and model "excerpt" — labelled as
  model text, never presented or checked as a verified quotation;
- source text actually obtained: provider-retrieved page text. Per the
  part-1 correction (§1, item 1), the docs document
  ``include="web_search_call.results"`` ONLY for image results
  (image_url/source_website_url/thumbnail_url/caption); no provider
  field carries text page content for text searches. This integration
  therefore records NO source text actually obtained: ``source_text``
  is always None, and numeric claim checks can only pass against
  provider citation metadata (which carries no numbers) — so they fail
  closed to a cited discovery answer (see agent/validate.py).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SourceLabel = Literal[
    "official_guidance", "research_publication", "culinary_source", "unclassified"
]


class WebSource(BaseModel):
    """One validated web source (server-shaped, not model-shaped)."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=500)
    title: str = Field(default="", max_length=300)
    # Model-generated "excerpt": NEVER a verified quotation.
    excerpt_model: str = Field(default="", max_length=500)
    # Provider citation metadata (verbatim copies when present).
    citation_title: str = Field(default="", max_length=300)
    published_at: str | None = Field(default=None, max_length=100)
    # Server-set retrieval timestamp (never model-set).
    retrieved_at: str = Field(min_length=1, max_length=100)
    classification: SourceLabel = "unclassified"
    # Source text actually obtained: always None in this integration
    # (no provider field carries text page content; see module docstring).
    source_text: str | None = None


class WebRef(BaseModel):
    """One clickable web reference for the client final."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=300)


class WebAnswer(BaseModel):
    """Cited discovery answer: points at pages, claims no verified facts."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1200)
    web_refs: list[WebRef] = Field(min_length=1, max_length=5)


def web_source_from_provider(
    *,
    url: str,
    title: str = "",
    excerpt_model: str = "",
    citation_title: str = "",
    published_at: str | None = None,
    retrieved_at: str = "",
    classification: str = "unclassified",
) -> WebSource:
    """Build a server-shaped source (retrieved_at set by the server)."""
    label: SourceLabel = "unclassified"
    if classification in ("official_guidance", "research_publication", "culinary_source"):
        label = classification  # type: ignore[assignment]
    return WebSource(
        url=url,
        title=title[:300],
        excerpt_model=excerpt_model[:500],
        citation_title=citation_title[:300],
        published_at=(published_at[:100] if published_at else None),
        retrieved_at=retrieved_at,
        classification=label,
        source_text=None,
    )


def web_answer_to_final(answer: WebAnswer, *, evidence_class: str = "external") -> dict[str, Any]:
    """Client-final mapping: text + clickable refs + external label."""
    return {
        "type": "web_answer",
        "text": answer.text,
        "web_refs": [ref.model_dump() for ref in answer.web_refs],
        "evidence_class": evidence_class,
        "note_source": "model",
    }


__all__ = ["WebAnswer", "WebRef", "WebSource", "web_answer_to_final", "web_source_from_provider"]
