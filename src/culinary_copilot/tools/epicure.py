"""Minimal cosine-neighbor adapter for Epicure Core's published data files.

Loads data only, without executing the model repository's Python code.
"""

import json
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np
from huggingface_hub import hf_hub_download
from pydantic import BaseModel
from safetensors.numpy import load_file

from culinary_copilot.config import Settings


class EpicureDisabledError(Exception):
    pass


class UnknownIngredientError(Exception):
    pass


class Pairing(BaseModel):
    ingredient: str
    score: float


class EpicureCore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = Lock()
        self._vectors: Any = None
        self._vocab: dict[str, int] = {}
        self._names: list[str] = []

    def _load(self) -> None:
        if self._vectors is not None:
            return
        with self._lock:
            if self._vectors is not None:
                return

            def download(filename: str) -> str:
                return hf_hub_download(
                    repo_id=self.settings.epicure_model_id,
                    revision=self.settings.epicure_revision,
                    filename=filename,
                    token=self.settings.hf_token.get_secret_value() or False,
                    cache_dir=str(Path(self.settings.hf_home) / "hub"),
                )

            vocab = json.loads(Path(download("vocab.json")).read_text())
            vectors = load_file(download("embeddings.safetensors"))["embeddings"]
            if sorted(vocab.values()) != list(range(len(vectors))):
                raise ValueError("Epicure vocabulary does not match embeddings")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if not np.isfinite(vectors).all() or np.any(norms == 0):
                raise ValueError("Epicure contains invalid embeddings")
            self._vocab = vocab
            self._names = sorted(vocab, key=vocab.__getitem__)
            self._vectors = vectors / norms

    def find_balanced_pairings(self, ingredient: str, k: int = 5) -> list[Pairing]:
        if not self.settings.epicure_enabled:
            raise EpicureDisabledError(
                "Set EPICURE_ENABLED=true to use local ingredient embeddings"
            )
        if not 1 <= k <= 20:
            raise ValueError("k must be between 1 and 20")
        self._load()
        seed = "_".join(ingredient.strip().lower().split())
        if seed not in self._vocab:
            raise UnknownIngredientError(seed)
        index = self._vocab[seed]
        scores = self._vectors @ self._vectors[index]
        scores[index] = -np.inf
        indices = np.argsort(-scores, kind="stable")[: min(k, len(scores) - 1)]
        return [Pairing(ingredient=self._names[i], score=float(scores[i])) for i in indices]
