"""Typed tool registry (Milestone 3, Phase 2, reviewed).

Each tool has a name, pydantic argument and result schemas
(``extra="forbid"``), a timeout from ``Settings.tool_timeout_s``
(Checkpoint 0: 10 s per tool, server-set, never caller-set), an
idempotency flag, and a cost class (free / paid / network).

``run_tool`` validates arguments, enforces the timeout, and returns
typed error results (``timeout`` / ``invalid_arguments`` /
``unavailable`` / ``permission_denied``) with a stable ``reason`` and
``next_action`` — never an exception into the caller. Sync vs async is
decided before calling (``inspect.iscoroutinefunction``, unwrapping
``__wrapped__`` and callable-object ``__call__``): async
implementations run once under ``wait_for``; sync ones run once via
``to_thread`` under ``wait_for``. A timed-out thread is abandoned, not
killed: it keeps running in the background and its late result is
discarded, so implementations must be side-effect free or idempotent
(or the tool marked non-idempotent). An async implementation is
cancelled instead (``CancelledError`` thrown into the coroutine):
the client-side HTTP connection is torn down, but the server may
already be executing — billing stays unknown, so dispatched paid
calls are kept as spent (see the ledger wrappers), never $0. Every call records a structured
event (session id, call id, tool, args digest, outcome/error, latency,
cost); with a session id the event is appended to ``session_events``
via ``PostgresSessionStore.append_event``, otherwise it goes to the
logger. Raw arguments are never written to the process log, only a
sha256 digest of the canonical JSON; the ``session_events`` copy also
keeps the digest only, unless the caller opts in with
``record_tool_args`` (bounded validated args for reviewable raw
trajectories). The event cost is the mode that actually ran when the
result carries a valid ``cost_class`` (e.g. ``search_recipes``
fulltext=free vs vector=paid); otherwise the tool's static class.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ValidationError

from culinary_copilot.domain.recommendations import (
    REASON_TOOL_INVALID_ARGUMENTS,
    REASON_TOOL_TIMEOUT,
    next_action_for,
)
from culinary_copilot.services.store import new_id

logger = logging.getLogger(__name__)

CostClass = Literal["free", "paid", "network"]

TOOL_CALL_EVENT_TYPE = "tool_call"


def args_digest(args: dict[str, Any]) -> str:
    """sha256 of canonical JSON (sorted keys, compact separators)."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


#: Review-trajectory args bound (P3-L-10): whole top-level keys while
#: they fit, so the stored copy stays valid JSON (a string slice can
#: break ``json.loads`` downstream in the evidence digest).
_BOUNDED_ARGS_LIMIT = 2000


def bounded_args_json(args: dict[str, Any], limit: int = _BOUNDED_ARGS_LIMIT) -> str:
    """Valid-JSON bounded args: whole keys in sorted order plus a flag.

    Keeps top-level keys (sorted, deterministic) while the serialized
    preview plus a ``_truncated`` flag fits; drops the last-kept key
    while it does not. The result always parses: ``{"_truncated":
    true}`` when no key fits, the full dump when everything fits.
    """
    try:
        full = json.dumps(args, default=str)
    except (TypeError, ValueError):
        return json.dumps({"_unserializable": True, "_truncated": True})
    if len(full) <= limit:
        return full
    kept: dict[str, Any] = {}
    for key in sorted(args):
        kept[key] = args[key]
        kept["_truncated"] = True
        try:
            text = json.dumps(kept, default=str)
        except (TypeError, ValueError):
            del kept[key]
            continue
        if len(text) > limit:
            del kept[key]
    kept["_truncated"] = True
    try:
        text = json.dumps(kept, default=str)
    except (TypeError, ValueError):
        return json.dumps({"_unserializable": True, "_truncated": True})
    if len(text) > limit:
        return json.dumps({"_truncated": True})
    return text


def _strict_nullable(node: dict[str, Any]) -> dict[str, Any]:
    """Wrap an optional property schema so explicit null is accepted."""
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and any(
        isinstance(b, dict) and b.get("type") == "null" for b in any_of
    ):
        return node
    return {"anyOf": [node, {"type": "null"}]}


def _strict_node(node: Any) -> Any:
    """Recursively convert one JSON Schema node to OpenAI strict mode.

    Strict function-calling requires, for every object: all properties
    listed in ``required``, ``additionalProperties: false``, and no
    ``default`` keys anywhere. Pydantic's ``model_json_schema()`` keeps
    defaults and leaves optional fields out of ``required``, so the
    provider rejects those schemas with a 400 — hence this transform
    (the pinned SDK's own strict helper keeps ``default`` keys, so it
    cannot be used here).
    """
    if isinstance(node, list):
        return [_strict_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {k: _strict_node(v) for k, v in node.items() if k != "default"}
    if "properties" in out and isinstance(out["properties"], dict):
        props = out["properties"]
        originally_required = set(node.get("required", []) or [])
        out["properties"] = {
            name: (_strict_nullable(sub) if name not in originally_required else sub)
            for name, sub in props.items()
        }
        out["required"] = sorted(props.keys())
        out["additionalProperties"] = False
    return out


def strict_parameters_schema(args_model: type[BaseModel]) -> dict[str, Any]:
    """``args_model``'s JSON Schema converted for strict function calling.

    Every property becomes required; properties that were optional in
    the Pydantic model become nullable so the model may pass explicit
    null (the registry drops nulls before validation, restoring the
    Pydantic defaults — the argument models stay unchanged).     ``$defs``
    are converted in place and ``$ref`` links kept.
    """
    converted: dict[str, Any] = _strict_node(args_model.model_json_schema())
    return converted


def strict_violations(schema: Any, path: str = "$") -> list[str]:
    """Strict-mode violations in a parameter schema (empty means clean).

    Checks the same rules the provider enforces: no ``default`` keys,
    and every object with ``properties`` lists all of them in
    ``required`` with ``additionalProperties: false``. Used by tests
    and the live runner's offline self-check; never sent anywhere.
    """
    found: list[str] = []
    if isinstance(schema, list):
        for i, item in enumerate(schema):
            found.extend(strict_violations(item, f"{path}[{i}]"))
        return found
    if not isinstance(schema, dict):
        return found
    if "default" in schema:
        found.append(f"{path}: 'default' not allowed in strict mode")
    props = schema.get("properties")
    declared = schema.get("type")
    is_object = (
        declared == "object"
        or (isinstance(declared, list) and "object" in declared)
        or isinstance(props, dict)
        or "additionalProperties" in schema
    )
    if is_object:
        # Strict mode allows no open objects: additionalProperties must
        # be exactly false — a schema there (e.g. dict[str, str]) is a
        # 400, not just a missing flag.
        if schema.get("additionalProperties") is not False:
            found.append(f"{path}: additionalProperties must be false")
    if isinstance(props, dict):
        required = schema.get("required")
        if not isinstance(required, list) or sorted(required) != sorted(props.keys()):
            found.append(f"{path}: required must list all properties")
        for name, sub in props.items():
            found.extend(strict_violations(sub, f"{path}.{name}"))
    for key in ("items", "additionalProperties", "contains"):
        if isinstance(schema.get(key), (dict, list)):
            found.extend(strict_violations(schema[key], f"{path}.{key}"))
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        branches = schema.get(key)
        if isinstance(branches, list):
            for i, branch in enumerate(branches):
                found.extend(strict_violations(branch, f"{path}.{key}[{i}]"))
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        for name, sub in defs.items():
            found.extend(strict_violations(sub, f"{path}.$defs.{name}"))
    return found


@dataclass(frozen=True)
class ToolDefinition:
    """Static tool metadata (schemas live on the arg/result models)."""

    name: str
    description: str
    args_model: type[BaseModel]
    timeout_s: float
    idempotent: bool
    cost_class: CostClass


@dataclass
class ToolContext:
    """Runtime dependencies injected per tool call (fakes in tests)."""

    settings: Any = None
    engine: Any = None
    session_store: Any = None
    embed_provider: Any = None
    epicure_core: Any = None
    epicure_cooc: Any = None
    epicure_chem: Any = None
    # Server-bound session id for permission-gated tools (Phase 5, part 2,
    # owner item 3): set per run by the agent loop / API layer from the URL
    # path, never from model arguments. A spoofed id is impossible by
    # construction: search_web's args model has no session field
    # (extra="forbid" rejects it) and the impl reads only this value.
    bound_session_id: str | None = None
    # Registry call id for this invocation (Phase 5 review fix 3):
    # run_tool sets a per-call copy before invoking the impl, so
    # parallel calls never share it. Impls use it for their own
    # events (slot claims, search events) to correlate with the
    # registry's tool_call event; absent (direct impl calls) they
    # fall back to uuid4.
    call_id: str | None = None
    # Bounded search sub-request provider (Phase 5, part 2): any object
    # with ``async complete_web_search(*, instruction, query,
    # max_output_tokens)``. None means unconfigured (tool_not_configured).
    search_provider: Any = None
    # In-run search limits for live evaluations (2026-10-03 overrun
    # fix): a SearchRunLimits-like object with ``max_per_session`` and
    # ``check_and_claim()``. search_web takes min(code limit, flag) for
    # the slot claim and refuses dispatches past the run/campaign
    # bounds. None means pre-limit behavior (unit tests, API path).
    search_limits: Any = None
    # Pairs whose full get_recipe outputs are still in the current
    # run's capped history (set per turn by the agent loop; None means
    # unknown, e.g. direct impl calls). get_recipe returns its short
    # duplicate pointer only for pairs in this set: a repeat the model
    # can no longer see comes back full. run_tool copies it per call.
    visible_full_recipes: set[tuple[str, str]] | None = None
    # Test hook: wrap the raw implementation (e.g. inject a sleeping fake).
    impl_overrides: dict[str, Callable[..., Any]] = field(default_factory=dict)
    # Review hook: also store the bounded validated args in the session
    # event (reviewable raw trajectories). Off by default: raw arguments
    # stay out of events and logs unless the caller opts in.
    record_tool_args: bool = False


# Per-tool timeout overrides by settings key (2026-10-03 search
# proposal): search_web reads SEARCH_WEB_TIMEOUT_S, every other tool
# reads TOOL_TIMEOUT_S. The literal tool name avoids importing the
# tool module here (it imports this registry).
_TOOL_TIMEOUT_SETTINGS = {"search_web": "search_web_timeout_s"}


def tool_timeout_s(tool_name: str, settings: Any, default: float = 10.0) -> float:
    """Effective timeout for one tool call (single source of truth).

    Precedence for a tool with a dedicated setting (currently
    ``search_web`` -> ``SEARCH_WEB_TIMEOUT_S``, owner decision
    2026-10-03):
    1. the dedicated setting when explicitly set (constructor kwarg
       or environment; pydantic ``model_fields_set``);
    2. ``TOOL_TIMEOUT_S`` when explicitly set (the owner deliberately
       retunes every tool, search_web included);
    3. the dedicated setting's code default (30 s for search_web:
       applies with no env var at all);
    4. the passed default (10 s; tools without a dedicated setting
       resolve here via ``TOOL_TIMEOUT_S`` first).
    Without the explicitness checks the dedicated default would either
    shadow a deliberately set general timeout or be shadowed by the
    general default. Used both by ``run_tool`` (via ``_timeout_for``)
    and by implementations that must bound their own sub-requests at
    most at the tool timeout.
    """

    def _positive(key: str) -> float | None:
        try:
            value = float(getattr(settings, key))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    override_key = _TOOL_TIMEOUT_SETTINGS.get(str(tool_name))
    if override_key is not None and settings is not None and hasattr(settings, override_key):
        fields_set = getattr(settings, "model_fields_set", None)

        def _explicit(key: str) -> bool:
            return (key in fields_set) if fields_set is not None else True

        if _explicit(override_key):
            value = _positive(override_key)
            if value is not None:
                return value
        if hasattr(settings, "tool_timeout_s") and _explicit("tool_timeout_s"):
            value = _positive("tool_timeout_s")
            if value is not None:
                return value
        value = _positive(override_key)
        if value is not None:
            return value
    if settings is not None and hasattr(settings, "tool_timeout_s"):
        value = _positive("tool_timeout_s")
        if value is not None:
            return value
    return float(default)


def _timeout_for(context: ToolContext, tool: ToolDefinition) -> float:
    return tool_timeout_s(tool.name, getattr(context, "settings", None), float(tool.timeout_s))


def _as_opt_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _is_async_callable(fn: Callable[..., Any]) -> bool:
    """True when calling ``fn`` returns a coroutine (decided before calling).

    Unwraps ``functools.wraps`` chains (``__wrapped__``) and checks a
    callable object's ``__call__`` so wrapped functions and callable
    objects are classified correctly.
    """
    if inspect.iscoroutinefunction(fn):
        return True
    unwrapped: Any = fn
    seen = 0
    while seen < 10:
        nxt = getattr(unwrapped, "__wrapped__", None)
        if nxt is None:
            break
        unwrapped = nxt
        seen += 1
        if inspect.iscoroutinefunction(unwrapped):
            return True
    if not inspect.isfunction(unwrapped) and not inspect.ismethod(unwrapped):
        call = getattr(unwrapped, "__call__", None)
        if call is not None and inspect.iscoroutinefunction(call):
            return True
    return False


def _event_cost(tool: ToolDefinition, result: dict[str, Any]) -> CostClass:
    """Cost of the mode that actually ran (result override wins)."""
    raw = result.get("cost_class")
    if raw == "free":
        return "free"
    if raw == "paid":
        return "paid"
    if raw == "network":
        return "network"
    return tool.cost_class


def _returned_identities(
    tool_name: str, parsed: Any, result: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Retrieval identities a successful call establishes (P3-A-01).

    Read from one place (the registry) so every implementation — real,
    fake, or override — records the same evidence. ``search_recipes``
    rows establish dataset-qualified identity only (``via: search``);
    ``get_recipe`` establishes the full document (``via: full``);
    ``search_techniques`` rows establish technique identity
    (``via: technique``). Failed lookups (``ok`` False) establish
    nothing. Bounded to 20.
    """
    if not result.get("ok"):
        return None
    if tool_name == "search_recipes":
        rows = result.get("results")
        if not isinstance(rows, list):
            return None
        out: list[dict[str, Any]] = []
        for row in rows[:20]:
            if not isinstance(row, dict):
                continue
            dataset_id, source_id = row.get("dataset_id"), row.get("source_id")
            if isinstance(dataset_id, str) and isinstance(source_id, str):
                out.append({"dataset_id": dataset_id, "source_id": source_id, "via": "search"})
        return out or None
    if tool_name == "get_recipe":
        dataset_id = getattr(parsed, "dataset_id", None)
        source_id = getattr(parsed, "source_id", None)
        if isinstance(dataset_id, str) and isinstance(source_id, str):
            return [{"dataset_id": dataset_id, "source_id": source_id, "via": "full"}]
        return None
    if tool_name == "search_techniques":
        rows = result.get("results")
        if not isinstance(rows, list):
            return None
        out = []
        for row in rows[:20]:
            if not isinstance(row, dict):
                continue
            doc_id, chunk_id = row.get("doc_id"), row.get("chunk_id")
            if (
                isinstance(doc_id, str)
                and isinstance(chunk_id, int)
                and not isinstance(chunk_id, bool)
            ):
                out.append({"doc_id": doc_id, "chunk_id": chunk_id, "via": "technique"})
        return out or None
    return None


def _result_count(tool_name: str, result: dict[str, Any]) -> int | None:
    """Row count for the two search tools (None for every other tool).

    Zero-hit searches record 0; a missing/non-list ``results`` on a
    search tool also records 0 so every search event carries the key.
    """
    if tool_name not in ("search_recipes", "search_techniques"):
        return None
    rows = result.get("results")
    return len(rows) if isinstance(rows, list) else 0


_EPICURE_RESULT_TOOLS = frozenset(
    {
        "find_balanced_pairings",
        "find_conventional_pairings",
        "find_flavor_pairings",
        "find_substitutions",
    }
)


def _result_facts(tool_name: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """Small result facts for the session evidence digest (None otherwise).

    Only successful calls record facts: pairing tools record the
    requested/queried ingredient plus the top 5 pairing names,
    ``get_recipe`` records the fetched title, ``search_web`` records
    the source count plus up to 5 classifications, titles and hosts,
    and ``search_techniques`` records up to 10 hits
    (doc/chunk/title). Everything is bounded and JSON-safe.
    """
    if not result.get("ok"):
        return None
    if tool_name in _EPICURE_RESULT_TOOLS:
        rows = result.get("pairings")
        if rows is None:
            rows = result.get("candidates")
        names: list[str] = []
        if isinstance(rows, list):
            for row in rows[:5]:
                if isinstance(row, dict) and row.get("ingredient"):
                    names.append(str(row["ingredient"])[:80])
        return {
            "requested": str(result.get("requested") or "")[:200],
            "queried_as": str(result.get("queried_as") or "")[:200],
            "names": names,
        }
    if tool_name == "get_recipe":
        recipe = result.get("recipe")
        title = recipe.get("title") if isinstance(recipe, dict) else None
        if result.get("duplicate_of_session_evidence"):
            # Repeat-fetch pointer: no recipe body, the title is top level.
            return {"title": str(result.get("title") or "")[:120], "duplicate": True}
        return {"title": str(title or "")[:120]}
    if tool_name == "search_web":
        sources = result.get("sources")
        classifications: list[str] = []
        titles: list[str] = []
        hosts: list[str] = []
        if isinstance(sources, list):
            from urllib.parse import urlparse

            for source in sources[:5]:
                if not isinstance(source, dict):
                    continue
                classifications.append(str(source.get("classification") or "")[:80])
                title = str(source.get("title") or source.get("citation_title") or "")[:80]
                titles.append(title)
                try:
                    hosts.append(str(urlparse(str(source.get("url") or "")).hostname or "")[:60])
                except Exception:
                    hosts.append("")
        return {
            "source_count": len(sources) if isinstance(sources, list) else 0,
            "classifications": classifications,
            "titles": titles,
            "hosts": hosts,
        }
    if tool_name == "search_techniques":
        rows = result.get("results")
        hits: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for row in rows[:10]:
                if not isinstance(row, dict):
                    continue
                doc_id, chunk_id = row.get("doc_id"), row.get("chunk_id")
                if isinstance(doc_id, str) and isinstance(chunk_id, int):
                    hits.append(
                        {
                            "doc_id": doc_id[:200],
                            "chunk_id": chunk_id,
                            "title": str(row.get("title") or "")[:120],
                        }
                    )
        return {"hits": hits}
    return None


def _record_event(
    *,
    context: ToolContext,
    session_id: str | None,
    call_id: str,
    tool_name: str,
    digest: str,
    outcome: str,
    error_type: str | None,
    reason: str | None,
    latency_ms: float,
    cost_class: CostClass,
    mode_ran: str | None = None,
    match: str | None = None,
    returned_identities: list[dict[str, Any]] | None = None,
    result_count: int | None = None,
    result_facts: dict[str, Any] | None = None,
    args: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "call_id": call_id,
        "tool": tool_name,
        "args_digest": digest,
        "outcome": outcome,
        "error_type": error_type,
        "reason": reason,
        "latency_ms": round(latency_ms, 2),
        "cost_class": cost_class,
    }
    if mode_ran is not None:
        payload["mode_ran"] = mode_ran
    if match is not None:
        payload["match"] = match
    if returned_identities is not None:
        payload["returned_identities"] = returned_identities
    if result_count is not None:
        payload["result_count"] = result_count
    if result_facts is not None:
        payload["result_facts"] = result_facts
    if session_id and getattr(context, "session_store", None) is not None:
        # Bounded args travel in the session event only when the caller
        # opts in (reviewable raw trajectories); the process log and the
        # default event keep the digest only. Minimization (Phase 5)
        # runs on the trajectory path too: emails, phones, street
        # addresses and "my <Name>" are scrubbed before storage.
        # Fail-closed (review fix 1): when the minimizer throws, store
        # the digest only plus args_minimization_failed — never the
        # unminimized args.
        if args is not None and bool(getattr(context, "record_tool_args", False)):
            try:
                from culinary_copilot.search.minimize import minimize_tool_args as _min_args

                payload["args"] = _min_args(args)
            except Exception:
                payload["args_minimization_failed"] = True
        try:
            context.session_store.append_event(session_id, TOOL_CALL_EVENT_TYPE, payload)
        except Exception:
            logger.warning(
                "tool event append failed",
                extra={"tool": tool_name, "call_id": call_id},
            )
    else:
        logger.info("tool_call", extra={"tool_event": payload})


async def run_tool(
    tool: ToolDefinition,
    impl: Callable[..., Awaitable[dict[str, Any]] | dict[str, Any]],
    raw_args: dict[str, Any],
    context: ToolContext,
    *,
    session_id: str | None = None,
    call_id: str | None = None,
) -> dict[str, Any]:
    """Validate, execute with timeout, and return a typed result dict.

    Argument validation failures return ``invalid_arguments``
    (reason ``tool_invalid_arguments``); timeouts return ``timeout``
    (reason ``tool_timeout``). Implementation-typed errors
    (``unavailable`` / ``permission_denied`` / ``invalid_arguments``
    with a specific reason) pass through with their ``next_action``.
    An exception escaping an implementation is a defect and becomes
    ``tool_internal_error`` (``contact_operator``), never raised.
    """
    cid = call_id or new_id("call")
    if isinstance(raw_args, dict):
        # Strict-mode schemas make every property required-but-nullable,
        # so the model sends explicit nulls for omitted optionals. Drop
        # them before validation so the Pydantic defaults apply; the
        # argument models stay unchanged.
        raw_args = {k: v for k, v in raw_args.items() if v is not None}
    try:
        parsed = tool.args_model.model_validate(raw_args)
        validated_args = parsed.model_dump()
    except ValidationError as exc:
        reason = REASON_TOOL_INVALID_ARGUMENTS
        result = {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": reason,
            "message": f"invalid arguments for {tool.name}: {exc.errors()[:3]}",
            "next_action": next_action_for(reason),
        }
        _record_event(
            context=context,
            session_id=session_id,
            call_id=cid,
            tool_name=tool.name,
            digest=args_digest(raw_args if isinstance(raw_args, dict) else {}),
            outcome="error",
            error_type="invalid_arguments",
            reason=reason,
            latency_ms=0.0,
            cost_class=tool.cost_class,
            result_count=_result_count(tool.name, result),
            args=dict(raw_args) if isinstance(raw_args, dict) else None,
        )
        return result

    digest = args_digest(validated_args)
    timeout = _timeout_for(context, tool)
    start = time.monotonic()
    override = context.impl_overrides.get(tool.name)
    target: Callable[..., Any] = override if override is not None else impl
    # Per-call copy carrying this invocation's call id (fix 3): the
    # shared context must not be mutated — parallel calls in one batch
    # would race on it. Impls read context.call_id for their own
    # events; failure to copy keeps the shared context untouched.
    try:
        call_context = replace(context, call_id=cid)
    except Exception:
        call_context = context
    try:
        if _is_async_callable(target):
            outcome_raw = await asyncio.wait_for(target(parsed, call_context), timeout=timeout)
        else:
            outcome_raw = await asyncio.wait_for(
                asyncio.to_thread(target, parsed, call_context), timeout=timeout
            )
        latency_ms = (time.monotonic() - start) * 1000.0
        typed: dict[str, Any]
        if not isinstance(outcome_raw, dict):
            reason = REASON_TOOL_INVALID_ARGUMENTS
            typed = {
                "ok": False,
                "error_type": "invalid_arguments",
                "reason": reason,
                "message": f"{tool.name} returned a non-mapping result",
                "next_action": next_action_for(reason),
            }
        else:
            typed = dict(outcome_raw)
        result = typed
        _record_event(
            context=context,
            session_id=session_id,
            call_id=cid,
            tool_name=tool.name,
            digest=digest,
            outcome="ok" if result.get("ok") else "error",
            error_type=_as_opt_str(result.get("error_type")),
            reason=_as_opt_str(result.get("reason")),
            latency_ms=latency_ms,
            cost_class=_event_cost(tool, result),
            mode_ran=_as_opt_str(result.get("mode_ran")),
            match=_as_opt_str(result.get("match")),
            returned_identities=_returned_identities(tool.name, parsed, result),
            result_count=_result_count(tool.name, result),
            result_facts=_result_facts(tool.name, result),
            args=dict(validated_args),
        )
        return result
    except (asyncio.TimeoutError, TimeoutError):
        latency_ms = (time.monotonic() - start) * 1000.0
        reason = REASON_TOOL_TIMEOUT
        result = {
            "ok": False,
            "error_type": "timeout",
            "reason": reason,
            "message": f"{tool.name} timed out after {timeout:g}s",
            "next_action": next_action_for(reason),
        }
        _record_event(
            context=context,
            session_id=session_id,
            call_id=cid,
            tool_name=tool.name,
            digest=digest,
            outcome="error",
            error_type="timeout",
            reason=reason,
            latency_ms=latency_ms,
            cost_class=tool.cost_class,
            result_count=_result_count(tool.name, result),
            args=dict(validated_args),
        )
        return result
    except Exception as exc:
        # Runner control-flow exceptions (runner_stop) propagate to the
        # live runner instead of becoming tool errors: campaign stops
        # (estimate breach, budget refusal) must halt the run, not feed
        # the agent another tool output.
        if getattr(exc, "runner_stop", False):
            raise
        latency_ms = (time.monotonic() - start) * 1000.0
        # An escaping exception is a defect, surfaced as
        # tool_internal_error (never raised, never truncated).
        from culinary_copilot.domain.recommendations import REASON_TOOL_INTERNAL_ERROR

        reason = REASON_TOOL_INTERNAL_ERROR
        result = {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": f"{tool.name} internal error: {type(exc).__name__}",
            "next_action": next_action_for(reason),
        }
        _record_event(
            context=context,
            session_id=session_id,
            call_id=cid,
            tool_name=tool.name,
            digest=digest,
            outcome="error",
            error_type="unavailable",
            reason=reason,
            latency_ms=latency_ms,
            cost_class=tool.cost_class,
            result_count=_result_count(tool.name, result),
            args=dict(validated_args),
        )
        return result


__all__ = [
    "CostClass",
    "TOOL_CALL_EVENT_TYPE",
    "ToolContext",
    "ToolDefinition",
    "args_digest",
    "run_tool",
    "tool_timeout_s",
]
