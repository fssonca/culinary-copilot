from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import MutableHeaders
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from culinary_copilot.api.clarification import build_router as build_clarification_router
from culinary_copilot.api.recommendations import build_router as build_recommendations_router
from culinary_copilot.api.retrieval import build_router as build_retrieval_router
from culinary_copilot.api.sessions import build_router as build_sessions_router
from culinary_copilot.config import Settings
from culinary_copilot.db import check_database, create_db_engine
from culinary_copilot.llm.client import OpenAIApplicationProvider
from culinary_copilot.recipes.repository import (
    SUPPORTED_DATASETS,
    get_recipe,
    search_all,
    search_recipes,
)
from culinary_copilot.tools.epicure import (
    EpicureCore,
    EpicureDisabledError,
    Pairing,
    UnknownIngredientError,
)

_UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)
_WEB_DIR = Path(__file__).resolve().parents[1] / "web"


class _ui_security_headers:
    """Security headers for the /ui static responses only (never /api).

    Pure ASGI middleware: it adds no tasks of its own, so streaming
    disconnect behaviour (cancellation, telemetry reason) is identical
    with or without it. FastAPI's ``@app.middleware("http")`` wrapper
    (BaseHTTPMiddleware) is deliberately not used here: its task-group
    teardown can deliver a bare cancellation that reaches a streaming
    workflow before the endpoint's own messaged cancel, degrading the
    telemetry reason to bare ``"cancelled"``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if path != "/ui" and not path.startswith("/ui/"):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers["Content-Security-Policy"] = _UI_CSP
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                # Revalidate on every load (2026-10-06): without it the
                # browser kept heuristically cached ES modules after a
                # UI change. Unchanged files still come back as a 304.
                headers["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _checked_dataset_id(dataset_id: str | None) -> str | None:
    if dataset_id is None:
        return None
    if not dataset_id.strip() or dataset_id not in SUPPORTED_DATASETS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported dataset_id; expected one of {sorted(SUPPORTED_DATASETS)}",
        )
    return dataset_id


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_db_engine(settings)
    epicure = EpicureCore(settings)
    from culinary_copilot.services.store import InMemoryClarificationStore

    clarification_store = InMemoryClarificationStore()
    llm_provider = OpenAIApplicationProvider(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.llm_enabled:
            try:
                await llm_provider.start()
            except Exception:
                pass
        try:
            yield
        finally:
            try:
                await llm_provider.aclose()
            except Exception:
                pass
            engine.dispose()

    app = FastAPI(title="Culinary Copilot", version="0.1.0", lifespan=lifespan)
    app.state.clarification_store = clarification_store
    app.state.llm_provider = llm_provider
    from culinary_copilot.services.session_store import PostgresSessionStore

    session_store = PostgresSessionStore(engine)
    app.state.session_store = session_store
    app.include_router(build_sessions_router(store=session_store, settings=settings))
    app.include_router(
        build_clarification_router(
            settings=settings,
            store=clarification_store,
            provider=llm_provider,
            engine=engine,
            epicure=epicure,
        )
    )
    # ADR 0001 step 2: the query embedding provider starts only when
    # EMBEDDINGS_ENABLED is set, with a model/dimension check. With
    # defaults (disabled, fulltext) this is None and behavior is unchanged.
    # When enabled but the provider cannot be built, fail startup loudly
    # instead of silently serving vector-unavailable.
    from culinary_copilot.tools.search_tools import build_embed_provider

    if bool(getattr(settings, "embeddings_enabled", False)):
        try:
            query_embed_provider = build_embed_provider(settings)
        except Exception as exc:
            raise RuntimeError(
                f"EMBEDDINGS_ENABLED=true but the query embedding provider failed to start: {exc}"
            ) from exc
        if query_embed_provider is None:
            raise RuntimeError("EMBEDDINGS_ENABLED=true but no query embedding provider was built")
    else:
        query_embed_provider = None
    app.state.query_embed_provider = query_embed_provider
    app.include_router(
        build_retrieval_router(
            store=clarification_store,
            engine=engine,
            settings=settings,
            embed_provider=query_embed_provider,
        )
    )
    # Bounded agent loop (Milestone 3, Phase 3): answers, select, SSE
    # stream over the typed tool registry. No agent loop runs unless a
    # client posts to the stream endpoint.
    from culinary_copilot.api.agent import build_router as build_agent_router

    app.include_router(
        build_agent_router(
            settings=settings,
            engine=engine,
            session_store=session_store,
            provider=llm_provider,
            embed_provider=query_embed_provider,
        )
    )
    # Recommendation-stage Epicure uses cached assets only (Stage B wiring);
    # Stage A tests inject the fake adapter directly at the service layer.
    from culinary_copilot.recommendations.epicure import CachedEpicureAdapter

    recommendation_epicure: Any = CachedEpicureAdapter(epicure)
    app.include_router(
        build_recommendations_router(
            store=clarification_store,
            engine=engine,
            settings=settings,
            provider=llm_provider,
            epicure=recommendation_epicure,
        )
    )

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/ui/", status_code=307)

    app.add_middleware(_ui_security_headers)
    if _WEB_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> dict[str, str]:
        try:
            check_database(engine)
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Database unavailable") from None
        return {"status": "ready"}

    @app.get("/api/v1/pairings", response_model=list[Pairing], tags=["tools"])
    def pairings(
        ingredient: str = Query(min_length=1, max_length=200),
        k: int = Query(default=5, ge=1, le=20),
    ) -> list[Pairing]:
        try:
            return epicure.find_balanced_pairings(ingredient, k)
        except EpicureDisabledError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except UnknownIngredientError:
            raise HTTPException(status_code=422, detail="Unknown canonical ingredient") from None
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(status_code=503, detail="Epicure model unavailable") from None

    @app.get("/api/v1/recipes", tags=["recipes"])
    def recipes(
        q: str = Query(min_length=1, max_length=500),
        ingredient: list[str] = Query(default=[]),
        max_minutes: float | None = Query(default=None, gt=0),
        limit: int = Query(default=5, ge=1, le=50),
        dataset_id: str | None = Query(
            default=None,
            max_length=200,
            description=(
                "Optional dataset filter. When omitted, search covers all "
                f"supported datasets: {sorted(SUPPORTED_DATASETS)}. "
                "Canonical identity is (dataset_id, source_id)."
            ),
        ),
    ) -> list[dict[str, Any]]:
        checked = _checked_dataset_id(dataset_id)
        try:
            if checked is None:
                return search_all(
                    engine, q, ingredients=ingredient, max_minutes=max_minutes, limit=limit
                )
            return search_recipes(
                engine,
                q,
                ingredients=ingredient,
                max_minutes=max_minutes,
                limit=limit,
                dataset_id=checked,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Recipe corpus unavailable") from None

    @app.get("/api/v1/recipes/{source_id}", tags=["recipes"])
    def recipe(
        source_id: str,
        dataset_id: str | None = Query(
            default=None,
            max_length=200,
            description=(
                "Optional dataset qualifier. With dataset_id, lookup matches "
                "the exact (dataset_id, source_id) pair and never falls back. "
                "Without it, legacy Food.com-first lookup is preserved. "
                "New clients should supply dataset_id."
            ),
        ),
    ) -> dict[str, Any]:
        checked = _checked_dataset_id(dataset_id)
        try:
            result = get_recipe(engine, source_id, dataset_id=checked)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Recipe corpus unavailable") from None
        if result is None:
            raise HTTPException(status_code=404, detail="Recipe not found")
        return result

    return app
