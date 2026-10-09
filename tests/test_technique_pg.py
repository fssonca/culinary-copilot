"""Phase 4 disposable-Postgres technique tests (no app DB writes).

Uses ``culinary_test_technique`` only. Loads a tiny synthetic corpus
(fixtures in tmp_path, never the git-ignored real corpus) and verifies
migration 006/007 behavior, loader idempotency, full-text search, and
the attribution rule (every hit carries attribution_text + licence_url).
Skipped when PostgreSQL is unreachable so offline runs stay green.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.recipes import import_data

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "techniques"))

import embed_techniques  # noqa: E402
import load  # noqa: E402

TEST_DB = "culinary_test_technique"


def _urls() -> tuple[str, str]:
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


@pytest.fixture(scope="module")
def engine():
    maint_url, test_url = _urls()
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
        maint.dispose()
        eng = create_engine(test_url)
        with eng.begin() as conn:
            import_data.apply_migrations(conn)
        yield eng
        eng.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for technique tests: {exc!r}")
    finally:
        try:
            maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            maint.dispose()
        except Exception:
            pass


def _mini_corpus(tmp: Path, prefix: str = "tech-test") -> tuple[Path, Path]:
    docs = {
        f"{prefix}-sear": (
            "Test Searing",
            "# Test Searing\n\n## Searing\n\nSear chicken in a hot pan for "
            "a brown crust. Rest before slicing.\n",
        ),
        f"{prefix}-chill": (
            "Test Chilling",
            "# Test Chilling\n\n## Chill\n\nChill leftovers within two hours "
            "in shallow containers.\n",
        ),
    }
    for doc_id, (title, body) in docs.items():
        (tmp / f"{doc_id}.txt").write_text(body, encoding="utf-8")
    manifest_docs: dict[str, Any] = {}
    for doc_id, (title, body) in docs.items():
        manifest_docs[doc_id] = {
            "doc_id": doc_id,
            "title": title,
            "final_title": title,
            "topic": "test",
            "url": f"https://example.invalid/{doc_id}",
            "publisher": "Test Publisher",
            "licence": "CC-BY-SA-4.0",
            "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
            "attribution_text": f'"{title}" — test attribution, CC BY-SA 4.0.',
            "retrieval_date_utc": "2026-09-28T00:00:00Z",
            "revision_id": "1",
            "sha256_raw": "0" * 64,
            "sha256_normalized": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "words": len(body.split()),
            "bytes": len(body.encode("utf-8")),
            "status": "ingested",
        }
    manifest = tmp / "manifest.json"
    manifest.write_text(
        json.dumps({"generated_utc": "2026-09-28T00:00:00Z", "docs": manifest_docs}),
        encoding="utf-8",
    )
    return tmp, manifest


def _load(tmp: Path, manifest: Path, test_url: str) -> int:
    return load.main(
        [
            "--database-url",
            test_url,
            "--expect-db-name",
            TEST_DB,
            "--expect-db-host",
            "localhost",
            "--corpus-dir",
            str(tmp),
            "--manifest",
            str(manifest),
        ]
    )


def test_006_applies_and_007_matches_extension(engine) -> None:
    with engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        }
        has_vector = (
            conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname='vector'")).scalar()
            or 0
        )
    assert "technique_documents" in tables
    assert "technique_chunks" in tables
    assert ("technique_embeddings" in tables) == bool(has_vector)


def test_loader_idempotent_and_fulltext_hits_carry_attribution(engine, tmp_path: Path) -> None:
    from culinary_copilot.recipes.technique_repository import search_techniques_fulltext

    _, test_url = _urls()
    corpus_dir, manifest = _mini_corpus(tmp_path)
    assert _load(corpus_dir, manifest, test_url) == 0
    assert _load(corpus_dir, manifest, test_url) == 0  # idempotent re-run
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM technique_chunks")).scalar()
        nulls = conn.execute(
            text("SELECT count(*) FROM technique_chunks WHERE search_vector IS NULL")
        ).scalar()
    assert count and count > 0
    assert nulls == 0
    hits, match = search_techniques_fulltext(engine, "brown crust chicken", limit=5)
    assert match == "all"
    assert hits, "expected a full-text hit for the searing fixture"
    assert hits[0]["doc_id"] == "tech-test-sear"
    for hit in hits:
        assert hit["attribution_text"], "excerpt without attribution_text"
        assert hit["licence_url"], "excerpt without licence link"
        assert len(hit["excerpt"]) <= 600 + 1


def test_fulltext_falls_back_to_any_term_match(engine, tmp_path: Path) -> None:
    from culinary_copilot.recipes.technique_repository import search_techniques_fulltext

    _, test_url = _urls()
    corpus_dir, manifest = _mini_corpus(tmp_path)
    assert _load(corpus_dir, manifest, test_url) == 0
    # 'quetzal' occurs nowhere: the all-terms match is empty, so the
    # same lexemes retry with OR semantics and still return the searing doc.
    hits, match = search_techniques_fulltext(
        engine, "how do I sear chicken in a hot quetzal pan", limit=5
    )
    assert match == "any"
    assert hits and hits[0]["doc_id"] == "tech-test-sear"


def test_poultry_species_terms_also_match_poultry(engine) -> None:
    # 2026-10-06 live session: "chicken safe internal temperature" missed
    # the FDA chunk that says "Poultry", so the plan's required
    # food-safety citation was never found.
    from culinary_copilot.recipes.technique_repository import _TSQUERY_ALL, _TSQUERY_ANY

    doc = "Poultry (ground, parts, whole) safe minimum internal temperature 165 F"
    with engine.connect() as conn:

        def matches(tsquery: str, query: str, body: str = doc) -> bool:
            sql = f"SELECT to_tsvector('english', :body) @@ {tsquery}"
            return bool(conn.execute(text(sql), {"body": body, "query": query}).scalar())

        assert matches(_TSQUERY_ALL, "chicken safe internal temperature")
        assert matches(_TSQUERY_ALL, "turkey internal temperature")
        assert matches(_TSQUERY_ALL, "goose safe temperature")
        # Only an alternative is added: other terms still must match.
        assert not matches(_TSQUERY_ALL, "chicken braising liquid")
        assert matches(_TSQUERY_ANY, "chicken braising liquid")
        # Lexemes that merely start with a species stem are untouched.
        assert not matches(_TSQUERY_ALL, "chickpea safe internal temperature")
        assert matches(_TSQUERY_ALL, "chicken thighs", "Sear the chicken thighs")


def test_tool_result_contract_on_pg(engine, tmp_path: Path) -> None:
    import asyncio

    from culinary_copilot.config import Settings
    from culinary_copilot.tools import all_tool_definitions, all_tool_impls, run_tool
    from culinary_copilot.tools.registry import ToolContext

    _, test_url = _urls()
    corpus_dir, manifest = _mini_corpus(tmp_path)
    assert _load(corpus_dir, manifest, test_url) == 0
    defs = {d.name: d for d in all_tool_definitions(timeout_s=10.0)}
    impls = all_tool_impls()

    class _Store:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def append_event(self, session_id: str, event_type: str, payload: object) -> object:
            self.events.append({"session_id": session_id, "type": event_type, "payload": payload})
            return self.events[-1]

    store = _Store()
    ctx = ToolContext(settings=Settings(_env_file=None), engine=engine, session_store=store)
    result = asyncio.run(
        run_tool(
            defs["search_techniques"],
            impls["search_techniques"],
            {"query": "chill"},
            ctx,
            session_id="ses-tech-1",
        )
    )
    assert result["ok"] is True
    assert result["mode_ran"] == "fulltext"
    assert result["match"] in ("all", "any")
    assert result["cost_class"] == "free"
    assert result["results"]
    for hit in result["results"]:
        assert hit["attribution_text"] and hit["licence_url"]
    tool_events = [e for e in store.events if e["type"] == "tool_call"]
    assert tool_events and tool_events[-1]["payload"]["match"] == result["match"]


def test_fake_embed_rehearsal_and_vector_candidates(engine, tmp_path: Path) -> None:

    from sqlalchemy import text

    from culinary_copilot.embeddings.provider import FakeEmbeddingProvider
    from culinary_copilot.recipes.technique_repository import technique_vector_candidates

    _, test_url = _urls()
    corpus_dir, manifest = _mini_corpus(tmp_path)
    assert _load(corpus_dir, manifest, test_url) == 0
    run_dir = tmp_path / "embed-run"
    rc = embed_techniques.main(
        [
            "--fake",
            "--database-url",
            test_url,
            "--run-dir",
            str(run_dir),
            "--ceiling-usd",
            "0.01",
        ]
    )
    assert rc == 0
    assert (run_dir / "ledger.json").is_file()
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM technique_embeddings")).scalar()
    assert n and n > 0
    provider = FakeEmbeddingProvider()
    import asyncio

    vectors = asyncio.run(provider.embed_texts(["sear chicken crust"])).vectors
    hits = technique_vector_candidates(engine, vectors[0], limit=5)
    assert hits, "expected vector candidates after the fake rehearsal"
    for hit in hits:
        assert hit["attribution_text"] and hit["licence_url"]


def test_vector_eval_reports_both_cutoffs_with_fake(engine, tmp_path: Path) -> None:
    import json as _json
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "techniques"))
    import eval_baseline

    _, test_url = _urls()
    corpus_dir, manifest = _mini_corpus(tmp_path)
    assert _load(corpus_dir, manifest, test_url) == 0
    run_dir = tmp_path / "embed-run-vec"
    assert (
        embed_techniques.main(["--fake", "--database-url", test_url, "--run-dir", str(run_dir)])
        == 0
    )
    out = tmp_path / "vector.json"
    rc = eval_baseline.main(
        [
            "--mode",
            "vector",
            "--fake",
            "--database-url",
            test_url,
            "--cases",
            str(
                Path(__file__).resolve().parents[1] / "evals" / "technique_retrieval" / "cases.json"
            ),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    report = _json.loads(out.read_text(encoding="utf-8"))
    assert report["mode"] == "vector"
    assert "UNCALIBRATED" in report["cutoff_note"]
    for key in ("cutoff_0_66", "no_cutoff"):
        assert "hit_rate_at_5" in report[key]
        assert "mrr" in report[key]
    assert report["rows"] and "relevant_distances" in report["rows"][0]


def test_embed_cli_records_reported_usage_not_estimate(
    engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    import culinary_copilot.embeddings.provider as _provider_module
    from culinary_copilot.embeddings.provider import EmbeddingUsage, FakeEmbeddingProvider

    class _SevenTokenFake(FakeEmbeddingProvider):
        async def embed_texts(self, texts: list[str]):  # type: ignore[no-untyped-def]
            result = await super().embed_texts(texts)
            result.usage = EmbeddingUsage(prompt_tokens=7, total_tokens=7)
            return result

    monkeypatch.setattr(_provider_module, "FakeEmbeddingProvider", _SevenTokenFake)
    _, test_url = _urls()
    corpus_dir, manifest = _mini_corpus(tmp_path, prefix="tech-test-usage")
    assert _load(corpus_dir, manifest, test_url) == 0
    run_dir = tmp_path / "embed-usage"
    assert (
        embed_techniques.main(["--fake", "--database-url", test_url, "--run-dir", str(run_dir)])
        == 0
    )
    ledger = _json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
    # Mini corpus embeds in a single request: reported usage is 7 tokens,
    # far below the byte estimate. used_tokens_total must carry the
    # reported figure; the estimate lives on as estimated_tokens_total.
    assert ledger["used_tokens_total"] == 7
    assert ledger["estimated_tokens_total"] > 7
    assert ledger["reserved_tokens"] == ledger["estimated_tokens_total"] * 2
    from sqlalchemy import text as _text

    with engine.connect() as conn:
        used = conn.execute(
            _text("SELECT used_tokens FROM embedding_runs WHERE run_id=:r"),
            {"r": "tech-embed-usage"},
        ).scalar_one()
    assert used == 7


def test_vector_eval_live_records_reported_query_usage(
    engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "techniques"))
    import eval_baseline

    import culinary_copilot.embeddings.provider as _provider_module
    from culinary_copilot.embeddings.provider import EmbeddingUsage, FakeEmbeddingProvider

    class _FiveTokenFake(FakeEmbeddingProvider):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

        async def embed_texts(self, texts: list[str]):  # type: ignore[no-untyped-def]
            result = await super().embed_texts(texts)
            result.usage = EmbeddingUsage(prompt_tokens=5, total_tokens=5)
            return result

    monkeypatch.setattr(_provider_module, "OpenAIEmbeddingProvider", _FiveTokenFake)
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_BUDGET_USD", "0.01")
    _, test_url = _urls()
    out = tmp_path / "live-eval.json"
    rc = eval_baseline.main(
        [
            "--mode",
            "vector",
            "--live",
            "--yes",
            "--ceiling-usd",
            "0.01",
            "--database-url",
            test_url,
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    report = _json.loads(out.read_text(encoding="utf-8"))
    assert report["query_tokens_used"] == 16 * 5
    assert report["query_tokens_reserved"] > report["query_tokens_used"]
    assert report["query_used_cost_usd"] >= 0.0
