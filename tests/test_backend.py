from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from culinary_copilot.api.app import create_app
from culinary_copilot.config import Settings
from culinary_copilot.domain.requests import CookingRequest
from culinary_copilot.tools.epicure import EpicureCore, EpicureDisabledError, UnknownIngredientError


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_health_and_disabled_model_do_not_download():
    with patch("culinary_copilot.tools.epicure.hf_hub_download") as download:
        with TestClient(create_app(settings(epicure_enabled=False))) as client:
            assert client.get("/health/live").json() == {"status": "ok"}
            assert client.get("/api/v1/pairings?ingredient=chicken").status_code == 503
            assert client.get("/api/v1/pairings?ingredient=chicken&k=0").status_code == 422
        download.assert_not_called()


def test_readiness_failure_does_not_leak_credentials():
    with patch("culinary_copilot.api.app.check_database") as check:
        with TestClient(create_app(settings())) as client:
            assert client.get("/health/ready").status_code == 200
            check.side_effect = OperationalError("secret connection details", {}, Exception())
            response = client.get("/health/ready")
            assert response.status_code == 503
            assert response.json() == {"detail": "Database unavailable"}


def test_request_schema():
    assert CookingRequest(ingredients=[" tomato "], portions=2).ingredients == ["tomato"]
    assert CookingRequest().portions is None
    for invalid in [{"portions": 0}, {"ingredients": [" "]}, {"time_minutes": -1}, {"typo": 1}]:
        with pytest.raises(ValidationError):
            CookingRequest(**invalid)


def test_cosine_neighbors_load_once_and_exclude_seed(tmp_path):
    from safetensors.numpy import save_file

    vocab = tmp_path / "vocab.json"
    vocab.write_text('{"olive_oil": 0, "tomato": 1, "water": 2}')
    tensors = tmp_path / "embeddings.safetensors"
    save_file(
        {"embeddings": np.array([[2.0, 0.0], [3.0, 4.0], [0.0, 2.0]], dtype=np.float32)}, tensors
    )
    model = EpicureCore(settings(epicure_enabled=True))
    with patch(
        "culinary_copilot.tools.epicure.hf_hub_download", side_effect=[str(vocab), str(tensors)]
    ) as download:
        result = model.find_balanced_pairings(" Olive Oil ", 20)
        assert [p.ingredient for p in result] == ["tomato", "water"]
        assert result[0].score == pytest.approx(0.6)
        model.find_balanced_pairings("tomato")
        assert download.call_count == 2
        with pytest.raises(UnknownIngredientError):
            model.find_balanced_pairings("unknown")


def test_disabled_direct_tool_and_invalid_k():
    with pytest.raises(EpicureDisabledError):
        EpicureCore(settings(epicure_enabled=False)).find_balanced_pairings("chicken")
    with pytest.raises(ValueError):
        EpicureCore(settings(epicure_enabled=True)).find_balanced_pairings("chicken", 0)


def test_model_errors_have_controlled_responses():
    with TestClient(create_app(settings(epicure_enabled=True))) as client:
        with patch.object(
            EpicureCore, "find_balanced_pairings", side_effect=UnknownIngredientError()
        ):
            assert client.get("/api/v1/pairings?ingredient=unknown").status_code == 422
        with patch.object(
            EpicureCore, "find_balanced_pairings", side_effect=OSError("private path")
        ):
            response = client.get("/api/v1/pairings?ingredient=chicken")
            assert response.status_code == 503
            assert response.json()["detail"] == "Epicure model unavailable"
