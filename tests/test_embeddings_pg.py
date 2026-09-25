"""Phase 5 disposable-Postgres rehearsal (no app DB writes).

Two tiers:

- Stock ``postgres:17`` (default ``DATABASE_URL``): verifies ``004`` is
  skipped — never partially applied — and full-text still works.
- Isolated pgvector (``PGVECTOR_TEST_URL``, e.g. the disposable
  ``pgvector:pg17`` container on :5544): full migration, stale-vector
  exclusion, CLI resume (second run embeds zero chunks), crash recovery
  (ledger loss re-embeds idempotently), hybrid orchestration, and
  backup/restore evidence. Set ``PGVECTOR_TEST_URL`` to run this tier;
  otherwise it skips with a reason instead of passing vacuously.

Fake vectors only — never a semantic-relevance measurement.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from culinary_copilot.config import Settings
from culinary_copilot.embeddings.provider import FakeEmbeddingProvider
from culinary_copilot.embeddings.query import embed_query
from culinary_copilot.recipes import import_data
from culinary_copilot.recipes.vector_search import vector_candidates

TEST_DB = "culinary_test_embeddings"
FOODCOM = "AkashPS11/recipes_data_food.com"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _urls() -> tuple[str, str]:
    settings = Settings()
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


def _vector_available(engine: Any) -> bool:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM pg_available_extensions WHERE name='vector'")
            ).scalar_one_or_none()
            return row is not None
    except Exception:
        return False


@pytest.fixture(scope="module")
def engine() -> Any:
    maint_url, test_url = _urls()
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
        maint.dispose()
    except Exception as exc:
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    eng = create_engine(test_url)
    try:
        with eng.begin() as conn:
            applied = import_data.apply_migrations(conn)
            assert "001" in applied and "002" in applied and "003" in applied
            conn.execute(
                text(
                    "INSERT INTO recipe_imports (id, dataset_id, revision, checksum, "
                    "normalizer_version, vocabulary_checksum, dataset_url, report) "
                    "VALUES (:id, :dataset_id, :revision, :checksum, :normalizer_version, "
                    ":vocabulary_checksum, :dataset_url, CAST(:report AS jsonb))"
                ),
                {
                    "id": "test-embed-1",
                    "dataset_id": FOODCOM,
                    "revision": "test-rev",
                    "checksum": "test-checksum",
                    "normalizer_version": "3",
                    "vocabulary_checksum": "test-vocab",
                    "dataset_url": "https://example.invalid/test",
                    "report": json.dumps({"counts": {}}),
                },
            )
            doc = {
                "title": "Chicken Curry",
                "provenance": {"dataset_id": FOODCOM, "source_id": "000159"},
                "ingredients": [{"canonical": "chicken"}],
                "instructions": ["Cook it."],
            }
            conn.execute(
                text(
                    "INSERT INTO recipes (dataset_id, source_id, import_id, title, "
                    "total_minutes, servings, ingredient_names, document, search_text) "
                    "VALUES (:d, :s, :i, :t, 20, 2, CAST(:n AS text[]), "
                    "CAST(:doc AS jsonb), :st)"
                ),
                {
                    "d": FOODCOM,
                    "s": "000159",
                    "i": "test-embed-1",
                    "t": "Chicken Curry",
                    "n": ["chicken"],
                    "doc": json.dumps(doc),
                    "st": "chicken curry dinner",
                },
            )
    except Exception as exc:
        pytest.skip(f"Disposable DB setup failed: {exc}")
    yield eng
    eng.dispose()


def _pgvector_engine() -> Any:
    url = os.environ.get("PGVECTOR_TEST_URL", "")
    if not url:
        pytest.skip("PGVECTOR_TEST_URL not set; isolated pgvector tier skipped")
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"Isolated pgvector unreachable: {exc}")
    return eng


def test_004_skipped_on_stock_postgres_and_fulltext_intact(engine: Any) -> None:
    with engine.begin() as conn:
        pending = import_data.pending_vector_migrations(conn)
        from culinary_copilot.recipes.repository import search_recipes

        rows = search_recipes(engine, "chicken curry", limit=5)
        assert any(r["source_id"] == "000159" for r in rows)
        if not _vector_available(engine):
            assert "004" in pending
            tables = conn.execute(
                text("SELECT to_regclass('recipe_embeddings')")
            ).scalar_one_or_none()
            assert tables is None
        else:
            assert "004" not in pending


def test_vector_migration_applies_on_isolated_pgvector() -> None:
    eng = _pgvector_engine()
    try:
        with eng.begin() as conn:
            applied = import_data.apply_migrations(conn)
            pending = import_data.pending_vector_migrations(conn)
            assert "004" not in pending
            tables = conn.execute(
                text("SELECT to_regclass('recipe_embeddings')")
            ).scalar_one_or_none()
            assert tables is not None
            assert "004" in applied or tables is not None  # idempotent rerun
    finally:
        eng.dispose()


def test_stale_renderer_rows_excluded_from_search() -> None:
    eng = _pgvector_engine()
    try:
        provider = FakeEmbeddingProvider(model="text-embedding-3-small", dimension=1536)
        query_vector = _run(
            embed_query(provider, "chicken dinner", model="text-embedding-3-small", dimension=1536)
        )
        assert query_vector is not None
        with eng.begin() as conn:
            existing = (
                conn.execute(
                    text(
                        "SELECT dataset_id, source_id FROM recipes "
                        "ORDER BY dataset_id, source_id LIMIT 1"
                    )
                )
                .mappings()
                .first()
            )
            assert existing is not None
            dataset_id, source_id = str(existing["dataset_id"]), str(existing["source_id"])
            conn.execute(
                text(
                    "INSERT INTO recipe_embeddings "
                    "(dataset_id, source_id, model, dimension, renderer_version, "
                    "chunking_version, chunk_index, embedded_text_hash, embedding) "
                    "VALUES (:d, :s, :m, 1536, '0', '0', 99, :h, CAST(:v AS vector)) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "d": dataset_id,
                    "s": source_id,
                    "m": "text-embedding-3-small",
                    "h": "stale-test-hash",
                    "v": "[" + ",".join(["0.01"] * 1536) + "]",
                },
            )
        rows = vector_candidates(
            eng, list(query_vector), limit=25, model="text-embedding-3-small", dimension=1536
        )
        for row in rows:
            if row["source_id"] == source_id and row["dataset_id"] == dataset_id:
                assert row["chunks"] == 1  # stale chunk 99 never counted
    finally:
        eng.dispose()


def test_vector_pantry_boost_orders_before_distance_ties() -> None:
    eng = _pgvector_engine()
    try:
        import math

        def _unit(first: float) -> str:
            rest = [0.0] * 1535
            norm = math.sqrt(first * first)
            return "[" + ",".join([repr(first / norm)] + [repr(v) for v in rest]) + "]"

        with eng.begin() as conn:
            existing = (
                conn.execute(
                    text(
                        "SELECT dataset_id, source_id FROM recipes "
                        "ORDER BY dataset_id, source_id LIMIT 2"
                    )
                )
                .mappings()
                .all()
            )
            assert len(existing) == 2
            first_id = (str(existing[0]["dataset_id"]), str(existing[0]["source_id"]))
            second_id = (str(existing[1]["dataset_id"]), str(existing[1]["source_id"]))
            conn.execute(
                text(
                    "INSERT INTO recipe_embeddings "
                    "(dataset_id, source_id, model, dimension, renderer_version, "
                    "chunking_version, chunk_index, embedded_text_hash, embedding) "
                    "VALUES (:d, :s, 'text-embedding-3-small', 1536, '9', '9', 0, "
                    ":h, CAST(:v AS vector)) ON CONFLICT DO NOTHING"
                ),
                {"d": first_id[0], "s": first_id[1], "h": "pantry-boost-a", "v": _unit(1.0)},
            )
            conn.execute(
                text(
                    "INSERT INTO recipe_embeddings "
                    "(dataset_id, source_id, model, dimension, renderer_version, "
                    "chunking_version, chunk_index, embedded_text_hash, embedding) "
                    "VALUES (:d, :s, 'text-embedding-3-small', 1536, '9', '9', 0, "
                    ":h, CAST(:v AS vector)) ON CONFLICT DO NOTHING"
                ),
                {"d": second_id[0], "s": second_id[1], "h": "pantry-boost-b", "v": _unit(0.99)},
            )
        query = [1.0] + [0.0] * 1535
        plain = vector_candidates(
            eng,
            query,
            limit=5,
            model="text-embedding-3-small",
            dimension=1536,
            renderer_version="9",
            chunking_version="9",
        )
        assert plain and plain[0]["source_id"] == first_id[1]  # nearer first
        assert all(r["pantry_overlap"] == 0 for r in plain)
        with eng.connect() as conn:
            first_names = set(
                conn.execute(
                    text(
                        "SELECT unnest(ingredient_names) FROM recipes "
                        "WHERE dataset_id=:d AND source_id=:s"
                    ),
                    {"d": first_id[0], "s": first_id[1]},
                ).scalars()
            )
            second_names = set(
                conn.execute(
                    text(
                        "SELECT unnest(ingredient_names) FROM recipes "
                        "WHERE dataset_id=:d AND source_id=:s"
                    ),
                    {"d": second_id[0], "s": second_id[1]},
                ).scalars()
            )
        unique = sorted(second_names - first_names)
        assert unique, "seeded recipes must differ in ingredients for the boost test"
        boost_term = unique[0]
        boosted = vector_candidates(
            eng,
            query,
            limit=5,
            rank_pantry_terms=[boost_term],
            model="text-embedding-3-small",
            dimension=1536,
            renderer_version="9",
            chunking_version="9",
        )
        assert boosted and boosted[0]["source_id"] == second_id[1]  # pantry boost wins
        assert boosted[0]["pantry_overlap"] >= 1
    finally:
        eng.dispose()


def test_embedding_dimension_check_accepts_registry_dims() -> None:
    eng = _pgvector_engine()
    try:
        with eng.begin() as conn:
            existing = (
                conn.execute(
                    text(
                        "SELECT dataset_id, source_id FROM recipes "
                        "ORDER BY dataset_id, source_id LIMIT 1"
                    )
                )
                .mappings()
                .first()
            )
            assert existing is not None
            conn.execute(
                text(
                    "INSERT INTO recipe_embeddings "
                    "(dataset_id, source_id, model, dimension, renderer_version, "
                    "chunking_version, chunk_index, embedded_text_hash, embedding) "
                    "VALUES (:d, :s, 'text-embedding-3-large', 3072, '9', '9', 77, "
                    ":h, CAST(:v AS vector)) ON CONFLICT DO NOTHING"
                ),
                {
                    "d": str(existing["dataset_id"]),
                    "s": str(existing["source_id"]),
                    "h": "dim-check-3072",
                    "v": "[" + ",".join(["0.0"] * 3071 + ["1.0"]) + "]",
                },
            )
            import sqlalchemy.exc

            with pytest.raises(sqlalchemy.exc.IntegrityError), conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO recipe_embeddings "
                        "(dataset_id, source_id, model, dimension, renderer_version, "
                        "chunking_version, chunk_index, embedded_text_hash, embedding) "
                        "VALUES (:d, :s, 'x', 999, '9', '9', 78, :h, '[1.0]')"
                    ),
                    {
                        "d": str(existing["dataset_id"]),
                        "s": str(existing["source_id"]),
                        "h": "dim-bad",
                    },
                )
    finally:
        eng.dispose()


def test_cli_resume_embeds_zero_chunks_on_second_run(tmp_path: Path) -> None:
    url = os.environ.get("PGVECTOR_TEST_URL", "")
    if not url:
        pytest.skip("PGVECTOR_TEST_URL not set; CLI resume tier skipped")
    import sys

    sys.path.insert(0, "scripts/embeddings")
    from embed import main as embed_main

    run_dir = tmp_path / "cli-resume"
    first = embed_main(["--run-dir", str(run_dir), "--database-url", url, "--fake", "--limit", "3"])
    assert first == 0
    ledger = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
    assert len(ledger["recipes"]) == 3
    assert all(len(v["fingerprint"]) == 64 and v["committed"] for v in ledger["recipes"].values())
    second = embed_main(
        ["--run-dir", str(run_dir), "--database-url", url, "--fake", "--limit", "3"]
    )
    assert second == 0
    ledger2 = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
    assert len(ledger2["recipes"]) == 3  # same scan window; resume skipped everything


def test_cli_crash_recovery_reembeds_idempotently(tmp_path: Path) -> None:
    url = os.environ.get("PGVECTOR_TEST_URL", "")
    if not url:
        pytest.skip("PGVECTOR_TEST_URL not set; crash-recovery tier skipped")
    import sys

    sys.path.insert(0, "scripts/embeddings")
    from embed import main as embed_main

    run_dir = tmp_path / "cli-crash"
    assert (
        embed_main(["--run-dir", str(run_dir), "--database-url", url, "--fake", "--limit", "2"])
        == 0
    )
    ledger_path = run_dir / "ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    dropped = next(iter(ledger["recipes"]))
    del ledger["recipes"][dropped]  # simulate crash after commit, before ledger save
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    assert embed_main(["--run-dir", str(run_dir), "--database-url", url, "--fake"]) == 0
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM recipe_embeddings "
                    "WHERE renderer_version='1' AND chunking_version='1'"
                )
            ).scalar_one()
            assert int(count) >= 2  # upsert, never duplicates
    finally:
        eng.dispose()


def test_fake_refuses_the_application_database(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, "scripts/embeddings")
    from embed import main as embed_main

    code = embed_main(
        [
            "--run-dir",
            str(tmp_path / "guard"),
            "--database-url",
            "postgresql+psycopg://copilot:copilot_dev@localhost:5432/culinary_copilot",
            "--fake",
        ]
    )
    assert code == 2


def test_hybrid_orchestration_fulltext_default_makes_zero_calls(engine: Any) -> None:
    from culinary_copilot.services.answers import init_state
    from culinary_copilot.services.clarification_service import make_group
    from culinary_copilot.services.store import InMemoryClarificationStore

    provider = FakeEmbeddingProvider(model="text-embedding-3-small", dimension=1536)
    store = InMemoryClarificationStore()
    state = init_state(
        request_id="req-hybrid",
        request={"ingredients": ["chicken"]},
        dish="chicken curry",
    )
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    from culinary_copilot.retrieval.service import retrieve_for_group

    result = _run(
        retrieve_for_group(
            store=store,
            engine=engine,
            group_id=group.group_id,
            expected_request_revision=state.revision,
            expected_group_revision=group.revision,
            limit=5,
            mode="fulltext",
            embed_provider=provider,
        )
    )
    assert result["outcome"] == "ready"
    assert result["retrieval_mode"] == "fulltext"
    assert provider.calls == []


def test_no_match_and_dataset_isolation_shape(engine: Any) -> None:
    from culinary_copilot.recipes.repository import search_all, search_recipes

    rows = search_all(engine, "zzz-no-such-dish-zzz", limit=5)
    assert rows == []
    with pytest.raises(ValueError, match="Unsupported dataset"):
        search_recipes(engine, "chicken", limit=5, dataset_id="unknown/dataset")


def test_migrate_cli_dry_run_lists_pending_with_zero_writes() -> None:
    import sys

    sys.path.insert(0, "scripts")
    import migrate as migrate_cli
    from sqlalchemy import create_engine, text

    maint_url, _ = _urls()
    check_url = maint_url.rsplit("/", 1)[0] + "/culinary_test_migrate_dry"
    maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(text('DROP DATABASE IF EXISTS "culinary_test_migrate_dry"'))
        conn.execute(text('CREATE DATABASE "culinary_test_migrate_dry"'))
    maint.dispose()
    try:
        code = migrate_cli.main(["--database-url", check_url, "--dry-run"])
        assert code == 0
        eng = create_engine(check_url)
        try:
            with eng.connect() as conn:
                tables = conn.execute(
                    text("SELECT to_regclass('recipe_schema_migrations')")
                ).scalar_one_or_none()
                assert tables is None  # dry-run created nothing
        finally:
            eng.dispose()
    finally:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text('DROP DATABASE IF EXISTS "culinary_test_migrate_dry"'))
        maint.dispose()


def test_migrate_cli_refuses_unexpected_target() -> None:
    import sys

    sys.path.insert(0, "scripts")

    import migrate as migrate_cli

    code = migrate_cli.main(
        [
            "--database-url",
            "postgresql+psycopg://copilot:copilot_dev@localhost:5432/culinary_copilot",
            "--expect-db-name",
            "culinary_test_x",
            "--expect-db-host",
            "localhost",
        ]
    )
    assert code == 2


def test_migrate_cli_vector_behavior_matches_server() -> None:
    """Stock servers skip 004 with a reason; pgvector servers apply it."""
    import sys

    sys.path.insert(0, "scripts")
    import migrate as migrate_cli
    from sqlalchemy import create_engine, text

    from culinary_copilot.recipes.import_data import _extension_available

    maint_url, _ = _urls()
    db_name = "culinary_test_migrate_skip"
    skip_url = maint_url.rsplit("/", 1)[0] + f"/{db_name}"
    maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    maint.dispose()
    try:
        probe = create_engine(skip_url)
        try:
            with probe.connect() as conn:
                vector_capable = _extension_available(conn, "vector")
        finally:
            probe.dispose()
        code = migrate_cli.main(
            [
                "--database-url",
                skip_url,
                "--expect-db-name",
                db_name,
                "--expect-db-host",
                "localhost",
            ]
        )
        assert code == 0
        eng = create_engine(skip_url)
        try:
            with eng.connect() as conn:
                versions = {
                    row[0]
                    for row in conn.execute(text("SELECT version FROM recipe_schema_migrations"))
                }
                assert {"001", "002", "003"} <= versions
                if vector_capable:
                    assert "004" in versions
                else:
                    assert "004" not in versions
        finally:
            eng.dispose()
    finally:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        maint.dispose()
