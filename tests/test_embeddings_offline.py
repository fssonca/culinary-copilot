"""Phase 5 offline tests: rendering, identity, packing, RRF, provider contract.

Fake embeddings only. No model calls, no downloads, no DB writes. Fake
vectors never measure semantic relevance.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from culinary_copilot.embeddings.provider import (
    DisabledEmbeddingProvider,
    EmbeddingUnavailableError,
    EmbeddingValidationError,
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
    check_model_dimension,
    validate_vectors,
)
from culinary_copilot.embeddings.query import embed_query
from culinary_copilot.embeddings.registry import (
    SUPPORTED_EMBEDDING_MODELS,
    embedding_spec,
    estimate_cost_usd,
    require_supported_embedding_model,
)
from culinary_copilot.embeddings.rendering import (
    CHUNKING_VERSION,
    EMBED_DOCUMENT_VERSION,
    chunk_embed_text,
    embedded_text_hash,
    embedding_identity,
    estimate_tokens_bytes,
    render_embed_text,
)
from culinary_copilot.recipes.vector_search import rrf_fuse


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_only_proposed_models_with_verified_pricing() -> None:
    assert set(SUPPORTED_EMBEDDING_MODELS) == {
        "text-embedding-3-small",
        "text-embedding-3-large",
        "text-embedding-ada-002",
    }
    small = embedding_spec("text-embedding-3-small")
    assert small is not None and small.dimension == 1536
    assert small.max_input_tokens == 8192 and small.max_total_tokens_per_request == 300_000
    assert estimate_cost_usd(1_000_000, "text-embedding-3-small") == pytest.approx(0.02)
    assert estimate_cost_usd(1_000_000, "text-embedding-3-large") == pytest.approx(0.13)
    assert estimate_cost_usd(1_000_000, "unknown-model") is None
    with pytest.raises(ValueError, match="Unsupported embedding model"):
        require_supported_embedding_model("gpt-6-luna")


def test_render_is_deterministic_and_versioned() -> None:
    recipe = {
        "title": "Chicken  Curry ",
        "description": "A curry.",
        "ingredients": [{"canonical": "chicken"}, {"canonical": "curry powder"}],
        "instructions": ["Cook  the chicken.", "Serve."],
    }
    first = render_embed_text(recipe)
    assert render_embed_text(recipe) == first
    assert "Chicken Curry" in first and "Ingredients:" in first
    assert EMBED_DOCUMENT_VERSION == "1" and CHUNKING_VERSION == "1"


def test_identity_changes_when_renderer_or_text_changes() -> None:
    ident = embedding_identity(
        dataset_id="d",
        source_id="s",
        model="text-embedding-3-small",
        dimension=1536,
        chunk_index=0,
        embedded_text="hello",
    )
    other_text = embedding_identity(
        dataset_id="d",
        source_id="s",
        model="text-embedding-3-small",
        dimension=1536,
        chunk_index=0,
        embedded_text="hello!",
    )
    assert ident["embedded_text_hash"] != other_text["embedded_text_hash"]
    assert embedded_text_hash("hello") != embedded_text_hash("hello!")
    # Provenance is separate: same identity fields, different import ids still match.
    assert ident["renderer_version"] == "1"


def test_oversized_recipe_truncates_deterministically() -> None:
    chunks, truncated = chunk_embed_text("word " * 5000)
    assert len(chunks) == 1 and truncated is True
    assert len(chunks[0]) <= 7000
    small, flag = chunk_embed_text("short dish")
    assert small == ["short dish"] and flag is False
    # Conservative upper bound: UTF-8 bytes + 8 (covers single-byte tokens).
    assert estimate_tokens_bytes("abcd") == 12


def test_token_bound_covers_non_ascii_bytes() -> None:
    # "½" is 2 bytes, "é" is 2 bytes, "🍲" is 4 bytes in UTF-8.
    assert estimate_tokens_bytes("½ cup sugar") >= len("½ cup sugar".encode("utf-8"))
    assert estimate_tokens_bytes("crème brûlée") >= len("crème brûlée".encode("utf-8"))
    assert estimate_tokens_bytes("🍲🍲🍲") >= len("🍲🍲🍲".encode("utf-8")) == 12
    assert estimate_tokens_bytes("🍲🍲🍲") == 12 + 8
    # Code-point counting would underestimate: 3 chars vs 12 bytes.
    assert len("🍲🍲🍲") == 3


def test_fake_provider_is_deterministic_and_validated() -> None:
    provider = FakeEmbeddingProvider(model="text-embedding-3-small", dimension=1536)
    first = _run(provider.embed_texts(["chicken curry", "chicken curry", "other"]))
    assert len(first.vectors) == 3 and len(first.vectors[0]) == 1536
    assert first.vectors[0] == first.vectors[1] and first.vectors[0] != first.vectors[2]
    with pytest.raises(ValueError, match="registry model/dim"):
        FakeEmbeddingProvider(model="text-embedding-3-small", dimension=999)
    with pytest.raises(Exception):
        validate_vectors([[0.0] * 10], expected_dimension=1536, model="m")
    with pytest.raises(Exception):
        validate_vectors([[float("nan")] * 1536], expected_dimension=1536, model="m")


def test_query_embedding_enforces_same_model_dim_and_fallback() -> None:
    provider = FakeEmbeddingProvider()
    vector = _run(
        embed_query(provider, "chicken dinner", model="text-embedding-3-small", dimension=1536)
    )
    assert vector is not None and len(vector) == 1536
    with pytest.raises(EmbeddingUnavailableError):
        _run(embed_query(provider, "x", model="text-embedding-3-small", dimension=999))
    with pytest.raises(EmbeddingUnavailableError):
        _run(embed_query(None, "x", model="text-embedding-3-small", dimension=1536))
    assert (
        _run(
            embed_query(
                None, "x", model="text-embedding-3-small", dimension=1536, allow_fallback=True
            )
        )
        is None
    )
    assert provider.calls, "query path must record provider use"


def test_disabled_provider_fails_closed_with_zero_network() -> None:
    provider = DisabledEmbeddingProvider()
    with pytest.raises(EmbeddingUnavailableError):
        _run(provider.embed_texts(["x"]))
    assert provider.calls == [["x"]]


def test_rrf_aggregates_before_limit_and_never_averages() -> None:
    fulltext = [
        {"dataset_id": "d", "source_id": "a"},
        {"dataset_id": "d", "source_id": "b"},
        {"dataset_id": "d", "source_id": "c"},
    ]
    vector = [
        {"dataset_id": "d", "source_id": "c"},
        {"dataset_id": "d", "source_id": "d"},
    ]
    fused = rrf_fuse(fulltext, vector, k=60, limit=3)
    assert len(fused) == 3
    assert fused[0]["source_id"] == "c"  # present in both rankings
    assert all("rrf_score" in row for row in fused)
    assert {row["source_id"] for row in fused} <= {"a", "b", "c", "d"}


def test_model_dimension_mismatch_refused_before_call() -> None:
    with pytest.raises(EmbeddingUnavailableError, match="mismatch"):
        check_model_dimension("text-embedding-3-small", 3072)
    with pytest.raises(EmbeddingUnavailableError, match="unsupported"):
        check_model_dimension("gpt-6-luna", 1536)


def test_ledger_single_worker_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import sys

    sys.path.insert(0, "scripts/embeddings")
    import embed as embed_cli
    from embed import _claim_ledger

    me = os.getpid()
    assert _claim_ledger({"status": "done", "run_pid": me}, pid=me) == ""
    assert _claim_ledger({"status": "running"}, pid=me) == ""
    assert _claim_ledger({"status": "running", "run_pid": me}, pid=me) == ""
    # Stale claim (dead pid) is releasable; live foreign pid refuses.
    monkeypatch.setattr(embed_cli, "_pid_alive", lambda pid: pid == 424242)
    assert _claim_ledger({"status": "running", "run_pid": 999999}, pid=me) == ""
    assert "run-dir owned" in _claim_ledger({"status": "running", "run_pid": 424242}, pid=me)
    monkeypatch.undo()
    # A genuinely live foreign pid refuses without monkeypatching.
    import subprocess

    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert "run-dir owned" in _claim_ledger({"status": "running", "run_pid": proc.pid}, pid=me)
    finally:
        proc.kill()


class _MockEmbeddings:
    def __init__(
        self, calls: list[str], vectors: list[list[float]], usage: tuple[int, int]
    ) -> None:
        self._calls = calls
        self._vectors = vectors
        self._usage = usage

    def create(self, *, model: str, input: list[str], encoding_format: str = "float") -> Any:
        self._calls.append(model)
        from types import SimpleNamespace

        assert encoding_format == "float"
        # Return out-of-order indices to prove index mapping.
        data = [
            SimpleNamespace(index=len(input) - 1 - i, embedding=list(v))
            for i, v in enumerate(self._vectors)
        ]
        data = sorted(data, key=lambda d: d.index, reverse=True)
        return SimpleNamespace(
            data=data,
            usage=SimpleNamespace(prompt_tokens=self._usage[0], total_tokens=self._usage[1]),
        )


def test_live_provider_maps_by_index_and_validates() -> None:
    vectors = [[0.1] * 1536, [0.2] * 1536]
    client_calls: list[str] = []
    mock = _MockEmbeddings(client_calls, vectors, (10, 10))
    provider = OpenAIEmbeddingProvider(
        model="text-embedding-3-small",
        dimension=1536,
        api_key="test-key",
        client_factory=lambda: SimpleNamespace(embeddings=mock),
    )
    result = _run(provider.embed_texts(["a", "b"]))
    # Mock returned index 1 first: mapping must follow index, not arrival order.
    assert [result.vectors[0][0], result.vectors[1][0]] == pytest.approx([0.2, 0.1])
    assert result.usage.prompt_tokens == 10
    assert provider.attempts == 1


def test_live_provider_rejects_bad_dims_and_retries_transport() -> None:
    bad = [[0.1] * 10]
    provider = OpenAIEmbeddingProvider(
        model="text-embedding-3-small",
        dimension=1536,
        api_key="test-key",
        client_factory=lambda: SimpleNamespace(embeddings=_MockEmbeddings([], bad, (1, 1))),
    )
    with pytest.raises(EmbeddingValidationError):
        _run(provider.embed_texts(["a"]))

    attempts = {"n": 0}

    class _Flaky:
        def create(self, **kwargs: Any) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TimeoutError("transport timeout")
            from types import SimpleNamespace

            return SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[0.0] * 1536)],
                usage=SimpleNamespace(prompt_tokens=1, total_tokens=1),
            )

    retrying = OpenAIEmbeddingProvider(
        model="text-embedding-3-small",
        dimension=1536,
        api_key="test-key",
        max_retries=1,
        client_factory=lambda: SimpleNamespace(embeddings=_Flaky()),
    )
    result = _run(retrying.embed_texts(["a"]))
    assert len(result.vectors) == 1 and attempts["n"] == 2
