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
- Native function calling (Responses API): a strict ``get_recipe`` function
  tool plus ``tool_choice={"type": "function", "name": "get_recipe"}``
  forces one allowlisted call; the returned ``function_call`` output item
  (``call_id``/``name``/JSON ``arguments``) is validated, dispatched, and
  answered with a ``function_call_output`` item carrying the same
  ``call_id`` before the final structured turn. Verified against installed
  SDK 3.16.2 types (``FunctionToolParam``, ``ToolChoiceFunctionParam``,
  ``ResponseFunctionToolCall`` with ``call_id``, ``FunctionCallOutput``
  accepted in follow-up input) and the official function-calling guide;
  never live-tested here.

Retry ownership: the SDK is configured with ``max_retries=0`` and this
module performs the single bounded application retry loop, so SDK and
application retries cannot multiply. Only retryable transport failures
(timeout / connection / rate-limit / 5xx) are retried, at most
``llm_app_max_retries`` times. Authentication, bad-request, refusal,
incomplete, and schema failures are never retried.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any, Protocol

from pydantic import BaseModel, Field

from culinary_copilot.config import Settings


class ProviderCallError(RuntimeError):
    """Base for provider-call failures with per-attempt metadata.

    Carries only safe operator metadata (attempt counts, per-attempt
    request_sent flags, SDK exception type, HTTP status, provider
    request/response ids, token counts, incomplete reason) — never
    prompts, secrets, or raw model output.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        attempt_details: list[dict[str, Any]] | None = None,
        request_sent: bool = False,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        incomplete_reason: str | None = None,
        response_id: str | None = None,
        error_message: str | None = None,
        error_code: str | None = None,
        error_param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.attempt_details = list(attempt_details or [])
        self.request_sent = request_sent
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.reasoning_tokens = reasoning_tokens
        self.incomplete_reason = incomplete_reason
        self.response_id = response_id
        # Provider error-body fields (4xx diagnostics). The raw message is
        # capped here; the service redacts secrets before persisting.
        self.error_message = error_message
        self.error_code = error_code
        self.error_param = error_param


class ProviderDisabledError(ProviderCallError):
    pass


class ProviderAuthError(ProviderCallError):
    pass


class ProviderRateLimitError(ProviderCallError):
    pass


class ProviderTimeoutError(ProviderCallError):
    pass


class ProviderUnavailableError(ProviderCallError):
    """Transport/server-side failure: only real SDK/transport exceptions."""


class ProviderInternalError(ProviderCallError):
    """Local failure before the request was sent or while processing the
    response (construction bugs, unexpected shapes): never billed as a
    provider call."""


class ProviderBadRequestError(ProviderCallError):
    """HTTP 400 from the provider (client/config error): never retried."""


class ProviderNotFoundError(ProviderCallError):
    """HTTP 404 from the provider (unknown model/endpoint): never retried."""


class ProviderRequestError(ProviderCallError):
    """Other provider 4xx (auth-adjacent/validation): never retried."""


class ProviderRefusalError(ProviderCallError):
    pass


class ProviderContentFilterError(ProviderCallError):
    """Provider content filter trip: never retried, distinct from refusal."""


class ProviderIncompleteError(ProviderCallError):
    pass


class ProviderSchemaError(ProviderCallError):
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
    reasoning_tokens: int | None = None
    reasoning_effort: str | None = None
    response_id: str | None = None
    attempt_details: list[dict[str, Any]] = Field(default_factory=list)


class ApplicationLlmProvider(Protocol):
    async def start(self) -> None: ...
    async def aclose(self) -> None: ...
    async def complete_planning(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> ProviderOutcome: ...
    async def complete_recommendation(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> ProviderOutcome: ...
    async def complete_native_tool_turn(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
        response_model: type[BaseModel] | None,
    ) -> NativeTurnResult: ...


GET_RECIPE_FUNCTION: dict[str, Any] = {
    "type": "function",
    "name": "get_recipe",
    "description": (
        "Fetch one complete source recipe already listed in the candidate "
        "metadata by its exact dataset_id and source_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string"},
            "source_id": {"type": "string"},
        },
        "required": ["dataset_id", "source_id"],
        "additionalProperties": False,
    },
    "strict": True,
}


class NativeToolCall(BaseModel):
    """One validated native function call from a provider turn."""

    call_id: str = ""
    name: str = ""
    arguments: str = ""


# Documented Responses API input-item fields (API reference, "Create a model
# response" → input item objects). Only non-"Populated when items are
# returned via API" fields may be sent back:
# - FunctionCall: arguments, call_id, name, type (+ id, optional, allowed).
#   `status` is output-populated — the server rejects it on input with
#   400 unknown_parameter (observed live on run5 LIVE-10 turn 2).
#   SDK-internal extras (async_, caller, namespace, parsed_arguments) are
#   undocumented for input and never sent.
# - Reasoning: id, summary, type, content?, encrypted_content?. `status`
#   is output-populated and excluded. encrypted_content is populated by
#   default and required for stateless/ZDR continuation.
# - FunctionCallOutput (built by the service, unchanged): type, call_id,
#   output (all core input fields).
CHAIN_FUNCTION_CALL_FIELDS = ("type", "id", "call_id", "name", "arguments")
CHAIN_REASONING_FIELDS = ("type", "id", "summary", "content", "encrypted_content")
CHAINED_ITEM_FIELDS: dict[str, tuple[str, ...]] = {
    "function_call": CHAIN_FUNCTION_CALL_FIELDS,
    "function_call_output": ("type", "id", "call_id", "output", "name"),
    "reasoning": CHAIN_REASONING_FIELDS,
}


def _chain_fields(item: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
    """Project one output item onto the documented input field set.

    Accepts SDK objects (via model_dump) or plain dicts; omits None values
    so optional fields (id, encrypted_content, content) travel only when
    present.
    """
    if isinstance(item, dict):
        raw: dict[str, Any] = item
    else:
        try:
            raw = item.model_dump()
        except AttributeError:
            raw = {key: getattr(item, key, None) for key in allowed}
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in allowed if raw.get(key) is not None}


def validate_chained_input(input_items: list[Any]) -> None:
    """Offline guard: chained items must use only documented input fields.

    Runs before send; violations fail as controlled ProviderInternalError
    with request_sent=false (nothing billable left the process).
    """
    for index, item in enumerate(input_items or []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        allowed = CHAINED_ITEM_FIELDS.get(str(item_type)) if item_type else None
        if allowed is None:
            continue
        unknown = sorted(k for k in item.keys() if k not in allowed)
        if unknown:
            raise ProviderInternalError(
                f"chained input item {index} ({item_type}) has unknown keys: {unknown}",
                attempts=0,
                attempt_details=[],
                request_sent=False,
            )


def serialized_request(
    *,
    input_items: list[Any],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | None = None,
    response_model: type[BaseModel] | None = None,
) -> str:
    """Serialize the model-visible parts of one Responses request.

    Covers everything that carries instructions, context, or schemas:
    ``input`` items (messages, reasoning, function_call, and
    function_call_output items), ``tools``, ``tool_choice``, and the
    ``text.format`` structured-output schema, converted with the same SDK
    helper ``responses.parse`` uses (openai 3.16.2). Scalar parameters
    (model, effort, output cap, timeout) are omitted.

    Units: callers compare ``len()`` of this string (code points) with the
    character budgets. Characters are NOT tokens: the live runner's cost
    reservation uses UTF-8 byte lengths as the conservative token upper
    bound. Reasoning items sent by id may be expanded server-side into
    reasoning tokens that no local serialization can see; the runner
    reserves for those separately.
    """
    from openai.lib._parsing._responses import type_to_text_format_param

    body: dict[str, Any] = {"input": input_items}
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    if response_model is not None:
        body["text"] = {"format": type_to_text_format_param(response_model)}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)


def request_size(**kwargs: Any) -> dict[str, int]:
    """Characters (budget unit) and UTF-8 bytes (token upper bound)."""
    text = serialized_request(**kwargs)
    return {"chars": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def _process_tool_response(
    response: Any,
    *,
    model: str,
    effort: str,
    latency_ms: int,
    attempts: int,
    attempt_log: list[dict[str, Any]],
) -> NativeTurnResult:
    """Post-network processing for one native turn (controlled errors only).

    The response was received, so every error raised here marks
    request_sent=True; unexpected local failures are converted by the
    caller into ProviderInternalError with the request marked sent.
    """
    usage = _extract_usage(response)
    response_id = getattr(response, "id", None)
    response_id_str = str(response_id) if response_id else None
    status = str(getattr(response, "status", "completed") or "completed")
    if status == "incomplete":
        reason = _incomplete_reason(response)
        attempt_log.append(
            {
                "attempt": attempts,
                "latency_ms": latency_ms,
                "request_sent": True,
                "incomplete_reason": reason,
                "response_id": response_id,
            }
        )
        raise ProviderIncompleteError(
            "model response was incomplete",
            attempts=attempts,
            attempt_details=list(attempt_log),
            request_sent=True,
            input_tokens=usage[0],
            output_tokens=usage[1],
            reasoning_tokens=usage[2],
            incomplete_reason=reason,
            response_id=response_id_str,
        )
    parsed_model = getattr(response, "output_parsed", None)
    tool_calls: list[NativeToolCall] = []
    chain_items: list[dict[str, Any]] = []
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            call_id = str(getattr(item, "call_id", "") or "")
            name = str(getattr(item, "name", "") or "")
            arguments = getattr(item, "arguments", "") or ""
            tool_calls.append(NativeToolCall(call_id=call_id, name=name, arguments=str(arguments)))
            # Explicit documented input fields only (never model_dump):
            # status/async_/caller/namespace/parsed_arguments are rejected
            # or undocumented for input (observed 400 unknown_parameter).
            chain_items.append(_chain_fields(item, CHAIN_FUNCTION_CALL_FIELDS))
        elif item_type == "reasoning":
            # Reasoning items must replay into the next turn so the
            # model continues its reasoning (official function-calling
            # guide); only function_call items become tool calls.
            # Documented fields only (id/summary/content/encrypted_content).
            chained = _chain_fields(item, CHAIN_REASONING_FIELDS)
            if chained:
                chain_items.append(chained)
    if not tool_calls and parsed_model is None:
        refusal = _extract_refusal(response)
        if refusal:
            raise ProviderRefusalError(
                f"model refused: {refusal[:200]}",
                attempts=attempts,
                attempt_details=list(attempt_log),
                request_sent=True,
                input_tokens=usage[0],
                output_tokens=usage[1],
                reasoning_tokens=usage[2],
                response_id=response_id_str,
            )
        raise ProviderSchemaError(
            "model returned no output",
            attempts=attempts,
            attempt_details=list(attempt_log),
            request_sent=True,
            input_tokens=usage[0],
            output_tokens=usage[1],
            reasoning_tokens=usage[2],
            response_id=response_id_str,
        )
    parsed: dict[str, Any] | None = None
    if parsed_model is not None:
        try:
            parsed = parsed_model.model_dump()
        except AttributeError:
            parsed = dict(parsed_model)
    return NativeTurnResult(
        tool_calls=tool_calls,
        parsed=parsed,
        chain_items=chain_items,
        model=model,
        latency_ms=latency_ms,
        attempts=attempts,
        input_tokens=usage[0],
        output_tokens=usage[1],
        reasoning_tokens=usage[2],
        reasoning_effort=effort,
        response_id=response_id_str,
        attempt_details=list(attempt_log),
    )


class NativeTurnResult(BaseModel):
    """Controlled result of one native tool turn.

    ``tool_calls`` holds validated function calls (empty for a pure
    structured turn); ``parsed`` holds the structured payload when the
    turn was parsed into ``response_model``; ``chain_items`` holds the
    output items needed to continue the conversation (function_call
    items to answer with ``function_call_output``).
    """

    tool_calls: list[NativeToolCall] = Field(default_factory=list)
    parsed: dict[str, Any] | None = None
    chain_items: list[dict[str, Any]] = Field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    attempts: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    reasoning_effort: str | None = None
    response_id: str | None = None
    attempt_details: list[dict[str, Any]] = Field(default_factory=list)


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
        self.calls: list[dict[str, Any]] = []
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

    async def complete_recommendation(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> ProviderOutcome:
        """Offline fake for recommendation selection calls.

        Shares the script queue with planning: script entries may be dicts
        (parsed selection payloads) or raised exceptions (timeout/refusal/
        incomplete mapped by the service). Records calls separately.
        """
        self.calls.append({"system": system, "user": user, "kind": "recommendation"})
        if self.script:
            next_item = self.script.pop(0)
            if isinstance(next_item, BaseException):
                raise next_item
            parsed = dict(next_item)
            return ProviderOutcome(ok=True, parsed=parsed, model="fake", latency_ms=1, attempts=1)
        return ProviderOutcome(
            ok=True, parsed=dict(self.default_parsed), model="fake", latency_ms=1, attempts=1
        )

    async def complete_native_tool_turn(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
        response_model: type[BaseModel] | None,
    ) -> NativeTurnResult:
        """Offline fake for a native tool turn.

        Script entries represent actual function-call/result envelopes:
        ``{"native_tool_calls": [{"call_id":..., "name":..., "arguments":...}]}``
        for a tool turn (with optional ``{"native_reasoning_items": [...]}``
        replayed before the calls, mirroring real reasoning-model output),
        ``{"native_parsed": {...}}`` for a structured turn, or a raised
        exception mapped by the service.
        """
        self.calls.append(
            {
                "input": str(input_items)[:2000],
                "tools": str([t.get("name") for t in tools or []]),
                "kind": "native_tool",
                # Exact provider input (deep copy) for continuation tests.
                "input_items": copy.deepcopy(input_items),
                "tool_defs": copy.deepcopy(tools),
                "tool_choice": copy.deepcopy(tool_choice),
                "response_model": response_model,
            }
        )
        if self.script:
            next_item = self.script.pop(0)
            if isinstance(next_item, BaseException):
                raise next_item
            payload = dict(next_item)
            if "native_tool_calls" in payload:
                calls = [
                    NativeToolCall(
                        call_id=str(c.get("call_id", "")),
                        name=str(c.get("name", "")),
                        arguments=str(c.get("arguments", "")),
                    )
                    for c in payload.get("native_tool_calls", [])
                ]
                chain = [dict(r) for r in payload.get("native_reasoning_items", []) or []]
                chain.extend(
                    [
                        {
                            "type": "function_call",
                            "call_id": c.call_id,
                            "name": c.name,
                            "arguments": c.arguments,
                        }
                        for c in calls
                    ]
                )
                return NativeTurnResult(
                    tool_calls=calls, chain_items=chain, model="fake", latency_ms=1, attempts=1
                )
            parsed = dict(payload.get("native_parsed", payload))
            return NativeTurnResult(parsed=parsed, model="fake", latency_ms=1, attempts=1)
        return NativeTurnResult(
            parsed=dict(self.default_parsed), model="fake", latency_ms=1, attempts=1
        )


class OpenAIApplicationProvider:
    """AsyncOpenAI implementation using responses.parse + structured output."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None
        self._loop: Any = None

    async def start(self) -> None:
        # Record the creating loop ONCE (never overwrite): the underlying
        # HTTP pool is bound to it, and re-recording would blind the guard.
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
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

    def _check_loop(self) -> None:
        """Refuse cross-loop use with a clear controlled error.

        The SDK HTTP pool is bound to the creating loop; reuse from another
        loop raises an opaque `RuntimeError: Event loop is closed` before
        send. Fail here instead — never bill ambiguity, never silently
        recreate clients (that would hide pool/exhaustion bugs).
        """
        if self._loop is None:
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            return
        if current is not self._loop:
            raise ProviderInternalError(
                "event loop mismatch: this provider's client was created on a "
                "different event loop (one loop per run; do not share one "
                "provider across TestClient portals or loops)",
                attempts=0,
                attempt_details=[],
                request_sent=False,
            )

    async def complete_planning(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> ProviderOutcome:
        if not self.settings.llm_enabled:
            raise ProviderDisabledError("Application LLM is disabled (LLM_ENABLED=false)")
        await self.start()
        assert self._client is not None
        self._check_loop()
        model = self.settings.llm_app_model
        max_output = self.settings.llm_app_max_output_tokens
        max_retries = max(0, self.settings.llm_app_max_retries)
        return await self._complete_structured(
            system=system,
            user=user,
            response_model=response_model,
            model=model,
            max_output=max_output,
            max_retries=max_retries,
        )

    async def complete_recommendation(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> ProviderOutcome:
        """Structured recommendation selection call.

        Separate enablement (``LLM_RECOMMENDATION_ENABLED``) and limits
        (``LLM_REC_*``) from clarification planning. Disabled generation
        fails before any network access. One retry owner: SDK retries
        disabled; only retryable transport failures retried.
        """
        if not self.settings.llm_recommendation_enabled:
            raise ProviderDisabledError(
                "Recommendation generation is disabled (LLM_RECOMMENDATION_ENABLED=false)"
            )
        await self.start()
        assert self._client is not None
        self._check_loop()
        model = self.settings.llm_rec_model
        max_output = self.settings.llm_rec_max_output_tokens
        max_retries = max(0, self.settings.llm_rec_max_retries)
        return await self._complete_structured(
            system=system,
            user=user,
            response_model=response_model,
            model=model,
            max_output=max_output,
            max_retries=max_retries,
            timeout=self.settings.llm_rec_timeout_s,
            effort=self.settings.llm_rec_reasoning_effort,
        )

    async def complete_native_tool_turn(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
        response_model: type[BaseModel] | None,
    ) -> NativeTurnResult:
        """One native function-calling turn over the Responses API.

        Same enablement, model, output cap, timeout, and retry ownership
        as ``complete_recommendation``. Function calls are extracted from
        ``response.output`` items of type ``function_call`` (``call_id``,
        ``name``, JSON ``arguments``); structured payloads come from
        ``output_parsed`` when ``response_model`` is set. Absent output,
        refusals, and incomplete statuses map to the same controlled
        errors as structured calls.
        """
        if not self.settings.llm_recommendation_enabled:
            raise ProviderDisabledError(
                "Recommendation generation is disabled (LLM_RECOMMENDATION_ENABLED=false)"
            )
        await self.start()
        assert self._client is not None
        self._check_loop()
        # Offline guard: chained items must use only documented input
        # fields (unknown keys fail here, pre-send and unbilled, instead of
        # surfacing as a paid 400 unknown_parameter).
        validate_chained_input(input_items)
        model = self.settings.llm_rec_model
        max_output = self.settings.llm_rec_max_output_tokens
        max_retries = max(0, self.settings.llm_rec_max_retries)
        timeout = self.settings.llm_rec_timeout_s
        effort = self.settings.llm_rec_reasoning_effort
        last_error: Exception | None = None
        attempts = 0
        attempt_log: list[dict[str, Any]] = []
        for attempt in range(max_retries + 1):
            attempts = attempt + 1
            started = time.perf_counter()
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "input": input_items,
                    "max_output_tokens": max_output,
                    "reasoning": {"effort": effort},
                    "timeout": timeout,
                }
                if tools:
                    kwargs["tools"] = tools
                if tool_choice:
                    kwargs["tool_choice"] = tool_choice
                if response_model is not None:
                    kwargs["text_format"] = response_model
                response = await self._client.responses.parse(**kwargs)
            except Exception as exc:
                last_error = exc
                mapped = _map_sdk_error(exc)
                latency_ms = int((time.perf_counter() - started) * 1000)
                sent = _sent_by_shape(exc)
                entry: dict[str, Any] = {
                    "attempt": attempts,
                    "latency_ms": latency_ms,
                    "request_sent": sent,
                    **_sdk_meta(exc),
                }
                if mapped is not None:
                    entry["mapped_error"] = type(mapped).__name__
                attempt_log.append(entry)
                if mapped is not None and _retryable(mapped) and attempt < max_retries:
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                if mapped is not None:
                    if isinstance(mapped, ProviderCallError):
                        mapped.attempts = attempts
                        mapped.attempt_details = list(attempt_log)
                        mapped.request_sent = mapped.request_sent or sent
                    raise mapped from exc
                raise _split_fallback(
                    exc,
                    attempts=attempts,
                    attempt_log=attempt_log,
                    request_sent=sent,
                ) from exc
            latency_ms = int((time.perf_counter() - started) * 1000)
            try:
                outcome = _process_tool_response(
                    response,
                    model=model,
                    effort=effort,
                    latency_ms=latency_ms,
                    attempts=attempts,
                    attempt_log=attempt_log,
                )
            except ProviderCallError as exc:
                exc.request_sent = True
                raise
            except Exception as exc:
                raise _split_fallback(
                    exc,
                    attempts=attempts,
                    attempt_log=attempt_log,
                    request_sent=True,
                ) from exc
            return outcome
        assert last_error is not None
        mapped = _map_sdk_error(last_error)
        if mapped is not None:
            if isinstance(mapped, ProviderCallError):
                mapped.attempts = attempts
                mapped.attempt_details = list(attempt_log)
                mapped.request_sent = mapped.request_sent or bool(
                    attempt_log and attempt_log[-1].get("request_sent")
                )
            raise mapped from last_error
        raise _split_fallback(
            last_error,
            attempts=attempts,
            attempt_log=attempt_log,
            request_sent=bool(attempt_log and attempt_log[-1].get("request_sent")),
        ) from last_error

    async def _complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_model: type[BaseModel],
        model: str,
        max_output: int,
        max_retries: int,
        timeout: float | None = None,
        effort: str | None = None,
    ) -> ProviderOutcome:
        last_error: Exception | None = None
        attempts = 0
        attempt_log: list[dict[str, Any]] = []
        for attempt in range(max_retries + 1):
            attempts = attempt + 1
            started = time.perf_counter()
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "input": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "text_format": response_model,
                    "max_output_tokens": max_output,
                }
                if effort is not None:
                    # Recommendation-only reasoning effort; the planning path
                    # omits this (server default) and is out of scope.
                    kwargs["reasoning"] = {"effort": effort}
                if timeout is not None:
                    kwargs["timeout"] = timeout
                response = await self._client.responses.parse(**kwargs)
            except Exception as exc:
                last_error = exc
                mapped = _map_sdk_error(exc)
                latency_ms = int((time.perf_counter() - started) * 1000)
                sent = _sent_by_shape(exc)
                entry: dict[str, Any] = {
                    "attempt": attempts,
                    "latency_ms": latency_ms,
                    "request_sent": sent,
                    **_sdk_meta(exc),
                }
                if mapped is not None:
                    entry["mapped_error"] = type(mapped).__name__
                attempt_log.append(entry)
                if mapped is not None and _retryable(mapped) and attempt < max_retries:
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                if mapped is not None:
                    if isinstance(mapped, ProviderCallError):
                        mapped.attempts = attempts
                        mapped.attempt_details = list(attempt_log)
                        mapped.request_sent = mapped.request_sent or sent
                    raise mapped from exc
                raise _split_fallback(
                    exc,
                    attempts=attempts,
                    attempt_log=attempt_log,
                    request_sent=sent,
                ) from exc
            latency_ms = int((time.perf_counter() - started) * 1000)
            try:
                outcome = _process_structured_response(
                    response,
                    model=model,
                    effort=effort,
                    latency_ms=latency_ms,
                    attempts=attempts,
                    attempt_log=attempt_log,
                )
            except ProviderCallError as exc:
                exc.request_sent = True
                raise
            except Exception as exc:
                raise _split_fallback(
                    exc,
                    attempts=attempts,
                    attempt_log=attempt_log,
                    request_sent=True,
                ) from exc
            return outcome
        assert last_error is not None
        mapped = _map_sdk_error(last_error)
        if mapped is not None:
            if isinstance(mapped, ProviderCallError):
                mapped.attempts = attempts
                mapped.attempt_details = list(attempt_log)
                mapped.request_sent = mapped.request_sent or bool(
                    attempt_log and attempt_log[-1].get("request_sent")
                )
            raise mapped from last_error
        raise _split_fallback(
            last_error,
            attempts=attempts,
            attempt_log=attempt_log,
            request_sent=bool(attempt_log and attempt_log[-1].get("request_sent")),
        ) from last_error


def _process_structured_response(
    response: Any,
    *,
    model: str,
    effort: str | None,
    latency_ms: int,
    attempts: int,
    attempt_log: list[dict[str, Any]],
) -> ProviderOutcome:
    """Post-network processing for one structured call (controlled errors).

    The response was received, so every error raised here marks
    request_sent=True; unexpected local failures are converted by the
    caller into ProviderInternalError with the request marked sent.
    """
    usage = _extract_usage(response)
    response_id = getattr(response, "id", None)
    response_id_str = str(response_id) if response_id else None
    parsed_model = getattr(response, "output_parsed", None)
    status = str(getattr(response, "status", "completed") or "completed")
    if status == "incomplete":
        reason = _incomplete_reason(response)
        attempt_log.append(
            {
                "attempt": attempts,
                "latency_ms": latency_ms,
                "request_sent": True,
                "incomplete_reason": reason,
                "response_id": response_id,
            }
        )
        raise ProviderIncompleteError(
            "model response was incomplete",
            attempts=attempts,
            attempt_details=list(attempt_log),
            request_sent=True,
            input_tokens=usage[0],
            output_tokens=usage[1],
            reasoning_tokens=usage[2],
            incomplete_reason=reason,
            response_id=response_id_str,
        )
    if parsed_model is None:
        refusal = _extract_refusal(response)
        if refusal:
            raise ProviderRefusalError(
                f"model refused: {refusal[:200]}",
                attempts=attempts,
                attempt_details=list(attempt_log),
                request_sent=True,
                input_tokens=usage[0],
                output_tokens=usage[1],
                reasoning_tokens=usage[2],
                response_id=response_id_str,
            )
        raise ProviderSchemaError(
            "model returned no structured output",
            attempts=attempts,
            attempt_details=list(attempt_log),
            request_sent=True,
            input_tokens=usage[0],
            output_tokens=usage[1],
            reasoning_tokens=usage[2],
            response_id=response_id_str,
        )
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
        reasoning_tokens=usage[2],
        reasoning_effort=effort,
        response_id=response_id_str,
        attempt_details=list(attempt_log),
    )


def _retryable(exc: Exception) -> bool:
    # Only transient transport failures are retried: timeouts, connection
    # failures, provider 5xx, and rate limits. Authentication, bad-request
    # and other 4xx, refusals, content filters, incomplete responses, and
    # schema failures are never retried.
    return isinstance(exc, (ProviderTimeoutError, ProviderRateLimitError, ProviderUnavailableError))


def _sdk_error_body(exc: Exception) -> dict[str, str | None]:
    """Extract message/code/param from an SDK error body (unredacted).

    Bodies look like {"error": {"message", "code", "param", "type"}};
    anything else yields Nones. Callers bound and redact before persisting.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            out: dict[str, str | None] = {}
            for key in ("message", "code", "param"):
                value = err.get(key)
                out[key] = str(value)[:500] if value is not None else None
            return out
    return {"message": None, "code": None, "param": None}


def _map_sdk_error(exc: Exception) -> Exception | None:
    try:
        from openai import (
            APIConnectionError,
            APIResponseValidationError,
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            ContentFilterFinishReasonError,
            InternalServerError,
            LengthFinishReasonError,
            NotFoundError,
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
    if isinstance(exc, BadRequestError):
        fields = _sdk_error_body(exc)
        return ProviderBadRequestError(
            f"provider rejected the request: {exc}",
            error_message=fields["message"],
            error_code=fields["code"],
            error_param=fields["param"],
        )
    if isinstance(exc, NotFoundError):
        fields = _sdk_error_body(exc)
        return ProviderNotFoundError(
            f"provider resource not found: {exc}",
            error_message=fields["message"],
            error_code=fields["code"],
            error_param=fields["param"],
        )
    if isinstance(exc, APIStatusError):
        # Remaining 4xx (permission/conflict/unprocessable/...) are client
        # errors, never retried and never collapsed into unavailable/503.
        if 400 <= exc.status_code < 500:
            fields = _sdk_error_body(exc)
            return ProviderRequestError(
                f"provider rejected the request (HTTP {exc.status_code}): {exc}",
                error_message=fields["message"],
                error_code=fields["code"],
                error_param=fields["param"],
            )
        return ProviderUnavailableError(f"provider error (HTTP {exc.status_code})")
    if isinstance(exc, LengthFinishReasonError):
        # Truncated structured output raised at parse time: completion usage
        # (prompt/completion tokens) is carried when present.
        usage = getattr(exc, "completion", None)
        usage = getattr(usage, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", None)
        out_tok = getattr(usage, "completion_tokens", None)
        return ProviderIncompleteError(
            "model output hit the length limit during structured parsing",
            input_tokens=in_tok,
            output_tokens=out_tok,
            incomplete_reason="length_limit",
        )
    if isinstance(exc, ContentFilterFinishReasonError):
        return ProviderContentFilterError("provider content filter ended the response")
    if isinstance(exc, APIResponseValidationError):
        return ProviderSchemaError(f"provider response failed SDK validation: {exc}")
    return None


def _sdk_meta(exc: Exception) -> dict[str, Any]:
    """Safe per-attempt metadata: exception type, HTTP status, request id."""
    meta: dict[str, Any] = {"sdk_error": type(exc).__name__}
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        meta["http_status"] = status
    request_id = getattr(exc, "request_id", None)
    if request_id:
        meta["request_id"] = str(request_id)
    return meta


def _is_sdk_error(exc: Exception) -> bool:
    """True only for real SDK-side exceptions (transport/server/API)."""
    try:
        from openai import OpenAIError
    except ImportError:
        return False
    return isinstance(exc, OpenAIError)


def _sent_by_shape(exc: Exception) -> bool:
    """Whether an SDK exception proves a request reached the provider.

    Only errors that arise from an actual HTTP exchange count (transport,
    timeouts, HTTP statuses, completions attached). Anything else —
    including local construction bugs — counts as unsent unless a
    response was already received.
    """
    try:
        from openai import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            ContentFilterFinishReasonError,
            LengthFinishReasonError,
            RateLimitError,
        )
    except ImportError:
        return False
    return isinstance(
        exc,
        (
            APIConnectionError,
            APITimeoutError,
            APIStatusError,
            AuthenticationError,
            RateLimitError,
            LengthFinishReasonError,
            ContentFilterFinishReasonError,
        ),
    )
    """True only for real SDK-side exceptions (transport/server/API)."""
    try:
        from openai import OpenAIError
    except ImportError:
        return False
    return isinstance(exc, OpenAIError)


def _split_fallback(
    exc: Exception,
    *,
    attempts: int,
    attempt_log: list[dict[str, Any]],
    request_sent: bool,
) -> ProviderCallError:
    """Classify an unmapped exception: SDK/transport failures become
    ProviderUnavailableError; anything raised locally (construction bugs,
    unexpected response shapes) becomes ProviderInternalError, marked
    provider_reached=false unless a request was actually sent.
    """
    if _is_sdk_error(exc):
        return ProviderUnavailableError(
            f"provider call failed: {type(exc).__name__}",
            attempts=attempts,
            attempt_details=list(attempt_log),
            request_sent=request_sent,
        )
    return ProviderInternalError(
        f"local provider-path failure: {type(exc).__name__}: {exc}",
        attempts=attempts,
        attempt_details=list(attempt_log),
        request_sent=request_sent,
    )


def _incomplete_reason(response: Any) -> str | None:
    details = getattr(response, "incomplete_details", None)
    reason = getattr(details, "reason", None) if details is not None else None
    return str(reason) if reason else None


def _extract_usage(response: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None, None
    try:
        in_tok = getattr(usage, "input_tokens", None)
        out_tok = getattr(usage, "output_tokens", None)
        details = getattr(usage, "output_tokens_details", None)
        reason_tok = getattr(details, "reasoning_tokens", None)
        # Unknown usage stays unknown (None), never reported as zero.
        return (
            int(in_tok) if in_tok is not None else None,
            int(out_tok) if out_tok is not None else None,
            int(reason_tok) if reason_tok is not None else None,
        )
    except (TypeError, ValueError):
        return None, None, None


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
