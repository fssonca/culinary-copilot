from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.api.clarification import build_router as build_clarification_router
from culinary_copilot.api.retrieval import build_router as build_retrieval_router
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
    app.include_router(
        build_clarification_router(
            settings=settings,
            store=clarification_store,
            provider=llm_provider,
            engine=engine,
            epicure=epicure,
        )
    )
    app.include_router(build_retrieval_router(store=clarification_store, engine=engine))

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
