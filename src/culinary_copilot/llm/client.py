"""Application-facing async LLM boundary for clarification planning.

Separate from the ingestion Batch pipeline (``recipes/llm_batch.py``):
different enablement (``LLM_ENABLED`` vs ``LLM_INGESTION_ENABLED``),
different prompts/schemas, and an interactive Responses API client instead
of file batches.

Official OpenAI behavior verified against current docs (2026-09-23):

- Structured outputs via the Responses API parsing helper:
  ``client.responses.parse(model=..., input=[...], text_format=Model)``
  returns ``response.output_parsed`` for the typed result; see
  https://developers.openai.com/api/docs/guides/structured-outputs
  (Python ``responses.parse`` + ``text_format`` + ``output_parsed``).
- Safety refusals are programmatically detectable (refusal items / None
  parsed output); incomplete responses carry ``status="incomplete"``.
- ``AsyncOpenAI(..., timeout=..., max_retries=...)`` owns SDK retries.

Retry ownership: the SDK is configured with ``max_retries=0`` and this
module performs the single bounded application retry loop, so SDK and
application retries cannot multiply. Only retryable transport failures
(timeout / connection / rate-limit / 5xx) are retried, at most
``llm_app_max_retries`` times. Authentication, bad-request, refusal,
incomplete, and schema failures are never retried.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from pydantic import BaseModel, Field

from culinary_copilot.config import Settings


class ProviderDisabledError(RuntimeError):
    pass


class ProviderAuthError(RuntimeError):
    pass


class ProviderRateLimitError(RuntimeError):
    pass


class ProviderTimeoutError(RuntimeError):
    pass


class ProviderUnavailableError(RuntimeError):
    pass


class ProviderRefusalError(RuntimeError):
    pass


class ProviderIncompleteError(RuntimeError):
    pass


class ProviderSchemaError(RuntimeError):
    pass


class ProviderOutcome(BaseModel):
    """Controlled result of one bounded planning call."""

    ok: bool = False
    parsed: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    model: str = ""
    latency_ms: int = 0
    attempts: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None


class ApplicationLlmProvider(Protocol):
    async def start(self) -> None: ...
    async def aclose(self) -> None: ...
    async def complete_planning(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> ProviderOutcome: ...


def truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


class FakeApplicationProvider:
    """Offline fake for tests. Scripted outcomes, records call count."""

    def __init__(self, script: list[dict[str, Any] | BaseException] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[dict[str, str]] = []
        self.started = False
        self.closed = False
        self.default_parsed: dict[str, Any] = {"state_updates": [], "questions": []}

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def complete_planning(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> ProviderOutcome:
        self.calls.append({"system": system, "user": user})
        if self.script:
            next_item = self.script.pop(0)
            if isinstance(next_item, BaseException):
                raise next_item
            parsed = dict(next_item)
            return ProviderOutcome(ok=True, parsed=parsed, model="fake", latency_ms=1, attempts=1)
        return ProviderOutcome(
            ok=True, parsed=dict(self.default_parsed), model="fake", latency_ms=1, attempts=1
        )


class OpenAIApplicationProvider:
    """AsyncOpenAI implementation using responses.parse + structured output."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None

    async def start(self) -> None:
        if self._client is not None:
            return
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ProviderUnavailableError("openai package is not installed") from exc
        key = self.settings.openai_api_key.get_secret_value()
        if not key:
            raise ProviderAuthError("OPENAI_API_KEY is not configured")
        # One retry owner: SDK retries disabled; the loop below retries.
        self._client = AsyncOpenAI(
            api_key=key,
            timeout=self.settings.llm_app_timeout_s,
            max_retries=0,
        )

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def complete_planning(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> ProviderOutcome:
        if not self.settings.llm_enabled:
            raise ProviderDisabledError("Application LLM is disabled (LLM_ENABLED=false)")
        await self.start()
        assert self._client is not None
        model = self.settings.llm_app_model
        max_output = self.settings.llm_app_max_output_tokens
        max_retries = max(0, self.settings.llm_app_max_retries)
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(max_retries + 1):
            attempts = attempt + 1
            started = time.perf_counter()
            try:
                response = await self._client.responses.parse(
                    model=model,
                    input=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    text_format=response_model,
                    max_output_tokens=max_output,
                )
            except Exception as exc:
                last_error = exc
                mapped = _map_sdk_error(exc)
                if mapped is not None and _retryable(mapped) and attempt < max_retries:
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                if mapped is not None:
                    raise mapped from exc
                raise ProviderUnavailableError(
                    f"provider call failed: {type(exc).__name__}"
                ) from exc
            latency_ms = int((time.perf_counter() - started) * 1000)
            usage = _extract_usage(response)
            parsed_model = getattr(response, "output_parsed", None)
            status = str(getattr(response, "status", "completed") or "completed")
            if status == "incomplete":
                raise ProviderIncompleteError("model response was incomplete")
            if parsed_model is None:
                refusal = _extract_refusal(response)
                if refusal:
                    raise ProviderRefusalError(f"model refused: {refusal[:200]}")
                raise ProviderSchemaError("model returned no structured output")
            try:
                parsed = parsed_model.model_dump()
            except AttributeError:
                parsed = dict(parsed_model)
            return ProviderOutcome(
                ok=True,
                parsed=parsed,
                model=model,
                latency_ms=latency_ms,
                attempts=attempts,
                input_tokens=usage[0],
                output_tokens=usage[1],
            )
        assert last_error is not None
        mapped = _map_sdk_error(last_error)
        if mapped is not None:
            raise mapped from last_error
        raise ProviderUnavailableError("provider call failed") from last_error


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, (ProviderTimeoutError, ProviderRateLimitError, ProviderUnavailableError))


def _map_sdk_error(exc: Exception) -> Exception | None:
    try:
        from openai import (
            APIConnectionError,
            APITimeoutError,
            AuthenticationError,
            InternalServerError,
            RateLimitError,
        )
    except ImportError:
        return None
    if isinstance(exc, AuthenticationError):
        return ProviderAuthError("authentication failed; check OPENAI_API_KEY")
    if isinstance(exc, RateLimitError):
        return ProviderRateLimitError("rate limited by provider")
    if isinstance(exc, APITimeoutError):
        return ProviderTimeoutError("provider request timed out")
    if isinstance(exc, APIConnectionError):
        return ProviderUnavailableError("provider connection failed")
    if isinstance(exc, InternalServerError):
        return ProviderUnavailableError("provider internal error")
    return None


def _extract_usage(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    try:
        in_tok = getattr(usage, "input_tokens", None)
        out_tok = getattr(usage, "output_tokens", None)
        # Unknown usage stays unknown (None), never reported as zero.
        return (
            int(in_tok) if in_tok is not None else None,
            int(out_tok) if out_tok is not None else None,
        )
    except (TypeError, ValueError):
        return None, None


def _extract_refusal(response: Any) -> str:
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "refusal":
            return str(getattr(item, "refusal", "") or "")
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                return str(getattr(content, "refusal", "") or "")
    return ""


class PlanningPrompt(BaseModel):
    system: str = Field(max_length=8000)
    user: str = Field(max_length=20000)
