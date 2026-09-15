from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.config import Settings
from culinary_copilot.db import check_database, create_db_engine
from culinary_copilot.tools.epicure import (
    EpicureCore,
    EpicureDisabledError,
    Pairing,
    UnknownIngredientError,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_db_engine(settings)
    epicure = EpicureCore(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="Culinary Copilot", version="0.1.0", lifespan=lifespan)

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

    return app
