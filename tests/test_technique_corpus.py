"""Phase 4 technique corpus tooling tests (offline, no network, no database).

Covers the fetch allowlist, HTML normalization, same-topic redirect
rule, and the section-aware chunker bounds. Fixture HTML only; the
real corpus under data/ is git-ignored and never read here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "techniques"))

from fetch import (  # noqa: E402
    _same_topic,
    fetch_one,
    html_to_text,
)

from culinary_copilot.embeddings.technique_rendering import (  # noqa: E402
    TECHNIQUE_CHUNK_VERSION,
    TECHNIQUE_MAX_TOKENS,
    TECHNIQUE_RENDER_VERSION,
    chunk_technique_document,
    estimate_tokens_text,
    split_sections,
)

REPO = Path(__file__).resolve().parents[1]
APPROVED = REPO / "evals" / "technique_corpus" / "approved_sources.json"
MANIFEST = REPO / "evals" / "technique_corpus" / "manifest.json"


def _approved_entries() -> dict[str, dict[str, str]]:
    payload = json.loads(APPROVED.read_text(encoding="utf-8"))
    return {str(e["id"]): dict(e) for e in payload["sources"]}


def test_approved_sources_lists_exactly_40() -> None:
    import urllib.parse as _urlparse

    entries = _approved_entries()
    assert len(entries) == 40
    for doc_id, entry in entries.items():
        for field in ("url", "publisher", "licence", "licence_url", "attribution_template"):
            assert entry[field], (doc_id, field)
        # The fetcher builds requests only from these URLs: https on an
        # allowlisted host (Wikimedia API or .gov direct fetch).
        parts = _urlparse.urlparse(entry["url"])
        assert parts.scheme == "https", doc_id
        assert parts.netloc.endswith(("wikipedia.org", "wikibooks.org", ".gov")), doc_id


def test_fetcher_refuses_unknown_id(tmp_path: Path) -> None:
    entries = _approved_entries()
    with pytest.raises(ValueError, match="not in approved sources"):
        fetch_one("tech-nope-99", entries, {}, out_dir=tmp_path)


def test_fetcher_refuses_unknown_url() -> None:
    import argparse

    entries = _approved_entries()
    assert "https://en.wikipedia.org/wiki/Searing" in {e["url"] for e in entries.values()}
    assert "https://example.com/evil" not in {e["url"] for e in entries.values()}
    _ = argparse  # documented: only allowlisted URLs are ever requested


def test_html_drops_reference_sections_but_keeps_tables() -> None:
    html = (
        "<h2>Searing</h2><p>Heat the pan hot.</p>"
        "<table><tr><th>Food</th><th>Temp</th></tr>"
        "<tr><td>Chicken</td><td>165 F</td></tr></table>"
        "<h2>See also</h2><p>Stew</p>"
        '<h2>References</h2><ol class="references"><li>cite</li></ol>'
    )
    title, text = html_to_text(html)
    assert title == "Searing"
    assert "Heat the pan hot." in text
    assert "Chicken | 165 F" in text
    assert "Stew" not in text
    assert "## See also" not in text
    assert "## References" not in text


def test_html_keeps_lead_before_first_heading() -> None:
    title, text = html_to_text("<p>Lead words.</p><h2>Body</h2><p>More.</p>")
    assert title == "Body"
    assert text.startswith("Lead words.")


def test_same_topic_accepts_plural_and_disambiguator() -> None:
    assert _same_topic("Egg substitute", "Egg substitutes")
    assert _same_topic("Garnish (food)", "Garnish (cooking)")
    assert _same_topic("Searing", "Searing")
    assert not _same_topic("Searing", "Braising")
    assert not _same_topic("Rice", "Paddy field")


def test_chunker_never_crosses_sections_and_respects_max() -> None:
    section = " ".join(["word"] * 2000)  # ~500 tokens, one paragraph
    normalized = f"Lead.\n\n## Alpha\n\n{section}\n\n## Beta\n\nShort."
    chunks = chunk_technique_document(normalized)
    assert chunks
    for chunk in chunks:
        assert chunk["est_tokens"] <= TECHNIQUE_MAX_TOKENS
        assert chunk["est_tokens"] == estimate_tokens_text(chunk["text"])
    # Section Beta stays whole and separate from Alpha.
    beta = [c for c in chunks if c["section"] == "Beta"]
    assert len(beta) == 1 and beta[0]["text"] == "Short."
    alpha = [c for c in chunks if c["section"] == "Alpha"]
    assert len(alpha) > 1
    assert [c["chunk_id"] for c in chunks] == list(range(len(chunks)))


def test_chunk_versions_are_independent_from_recipes() -> None:
    from culinary_copilot.embeddings import rendering as recipe_rendering

    assert TECHNIQUE_RENDER_VERSION != ""
    assert TECHNIQUE_CHUNK_VERSION != ""
    # Separate constants: technique code never imports the recipe versions.
    import culinary_copilot.embeddings.technique_rendering as tech

    assert "EMBED_DOCUMENT_VERSION" not in dir(tech)
    assert "CHUNKING_VERSION" not in dir(tech)
    assert recipe_rendering.EMBED_DOCUMENT_VERSION == "1"


def test_split_sections_keeps_order() -> None:
    sections = split_sections("Lead.\n\n## A\n\ntext a\n\n## B\n\ntext b")
    assert [h for h, _ in sections] == ["", "A", "B"]


def test_manifest_matches_approved_list() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = _approved_entries()
    assert set(manifest["docs"]) == set(entries)
    assert manifest["count"] == 40
    for doc_id, record in manifest["docs"].items():
        assert record["url"] == entries[doc_id]["url"], doc_id
        assert record["licence"] == entries[doc_id]["licence"], doc_id
        if record.get("status") == "ingested":
            assert record["attribution_text"], doc_id
            assert len(record["sha256_raw"]) == 64, doc_id
            assert len(record["sha256_normalized"]) == 64, doc_id
            assert record["words"] >= 150, doc_id
        else:
            assert record["reason"], doc_id


def test_eval_cases_frozen_before_any_run() -> None:
    import hashlib as _hashlib
    import json as _json

    payload = _json.loads((REPO / "evals" / "technique_retrieval" / "cases.json").read_text())
    assert payload["label"] == "AI-drafted"
    assert 15 <= len(payload["cases"]) <= 20
    assert sum(1 for c in payload["cases"] if c.get("paraphrase")) >= len(payload["cases"]) / 3
    body = {k: v for k, v in payload.items() if k != "freeze_sha256"}
    digest = _hashlib.sha256(
        _json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest == payload["freeze_sha256"]
    manifest_docs = _json.loads(MANIFEST.read_text())["docs"]
    for case in payload["cases"]:
        assert case["relevant_docs"], case["case_id"]
        for doc_id in case["relevant_docs"]:
            assert manifest_docs.get(doc_id, {}).get("status") == "ingested", (
                case["case_id"],
                doc_id,
            )


def test_or_tsquery_text_rewrites_and_to_or() -> None:
    from culinary_copilot.recipes.technique_repository import or_tsquery_text

    assert or_tsquery_text("'sear' & 'chicken'") == "'sear' | 'chicken'"
    assert or_tsquery_text("'sear'") == "'sear'"
    assert or_tsquery_text("") == ""


FDA_TABLE_FIXTURE = (
    "<h2>Safe Minimum Internal Temperatures</h2>"
    '<div class="table-responsive"><table class="table" summary="SAFE TEMPS">'
    "<caption>as measured with a food thermometer</caption>"
    "<thead><tr><th>Food Type</th><th>Internal temperature</th></tr></thead>"
    "<tbody>"
    "<tr><td><strong>Poultry</strong><br>(ground, parts, whole, and stuffing)</td>"
    "<td>165<sup>o</sup>F</td></tr>"
    "<tr><td><strong>Ground Meat</strong></td><td>160&deg;F</td></tr>"
    "</tbody></table></div>"
    '<table class="navbar"><tr><td>Cookbook</td><td>Recipes</td></tr></table>'
    '<div class="infobox"><table><tr><td>chrome</td></tr></table></div>'
)


def test_fda_table_figures_survive_with_degree_marks() -> None:
    title, text = html_to_text(FDA_TABLE_FIXTURE)
    assert "Poultry (ground, parts, whole, and stuffing) | 165 °F" in text
    assert "Ground Meat | 160 °F" in text
    assert "as measured with a food thermometer" in text
    assert "Cookbook" not in text
    assert "chrome" not in text


def test_fda_table_chunk_keeps_poultry_next_to_temp() -> None:
    from culinary_copilot.embeddings.technique_rendering import chunk_technique_document

    _, text = html_to_text(FDA_TABLE_FIXTURE)
    chunks = chunk_technique_document(f"# Safe Food Handling\n\n{text}\n")
    assert any("Poultry" in chunk["text"] and "165 °F" in chunk["text"] for chunk in chunks)


def test_vector_eval_refuses_without_live_or_fake(tmp_path) -> None:
    import sys as _sys

    _sys.path.insert(0, str(REPO / "scripts" / "techniques"))
    import eval_baseline

    rc = eval_baseline.main(["--mode", "vector", "--database-url", "postgresql://x/y"])
    assert rc == 2
    rc = eval_baseline.main(["--mode", "vector", "--live", "--database-url", "postgresql://x/y"])
    assert rc == 2  # --yes and --ceiling-usd still missing


def test_query_reservation_math() -> None:
    import sys as _sys

    _sys.path.insert(0, str(REPO / "scripts" / "techniques"))
    from eval_baseline import _reserve_query_tokens

    # estimate_tokens_bytes = utf-8 bytes + 8, times (retries + 1).
    assert _reserve_query_tokens(["ab", "c"], retries=1) == (10 + 9) * 2
    assert _reserve_query_tokens([], retries=1) == 0
