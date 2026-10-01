"""Phase 2 typed tool layer tests (offline, key-free, no database).

Fake engines, fake Epicure cores, fake embedding providers and fake
session stores only. No model calls, no network, no application DB.
Disposable-DB permission flow lives in tests/test_tools_pg.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from culinary_copilot.config import Settings
from culinary_copilot.domain.recommendations import (
    REASON_CONVERT_UNSUPPORTED_UNIT,
    REASON_SCALE_MISSING_SERVINGS,
    REASON_TOOL_INTERNAL_ERROR,
    REASON_TOOL_INVALID_ARGUMENTS,
    REASON_TOOL_NOT_CONFIGURED,
    REASON_TOOL_PERMISSION_DENIED,
    REASON_TOOL_TIMEOUT,
    REASON_TOOL_UNAVAILABLE,
    next_action_for,
)
from culinary_copilot.embeddings.provider import (
    DisabledEmbeddingProvider,
    FakeEmbeddingProvider,
)
from culinary_copilot.tools import (
    ToolContext,
    all_tool_definitions,
    all_tool_impls,
    build_tool_context,
    run_tool,
)
from culinary_copilot.tools.registry import ToolDefinition, args_digest
from culinary_copilot.tools.search_tools import (
    TOOL_VECTOR_CUTOFF,
    build_embed_provider,
    resolve_search_mode,
    resolve_vector_cutoff,
)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _defs() -> dict[str, ToolDefinition]:
    return {d.name: d for d in all_tool_definitions(timeout_s=10.0)}


def _impls() -> dict[str, Any]:
    return all_tool_impls()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _call(name: str, args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _run(run_tool(_defs()[name], _impls()[name], args, context))


def _ctx(**kwargs: Any) -> ToolContext:
    base: dict[str, Any] = {"settings": _settings()}
    base.update(kwargs)
    return ToolContext(**base)  # type: ignore[arg-type]


# --- registry metadata -----------------------------------------------------


def test_all_tools_have_schemas_timeout_idempotency_cost() -> None:
    defs = _defs()
    assert len(defs) == 10
    for name, d in defs.items():
        assert d.name == name
        assert d.timeout_s == 10.0
        assert isinstance(d.idempotent, bool)
        assert d.cost_class in ("free", "paid", "network")
        # extra="forbid": unknown fields rejected at validation.
        assert d.args_model.model_config.get("extra") == "forbid"
    assert _defs()["search_recipes"].cost_class == "paid"
    assert _defs()["search_web"].cost_class == "network"
    assert _defs()["search_web"].idempotent is False


def test_tool_timeout_setting_is_read() -> None:
    s = _settings(tool_timeout_s=0.05)
    assert s.tool_timeout_s == 0.05
    ctx = _ctx(settings=s)
    assert ctx.settings.tool_timeout_s == 0.05


def test_args_digest_stable_and_hides_raw() -> None:
    d1 = args_digest({"b": 2, "a": 1})
    d2 = args_digest({"a": 1, "b": 2})
    assert d1 == d2
    assert "secret-query" not in args_digest({"query": "secret-query"})


# --- argument validation for every tool ------------------------------------


def test_argument_validation_for_every_tool() -> None:
    ctx = _ctx()
    bad: dict[str, dict[str, Any]] = {
        "search_recipes": {"query": "", "mode": "hybrid", "limit": 99, "extra": 1},
        "get_recipe": {"dataset_id": "", "source_id": ""},
        "find_balanced_pairings": {"ingredient": "", "k": 0},
        "find_conventional_pairings": {"ingredient": "", "k": 99},
        "find_flavor_pairings": {"k": 5},
        "find_substitutions": {"ingredient": ""},
        "scale_recipe": {"dataset_id": "x", "source_id": "y"},
        "convert_units": {"amount": -1, "from_unit": "g", "to_unit": "kg"},
        "search_techniques": {"query": ""},
        "search_web": {"session_id": "", "query": ""},
    }
    for name, args in bad.items():
        result = _call(name, args, ctx)
        assert result["ok"] is False, name
        assert result["error_type"] == "invalid_arguments", name
        assert result["reason"] == REASON_TOOL_INVALID_ARGUMENTS, name
        assert result["next_action"] == next_action_for(REASON_TOOL_INVALID_ARGUMENTS)


# --- timeouts ---------------------------------------------------------------


def test_timeout_returns_typed_error() -> None:
    from culinary_copilot.tools.search_tools import SearchRecipesArgs

    async def _sleep(args: Any, context: ToolContext) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"ok": True}

    tool = ToolDefinition(
        name="search_recipes",
        description="sleep fake",
        args_model=SearchRecipesArgs,
        timeout_s=0.05,
        idempotent=True,
        cost_class="free",
    )
    ctx = _ctx(settings=_settings(tool_timeout_s=0.05))
    result = _run(run_tool(tool, _sleep, {"query": "chicken"}, ctx))
    assert result["ok"] is False
    assert result["error_type"] == "timeout"
    assert result["reason"] == REASON_TOOL_TIMEOUT


def test_settings_timeout_overrides_definition() -> None:
    seen: dict[str, float] = {}

    async def _probe(args: Any, context: ToolContext) -> dict[str, Any]:
        from culinary_copilot.tools import all_tool_definitions as _all
        from culinary_copilot.tools.registry import _timeout_for

        seen["t"] = _timeout_for(context, _all(timeout_s=99.0)[7])
        return {"ok": True, "amount": 1.0, "unit": "g"}

    ctx = _ctx(settings=_settings(tool_timeout_s=7.5))
    tool = [d for d in all_tool_definitions(timeout_s=99.0) if d.name == "convert_units"][0]
    result = _run(run_tool(tool, _probe, {"amount": 1, "from_unit": "g", "to_unit": "g"}, ctx))
    assert result["ok"] is True
    assert seen["t"] == 7.5


# --- search_recipes mode selection ------------------------------------------


def test_default_mode_is_fulltext_and_provider_starts_only_when_enabled() -> None:
    s = _settings()
    assert s.retrieval_mode == "fulltext"
    assert s.embeddings_enabled is False
    assert build_embed_provider(s) is None
    mode, _ = resolve_search_mode(None, ToolContext(settings=s))
    assert mode == "fulltext"
    assert TOOL_VECTOR_CUTOFF == 0.66
    assert resolve_vector_cutoff(ToolContext(settings=s)) == 0.66
    s2 = _settings(retrieval_mode="vector", retrieval_vector_cutoff=0.5)
    mode2, _ = resolve_search_mode(None, ToolContext(settings=s2))
    assert mode2 == "vector"
    assert resolve_vector_cutoff(ToolContext(settings=s2)) == 0.5


def test_search_recipes_fulltext_makes_zero_embedding_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import culinary_copilot.recipes.repository as repo

    monkeypatch.setattr(
        repo,
        "search_all",
        lambda engine, query, limit=5, **kw: [{"dataset_id": "d", "source_id": "s", "title": "t"}],
    )
    provider = FakeEmbeddingProvider()
    ctx = _ctx(engine=object(), embed_provider=provider)
    result = _call("search_recipes", {"query": "chicken"}, ctx)
    assert result["ok"] is True
    assert result["mode_ran"] == "fulltext"
    assert provider.calls == []


def test_search_recipes_vector_uses_provider_and_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import culinary_copilot.recipes.vector_search as vs
    from culinary_copilot.embeddings import query as qmod

    async def _fake_embed(provider: Any, text: str, **kw: Any) -> list[float]:
        assert provider is not None
        r = await provider.embed_texts([text])
        return r.vectors[0]

    monkeypatch.setattr(qmod, "embed_query", _fake_embed)
    monkeypatch.setattr(
        vs,
        "vector_candidates",
        lambda engine, qv, **kw: [
            {"dataset_id": "d", "source_id": "a", "distance": 0.1},
            {"dataset_id": "d", "source_id": "b", "distance": 0.9},
        ],
    )
    provider = FakeEmbeddingProvider()
    ctx = _ctx(engine=object(), embed_provider=provider)
    result = _call("search_recipes", {"query": "soup", "mode": "vector"}, ctx)
    assert result["ok"] is True
    assert result["mode_ran"] == "vector"
    assert provider.calls == [["soup"]]
    ids = [r["source_id"] for r in result["results"]]
    assert ids == ["a"]  # 0.9 removed by the 0.66 cutoff, no fallback rows


def test_search_recipes_vector_without_embeddings_is_not_configured_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import culinary_copilot.recipes.repository as repo

    called = {"n": 0}

    def _boom(*a: Any, **k: Any) -> Any:
        called["n"] += 1
        raise AssertionError("must not fall back to full-text")

    monkeypatch.setattr(repo, "search_all", _boom)
    ctx = _ctx(engine=object(), embed_provider=None)  # disabled by default
    result = _call("search_recipes", {"query": "soup", "mode": "vector"}, ctx)
    assert result["ok"] is False
    assert result["error_type"] == "unavailable"
    assert result["reason"] == REASON_TOOL_NOT_CONFIGURED
    assert next_action_for(result["reason"]) == "contact_operator"
    assert called["n"] == 0


def test_search_recipes_disabled_provider_vector_is_not_configured() -> None:
    ctx = _ctx(
        engine=object(),
        embed_provider=DisabledEmbeddingProvider(),
        settings=_settings(embeddings_enabled=True),
    )
    result = _call("search_recipes", {"query": "soup", "mode": "vector"}, ctx)
    assert result["ok"] is False
    assert result["reason"] == REASON_TOOL_NOT_CONFIGURED


def test_search_recipes_transient_db_error_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    import culinary_copilot.recipes.repository as repo

    def _db_down(*a: Any, **k: Any) -> Any:
        raise SQLAlchemyError("connection lost")

    monkeypatch.setattr(repo, "search_all", _db_down)
    ctx = _ctx(engine=object())
    result = _call("search_recipes", {"query": "soup"}, ctx)
    assert result["ok"] is False
    assert result["reason"] == REASON_TOOL_UNAVAILABLE
    assert next_action_for(result["reason"]) == "retry"


def test_omitted_mode_follows_retrieval_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import culinary_copilot.recipes.repository as repo
    import culinary_copilot.recipes.vector_search as vs
    from culinary_copilot.embeddings import query as qmod

    monkeypatch.setattr(repo, "search_all", lambda *a, **k: [{"dataset_id": "d", "source_id": "s"}])

    async def _fake_embed(provider: Any, text: str, **kw: Any) -> list[float]:
        r = await provider.embed_texts([text])
        return r.vectors[0]

    monkeypatch.setattr(qmod, "embed_query", _fake_embed)
    monkeypatch.setattr(vs, "vector_candidates", lambda *a, **k: [])
    provider = FakeEmbeddingProvider()
    ctx = _ctx(
        engine=object(),
        embed_provider=provider,
        settings=_settings(retrieval_mode="vector"),
    )
    result = _call("search_recipes", {"query": "soup"}, ctx)
    assert result["ok"] is True
    assert result["mode_ran"] == "vector"


# --- get_recipe --------------------------------------------------------------


def test_get_recipe_exact_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    import culinary_copilot.recipes.repository as repo

    monkeypatch.setattr(
        repo,
        "get_recipe",
        lambda eng, sid, dataset_id=None: {"title": "T", "dataset_id": dataset_id},
    )
    ctx = _ctx(engine=object())
    ok = _call(
        "get_recipe",
        {"dataset_id": "odunola/foodie", "source_id": "abc"},
        ctx,
    )
    assert ok["ok"] is True
    missing = _call("get_recipe", {"dataset_id": "nope/none", "source_id": "abc"}, ctx)
    assert missing["ok"] is False


# --- Epicure variants ---------------------------------------------------------


class _FakeCore:
    def __init__(
        self,
        *,
        enabled: bool = True,
        pairs: list[tuple[str, float]] | None = None,
        fail: str | None = None,
    ) -> None:
        self.settings = _settings(epicure_enabled=enabled)
        self._pairs = pairs if pairs is not None else [("pork", 0.5), ("beef", 0.4)]
        self._fail = fail

    def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
        if self._fail == "unknown":
            from culinary_copilot.tools.epicure import UnknownIngredientError

            raise UnknownIngredientError(ingredient)
        if self._fail == "boom":
            raise OSError("missing asset")
        from culinary_copilot.tools.epicure import Pairing

        return [Pairing(ingredient=n, score=s) for n, s in self._pairs[:k]]


def test_epicure_variants_return_typed_results() -> None:
    ctx = _ctx(epicure_core=_FakeCore(), epicure_cooc=_FakeCore(), epicure_chem=_FakeCore())
    for name in (
        "find_balanced_pairings",
        "find_conventional_pairings",
        "find_flavor_pairings",
    ):
        result = _call(name, {"ingredient": "chicken", "k": 2}, ctx)
        assert result["ok"] is True, name
        assert len(result["pairings"]) == 2


def test_epicure_disabled_or_missing_is_not_configured() -> None:
    ctx = _ctx(
        epicure_core=_FakeCore(enabled=False),
        epicure_cooc=None,
        epicure_chem=_FakeCore(fail="boom"),
    )
    for name in (
        "find_balanced_pairings",
        "find_conventional_pairings",
        "find_flavor_pairings",
    ):
        result = _call(name, {"ingredient": "chicken"}, ctx)
        assert result["ok"] is False, name
        assert result["error_type"] == "unavailable", name
        assert result["reason"] == REASON_TOOL_NOT_CONFIGURED, name
        assert next_action_for(result["reason"]) == "contact_operator", name


def test_epicure_cache_only_never_downloads(tmp_path: Any) -> None:
    """Empty HF_HOME + guarded hf_hub_download: every Epicure tool errors.

    The guard fails if called without ``local_files_only=True``; with an
    empty cache dir every load then misses, so all four Epicure tools
    must return ``tool_not_configured`` with no network call possible.
    """
    import culinary_copilot.tools.epicure as epicure_mod
    from culinary_copilot.tools.epicure_tools import build_epicure_variants

    calls: list[dict[str, Any]] = []
    real = epicure_mod.hf_hub_download

    def _guarded(
        *,
        repo_id: str,
        revision: str,
        filename: str,
        local_files_only: bool = False,
        **kwargs: Any,
    ) -> str:
        calls.append({"local_files_only": local_files_only, "filename": filename})
        assert local_files_only is True, "tool path must be cache-only"
        from huggingface_hub.errors import LocalEntryNotFoundError

        raise LocalEntryNotFoundError(f"empty test cache: {filename}")

    epicure_mod.hf_hub_download = _guarded  # type: ignore[method-assign]
    try:
        settings = _settings(
            epicure_enabled=True,
            hf_home=str(tmp_path / "empty-hf"),
            epicure_cooc_revision="03edd311adde6e39a2eb6f9f3fa78f7396be6b53",
            epicure_chem_revision="2461ef3fbafab36d2b1111187a3df98721146861",
        )
        variants = build_epicure_variants(settings)
        ctx = _ctx(
            settings=settings,
            epicure_core=variants["core"],
            epicure_cooc=variants["cooc"],
            epicure_chem=variants["chem"],
        )
        for name in (
            "find_balanced_pairings",
            "find_conventional_pairings",
            "find_flavor_pairings",
            "find_substitutions",
        ):
            result = _call(name, {"ingredient": "chicken"}, ctx)
            assert result["ok"] is False, name
            assert result["reason"] == REASON_TOOL_NOT_CONFIGURED, name
    finally:
        epicure_mod.hf_hub_download = real
    assert calls, "expected cache-only load attempts"
    assert all(c["local_files_only"] is True for c in calls)


def test_epicure_unknown_ingredient_is_invalid_arguments() -> None:
    ctx = _ctx(epicure_core=_FakeCore(fail="unknown"))
    result = _call("find_balanced_pairings", {"ingredient": "unobtainium"}, ctx)
    assert result["ok"] is False
    assert result["error_type"] == "invalid_arguments"


def test_find_substitutions_labelled_unverified_no_dietary_claim() -> None:
    ctx = _ctx(epicure_core=_FakeCore())
    result = _call("find_substitutions", {"ingredient": "butter"}, ctx)
    assert result["ok"] is True
    assert result["candidates"]
    for cand in result["candidates"]:
        assert cand["verification"] == "unverified"
    blob = (result.get("disclaimer", "") + str(result["candidates"])).lower()
    assert "unverified" in blob
    assert "unknown" in blob
    # Disclaimer states no claim is made; candidates must not assert safety.
    assert "no dietary claim" in result.get("disclaimer", "").lower()
    for banned in ("safe for", "compatible with", "certified", "allergy-safe"):
        assert banned not in blob


# --- scale / convert ------------------------------------------------------------


def _doc(*, servings: Any, ingredients: list[dict[str, Any]]) -> dict[str, Any]:
    return {"servings": servings, "ingredients": ingredients}


def test_scale_supported_unknown_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import culinary_copilot.recipes.repository as repo

    doc = _doc(
        servings=4.0,
        ingredients=[
            {"canonical": "flour", "amount": "2", "unit": "cup", "quantity_text": "2 cup"},
            {"canonical": "salt", "amount": None, "unit": None, "quantity_text": None},
            {"canonical": "water", "amount": "1", "unit": None, "quantity_text": "1"},
        ],
    )
    monkeypatch.setattr(repo, "get_recipe", lambda *a, **k: doc)
    ctx = _ctx(engine=object())
    result = _call(
        "scale_recipe",
        {"dataset_id": "odunola/foodie", "source_id": "x", "target_servings": 8},
        ctx,
    )
    assert result["ok"] is True
    assert result["factor"] == 2.0
    assert len(result["scaled"]) == 1
    assert result["scaled"][0]["ingredient"] == "flour"
    assert result["scaled"][0]["scaled_amount_float"] == pytest.approx(4.0)
    assert result["scaled"][0]["approximate"] is False
    assert len(result["unknown_quantities"]) == 2  # never scaled, never dropped


def test_scale_qualitative_units_flagged_approximate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import culinary_copilot.recipes.repository as repo

    doc = _doc(
        servings=2.0,
        ingredients=[
            {"canonical": "salt", "amount": "1", "unit": "pinch", "quantity_text": "1 pinch"},
            {"canonical": "eggs", "amount": "2", "unit": "count", "quantity_text": "2"},
        ],
    )
    monkeypatch.setattr(repo, "get_recipe", lambda *a, **k: doc)
    ctx = _ctx(engine=object())
    result = _call(
        "scale_recipe",
        {"dataset_id": "odunola/foodie", "source_id": "x", "target_servings": 4},
        ctx,
    )
    assert result["ok"] is True
    by_name = {s["ingredient"]: s for s in result["scaled"]}
    assert by_name["salt"]["approximate"] is True
    assert by_name["salt"]["scaled_amount_float"] == pytest.approx(2.0)
    assert by_name["eggs"]["approximate"] is False


def test_scale_missing_servings_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import culinary_copilot.recipes.repository as repo

    monkeypatch.setattr(repo, "get_recipe", lambda *a, **k: _doc(servings=None, ingredients=[]))
    ctx = _ctx(engine=object())
    result = _call(
        "scale_recipe",
        {"dataset_id": "odunola/foodie", "source_id": "x", "target_servings": 4},
        ctx,
    )
    assert result["ok"] is False
    assert result["reason"] == REASON_SCALE_MISSING_SERVINGS


def test_convert_units_ok_and_refusals() -> None:
    ctx = _ctx()
    ok = _call("convert_units", {"amount": 1, "from_unit": "cup", "to_unit": "ml"}, ctx)
    assert ok["ok"] is True
    assert ok["amount"] == pytest.approx(236.588, rel=1e-3)
    assert ok["unit"] == "ml"
    assert ok["unit_system"] == "metric"
    us = _call("convert_units", {"amount": 1000, "from_unit": "ml", "to_unit": "cup"}, ctx)
    assert us["ok"] is True
    assert us["unit_system"] == "us_customary"
    mass = _call("convert_units", {"amount": 1000, "from_unit": "g", "to_unit": "kg"}, ctx)
    assert mass["ok"] is True and mass["amount"] == pytest.approx(1.0)
    assert mass["unit_system"] == "metric"
    bad = _call("convert_units", {"amount": 1, "from_unit": "bathtub", "to_unit": "g"}, ctx)
    assert bad["ok"] is False
    assert bad["reason"] == REASON_CONVERT_UNSUPPORTED_UNIT
    cross = _call("convert_units", {"amount": 1, "from_unit": "g", "to_unit": "cup"}, ctx)
    assert cross["ok"] is False
    assert cross["reason"] == REASON_CONVERT_UNSUPPORTED_UNIT


def test_convert_units_count_never_converts() -> None:
    ctx = _ctx()
    same = _call("convert_units", {"amount": 3, "from_unit": "eggs", "to_unit": "eggs"}, ctx)
    # Unknown bare words are not units at all: refused, not converted.
    assert same["ok"] is False
    assert same["reason"] == REASON_CONVERT_UNSUPPORTED_UNIT
    out = _call("convert_units", {"amount": 2, "from_unit": "count", "to_unit": "g"}, ctx)
    assert out["ok"] is False
    assert out["reason"] == REASON_CONVERT_UNSUPPORTED_UNIT
    back = _call("convert_units", {"amount": 2, "from_unit": "g", "to_unit": "count"}, ctx)
    assert back["ok"] is False
    assert back["reason"] == REASON_CONVERT_UNSUPPORTED_UNIT


# --- stubs ----------------------------------------------------------------------


def test_search_techniques_without_engine_not_configured() -> None:
    result = _call("search_techniques", {"query": "braise"}, _ctx())
    assert result["ok"] is False
    assert result["error_type"] == "unavailable"
    assert result["reason"] == REASON_TOOL_NOT_CONFIGURED
    assert next_action_for(result["reason"]) == "contact_operator"


def test_search_techniques_mode_schema() -> None:
    from culinary_copilot.tools.technique_tools import (
        SearchTechniquesArgs,
        resolve_technique_mode,
    )

    assert SearchTechniquesArgs(query="braise").mode is None
    assert SearchTechniquesArgs(query="braise", mode="vector").mode == "vector"
    ctx = _ctx()
    assert resolve_technique_mode(None, ctx) == "fulltext"
    assert resolve_technique_mode("vector", ctx) == "vector"
    vec = _ctx(settings=_settings(technique_retrieval_mode="vector"))
    assert resolve_technique_mode(None, vec) == "vector"


def test_search_techniques_vector_without_provider_not_configured() -> None:
    result = _call("search_techniques", {"query": "braise", "mode": "vector"}, _ctx())
    assert result["ok"] is False
    assert result["reason"] == REASON_TOOL_NOT_CONFIGURED
    assert result["next_action"] == "contact_operator"


def test_search_techniques_fulltext_missing_tables_not_configured() -> None:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    result = _call("search_techniques", {"query": "braise"}, _ctx(engine=engine))
    assert result["ok"] is False
    assert result["reason"] == REASON_TOOL_NOT_CONFIGURED
    assert result["next_action"] == "contact_operator"


class _FakeSessionState:
    def __init__(self, allowed: bool) -> None:
        self.internet_search_allowed = allowed


class _FakeSessionStore:
    def __init__(self, allowed: bool) -> None:
        self._allowed = allowed
        self.events: list[dict[str, Any]] = []

    def get(self, session_id: str) -> Any:
        return _FakeSessionState(self._allowed)

    def append_event(self, session_id: str, event_type: str, payload: Any) -> Any:
        self.events.append({"session_id": session_id, "type": event_type, "payload": payload})
        return self.events[-1]


def test_search_web_denied_off_then_unavailable_on() -> None:
    denied = _call(
        "search_web",
        {"session_id": "ses-1", "query": "ramen"},
        _ctx(session_store=_FakeSessionStore(allowed=False)),
    )
    assert denied["ok"] is False
    assert denied["error_type"] == "permission_denied"
    assert denied["reason"] == REASON_TOOL_PERMISSION_DENIED
    allowed = _call(
        "search_web",
        {"session_id": "ses-1", "query": "ramen"},
        _ctx(session_store=_FakeSessionStore(allowed=True)),
    )
    assert allowed["ok"] is False
    assert allowed["error_type"] == "unavailable"
    assert allowed["reason"] == REASON_TOOL_NOT_CONFIGURED
    assert next_action_for(allowed["reason"]) == "contact_operator"


def test_tool_call_event_logged_with_digest_not_raw_args() -> None:
    store = _FakeSessionStore(allowed=False)
    ctx = _ctx(session_store=store)
    _run(
        run_tool(
            _defs()["search_web"],
            _impls()["search_web"],
            {"session_id": "ses-9", "query": "super-secret-query"},
            ctx,
            session_id="ses-9",
            call_id="call-test-1",
        )
    )
    assert len(store.events) == 1
    payload = store.events[0]["payload"]
    assert payload["tool"] == "search_web"
    assert payload["call_id"] == "call-test-1"
    assert payload["error_type"] == "permission_denied"
    assert "cost_class" in payload and "latency_ms" in payload
    assert "super-secret-query" not in str(payload)


# --- defaults unchanged ------------------------------------------------------------


def test_recommendations_path_still_fulltext() -> None:
    from pathlib import Path

    service = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "culinary_copilot"
        / "recommendations"
        / "service.py"
    ).read_text(encoding="utf-8")
    assert "retrieval_mode" not in service  # ADR step 4 not done: explicit full-text


def test_new_tool_reasons_all_mapped() -> None:
    assert next_action_for(REASON_TOOL_TIMEOUT) == "retry"
    assert next_action_for(REASON_TOOL_UNAVAILABLE) == "retry"
    assert next_action_for(REASON_TOOL_INVALID_ARGUMENTS) == "change_request"
    assert next_action_for(REASON_TOOL_PERMISSION_DENIED) == "change_request"
    assert next_action_for(REASON_SCALE_MISSING_SERVINGS) == "change_request"
    assert next_action_for(REASON_CONVERT_UNSUPPORTED_UNIT) == "change_request"
    assert next_action_for(REASON_TOOL_NOT_CONFIGURED) == "contact_operator"
    assert next_action_for(REASON_TOOL_INTERNAL_ERROR) == "contact_operator"


def test_escaping_exception_is_internal_error() -> None:
    async def _boom(args: Any, context: ToolContext) -> dict[str, Any]:
        raise RuntimeError("defect")

    from culinary_copilot.tools.search_tools import SearchRecipesArgs

    tool = ToolDefinition(
        name="search_recipes",
        description="boom fake",
        args_model=SearchRecipesArgs,
        timeout_s=10.0,
        idempotent=True,
        cost_class="free",
    )
    result = _run(run_tool(tool, _boom, {"query": "x"}, _ctx()))
    assert result["ok"] is False
    assert result["reason"] == REASON_TOOL_INTERNAL_ERROR
    assert result["next_action"] == "contact_operator"


def test_sync_impl_called_exactly_once() -> None:
    from culinary_copilot.tools.measure_tools import ConvertUnitsArgs

    calls = {"n": 0}

    def _sync(args: Any, context: ToolContext) -> dict[str, Any]:
        calls["n"] += 1
        return {"ok": True, "amount": 1.0, "unit": "g", "unit_system": "metric"}

    tool = ToolDefinition(
        name="convert_units",
        description="sync fake",
        args_model=ConvertUnitsArgs,
        timeout_s=10.0,
        idempotent=True,
        cost_class="free",
    )
    result = _run(run_tool(tool, _sync, {"amount": 1, "from_unit": "g", "to_unit": "g"}, _ctx()))
    assert result["ok"] is True
    assert calls["n"] == 1


def test_slow_sync_impl_times_out() -> None:
    import time as _time

    from culinary_copilot.tools.measure_tools import ConvertUnitsArgs

    def _slow(args: Any, context: ToolContext) -> dict[str, Any]:
        _time.sleep(5)
        return {"ok": True}

    tool = ToolDefinition(
        name="convert_units",
        description="slow sync fake",
        args_model=ConvertUnitsArgs,
        timeout_s=0.05,
        idempotent=True,
        cost_class="free",
    )
    ctx = _ctx(settings=_settings(tool_timeout_s=0.05))
    result = _run(run_tool(tool, _slow, {"amount": 1, "from_unit": "g", "to_unit": "g"}, ctx))
    assert result["ok"] is False
    assert result["error_type"] == "timeout"
    assert result["reason"] == REASON_TOOL_TIMEOUT


def _wraps_async(fn: Any) -> Any:
    """functools.wraps decorator preset (defined before use)."""
    import functools

    def _deco(g: Any) -> Any:
        return functools.wraps(fn)(g)

    return _deco


def test_async_detection_unwraps_and_callable_objects() -> None:
    import functools

    from culinary_copilot.tools.registry import _is_async_callable

    async def _a(args: Any, context: ToolContext) -> dict[str, Any]:
        return {"ok": True}

    def _s(args: Any, context: ToolContext) -> dict[str, Any]:
        return {"ok": True}

    @_wraps_async(_a)
    def _wrapped(args: Any, context: ToolContext) -> dict[str, Any]:
        return {"ok": True}

    class _AsyncCallable:
        async def __call__(self, args: Any, context: ToolContext) -> dict[str, Any]:
            return {"ok": True}

    class _SyncCallable:
        def __call__(self, args: Any, context: ToolContext) -> dict[str, Any]:
            return {"ok": True}

    assert _is_async_callable(_a) is True
    assert _is_async_callable(_s) is False
    assert _is_async_callable(_wrapped) is True
    assert _is_async_callable(_AsyncCallable()) is True
    assert _is_async_callable(_SyncCallable()) is False
    assert _is_async_callable(functools.partial(_a)) is True


def test_build_tool_context_reads_settings() -> None:
    s = _settings(tool_timeout_s=10.0)
    ctx = build_tool_context(settings=s, engine=None)
    assert ctx.settings.tool_timeout_s == 10.0
    # Disabled by default: no provider started (ADR step 2).
    assert ctx.embed_provider is None


def test_search_recipes_event_cost_follows_mode_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import culinary_copilot.recipes.repository as repo
    import culinary_copilot.recipes.vector_search as vs
    from culinary_copilot.embeddings import query as qmod

    monkeypatch.setattr(repo, "search_all", lambda *a, **k: [{"dataset_id": "d", "source_id": "s"}])

    async def _fake_embed(provider: Any, text: str, **kw: Any) -> list[float]:
        r = await provider.embed_texts([text])
        return r.vectors[0]

    monkeypatch.setattr(qmod, "embed_query", _fake_embed)
    monkeypatch.setattr(vs, "vector_candidates", lambda *a, **k: [])

    store = _FakeSessionStore(allowed=False)
    provider = FakeEmbeddingProvider()
    full_ctx = _ctx(engine=object(), embed_provider=provider, session_store=store)
    _run(
        run_tool(
            _defs()["search_recipes"],
            _impls()["search_recipes"],
            {"query": "soup"},
            full_ctx,
            session_id="ses-cost",
            call_id="call-fulltext",
        )
    )
    vec_ctx = _ctx(
        engine=object(),
        embed_provider=provider,
        session_store=store,
        settings=_settings(retrieval_mode="vector"),
    )
    _run(
        run_tool(
            _defs()["search_recipes"],
            _impls()["search_recipes"],
            {"query": "soup"},
            vec_ctx,
            session_id="ses-cost",
            call_id="call-vector",
        )
    )
    by_call = {e["payload"]["call_id"]: e["payload"] for e in store.events}
    assert by_call["call-fulltext"]["cost_class"] == "free"
    assert by_call["call-fulltext"]["mode_ran"] == "fulltext"
    assert by_call["call-vector"]["cost_class"] == "paid"
    assert by_call["call-vector"]["mode_ran"] == "vector"


def test_create_app_embed_provider_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from culinary_copilot.api.app import create_app

    # Disabled (default): startup succeeds, provider None, behavior unchanged.
    app = create_app(_settings())
    assert app.state.query_embed_provider is None

    # Enabled but the build fails: startup fails loudly instead of None.
    import culinary_copilot.tools.search_tools as st

    def _boom(settings: Any) -> Any:
        raise ValueError("embedding model/dimension mismatch")

    monkeypatch.setattr(st, "build_embed_provider", _boom)
    with pytest.raises(RuntimeError, match="query embedding provider"):
        create_app(_settings(embeddings_enabled=True))


def test_technique_summaries_and_hits_always_carry_attribution() -> None:
    from culinary_copilot.agent.loop import _summarize_result

    result = {
        "ok": True,
        "mode_ran": "fulltext",
        "results": [
            {
                "doc_id": "tech-sear-01",
                "chunk_id": 0,
                "section": "Searing",
                "title": "Searing",
                "url": "https://en.wikipedia.org/wiki/Searing",
                "licence": "CC-BY-SA-4.0",
                "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "attribution_text": '"Searing" — test attribution',
                "excerpt": "Sear chicken in a hot pan.",
            }
        ],
    }
    summary = _summarize_result("search_techniques", result)
    assert summary["results"][0]["attribution_text"]
    assert summary["results"][0]["licence_url"]


# --- strict function-calling schemas ---------------------------------------


def test_sent_tool_schemas_pass_strict_checker() -> None:
    from culinary_copilot.agent.loop import function_defs_for
    from culinary_copilot.tools.registry import strict_violations

    sent = function_defs_for(list(_defs().values()))
    assert len(sent) == 10
    for tool in sent:
        assert tool["strict"] is True, tool["name"]
        assert strict_violations(tool["parameters"]) == [], tool["name"]


def test_directive_response_schema_passes_strict_checker() -> None:
    from openai.lib._parsing._responses import type_to_text_format_param

    from culinary_copilot.agent.loop import AgentDirective
    from culinary_copilot.tools.registry import strict_violations

    converted = type_to_text_format_param(AgentDirective)
    assert converted["strict"] is True
    assert strict_violations(converted["schema"]) == []


def test_open_object_schemas_are_flagged() -> None:
    from pydantic import BaseModel

    from culinary_copilot.tools.registry import strict_violations

    class _Open(BaseModel):
        mapping: dict[str, str]

    violations = strict_violations(_Open.model_json_schema())
    assert any("mapping" in v and "additionalProperties" in v for v in violations)


def test_closed_source_model_dump_shape() -> None:
    from culinary_copilot.agent.loop import PlanPayload

    plan = PlanPayload.model_validate(
        {
            "source": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
            "mise_en_place": ["chop"],
            "steps": ["cook"],
            "plating": "bowls",
            "quantities": [],
            "adaptations": [],
        }
    )
    assert plan.model_dump()["source"] == {
        "dataset_id": "odunola/foodie",
        "source_id": "curry-1",
    }


def _echo_impl(seen: dict[str, Any]) -> Any:
    async def _echo(args: Any, context: ToolContext) -> dict[str, Any]:
        seen.update(args.model_dump())
        return {"ok": True}

    return _echo


def _echo_tool(name: str, args_model: Any) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="echo fake",
        args_model=args_model,
        timeout_s=10.0,
        idempotent=True,
        cost_class="free",
    )


def _run_echo(tool: ToolDefinition, seen: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    return _run(run_tool(tool, _echo_impl(seen), args, _ctx()))


def test_null_optional_arguments_fall_back_to_defaults() -> None:
    from culinary_copilot.tools.epicure_tools import PairingsArgs
    from culinary_copilot.tools.search_tools import SearchRecipesArgs
    from culinary_copilot.tools.technique_tools import SearchTechniquesArgs

    seen: dict[str, Any] = {}
    result = _run_echo(
        _echo_tool("search_recipes", SearchRecipesArgs),
        seen,
        {"query": "soup", "limit": None, "mode": None},
    )
    assert result["ok"] is True
    assert seen == {"query": "soup", "limit": 5, "mode": None}

    seen.clear()
    result = _run_echo(
        _echo_tool("search_techniques", SearchTechniquesArgs),
        seen,
        {"query": "sear", "limit": None, "mode": None},
    )
    assert result["ok"] is True
    assert seen == {"query": "sear", "limit": 5, "mode": None}

    seen.clear()
    result = _run_echo(
        _echo_tool("find_flavor_pairings", PairingsArgs),
        seen,
        {"ingredient": "chicken", "k": None},
    )
    assert result["ok"] is True
    assert seen["k"] == 5


def test_explicit_optional_values_still_apply() -> None:
    from culinary_copilot.tools.search_tools import SearchRecipesArgs

    seen: dict[str, Any] = {}
    result = _run_echo(
        _echo_tool("search_recipes", SearchRecipesArgs),
        seen,
        {"query": "soup", "limit": 3, "mode": "fulltext"},
    )
    assert result["ok"] is True
    assert seen == {"query": "soup", "limit": 3, "mode": "fulltext"}


def test_tool_call_event_opt_in_records_bounded_args() -> None:
    from culinary_copilot.tools.search_tools import SearchRecipesArgs

    seen: dict[str, Any] = {}
    tool = _echo_tool("search_recipes", SearchRecipesArgs)
    store = _FakeSessionStore(allowed=True)
    ctx = _ctx(session_store=store)
    ctx.record_tool_args = True
    result = _run(run_tool(tool, _echo_impl(seen), {"query": "soup"}, ctx, session_id="s"))
    assert result["ok"] is True
    payload = store.events[0]["payload"]
    assert payload["args_digest"] == args_digest({"query": "soup", "limit": 5, "mode": None})
    import json as _json

    assert _json.loads(str(payload["args"])) == {"query": "soup", "limit": 5, "mode": None}


def test_search_techniques_event_records_technique_identities_and_count() -> None:
    from culinary_copilot.tools.registry import ToolContext

    store = _FakeSessionStore(allowed=True)
    rows = [
        {"doc_id": "tech-egg-boil-18", "chunk_id": 0, "title": "Boiled egg"},
        {"doc_id": "tech-egg-boil-18", "chunk_id": 2, "title": "Boiled egg"},
        {"dataset_id": "odunola/foodie", "source_id": "x"},  # not a technique hit
    ]

    def _tech(args: Any, context: Any) -> dict[str, Any]:
        return {"ok": True, "mode_ran": "fulltext", "match": "all", "results": list(rows)}

    ctx = ToolContext(
        settings=_settings(),
        engine=None,
        session_store=store,
        impl_overrides={"search_techniques": _tech},
    )
    result = _run(
        run_tool(
            _defs()["search_techniques"],
            _impls()["search_techniques"],
            {"query": "eggs"},
            ctx,
            session_id="ses-tech",
            call_id="call-tech-1",
        )
    )
    assert result["ok"] is True
    payload = store.events[0]["payload"]
    assert payload["result_count"] == 3
    assert payload["returned_identities"] == [
        {"doc_id": "tech-egg-boil-18", "chunk_id": 0, "via": "technique"},
        {"doc_id": "tech-egg-boil-18", "chunk_id": 2, "via": "technique"},
    ]


def test_zero_hit_search_records_result_count_zero() -> None:
    from culinary_copilot.tools.registry import ToolContext

    store = _FakeSessionStore(allowed=True)

    def _empty(args: Any, context: Any) -> dict[str, Any]:
        return {"ok": True, "mode_ran": "fulltext", "match": "all", "results": []}

    for tool_name in ("search_recipes", "search_techniques"):
        ctx = ToolContext(
            settings=_settings(),
            engine=None,
            session_store=store,
            impl_overrides={tool_name: _empty},
        )
        result = _run(
            run_tool(
                _defs()[tool_name],
                _impls()[tool_name],
                {"query": "nothing matches this"},
                ctx,
                session_id="ses-zero",
                call_id=f"call-{tool_name}-0",
            )
        )
        assert result["ok"] is True
    payloads = [e["payload"] for e in store.events]
    assert len(payloads) == 2
    for payload in payloads:
        assert payload["outcome"] == "ok"
        assert payload["result_count"] == 0
        assert "returned_identities" not in payload


class _VocabCore:
    """Fake adapter with the real vocabulary contract (vocabulary hook
    plus UnknownIngredientError on a miss)."""

    def __init__(
        self,
        vocab: tuple[str, ...] = (
            "chicken",
            "lentil",
            "olive_oil",
            "dragon_fruit",
            "pork",
            "beef",
            "garlic",
            "onion",
        ),
        pairs: list[tuple[str, float]] | None = None,
    ) -> None:
        self.settings = _settings(epicure_enabled=True)
        self._names = list(vocab)
        self._pairs = pairs if pairs is not None else [("pork", 0.5), ("beef", 0.4)]
        self.seen: list[str] = []

    def vocabulary(self) -> list[str]:
        return list(self._names)

    def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
        from culinary_copilot.tools.epicure import UnknownIngredientError

        self.seen.append(ingredient)
        if ingredient not in self._names:
            raise UnknownIngredientError(ingredient)
        from culinary_copilot.tools.epicure import Pairing

        return [Pairing(ingredient=n, score=s) for n, s in self._pairs[:k]]


def _vocab_ctx() -> Any:
    core = _VocabCore()
    return _ctx(epicure_core=core, epicure_cooc=core, epicure_chem=core), core


def test_epicure_normalization_order_and_queried_as() -> None:
    ctx, core = _vocab_ctx()
    cases = {
        "lentils": "lentil",
        "red lentils": "lentil",
        "red_lentil": "lentil",
        "chicken breast": "chicken",
        "roast chicken": "chicken",
        "lentil": "lentil",
        "olive oil": "olive_oil",
        "extra virgin olive oil": "olive_oil",
    }
    for name in (
        "find_balanced_pairings",
        "find_conventional_pairings",
        "find_flavor_pairings",
        "find_substitutions",
    ):
        for raw, expected in cases.items():
            result = _call(name, {"ingredient": raw}, ctx)
            assert result["ok"] is True, (name, raw)
            assert result["requested"] == raw, (name, raw)
            assert result["queried_as"] == expected, (name, raw)
    assert set(core.seen) <= {"lentil", "chicken", "olive_oil"}


def test_epicure_miss_lists_suggestions() -> None:
    ctx, _ = _vocab_ctx()
    result = _call("find_balanced_pairings", {"ingredient": "chikcen"}, ctx)
    assert result["ok"] is False
    assert result["error_type"] == "invalid_arguments"
    assert result["reason"] == REASON_TOOL_INVALID_ARGUMENTS
    assert "chicken" in result["message"]
    assert "queried_as" not in result


def test_bounded_args_json_stays_valid_json() -> None:
    import json as _json

    from culinary_copilot.tools.registry import bounded_args_json

    small = {"query": "soup", "limit": 5}
    assert _json.loads(bounded_args_json(small)) == small
    big = {"query": "x" * 5000, "limit": 5}
    out = bounded_args_json(big)
    assert len(out) <= 2000
    parsed = _json.loads(out)
    assert parsed["_truncated"] is True
    assert parsed["limit"] == 5  # whole small keys survive
    assert _json.loads(bounded_args_json({"query": "y" * 5000})) == {"_truncated": True}
