"""Phase 6 offline tests: vector cutoff (c) and full-text gate (b).

No database, network, or paid calls. Repository and vector search are
patched; FakeEmbeddingProvider supplies deterministic query vectors.
Defaults (cutoff None, gate False) must leave Phase 5 behaviour unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from culinary_copilot.embeddings.provider import (
    EmbeddingUnavailableError,
    FakeEmbeddingProvider,
)
from culinary_copilot.recipes.vector_search import (
    VECTOR_CUTOFF_ABSTENTION,
    apply_vector_cutoff,
    rrf_fuse,
)
from culinary_copilot.retrieval.hybrid import retrieve_with_mode

FOODCOM = "AkashPS11/recipes_data_food.com"
FOODIE = "odunola/foodie"


def _query(text: str = "chicken curry") -> Any:
    return SimpleNamespace(
        query_text=text,
        required_ingredients=[],
        max_minutes=None,
        dataset_id=None,
        match_any_ingredients=None,
        rank_pantry_terms=[],
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _v(dataset: str, source: str, distance: float) -> dict[str, Any]:
    return {"dataset_id": dataset, "source_id": source, "distance": distance}


def _f(dataset: str, source: str) -> dict[str, Any]:
    return {"dataset_id": dataset, "source_id": source}


def test_defaults_leave_phase5_behaviour_unchanged() -> None:
    vector = [_v(FOODIE, "a", 0.9), _v(FOODIE, "b", 0.95)]
    fulltext = [_f(FOODIE, "c")]
    provider = FakeEmbeddingProvider()
    with (
        patch(
            "culinary_copilot.recipes.vector_search.vector_candidates",
            return_value=vector,
        ),
        patch(
            "culinary_copilot.retrieval.service._search_sync",
            return_value=fulltext,
        ),
    ):
        out_v = _run(
            retrieve_with_mode(object(), _query(), limit=5, mode="vector", provider=provider)
        )
        assert [r["source_id"] for r in out_v["rows"]] == ["a", "b"]
        assert out_v["abstention_reason"] is None
        out_h = _run(
            retrieve_with_mode(object(), _query(), limit=5, mode="hybrid", provider=provider)
        )
        expected = rrf_fuse(fulltext, vector, k=60, limit=5)
        assert [r["source_id"] for r in out_h["rows"]] == [r["source_id"] for r in expected]
        assert out_h["abstention_reason"] is None


def test_vector_cutoff_filters_and_abstains_with_reason_code() -> None:
    vector = [_v(FOODIE, "near", 0.3), _v(FOODIE, "far", 0.8)]
    provider = FakeEmbeddingProvider()
    with patch(
        "culinary_copilot.recipes.vector_search.vector_candidates",
        return_value=vector,
    ):
        out = _run(
            retrieve_with_mode(
                object(),
                _query(),
                limit=5,
                mode="vector",
                provider=provider,
                vector_distance_cutoff=0.5,
            )
        )
        assert [r["source_id"] for r in out["rows"]] == ["near"]
        assert out["abstention_reason"] is None
        assert out["vector_count_raw"] == 2 and out["vector_count_kept"] == 1

        empty = _run(
            retrieve_with_mode(
                object(),
                _query(),
                limit=5,
                mode="vector",
                provider=provider,
                vector_distance_cutoff=0.1,
            )
        )
        assert empty["rows"] == []
        assert empty["abstention_reason"] == VECTOR_CUTOFF_ABSTENTION == "vector_cutoff_abstention"
        assert empty["fallback"] is None  # never a silent full-text fallback


def test_vector_empty_eligible_set_is_not_cutoff_abstention() -> None:
    provider = FakeEmbeddingProvider()
    with patch(
        "culinary_copilot.recipes.vector_search.vector_candidates",
        return_value=[],
    ):
        out = _run(
            retrieve_with_mode(
                object(),
                _query(),
                limit=5,
                mode="vector",
                provider=provider,
                vector_distance_cutoff=0.5,
            )
        )
        assert out["rows"] == []
        assert out["abstention_reason"] is None


def test_hybrid_keeps_fulltext_when_cutoff_removes_every_vector() -> None:
    vector = [_v(FOODIE, "far1", 0.9), _v(FOODIE, "far2", 0.95)]
    fulltext = [_f(FOODIE, "ft1"), _f(FOODIE, "ft2")]
    provider = FakeEmbeddingProvider()
    with (
        patch(
            "culinary_copilot.recipes.vector_search.vector_candidates",
            return_value=vector,
        ),
        patch(
            "culinary_copilot.retrieval.service._search_sync",
            return_value=fulltext,
        ),
    ):
        out = _run(
            retrieve_with_mode(
                object(),
                _query(),
                limit=5,
                mode="hybrid",
                provider=provider,
                vector_distance_cutoff=0.1,
            )
        )
        assert out["vector_count_raw"] == 2 and out["vector_count_kept"] == 0
        assert [r["source_id"] for r in out["rows"]] == ["ft1", "ft2"]
        assert out["abstention_reason"] is None


def test_hybrid_abstains_when_cutoff_empties_vectors_and_fusion_is_empty() -> None:
    vector = [_v(FOODIE, "far1", 0.9), _v(FOODIE, "far2", 0.95)]
    provider = FakeEmbeddingProvider()
    with (
        patch(
            "culinary_copilot.recipes.vector_search.vector_candidates",
            return_value=vector,
        ),
        patch("culinary_copilot.retrieval.service._search_sync", return_value=[]),
    ):
        out = _run(
            retrieve_with_mode(
                object(),
                _query(),
                limit=5,
                mode="hybrid",
                provider=provider,
                vector_distance_cutoff=0.1,
            )
        )
        assert out["rows"] == []
        assert out["abstention_reason"] == VECTOR_CUTOFF_ABSTENTION
        assert out["fallback"] is None  # abstention, not a silent fallback
        # Without a cutoff the same empty-fulltext hybrid returns vectors, no code.
        plain = _run(
            retrieve_with_mode(object(), _query(), limit=5, mode="hybrid", provider=provider)
        )
        assert [r["source_id"] for r in plain["rows"]] == ["far1", "far2"]
        assert plain["abstention_reason"] is None


def test_fulltext_gate_adds_vectors_only_when_fulltext_nonempty() -> None:
    vector = [_v(FOODIE, "v1", 0.2)]
    provider = FakeEmbeddingProvider()
    # Gate with empty full-text: vector results must be ignored.
    with (
        patch(
            "culinary_copilot.recipes.vector_search.vector_candidates",
            return_value=vector,
        ),
        patch("culinary_copilot.retrieval.service._search_sync", return_value=[]),
    ):
        gated = _run(
            retrieve_with_mode(
                object(), _query(), limit=5, mode="hybrid", provider=provider, fulltext_gate=True
            )
        )
        assert gated["rows"] == []
        ungated = _run(
            retrieve_with_mode(
                object(), _query(), limit=5, mode="hybrid", provider=provider, fulltext_gate=False
            )
        )
        assert [r["source_id"] for r in ungated["rows"]] == ["v1"]
    # Gate with non-empty full-text: identical to no-gate fusion.
    fulltext = [_f(FOODIE, "ft1")]
    with (
        patch(
            "culinary_copilot.recipes.vector_search.vector_candidates",
            return_value=vector,
        ),
        patch("culinary_copilot.retrieval.service._search_sync", return_value=fulltext),
    ):
        gated = _run(
            retrieve_with_mode(
                object(), _query(), limit=2, mode="hybrid", provider=provider, fulltext_gate=True
            )
        )
        plain = _run(
            retrieve_with_mode(
                object(), _query(), limit=2, mode="hybrid", provider=provider, fulltext_gate=False
            )
        )
        assert [r["source_id"] for r in gated["rows"]] == [r["source_id"] for r in plain["rows"]]


def test_identities_exact_no_cross_dataset_match() -> None:
    rows = [_v(FOODCOM, "000159", 0.2), _v(FOODIE, "000159", 0.3)]
    kept = apply_vector_cutoff(rows, 0.25)
    assert [(r["dataset_id"], r["source_id"]) for r in kept] == [(FOODCOM, "000159")]
    assert kept[0] is rows[0]  # no identity rewriting


def test_cutoff_rejects_nonfinite_distances() -> None:
    rows = [
        _v(FOODIE, "ok", 0.2),
        {"dataset_id": FOODIE, "source_id": "nan", "distance": float("nan")},
        {"dataset_id": FOODIE, "source_id": "missing"},
    ]
    kept = apply_vector_cutoff(rows, 0.5)
    assert [r["source_id"] for r in kept] == ["ok"]
    with pytest.raises(ValueError):
        apply_vector_cutoff(rows, float("nan"))


def test_no_fallback_embedding_failure_is_recorded_error() -> None:
    with pytest.raises(EmbeddingUnavailableError):
        _run(
            retrieve_with_mode(
                object(), _query(), limit=5, mode="vector", provider=None, allow_fallback=False
            )
        )
    with patch("culinary_copilot.retrieval.service._search_sync", return_value=[]):
        out = _run(
            retrieve_with_mode(
                object(), _query(), limit=5, mode="vector", provider=None, allow_fallback=True
            )
        )
    assert out["fallback"] == "fulltext (embeddings unavailable, disclosed)"


def test_filters_still_apply_to_vector_results_in_sql() -> None:
    """Vector SQL must receive the query's required/time/dataset filters."""
    import culinary_copilot.recipes.vector_search as vs

    seen: dict[str, Any] = {}

    def _fake(engine: Any, vector: list[float], **kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return []

    provider = FakeEmbeddingProvider()
    query = SimpleNamespace(
        query_text="soup",
        required_ingredients=["chicken"],
        max_minutes=30.0,
        dataset_id=FOODIE,
        match_any_ingredients=["tofu"],
        rank_pantry_terms=["chicken"],
    )
    with patch.object(vs, "vector_candidates", side_effect=_fake):
        _run(retrieve_with_mode(object(), query, limit=5, mode="vector", provider=provider))
    assert seen["ingredients"] == ["chicken"]
    assert seen["max_minutes"] == 30.0
    assert seen["dataset_id"] == FOODIE
    assert seen["match_any_ingredients"] == ["tofu"]
    assert seen["rank_pantry_terms"] == ["chicken"]
