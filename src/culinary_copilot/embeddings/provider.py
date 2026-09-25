"""Embedding provider boundary: corpus and query vectors share one model.

``batch`` means multiple inputs in one synchronous ``/v1/embeddings``
request — not the asynchronous Batch API. Each request carries per-input
and aggregate token limits, per-index response mapping, one usage block,
bounded timeouts/retries, and dimension/numeric validation.

Query embedding runs async and outside the synchronous SQL repository.
Full-text mode must make zero provider calls. Corpus and query vectors
must use the same model and dimension; mismatches are refused before
any network access. When embeddings are unavailable the caller gets a
controlled failure (``EmbeddingUnavailableError``) and must either fail
closed or fall back to full-text with that fallback explicitly disclosed
— never silently.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from culinary_copilot.embeddings.registry import embedding_spec


class EmbeddingUnavailableError(Exception):
    """Embeddings cannot be produced (disabled, unconfigured, timeout)."""


class EmbeddingValidationError(Exception):
    """Provider returned unusable vectors (wrong dims, non-finite)."""


@dataclass(frozen=True)
class EmbeddingUsage:
    prompt_tokens: int = 0
    total_tokens: int = 0


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    dimension: int
    usage: EmbeddingUsage = field(default_factory=EmbeddingUsage)


class EmbeddingProvider(Protocol):
    """Async boundary for embedding ``texts`` in order."""

    model: str
    dimension: int

    async def embed_texts(self, texts: list[str]) -> EmbeddingResult: ...


def validate_vectors(vectors: list[list[float]], *, expected_dimension: int, model: str) -> None:
    """Raise ``EmbeddingValidationError`` on dims/shape/numeric violations."""
    if not vectors:
        raise EmbeddingValidationError("empty embedding response")
    for vector in vectors:
        if len(vector) != expected_dimension:
            raise EmbeddingValidationError(
                f"{model}: expected dimension {expected_dimension}, got {len(vector)}"
            )
        for value in vector:
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise EmbeddingValidationError(f"{model}: non-finite embedding value")


class FakeEmbeddingProvider:
    """Deterministic offline provider: no network, fixed dims, call log.

    Vectors are derived from the text hash so distinct texts differ and
    identical texts repeat exactly. Never measures semantic relevance.
    """

    def __init__(self, *, model: str = "text-embedding-3-small", dimension: int = 1536) -> None:
        spec = embedding_spec(model)
        if spec is None or spec.dimension != dimension:
            raise ValueError(
                f"Fake provider requires a registry model/dim pair, got {model}/{dimension}"
            )
        self.model = model
        self.dimension = dimension
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            raise EmbeddingValidationError("empty embedding request")
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            seed = abs(hash((self.model, text))) % (2**32)
            vector = [(((seed >> (i % 24)) & 0xFF) / 255.0 - 0.5) for i in range(self.dimension)]
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        validate_vectors(vectors, expected_dimension=self.dimension, model=self.model)
        usage = EmbeddingUsage(
            prompt_tokens=sum(len(t.encode("utf-8")) + 8 for t in texts),
            total_tokens=sum(len(t.encode("utf-8")) + 8 for t in texts),
        )
        return EmbeddingResult(
            vectors=vectors, model=self.model, dimension=self.dimension, usage=usage
        )


class DisabledEmbeddingProvider:
    """Fails closed: records the attempt, makes zero calls."""

    def __init__(self, *, model: str = "text-embedding-3-small", dimension: int = 1536) -> None:
        self.model = model
        self.dimension = dimension
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        raise EmbeddingUnavailableError("embeddings disabled/unconfigured")


class OpenAIEmbeddingProvider:
    """Live ``/v1/embeddings`` provider behind explicit enablement.

    Implemented (not executed in Phase 5): retries only timeout/connection/
    rate-limit/5xx up to ``max_retries``; 400s, empty inputs, and schema
    failures never retried. Response vectors map by their ``index`` field
    and are validated before return. ``client_factory`` allows mocked
    responses in tests without network access.
    """

    def __init__(
        self,
        *,
        model: str = "text-embedding-3-small",
        dimension: int = 1536,
        api_key: str = "",
        timeout_s: float = 20.0,
        max_retries: int = 1,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        check_model_dimension(model, dimension)
        if not api_key and client_factory is None:
            raise EmbeddingUnavailableError("live embeddings need OPENAI_API_KEY")
        self.model = model
        self.dimension = dimension
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.client_factory = client_factory
        self.calls: list[list[str]] = []
        self.attempts = 0

    def _client(self) -> Any:
        if self.client_factory is not None:
            return self.client_factory()
        from openai import OpenAI

        return OpenAI(api_key=self.api_key, timeout=self.timeout_s, max_retries=0)

    async def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        if not texts or any(not t.strip() for t in texts):
            raise EmbeddingValidationError("empty embedding input")
        self.calls.append(list(texts))
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.attempts += 1
            try:
                client = self._client()
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.embeddings.create,
                        model=self.model,
                        input=texts,
                        encoding_format="float",
                    ),
                    timeout=self.timeout_s,
                )
                return _response_to_result(response, model=self.model, dimension=self.dimension)
            except EmbeddingValidationError:
                raise
            except Exception as exc:
                last_error = exc
                if not _retryable(exc) or attempt >= self.max_retries:
                    raise EmbeddingUnavailableError(
                        f"embedding request failed: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(0.2 * (attempt + 1))
        raise EmbeddingUnavailableError("embedding request failed") from last_error


def _retryable(exc: Exception) -> bool:
    name = type(exc).__name__
    text = f"{name}: {exc}"
    if "BadRequest" in name or "Validation" in name or "Authentication" in name:
        return False
    return (
        any(
            token in text
            for token in (
                "Timeout",
                "timeout",
                "Connection",
                "RateLimit",
                "500",
                "502",
                "503",
                "529",
            )
        )
        or "Timeout" in name
        or "Connection" in name
        or "RateLimit" in name
        or "Internal" in name
    )


def _response_to_result(response: Any, *, model: str, dimension: int) -> EmbeddingResult:
    """Map a ``/v1/embeddings`` response by ``index``; validate everything."""
    data = getattr(response, "data", None)
    if not data:
        raise EmbeddingValidationError("empty embedding response")
    ordered: list[list[float] | None] = [None] * len(data)
    for item in data:
        index = int(getattr(item, "index", -1))
        if not 0 <= index < len(data):
            raise EmbeddingValidationError("embedding index out of range")
        if ordered[index] is not None:
            raise EmbeddingValidationError("duplicate embedding index")
        ordered[index] = [float(v) for v in item.embedding]
    if any(v is None for v in ordered):
        raise EmbeddingValidationError("missing embedding index")
    vectors = [list(v) for v in ordered if v is not None]
    validate_vectors(vectors, expected_dimension=dimension, model=model)
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or prompt_tokens)
    return EmbeddingResult(
        vectors=vectors,
        model=model,
        dimension=dimension,
        usage=EmbeddingUsage(prompt_tokens=prompt_tokens, total_tokens=total_tokens),
    )


def check_model_dimension(model: str, dimension: int) -> None:
    """Refuse query/corpus model/dimension mismatches before any call."""
    spec = embedding_spec(model)
    if spec is None:
        raise EmbeddingUnavailableError(f"unsupported embedding model {model!r}")
    if spec.dimension != dimension:
        raise EmbeddingUnavailableError(
            f"embedding dimension mismatch for {model}: "
            f"configured {dimension}, registry {spec.dimension}"
        )


def to_pgvector_literal(vector: list[float]) -> str:
    """Render a pgvector input literal; validated finite beforehand."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def result_to_dict(result: EmbeddingResult) -> dict[str, Any]:
    """Structured usage payload for ledgers and telemetry (counts only)."""
    return {
        "model": result.model,
        "dimension": result.dimension,
        "count": len(result.vectors),
        "prompt_tokens": result.usage.prompt_tokens,
        "total_tokens": result.usage.total_tokens,
    }
