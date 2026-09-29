"""Section-aware technique chunking (Milestone 3, Phase 4).

Separate from recipe ``embeddings/rendering.py`` on purpose: technique
documents are expository multi-section texts (heat steps, safety tables)
while recipes render as one truncated blob. Re-chunking techniques must
never invalidate recipe vectors and vice versa, so this module carries
its own ``TECHNIQUE_RENDER_VERSION`` / ``TECHNIQUE_CHUNK_VERSION``.
Reuse identity for one technique chunk::

    doc_id + model + dimension
    + technique renderer version + technique chunking version
    + sha256 of the exact embedded (chunk) text

Token rule: the repo has no tokenizer; sizes use the documented chars/4
fallback (``agent/loop.py::estimate_tokens``). Target ~400 tokens, hard
maximum 800 tokens per chunk (owner decision §7). A chunk never crosses
a section boundary: short sections stay whole, over-long sections split
on paragraph (then sentence) boundaries.

Version history: "1" single-paragraph packing without table support;
"2" keeps data-table rows (normalizer v2) as chunk text.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

TECHNIQUE_RENDER_VERSION = "2"
TECHNIQUE_CHUNK_VERSION = "1"

#: Chunk sizing in fallback tokens (chars/4). Owner-approved §7.
TECHNIQUE_TARGET_TOKENS = 400
TECHNIQUE_MAX_TOKENS = 800

#: Normalized text marks sections as ``## heading`` lines (see
#: ``scripts/techniques/fetch.py``). Text before the first heading belongs
#: to the implicit ``""`` (lead) section.
_HEADING_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def estimate_tokens_text(text: str) -> int:
    """Fallback token estimate (chars/4, minimum 1)."""
    return max(1, len(text) // 4)


def embedded_text_hash(text: str) -> str:
    """SHA-256 of the exact embedded chunk text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_sections(normalized: str) -> list[tuple[str, str]]:
    """Split normalized text into ``(section, body)`` pairs in order."""
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in (normalized or "").splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = match.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)
    sections.append((current_heading, "\n".join(current_lines).strip()))
    return [(heading, body) for heading, body in sections if body]


def _split_paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _split_sentences(paragraph: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", paragraph.strip())
    return [p.strip() for p in parts if p.strip()]


def chunk_section(section: str, body: str) -> list[dict[str, Any]]:
    """Pack one section into 1+ chunks within the token budget."""
    paragraphs = _split_paragraphs(body)
    if not paragraphs:
        return []
    whole = "\n\n".join(paragraphs)
    if estimate_tokens_text(whole) <= TECHNIQUE_MAX_TOKENS:
        return [{"section": section, "text": whole}]
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    current_tokens = 0
    # Join separators ("\n\n") cost tokens too; charge one per join so the
    # joined chunk text never exceeds the maximum.
    separator_tokens = estimate_tokens_text("\n\n")

    def _flush() -> None:
        if current:
            chunks.append({"section": section, "text": "\n\n".join(current)})

    def _fits(extra_tokens: int) -> bool:
        join = separator_tokens if current else 0
        return current_tokens + join + extra_tokens <= TECHNIQUE_MAX_TOKENS

    for paragraph in paragraphs:
        units = [paragraph]
        if estimate_tokens_text(paragraph) > TECHNIQUE_MAX_TOKENS:
            units = _split_sentences(paragraph) or [paragraph]
        for unit in units:
            unit_tokens = estimate_tokens_text(unit)
            if unit_tokens > TECHNIQUE_MAX_TOKENS:
                # A single pathological unit: hard-cut on characters so the
                # maximum is never exceeded (still within one section).
                budget_chars = TECHNIQUE_MAX_TOKENS * 4
                for start in range(0, len(unit), budget_chars):
                    piece = unit[start : start + budget_chars].strip()
                    if not piece:
                        continue
                    if current and not _fits(estimate_tokens_text(piece)):
                        _flush()
                        current.clear()
                        current_tokens = 0
                    if current:
                        current_tokens += separator_tokens
                    current.append(piece)
                    current_tokens += estimate_tokens_text(piece)
                continue
            if current and not _fits(unit_tokens):
                _flush()
                current.clear()
                current_tokens = 0
            if current:
                current_tokens += separator_tokens
            current.append(unit)
            current_tokens += unit_tokens
    _flush()
    return chunks


def chunk_technique_document(normalized: str) -> list[dict[str, Any]]:
    """Chunk a whole normalized document; assigns ``chunk_id`` in order."""
    out: list[dict[str, Any]] = []
    for section, body in split_sections(normalized):
        for piece in chunk_section(section, body):
            out.append(
                {
                    "chunk_id": len(out),
                    "section": piece["section"],
                    "text": piece["text"],
                    "text_hash": embedded_text_hash(piece["text"]),
                    "est_tokens": estimate_tokens_text(piece["text"]),
                }
            )
    return out


def technique_embedding_identity(
    *, doc_id: str, model: str, dimension: int, chunk_id: int, embedded_text: str
) -> dict[str, Any]:
    """Full reusable identity for one technique chunk embedding."""
    return {
        "doc_id": doc_id,
        "model": model,
        "dimension": dimension,
        "renderer_version": TECHNIQUE_RENDER_VERSION,
        "chunking_version": TECHNIQUE_CHUNK_VERSION,
        "chunk_index": chunk_id,
        "embedded_text_hash": embedded_text_hash(embedded_text),
    }


__all__ = [
    "TECHNIQUE_CHUNK_VERSION",
    "TECHNIQUE_MAX_TOKENS",
    "TECHNIQUE_RENDER_VERSION",
    "TECHNIQUE_TARGET_TOKENS",
    "chunk_section",
    "chunk_technique_document",
    "embedded_text_hash",
    "estimate_tokens_text",
    "split_sections",
    "technique_embedding_identity",
]
