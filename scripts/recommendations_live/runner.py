"""Bounded Phase 3 live-evaluation runner (dry-run by default, never live).

Modes:
- dry-run (default): zero provider calls, zero spend. Drives the real
  application HTTP workflows (clarification groups + answers +
  recommendations) against the local corpus read-only, captures the
  would-be provider payloads, and records per-case cost reservations.
  Local PostgreSQL reads are SELECT-only; no OpenAI traffic occurs and no
  key is required.
- live: requires --live plus explicit --ceiling-usd and explicit
  --price-input-per-1m/--price-output-per-1m (missing/unverified pricing
  blocks submission otherwise). The first authorized evaluation call is
  the model-access check; no extra probe is made.
- --rehearse: zero-network end-to-end rehearsal with a scripted SDK stub
  (no key, no spend); --rehearse-scenario injects one failure envelope
  per provider-reaching call on separate runs.
- --rehearse-transport: zero-network rehearsal through the REAL
  AsyncOpenAI client against a localhost stub server (loopback only):
  exercises connection reuse, event-loop handling, and SDK parsing
  exactly as live. The documented pre-live rehearsal.

All run traffic (live + rehearsals) executes on ONE event loop for the
whole run, matching production: the provider is created, started, used,
and closed on that loop, and every case's HTTP runs there too. Sharing
one provider across per-case TestClient portals reuses pooled SDK
connections on dead loops (`RuntimeError: Event loop is closed` on
alternating calls); this structure makes that impossible. The sync
TestClient path remains for offline tests and dry-run only.

Budget model (conservative, documented):
- Input token upper bound = UTF-8 byte length of the exact payload
  (every token spans >= 1 byte, so bytes bound tokens; no chars/4 claim).
  Live reservations reuse dry-run measured bytes when available, else the
  configured cap (max_input_chars * 4 bytes/char per turn) as the bound.
- Output bound = max_output_tokens per attempt (6500: measured 5414-char
  worst-case valid label selection + 1000-token reasoning allowance; covers
  reasoning + text together). Explicit reasoning effort (minimal) is sent
  on every call and recorded in every case artifact.
- Attempts bound = turns_max * (max_retries + 1); tool calls and turns
  bounded separately by REC_MAX_* settings.
- Cases that make no provider call cost $0 (reservation released); error
  failures without provider processing (disabled/corpus/auth/not-found/
  rate-limited) are free. Unknown reported usage on completed provider
  calls is charged at the full reservation, never zero.
- Epicure: cached assets verified present at the pinned revision, so the
  runner enables consultation for generation cases (the isolated
  degradation case keeps its override); the effective switch is recorded
  per case.
- Aggregate ceiling, per-case reservation before submission, stop rules
  (unsupported claim / fidelity mismatch / 3 consecutive paid failures /
  provider access / spend projection), serial execution, progress
  persistence (paid cases never silently repeated), and summary.json on
  every exit path with the stop reason. Per-case records carry
  provider_reached, attempts, and attempt metadata.

Usage:
    uv run python scripts/recommendations_live/runner.py \\
        --cases evals/cases/phase3_live_cases.json \\
        --out evals/results/phase3_live --state-name state.json
    uv run python scripts/recommendations_live/runner.py --cases ... --out ... \\
        --live --ceiling-usd 1.00 --price-input-per-1m 0.05 --price-output-per-1m 0.40
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

# ruff: noqa: E402 -- sys.path bootstrap precedes src imports (repo convention).
from culinary_copilot.api.clarification import build_router as build_clarification_router
from culinary_copilot.api.recommendations import build_router as build_recommendations_router
from culinary_copilot.config import Settings
from culinary_copilot.llm.client import GET_RECIPE_FUNCTION
from culinary_copilot.recommendations.evidence import render_recipe
from culinary_copilot.tools.epicure import EpicureCore

EXPECTED_OUTCOMES = ("recommendation", "clarification", "insufficient_evidence")
OVERRIDABLE_SETTINGS = ("epicure_enabled",)
# Error reasons that prove no provider call happened (free failures).
UNBILLED_REASONS = {
    "generation_disabled",
    "corpus_unavailable",
    "unknown_group",
    "stale",
    "malformed",
    "provider_auth",
    "provider_not_found",
    "provider_rate_limited",
}
# Free failures that still stop the run (access/config, not transient).
ACCESS_STOP_REASONS = {"provider_auth", "provider_not_found"}
# Deterministic local failures stop the run immediately (never worth
# repeating across cases); unpaid ones cost $0, post-response ones bill
# their reservation because the provider did process the request.
INTERNAL_STOP_REASONS = {"provider_internal_error"}
# Free failures where the provider API was still contacted (rejected or
# throttled): reached but billed nothing. All other free failures happen
# pre-provider.
REACHED_BUT_UNBILLED = {"provider_auth", "provider_not_found", "provider_rate_limited"}
STATE_VERSION = "phase3-runner-v1"


class DryRunCapture(Exception):
    """Control exception: would-be provider payload captured, zero spend."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        super().__init__("dry-run capture")
        self.payloads = payloads


class GuardProvider:
    """Dry-run provider: forbids clarification calls, captures rec payloads."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def complete_planning(self, **kwargs: Any) -> Any:
        raise AssertionError("clarification LLM call attempted during guarded run")

    async def complete_recommendation(
        self, *, system: str, user: str, response_model: Any = None
    ) -> Any:
        # The structured-output schema travels with the request and is
        # billed as input: measured bytes include it.
        payload: dict[str, Any] = {"system": system, "user": user}
        if response_model is not None:
            payload["text_format"] = _schema_param(response_model)
        self.calls.append({"kind": "recommendation", "bytes": _bytes(payload)})
        raise DryRunCapture([payload])

    async def complete_native_tool_turn(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
        response_model: Any = None,
    ) -> Any:
        payload: dict[str, Any] = {"input_items": input_items}
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if response_model is not None:
            payload["text_format"] = _schema_param(response_model)
        self.calls.append({"kind": "native_tool", "bytes": _bytes(payload)})
        raise DryRunCapture([payload])


def _schema_param(response_model: Any) -> dict[str, Any]:
    """The text.format parameter the SDK derives from a response model."""
    from openai.lib._parsing._responses import type_to_text_format_param

    return dict(type_to_text_format_param(response_model))


LABEL_RE = r"- label: ['\"](\d+)['\"]"


def _rehearsal_refs(text: str) -> tuple[str, list[str], list[str]]:
    """First offered label + its refs parsed from rendered evidence text."""
    import re

    labels = re.findall(LABEL_RE, text)
    label = labels[0] if labels else "1"
    first = re.split(LABEL_RE, text, maxsplit=2)
    block = first[2] if len(first) > 2 else text
    # Stop at the next candidate block when the full payload was passed.
    nxt = re.search(r"\n- label: ['\"]", block)
    if nxt:
        block = block[: nxt.start()]
    ings = re.findall(r"^\s*(ing-\d+):", block, re.M)
    steps = re.findall(r"^\s*(step-\d+):", block, re.M)
    return label, ings, steps


def _rehearsal_body(
    *, output: list[dict[str, Any]], call_no: int, status: str = "completed"
) -> dict[str, Any]:
    """Wire-format Responses API body shared by both rehearsal transports."""
    return {
        "id": f"resp-rehearse-{call_no}",
        "object": "response",
        "created_at": 1758720000,
        "model": "gpt-5-nano",
        "status": status,
        "output": output,
        "usage": {
            "input_tokens": 2000,
            "output_tokens": 224,
            "total_tokens": 2224,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _rehearsal_decision(payload: dict[str, Any], call_no: int) -> dict[str, Any]:
    """Success envelope derived from the request's own evidence.

    Shared by the SDK stub (duck-typed namespace) and the localhost HTTP
    stub (wire JSON through the real client): first offered label with its
    exact refs, so selections validate through the real service path.
    """
    input_items = payload.get("input", [])
    texts = [
        str(item.get("content", ""))
        if isinstance(item, dict)
        else (item if isinstance(item, str) else "")
        for item in input_items
    ]
    if payload.get("tools"):
        label, _, _ = _rehearsal_refs("\n".join(texts))
        return _rehearsal_body(
            output=[
                {
                    "type": "function_call",
                    "id": f"fc-rehearse-{call_no}",
                    "call_id": f"call-rehearse-{call_no}",
                    "name": "get_recipe",
                    "arguments": json.dumps({"candidate_label": label}),
                }
            ],
            call_no=call_no,
        )
    tool_outputs = [
        item
        for item in input_items
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    if tool_outputs:
        label, ings, steps = _rehearsal_refs(str(tool_outputs[0].get("output", "")))
    else:
        label, ings, steps = _rehearsal_refs("\n".join(texts))
    parsed = {
        "candidate_label": label,
        "ingredient_refs": ings,
        "step_refs": steps,
        # Typed propositions only (the free-text contract is retired); the
        # server checks prerequisites and may omit any of these.
        "reasons": [
            {"type": "dish_named_in_title", "ingredient_refs": []},
            {"type": "reported_time_within_limit", "ingredient_refs": []},
        ],
        "questions": [{"type": "desired_portions"}],
    }
    return _rehearsal_body(
        output=[
            {
                "type": "message",
                "id": f"msg-rehearse-{call_no}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(parsed), "annotations": []}],
            }
        ],
        call_no=call_no,
    )


def _ns_response(
    *, status: str, output: list[Any], parsed: dict[str, Any] | None, call_no: int
) -> Any:
    """SDK-shaped duck-typed response (zero network)."""
    from types import SimpleNamespace

    namespace: dict[str, Any] = {}
    if parsed is not None:
        payload = dict(parsed)

        def _dump(payload: dict[str, Any] = payload) -> dict[str, Any]:
            return dict(payload)

        namespace["output_parsed"] = SimpleNamespace(model_dump=_dump)
    else:
        namespace["output_parsed"] = None
    return SimpleNamespace(
        id=f"resp-rehearse-{call_no}",
        status=status,
        incomplete_details=(
            SimpleNamespace(reason="max_output_tokens") if status == "incomplete" else None
        ),
        usage=SimpleNamespace(
            input_tokens=2000,
            output_tokens=224,
            total_tokens=2224,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
        output=output,
        **namespace,
    )


def _ns_wire_body(body: dict[str, Any]) -> Any:
    """Wrap a wire-format rehearsal body as an SDK-shaped namespace."""
    from types import SimpleNamespace

    output: list[Any] = []
    parsed: dict[str, Any] | None = None
    for item in body.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            payload = dict(item)

            def _dump_call(payload: dict[str, Any] = payload) -> dict[str, Any]:
                return dict(payload)

            output.append(SimpleNamespace(model_dump=_dump_call, **payload))
        elif item.get("type") == "message":
            for content in item.get("content", []) or []:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    parsed = json.loads(content.get("text", "{}"))
    usage = body.get("usage", {})
    details = usage.get("output_tokens_details") or {}
    parsed_ns: Any = None
    if parsed is not None:
        payload = dict(parsed)

        def _dump_parsed(payload: dict[str, Any] = payload) -> dict[str, Any]:
            return dict(payload)

        parsed_ns = SimpleNamespace(model_dump=_dump_parsed)
    return SimpleNamespace(
        id=body.get("id"),
        status=body.get("status", "completed"),
        incomplete_details=None,
        usage=SimpleNamespace(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            output_tokens_details=SimpleNamespace(reasoning_tokens=details.get("reasoning_tokens")),
        ),
        output=output,
        output_parsed=parsed_ns,
    )


class RehearsalResponses:
    """Zero-network scripted SDK surface for --rehearse.

    Success turns are derived from the request's own evidence (first
    offered label with its exact refs), so selections validate through
    the real service path. Failure scenarios inject one envelope per
    provider-reaching call. No key, no network, no spend.
    """

    SCENARIOS = ("success", "incomplete", "unavailable", "bad-request", "internal")

    def __init__(self, scenario: str = "success") -> None:
        if scenario not in self.SCENARIOS:
            raise ValueError(f"unknown rehearsal scenario: {scenario!r}")
        self.scenario = scenario
        self.calls = 0

    def _fail(self) -> Any:
        import httpx

        # NOTE: httpx objects passed to openai exceptions (test-only shapes).
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        if self.scenario == "unavailable":
            from openai import APIConnectionError

            raise APIConnectionError(request=request)  # type: ignore[arg-type]
        if self.scenario == "bad-request":
            from openai import BadRequestError

            response = httpx.Response(400, request=request)
            error_kwargs: dict[str, Any] = {"response": response, "body": {}}
            raise BadRequestError("rehearsal bad request", **error_kwargs)
        raise ValueError("rehearsal local failure")

    async def parse(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.scenario in ("unavailable", "bad-request", "internal"):
            self._fail()
        if self.scenario == "incomplete":
            return _ns_response(status="incomplete", output=[], parsed=None, call_no=self.calls)
        return _ns_wire_body(_rehearsal_decision(dict(kwargs), self.calls))


def _rehearsal_provider(settings: Any, scenario: str) -> Any:
    """Real provider class with a stubbed SDK surface (zero network)."""
    from types import SimpleNamespace

    from culinary_copilot.llm.client import OpenAIApplicationProvider

    provider = OpenAIApplicationProvider(settings)
    provider._client = SimpleNamespace(responses=RehearsalResponses(scenario))
    return provider


class _RehearsalStubHandler(BaseHTTPRequestHandler):
    """Localhost Responses API stub (loopback only, keep-alive).

    Answers every call with the shared rehearsal decision derived from the
    request's own evidence, so the REAL AsyncOpenAI client exercises
    connection reuse, event-loop handling, and SDK parsing exactly as in a
    live run. Zero external network.
    """

    protocol_version = "HTTP/1.1"
    server_version = "RehearsalStub/1"

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        ok, message, param = _validate_stub_input(payload.get("input"))
        if not ok:
            encoded = json.dumps(
                {
                    "error": {
                        "message": message,
                        "code": "unknown_parameter",
                        "param": param,
                        "type": "invalid_request_error",
                    }
                }
            ).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(encoded)
            return
        server = self.server
        server.call_no += 1  # type: ignore[attr-defined]
        body = _rehearsal_decision(payload, server.call_no)  # type: ignore[attr-defined]
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(encoded)


def _validate_stub_input(items: Any) -> tuple[bool, str, str]:
    """Strict input-item check mirroring the server's unknown_parameter 400.

    Returns (ok, message, param). Only chained item types are checked
    (function_call / function_call_output / reasoning) against the
    documented input field set; base messages pass through untouched.
    """
    from culinary_copilot.llm.client import CHAINED_ITEM_FIELDS

    if not isinstance(items, list):
        return True, "", ""
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        allowed = CHAINED_ITEM_FIELDS.get(str(item.get("type", "")))
        if allowed is None:
            continue
        for key in item.keys():
            if key not in allowed:
                param = f"input[{index}].{key}"
                return False, f"Unknown parameter: '{param}'.", param
    return True, "", ""


def _start_rehearsal_stub() -> tuple[Any, Any, str]:
    """Start the loopback stub; returns (server, thread, base_url)."""
    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), _RehearsalStubHandler)
    server.call_no = 0  # type: ignore[attr-defined]
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}/v1"


def _transport_rehearsal_provider(settings: Any, base_url: str) -> Any:
    """Real provider class AND real SDK client against the loopback stub."""
    from openai import AsyncOpenAI

    from culinary_copilot.llm.client import OpenAIApplicationProvider

    provider = OpenAIApplicationProvider(settings)
    provider._client = AsyncOpenAI(
        api_key="test-key",
        base_url=base_url,
        max_retries=0,
        timeout=float(settings.llm_rec_timeout_s),
    )
    return provider


def _bytes(payload: Any) -> int:
    return len(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))


def _sha(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class LiveCase:
    case_id: str
    purpose: str
    message: str
    dish: str | None
    request: dict[str, Any]
    answer_steps: list[dict[str, Any]]
    limit: int
    dataset_id: str | None
    tool_mode: bool
    expected_status: int
    expected_outcome: str
    invariants: list[str]
    config_override: dict[str, Any] = field(default_factory=dict)
    synthetic_fixture: dict[str, Any] | None = None


@contextlib.contextmanager
def synthetic_fixture_boundary(case: LiveCase) -> Any:
    """Test-only repository boundary for synthetic fixture cases.

    Serves the fixture rows/doc through in-process patches of the
    repository functions (the same pattern the offline tests use). The
    production API, repository code, and application corpus are untouched:
    nothing here is reachable outside this runner process, and nothing is
    written to the corpus.
    """
    from unittest.mock import patch

    fixture = case.synthetic_fixture
    if not fixture:
        yield
        return
    rows = fixture.get("rows", [])
    doc = fixture.get("doc")
    with (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.search_recipes", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=doc),
    ):
        yield


def load_cases(path: Path) -> list[LiveCase]:
    data = json.loads(path.read_text())
    raw_cases = data.get("cases", [])
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("case file must contain a non-empty cases list")
    if len(raw_cases) > 10:
        raise ValueError(f"at most 10 cases supported; got {len(raw_cases)}")
    cases: list[LiveCase] = []
    seen: set[str] = set()
    for entry in raw_cases:
        case_id = str(entry.get("id", ""))
        if not case_id or case_id in seen:
            raise ValueError(f"duplicate or missing case id: {case_id!r}")
        seen.add(case_id)
        clarification = entry.get("clarification") or {}
        rec_request = entry.get("recommendation_request") or {}
        limit = int(rec_request.get("limit", 3))
        if not 1 <= limit <= 5:
            raise ValueError(f"{case_id}: limit must be within 1..5")
        expected_outcome = str(entry.get("expected_outcome", ""))
        if expected_outcome not in EXPECTED_OUTCOMES:
            raise ValueError(f"{case_id}: unknown expected_outcome {expected_outcome!r}")
        override = entry.get("config_override") or {}
        for key in override:
            if key not in OVERRIDABLE_SETTINGS:
                raise ValueError(f"{case_id}: config_override key {key!r} not isolatable")
        fixture = entry.get("synthetic_fixture")
        if fixture is not None:
            # Synthetic source fixtures are test-only: clearly labeled,
            # self-contained (rows + doc), never touching the corpus.
            if not isinstance(fixture, dict) or fixture.get("synthetic") is not True:
                raise ValueError(f"{case_id}: synthetic_fixture must set synthetic: true")
            rows = fixture.get("rows")
            doc = fixture.get("doc")
            if not isinstance(rows, list) or not rows or not isinstance(doc, dict):
                raise ValueError(f"{case_id}: synthetic_fixture needs rows + doc")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not row.get("dataset_id")
                    or not row.get("source_id")
                ):
                    raise ValueError(f"{case_id}: fixture rows need dataset_id + source_id")
            if str(doc.get("title", "")).find("SYNTHETIC") < 0:
                raise ValueError(f"{case_id}: fixture doc title must be labeled SYNTHETIC")
        steps = entry.get("answer_steps") or []
        parsed_steps: list[dict[str, Any]] = []
        for step in steps:
            if not step.get("target") or ("text" not in step and "selected" not in step):
                raise ValueError(f"{case_id}: answer_steps need target + text/selected")
            parsed: dict[str, Any] = {"target": str(step["target"])}
            if step.get("text") is not None:
                parsed["text"] = str(step["text"])
            if step.get("selected") is not None:
                selected = step["selected"]
                if not isinstance(selected, list) or not selected:
                    raise ValueError(f"{case_id}: answer_steps selected must be a non-empty list")
                parsed["selected"] = [str(s) for s in selected]
            parsed_steps.append(parsed)
        cases.append(
            LiveCase(
                case_id=case_id,
                purpose=str(entry.get("purpose", "")),
                message=str(clarification.get("message") or entry.get("purpose") or case_id),
                dish=clarification.get("dish"),
                request=dict(clarification.get("request") or {}),
                answer_steps=parsed_steps,
                limit=limit,
                dataset_id=rec_request.get("dataset_id"),
                tool_mode=bool(rec_request.get("tool_mode", False)),
                expected_status=int(entry.get("expected_status", 200)),
                expected_outcome=expected_outcome,
                invariants=[str(i) for i in entry.get("invariants", [])],
                config_override={k: bool(v) for k, v in override.items()},
                synthetic_fixture=fixture,
            )
        )
    return cases


def fresh_settings(**overrides: Any) -> Settings:
    """Settings honoring local env (DB URL, keys) with explicit field overrides."""
    settings = Settings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _new_client(settings: Settings, store: Any, provider: Any) -> Any:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        build_clarification_router(
            settings=settings, store=store, provider=provider, engine=None, epicure=None
        )
    )
    return app


def build_app(settings: Settings, store: Any, provider: Any, engine: Any) -> Any:
    """Assemble the evaluation FastAPI app (shared by sync/async clients)."""
    from culinary_copilot.recommendations.epicure import CachedEpicureAdapter

    app = _new_client(settings, store, provider)
    app.include_router(
        build_recommendations_router(
            store=store,
            engine=engine,
            settings=settings,
            provider=provider,
            epicure=CachedEpicureAdapter(EpicureCore(settings)),
        )
    )
    return app


def make_clients(settings: Settings, store: Any, provider: Any, engine: Any) -> Any:
    """Sync TestClient (tests + dry-run; one portal loop per use).

    Live and rehearsal traffic uses one loop for the whole run via
    ``async_app_client`` instead: sharing a provider across TestClient
    portals reuses pooled SDK connections on dead loops
    (`RuntimeError: Event loop is closed` on every other call).
    """
    from fastapi.testclient import TestClient

    return TestClient(build_app(settings, store, provider, engine))


def async_app_client(app: Any) -> Any:
    """One-loop HTTP client for run traffic (must be used + closed on the
    single run loop; see main)."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://eval")


def drive_clarification(client: Any, case: LiveCase) -> dict[str, Any]:
    """Create a group and apply answer steps through the real HTTP workflow.

    Sync TestClient variant (tests + dry-run). The live/rehearsal path uses
    ``drive_clarification_async`` on the single run loop instead.
    """
    created = client.post(
        "/api/v1/clarification/groups",
        json={
            "message": case.message,
            "dish": case.dish,
            "request": case.request,
            "use_llm": False,
        },
    )
    if created.status_code != 200:
        raise RuntimeError(f"group creation failed: {created.status_code} {created.text[:300]}")
    body = created.json()
    group_id = body["group_id"]
    for step in case.answer_steps:
        fetched = client.get(f"/api/v1/clarification/groups/{group_id}").json()
        pending = [q for q in fetched.get("questions", []) if q.get("target") == step["target"]]
        if not pending:
            raise RuntimeError(f"{case.case_id}: no pending question for target {step['target']!r}")
        question = pending[0]
        payload: dict[str, Any] = {"question_id": question["id"]}
        if step.get("selected"):
            payload["selected"] = step["selected"]
        if step.get("text"):
            payload["text"] = step["text"]
        answered = client.post(
            f"/api/v1/clarification/groups/{group_id}/answers",
            json={
                "request_revision": fetched["request_revision"],
                "group_revision": fetched["group_revision"],
                "answers": [payload],
            },
        )
        if answered.status_code != 200:
            raise RuntimeError(
                f"{case.case_id}: answer step failed: {answered.status_code} {answered.text[:300]}"
            )
    final: dict[str, Any] = client.get(f"/api/v1/clarification/groups/{group_id}").json()
    return dict(final)


async def drive_clarification_async(client: Any, case: LiveCase) -> dict[str, Any]:
    """Async twin of drive_clarification for the single-loop run path."""
    created = await client.post(
        "/api/v1/clarification/groups",
        json={
            "message": case.message,
            "dish": case.dish,
            "request": case.request,
            "use_llm": False,
        },
    )
    if created.status_code != 200:
        raise RuntimeError(f"group creation failed: {created.status_code} {created.text[:300]}")
    body = created.json()
    group_id = body["group_id"]
    for step in case.answer_steps:
        fetched = (await client.get(f"/api/v1/clarification/groups/{group_id}")).json()
        pending = [q for q in fetched.get("questions", []) if q.get("target") == step["target"]]
        if not pending:
            raise RuntimeError(f"{case.case_id}: no pending question for target {step['target']!r}")
        question = pending[0]
        payload: dict[str, Any] = {"question_id": question["id"]}
        if step.get("selected"):
            payload["selected"] = step["selected"]
        if step.get("text"):
            payload["text"] = step["text"]
        answered = await client.post(
            f"/api/v1/clarification/groups/{group_id}/answers",
            json={
                "request_revision": fetched["request_revision"],
                "group_revision": fetched["group_revision"],
                "answers": [payload],
            },
        )
        if answered.status_code != 200:
            raise RuntimeError(
                f"{case.case_id}: answer step failed: {answered.status_code} {answered.text[:300]}"
            )
    final: dict[str, Any] = (await client.get(f"/api/v1/clarification/groups/{group_id}")).json()
    return dict(final)


def reserve_for_case(
    *,
    turn_bounds: list[int],
    attempts_per_turn: int,
    max_output_tokens: int,
    price_in: float,
    price_out: float,
) -> dict[str, Any]:
    """Conservative pre-call reservation from per-turn byte bounds.

    Each turn bound covers its complete input (measured payload bytes or
    the cap bound, plus tool-schema bytes and carried context where they
    apply). Input tokens ≤ bytes (every token spans >= 1 byte); output
    tokens ≤ max_output_tokens per attempt (covers reasoning + text).
    Cost reserves every permitted attempt, never just the first.
    """
    attempts_total = attempts_per_turn * len(turn_bounds)
    input_tokens = attempts_per_turn * sum(turn_bounds)
    output_tokens = max_output_tokens * attempts_total
    cost = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    return {
        "turn_bounds_bytes": list(turn_bounds),
        "turns_max": len(turn_bounds),
        "attempts_per_turn": attempts_per_turn,
        "attempts_max": attempts_total,
        "input_tokens_bound": input_tokens,
        "output_tokens_bound": output_tokens,
        "cost_usd": cost,
    }


def turn_bounds_for_case(
    *,
    tool_mode: bool,
    dry_payload_bytes: int,
    max_input_chars: int,
    evidence_max_chars: int,
    max_output_tokens: int = 0,
) -> list[int]:
    """Per-turn input byte bounds for one case.

    The service refuses to send any turn whose complete serialized request
    (messages, continuation items, tool result, tools, schemas) exceeds
    ``max_input_chars`` characters, and a character is at most 4 UTF-8
    bytes, so ``max_input_chars * 4`` bounds every turn's input.

    Default path: one selection turn bounded by the measured dry-run
    payload (which includes the response schema) or the cap bound.
    Native tool path: turn 1 = measured metadata payload (incl. tools)
    plus a tool-schema margin, or the cap bound; turn 2 = the cap bound
    (final instructions + retained context + continuation items + exact
    tool result + schema, enforced pre-send) plus ``max_output_tokens``
    for turn-1 output items (reasoning, function_call) the provider may
    expand server-side from item ids, which no local serialization sees.
    ``evidence_max_chars`` is kept for signature compatibility; the tool
    result is already inside the enforced turn-2 cap.
    """
    cap_bound = max_input_chars * 4
    schema_bytes = len(json.dumps(GET_RECIPE_FUNCTION).encode("utf-8"))
    if not tool_mode:
        return [dry_payload_bytes or cap_bound]
    turn1 = (dry_payload_bytes + schema_bytes + 256) if dry_payload_bytes else cap_bound
    turn2 = cap_bound + max(0, max_output_tokens)
    return [turn1, turn2]


def load_dry_payloads(path: str | None) -> dict[str, int]:
    """Dry-run payload bytes per case id (for reservations in a fresh dir)."""
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    out: dict[str, int] = {}
    for case_id, record in (data.get("cases") or {}).items():
        dry = (record or {}).get("dry") or {}
        if dry.get("payload_bytes"):
            out[str(case_id)] = int(dry["payload_bytes"])
    return out


def probe_reservation(
    *, settings: Settings, case: LiveCase, dry_bytes: int, prices: dict[str, float]
) -> dict[str, Any]:
    """Pre-submission reservation using the same bounds as execution."""
    turns_max = min(settings.rec_max_provider_turns, 2 if case.tool_mode else 1)
    turn_bounds = turn_bounds_for_case(
        tool_mode=case.tool_mode,
        dry_payload_bytes=dry_bytes,
        max_input_chars=settings.llm_rec_max_input_chars,
        evidence_max_chars=settings.rec_evidence_max_chars,
        max_output_tokens=settings.llm_rec_max_output_tokens,
    )[:turns_max]
    return reserve_for_case(
        turn_bounds=turn_bounds,
        attempts_per_turn=settings.llm_rec_max_retries + 1,
        max_output_tokens=settings.llm_rec_max_output_tokens,
        price_in=prices["input"],
        price_out=prices["output"],
    )


def check_fidelity(engine: Any, response: dict[str, Any], case: Any = None) -> dict[str, Any]:
    """Re-fetch the selected source and compare with the rendered recipe.

    Fixture-aware: the re-fetch runs through the same repository boundary
    the case used, results are labeled synthetic, and a selection outside
    the case corpus/fixture (or any refetch failure) is a recorded grading
    failure — never an exception. run3 crashed here on SYNTHETIC ids.

    NOTE: repository access goes through the module attribute (not a
    from-import binding) so the in-process boundary patches apply.
    """
    from culinary_copilot.recipes import repository as repository_module
    from culinary_copilot.recipes.repository import SUPPORTED_DATASETS
    from culinary_copilot.recommendations.evidence import build_candidate

    if response.get("outcome") != "recommendation":
        return {"checked": False}
    selection = response.get("selection", {})
    dataset_id = str(selection.get("dataset_id", ""))
    source_id = str(selection.get("source_id", ""))
    synthetic = bool(case is not None and getattr(case, "synthetic_fixture", None))
    allowed = set(SUPPORTED_DATASETS)
    fixture = getattr(case, "synthetic_fixture", None) if case is not None else None
    if isinstance(fixture, dict):
        for row in fixture.get("rows", []) or []:
            if isinstance(row, dict) and row.get("dataset_id"):
                allowed.add(str(row["dataset_id"]))
    if dataset_id not in allowed:
        return {
            "checked": True,
            "match": False,
            "synthetic": synthetic,
            "reason": "selection outside case corpus/fixture",
        }
    try:
        boundary = (
            synthetic_fixture_boundary(case) if case is not None else contextlib.nullcontext()
        )
        with boundary:
            doc = repository_module.get_recipe(engine, source_id, dataset_id=dataset_id)
        if doc is None:
            return {
                "checked": True,
                "match": False,
                "synthetic": synthetic,
                "reason": "selected source not refetchable",
            }
        candidate = build_candidate(
            dataset_id=dataset_id,
            source_id=source_id,
            title=None,
            doc=doc,
        )
        expected = render_recipe(candidate)
        match = expected == response.get("recipe")
        return {
            "checked": True,
            "match": match,
            "synthetic": synthetic,
            "fingerprint": _sha(
                {
                    "dataset_id": selection.get("dataset_id"),
                    "source_id": selection.get("source_id"),
                    "content_hash": doc.get("content_hash"),
                }
            ),
        }
    except Exception as exc:
        return {
            "checked": True,
            "match": False,
            "synthetic": synthetic,
            "reason": f"refetch failed: {type(exc).__name__}: {exc}"[:300],
        }


def check_unsupported_claim(response: dict[str, Any]) -> str | None:
    """Stop-rule check on a received body (structural, no keyword list).

    A dietary verdict may never be ``supported``. On a recommendation,
    every published ``selection_reasons``/``needs`` string must be exactly
    the server wording of an accepted proposition of an allowlisted type,
    and rejected propositions may carry only kind/index/type/code (no
    echoed text).
    """
    from typing import get_args

    from culinary_copilot.domain.recommendations import QuestionType, ReasonType

    for assessment in response.get("constraints", []):
        if (
            assessment.get("constraint", "").startswith("dietary:")
            and assessment.get("verdict") == "supported"
        ):
            return f"unsupported dietary support claim: {assessment}"
    if response.get("outcome") != "recommendation":
        return None
    propositions = response.get("propositions")
    if not isinstance(propositions, dict):
        return "recommendation lacks typed propositions"
    reasons = propositions.get("reasons") or []
    questions = propositions.get("questions") or []
    if [r.get("text") for r in reasons] != list(response.get("selection_reasons") or []):
        return "published selection_reasons differ from accepted typed propositions"
    if [q.get("text") for q in questions] != list(response.get("needs") or []):
        return "published needs differ from accepted typed propositions"
    reason_types, question_types = set(get_args(ReasonType)), set(get_args(QuestionType))
    for reason in reasons:
        if reason.get("type") not in reason_types:
            return f"non-allowlisted reason type published: {str(reason.get('type'))[:60]}"
    for question in questions:
        if question.get("type") not in question_types:
            return f"non-allowlisted question type published: {str(question.get('type'))[:60]}"
    for entry in response.get("rejected_propositions") or []:
        if not isinstance(entry, dict) or set(entry) != {"kind", "index", "type", "code"}:
            return "rejected proposition carries fields beyond kind/index/type/code"
    return None


def apply_effective_epicure(*, settings: Any, case: LiveCase) -> bool:
    """Effective Epicure switch for one case.

    Cached Epicure assets are verified present at the pinned revision
    (offline check, no download), so generation cases consult them; a case
    with an explicit ``epicure_enabled`` override (the isolated
    degradation case) keeps its override. Returns the effective value for
    the record.
    """
    if "epicure_enabled" not in case.config_override:
        settings.epicure_enabled = True
    return bool(settings.epicure_enabled)


def write_summary(
    *,
    out_dir: Path,
    state: RunState,
    stop: str | None,
    spent: float,
    ceiling_usd: float,
    prices: dict[str, float],
) -> dict[str, Any]:
    """Persist summary.json on every exit path (complete or any stop rule)."""
    cases_summary: dict[str, Any] = {}
    for case_id, record in (state.data.get("cases") or {}).items():
        live = (record or {}).get("live") or {}
        cases_summary[str(case_id)] = {
            "status": live.get("status"),
            "status_code": live.get("status_code"),
            "outcome": live.get("outcome"),
            "paid": live.get("paid"),
            "spent_usd": live.get("spent_usd"),
            "provider_reached": live.get("provider_reached"),
            "attempts": live.get("attempts"),
            "grading_error": bool(live.get("grading_error")),
            "final": live.get("final"),
        }
    summary = {
        "spent_usd_estimate": spent,
        "ceiling_usd": ceiling_usd,
        "prices": prices,
        "stop": stop,
        "cases": cases_summary,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    state.data["summary"] = summary
    state.save()
    return summary


@dataclass
class RunState:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    def load(self) -> None:
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {"version": STATE_VERSION, "cases": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))

    def case_record(self, case_id: str) -> dict[str, Any]:
        cases = self.data.setdefault("cases", {})
        record: dict[str, Any] = cases.setdefault(case_id, {})
        return record


def execute_dry_case(
    *,
    case: LiveCase,
    settings: Settings,
    engine: Any,
    out_dir: Path,
    state: RunState,
) -> dict[str, Any]:
    from culinary_copilot.services.store import InMemoryClarificationStore

    for key, value in case.config_override.items():
        setattr(settings, key, value)
    epicure_effective = apply_effective_epicure(settings=settings, case=case)
    store = InMemoryClarificationStore()
    provider = GuardProvider()
    client = make_clients(settings, store, provider, engine)
    clarification = drive_clarification(client, case)
    group_id = clarification["group_id"]
    payloads: list[dict[str, Any]] = []
    captured: DryRunCapture | None = None
    body = {
        "group_id": group_id,
        "request_revision": clarification["request_revision"],
        "group_revision": clarification["group_revision"],
        "limit": case.limit,
        "tool_mode": case.tool_mode,
    }
    if case.dataset_id:
        body["dataset_id"] = case.dataset_id
    try:
        with synthetic_fixture_boundary(case):
            resp = client.post("/api/v1/recommendations", json=body)
    except DryRunCapture as exc:  # TestClient re-raises; see note below
        captured = exc
        resp = None
    # NOTE: fastapi TestClient raises server exceptions through; the guard
    # provider raises DryRunCapture inside the endpoint, which surfaces here.
    record: dict[str, Any] = {
        "case_id": case.case_id,
        "mode": "dry-run",
        "provider_calls": 0,
        "provider_reached": False,
        "attempts": 0,
        "spent_usd": 0.0,
        "epicure_enabled_effective": epicure_effective,
        "synthetic_fixture": bool(case.synthetic_fixture),
        "clarification": {
            "outcome": clarification.get("outcome"),
            "ready": clarification.get("ready_for_retrieval"),
            "request_revision": clarification.get("request_revision"),
            "group_revision": clarification.get("group_revision"),
        },
        "guard_calls": len(provider.calls),
        "payload_bytes": 0,
        "payload_sha": "",
    }
    if captured is not None:
        payloads = captured.payloads
        record["payload_bytes"] = sum(_bytes(p) for p in payloads)
        record["payload_sha"] = _sha(payloads)
        record["payload_turns"] = len(payloads)
    else:
        # No provider call needed (clarification/insufficient pre-provider):
        # record the real outcome body for traceability.
        assert resp is not None
        content = resp.json()
        record["no_provider_call_needed"] = True
        record["outcome"] = content.get("outcome")
        record["insufficient_reason"] = content.get("insufficient_reason")
        record["clarification_reason"] = content.get("clarification_reason")
    (out_dir / f"case-{case.case_id}-dry.json").write_text(json.dumps(record, indent=2))
    state.case_record(case.case_id)["dry"] = record
    state.save()
    return record


def execute_live_case(
    *,
    case: LiveCase,
    settings: Settings,
    engine: Any,
    provider: Any,
    out_dir: Path,
    state: RunState,
    prices: dict[str, float],
    dry_payload_bytes: int,
) -> dict[str, Any]:
    from culinary_copilot.services.store import InMemoryClarificationStore

    for key, value in case.config_override.items():
        setattr(settings, key, value)
    epicure_effective = apply_effective_epicure(settings=settings, case=case)
    store = InMemoryClarificationStore()
    client = make_clients(settings, store, provider, engine)
    clarification = drive_clarification(client, case)
    group_id = clarification["group_id"]
    turns_max = min(settings.rec_max_provider_turns, 2 if case.tool_mode else 1)
    attempts_per_turn = settings.llm_rec_max_retries + 1
    turn_bounds = turn_bounds_for_case(
        tool_mode=case.tool_mode,
        dry_payload_bytes=dry_payload_bytes,
        max_input_chars=settings.llm_rec_max_input_chars,
        evidence_max_chars=settings.rec_evidence_max_chars,
        max_output_tokens=settings.llm_rec_max_output_tokens,
    )[:turns_max]
    reservation = reserve_for_case(
        turn_bounds=turn_bounds,
        attempts_per_turn=attempts_per_turn,
        max_output_tokens=settings.llm_rec_max_output_tokens,
        price_in=prices["input"],
        price_out=prices["output"],
    )
    # Per-turn worst-case bounds for post-hoc billing of partial usage.
    reservation["turn_bounds"] = list(turn_bounds)
    reservation["max_output_tokens"] = settings.llm_rec_max_output_tokens
    body = {
        "group_id": group_id,
        "request_revision": clarification["request_revision"],
        "group_revision": clarification["group_revision"],
        "limit": case.limit,
        "tool_mode": case.tool_mode,
    }
    if case.dataset_id:
        body["dataset_id"] = case.dataset_id
    record: dict[str, Any] = {
        "case_id": case.case_id,
        "mode": "live",
        "model": settings.llm_rec_model,
        "reasoning": {
            "effort": settings.llm_rec_reasoning_effort,
            "max_output_tokens": settings.llm_rec_max_output_tokens,
        },
        "epicure_enabled_effective": epicure_effective,
        "synthetic_fixture": bool(case.synthetic_fixture),
        "reservation_usd": reservation["cost_usd"],
        "reservation": reservation,
        "prices": {"input": prices["input"], "output": prices["output"]},
    }
    # Write-ahead persistence 1/3: mark "submitting" BEFORE the HTTP call,
    # so a crash can never lead to a silent paid resubmission. A resumed
    # run refuses to resubmit "submitting" cases without human review.
    record["status"] = "submitting"
    state.case_record(case.case_id)["live"] = dict(record)
    state.save()
    try:
        with synthetic_fixture_boundary(case):
            resp = client.post("/api/v1/recommendations", json=body)
    except BaseException as exc:
        # The request may or may not have been sent: persist that fact and
        # re-raise. main()'s finally block still writes summary.json.
        record["status"] = "submission-unknown"
        record["submission_error"] = f"{type(exc).__name__}: {exc}"[:300]
        _persist_live_record(out_dir, state, case.case_id, record)
        raise
    record["status"] = "received"
    record["status_code"] = resp.status_code
    try:
        content = resp.json() if resp.status_code == 200 else None
        if content is None:
            try:
                content = {"error_status": resp.status_code, "detail": resp.json()}
            except ValueError:
                content = {"error_status": resp.status_code, "detail": resp.text[:500]}
    except Exception as exc:
        content = {
            "error_status": resp.status_code,
            "detail": f"unreadable body: {type(exc).__name__}",
        }
    record["response"] = content
    # Write-ahead persistence 2/3: raw response (status, body, attempt
    # metadata, usage, ids) BEFORE any grading or fidelity work.
    _persist_live_record(out_dir, state, case.case_id, record)
    # Write-ahead persistence 3/3: grading inside a guard. Any exception
    # becomes a recorded grading_error (the run stops with that reason).
    try:
        _grade_live_content(record, content, resp.status_code, reservation, engine, case, prices)
    except Exception as exc:
        record["grading_error"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }
        record["stop"] = True
    _persist_live_record(out_dir, state, case.case_id, record)
    return record


def _persist_live_record(
    out_dir: Path, state: RunState, case_id: str, record: dict[str, Any]
) -> None:
    """Best-effort persistence that never masks the caller's exception."""
    try:
        (out_dir / f"case-{case_id}-live.json").write_text(
            json.dumps(record, indent=2, default=str)
        )
    except Exception:
        pass
    try:
        state.case_record(case_id)["live"] = dict(record)
        state.save()
    except Exception:
        pass


def _turn_bound_cost(
    index: int, attempts: int, reservation: dict[str, Any], prices: dict[str, float]
) -> float | None:
    """Worst-case cost for one turn: its input bound + output cap × attempts
    actually used. None when the reservation carries no per-turn bounds."""
    bounds = reservation.get("turn_bounds") or reservation.get("turn_bounds_bytes") or []
    max_output = reservation.get("max_output_tokens") or 0
    if index >= len(bounds) or not max_output:
        return None
    return float(
        (bounds[index] * prices["input"] + max_output * attempts * prices["output"]) / 1_000_000
    )


def _bill_usage(
    usage: dict[str, Any], reservation: dict[str, Any], prices: dict[str, float]
) -> tuple[float, str]:
    """Bill known turns at actual tokens; bill unknown turns at their full
    per-turn bound (never below actual exposure). Falls back to the full
    reservation when no turn detail exists."""
    in_tok = usage.get("input_tokens")
    out_tok = usage.get("output_tokens")
    if in_tok is not None and out_tok is not None:
        return (
            in_tok * prices["input"] + out_tok * prices["output"]
        ) / 1_000_000, "actual reported usage"
    turns = usage.get("turns") or []
    if not turns:
        return reservation["cost_usd"], "unknown usage charged at full reservation"
    charged = 0.0
    known_turns = 0
    unknown_turns = 0
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        t_in, t_out = turn.get("input_tokens"), turn.get("output_tokens")
        if t_in is not None and t_out is not None:
            charged += (t_in * prices["input"] + t_out * prices["output"]) / 1_000_000
            known_turns += 1
            continue
        bound = _turn_bound_cost(index, int(turn.get("attempts") or 0), reservation, prices)
        if bound is None:
            return reservation["cost_usd"], "unknown usage charged at full reservation"
        charged += bound
        unknown_turns += 1
    return charged, (
        f"per-turn billing: {known_turns} known turn(s) actual + "
        f"{unknown_turns} unknown turn(s) at bound"
    )


def _grade_live_content(
    record: dict[str, Any],
    content: dict[str, Any],
    status_code: int,
    reservation: dict[str, Any],
    engine: Any,
    case: LiveCase,
    prices: dict[str, float],
) -> None:
    """Derive billing + grading fields from a received response (in place).

    Raises on grading bugs; the caller records those as grading_error.
    """
    if status_code != 200:
        reason = ""
        inner: dict[str, Any] = {}
        detail = content.get("detail", {})
        if isinstance(detail, dict):
            # FastAPI HTTPException envelope nests our error dict one level
            # deeper: {"detail": {"reason": ..., ...}}.
            nested = detail.get("detail")
            inner = dict(nested) if isinstance(nested, dict) else dict(detail)
            reason = str(inner.get("reason", ""))
        # provider_reached/paid derive from recorded attempt metadata, not
        # from status codes: explicit flag first, then per-attempt
        # request_sent flags, then the legacy reason fallback (no metadata).
        attempt_errors = inner.get("attempt_errors") or []
        has_meta = (
            bool(attempt_errors)
            or inner.get("attempts") is not None
            or inner.get("provider_reached") is not None
        )
        if has_meta:
            reached = bool(inner.get("provider_reached")) or any(
                bool(e.get("request_sent")) for e in attempt_errors if isinstance(e, dict)
            )
        else:
            reached = reason in REACHED_BUT_UNBILLED
        paid = (
            bool(reached) and reason not in UNBILLED_REASONS and status_code not in (404, 409, 422)
        )
        stop = reason in ACCESS_STOP_REASONS or reason in INTERNAL_STOP_REASONS
        if paid:
            charged, requests_sent, usage_note = _bill_failure(inner, reservation, prices)
        else:
            charged, requests_sent, usage_note = 0.0, 0, "no request sent; $0"
            if bool(reached):
                # Reached but free (rate-limited / not-found / 4xx-free set):
                # requests went out, nothing billable.
                requests_sent = int(inner.get("attempts") or 0) + sum(
                    int(t.get("attempts") or 0)
                    for t in ((inner.get("prior_turns") or {}).get("turns") or [])
                    if isinstance(t, dict)
                )
                usage_note = "reached but free; $0"
        record.update(
            {
                "paid": paid,
                "spent_usd": charged,
                "provider_reached": bool(reached),
                "attempts": inner.get("attempts"),
                "requests_sent": requests_sent,
                "usage_note": usage_note,
                "attempt_errors": attempt_errors or None,
                "request_ids": _request_ids(inner),
                "error_reason": reason,
                "stop": stop,
            }
        )
        return
    usage = content.get("usage", None)
    if usage is None:
        # No provider call was made (clarification/insufficient_evidence
        # decided pre-provider): cost $0, reservation released.
        record.update(
            {
                "paid": False,
                "spent_usd": 0.0,
                "provider_reached": False,
                "attempts": 0,
                "requests_sent": 0,
                "usage_note": "no provider call; reservation released",
                "outcome": content.get("outcome"),
                "unsupported_claim": check_unsupported_claim(content),
                "fidelity": check_fidelity(engine, content, case),
            }
        )
        return
    charged, usage_note = _bill_usage(usage, reservation, prices)
    record.update(
        {
            "paid": True,
            "spent_usd": charged,
            "provider_reached": True,
            "usage_note": usage_note,
            "outcome": content.get("outcome"),
            "attempts": usage.get("attempts"),
            "requests_sent": usage.get("attempts"),
            "requests_per_turn": [
                turn.get("attempts") if isinstance(turn, dict) else None
                for turn in usage.get("turns") or []
            ]
            or None,
            "request_ids": _request_ids(usage),
            "unsupported_claim": check_unsupported_claim(content),
            "fidelity": check_fidelity(engine, content, case),
        }
    )


def _request_ids(source: dict[str, Any]) -> list[str]:
    """Provider request/response ids found in attempt metadata or turns,
    including prior turns kept on later-turn failures."""
    found: list[str] = []

    def _add(value: Any) -> None:
        if value and str(value) not in found:
            found.append(str(value))

    for entry in source.get("attempt_errors") or []:
        if not isinstance(entry, dict):
            continue
        for key in ("request_id", "response_id"):
            _add(entry.get(key))
    prior = source.get("prior_turns") or {}
    turns = list(source.get("turns") or []) + list(prior.get("turns") or [])
    for turn in turns:
        if isinstance(turn, dict):
            _add(turn.get("response_id"))
    for value in source.get("prior_response_ids") or []:
        _add(value)
    return found


def _bill_failure(
    inner: dict[str, Any], reservation: dict[str, Any], prices: dict[str, float]
) -> tuple[float, int, str]:
    """Bill a failed provider call: actual cost for prior turns with known
    usage, full per-turn bound for unknown prior turns and the failed
    attempts. Returns (charged, requests_sent, note)."""
    prior = inner.get("prior_turns") or {}
    pturns = prior.get("turns") or []
    charged = 0.0
    for index, turn in enumerate(pturns):
        if not isinstance(turn, dict):
            continue
        t_in, t_out = turn.get("input_tokens"), turn.get("output_tokens")
        if t_in is not None and t_out is not None:
            charged += (t_in * prices["input"] + t_out * prices["output"]) / 1_000_000
            continue
        bound = _turn_bound_cost(index, int(turn.get("attempts") or 0), reservation, prices)
        if bound is None:
            fail_attempts = int(inner.get("attempts") or 0)
            return (
                reservation["cost_usd"],
                int(prior.get("attempts") or 0) + fail_attempts,
                "unknown usage charged at full reservation",
            )
        charged += bound
    fail_attempts = int(inner.get("attempts") or 0)
    requests_sent = int(prior.get("attempts") or 0) + fail_attempts
    if pturns and fail_attempts == 0:
        # The failing turn was never sent (e.g. a turn-2 continuation that
        # could not fit, or post-response validation after the last turn):
        # only the completed prior turns are billable.
        return (
            charged,
            requests_sent,
            "per-turn billing: completed turns only; failing turn never sent",
        )
    fail_bound = _turn_bound_cost(len(pturns), fail_attempts, reservation, prices)
    if not pturns and fail_attempts:
        return (
            reservation["cost_usd"],
            requests_sent,
            "unknown usage charged at full reservation",
        )
    if fail_bound is None:
        return (
            reservation["cost_usd"],
            requests_sent,
            "unknown usage charged at full reservation",
        )
    charged += fail_bound
    return (
        charged,
        requests_sent,
        "per-turn billing: prior known turns actual + unknown turns at bound",
    )


async def execute_live_case_async(
    *,
    case: LiveCase,
    settings: Settings,
    engine: Any,
    provider: Any,
    out_dir: Path,
    state: RunState,
    prices: dict[str, float],
    dry_payload_bytes: int,
) -> dict[str, Any]:
    """Async twin of execute_live_case for the single-loop run path.

    One httpx client per case, created and closed on the run loop; the
    provider (and its pooled SDK connections) lives on that same loop for
    the whole run. Logic (reservations, persistence order, grading) is
    identical to the sync variant, which remains for offline tests.
    """
    from culinary_copilot.services.store import InMemoryClarificationStore

    for key, value in case.config_override.items():
        setattr(settings, key, value)
    epicure_effective = apply_effective_epicure(settings=settings, case=case)
    store = InMemoryClarificationStore()
    http = async_app_client(build_app(settings, store, provider, engine))
    try:
        clarification = await drive_clarification_async(http, case)
        group_id = clarification["group_id"]
        turns_max = min(settings.rec_max_provider_turns, 2 if case.tool_mode else 1)
        attempts_per_turn = settings.llm_rec_max_retries + 1
        turn_bounds = turn_bounds_for_case(
            tool_mode=case.tool_mode,
            dry_payload_bytes=dry_payload_bytes,
            max_input_chars=settings.llm_rec_max_input_chars,
            evidence_max_chars=settings.rec_evidence_max_chars,
            max_output_tokens=settings.llm_rec_max_output_tokens,
        )[:turns_max]
        reservation = reserve_for_case(
            turn_bounds=turn_bounds,
            attempts_per_turn=attempts_per_turn,
            max_output_tokens=settings.llm_rec_max_output_tokens,
            price_in=prices["input"],
            price_out=prices["output"],
        )
        # Per-turn worst-case bounds for post-hoc billing of partial usage.
        reservation["turn_bounds"] = list(turn_bounds)
        reservation["max_output_tokens"] = settings.llm_rec_max_output_tokens
        body = {
            "group_id": group_id,
            "request_revision": clarification["request_revision"],
            "group_revision": clarification["group_revision"],
            "limit": case.limit,
            "tool_mode": case.tool_mode,
        }
        if case.dataset_id:
            body["dataset_id"] = case.dataset_id
        record: dict[str, Any] = {
            "case_id": case.case_id,
            "mode": "live",
            "model": settings.llm_rec_model,
            "reasoning": {
                "effort": settings.llm_rec_reasoning_effort,
                "max_output_tokens": settings.llm_rec_max_output_tokens,
            },
            "epicure_enabled_effective": epicure_effective,
            "synthetic_fixture": bool(case.synthetic_fixture),
            "reservation_usd": reservation["cost_usd"],
            "reservation": reservation,
            "prices": {"input": prices["input"], "output": prices["output"]},
        }
        record["status"] = "submitting"
        state.case_record(case.case_id)["live"] = dict(record)
        state.save()
        try:
            with synthetic_fixture_boundary(case):
                resp = await http.post("/api/v1/recommendations", json=body)
        except BaseException as exc:
            record["status"] = "submission-unknown"
            record["submission_error"] = f"{type(exc).__name__}: {exc}"[:300]
            _persist_live_record(out_dir, state, case.case_id, record)
            raise
        record["status"] = "received"
        record["status_code"] = resp.status_code
        try:
            content = resp.json() if resp.status_code == 200 else None
            if content is None:
                try:
                    content = {"error_status": resp.status_code, "detail": resp.json()}
                except ValueError:
                    content = {
                        "error_status": resp.status_code,
                        "detail": resp.text[:500],
                    }
        except Exception as exc:
            content = {
                "error_status": resp.status_code,
                "detail": f"unreadable body: {type(exc).__name__}",
            }
        record["response"] = content
        record["expected_outcome"] = case.expected_outcome
        record["expected_outcome_match"] = resp.status_code == case.expected_status and (
            isinstance(content, dict) and content.get("outcome") == case.expected_outcome
        )
        _persist_live_record(out_dir, state, case.case_id, record)
        try:
            _grade_live_content(
                record, content, resp.status_code, reservation, engine, case, prices
            )
        except Exception as exc:
            record["grading_error"] = {
                "type": type(exc).__name__,
                "message": str(exc)[:500],
            }
            record["stop"] = True
        _persist_live_record(out_dir, state, case.case_id, record)
        return record
    finally:
        try:
            await http.aclose()
        except Exception:
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded Phase 3 evaluation runner")
    parser.add_argument("--cases", required=True, help="Path to live-cases JSON")
    parser.add_argument("--out", required=True, help="Output directory for records")
    parser.add_argument("--state-name", default="state.json", help="State file name in --out")
    parser.add_argument("--live", action="store_true", help="Opt in to paid model calls")
    parser.add_argument(
        "--rehearse",
        action="store_true",
        help=(
            "Zero-network end-to-end rehearsal: full live path with a scripted "
            "SDK stub (no key, no spend). Documented precondition for --live."
        ),
    )
    parser.add_argument(
        "--rehearse-scenario",
        default="success",
        choices=list(RehearsalResponses.SCENARIOS),
        help="Failure envelope injected per provider-reaching call (separate runs)",
    )
    parser.add_argument(
        "--rehearse-transport",
        action="store_true",
        help=(
            "Zero-network rehearsal through the REAL AsyncOpenAI client against "
            "a localhost stub server (loopback only): exercises connection "
            "reuse, event-loop handling, and SDK parsing exactly as live. "
            "The documented pre-live rehearsal."
        ),
    )
    parser.add_argument("--ceiling-usd", type=float, default=0.0)
    parser.add_argument("--price-input-per-1m", type=float, default=None)
    parser.add_argument("--price-output-per-1m", type=float, default=None)
    parser.add_argument("--model", default=None, help="Must match configured LLM_REC_MODEL")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop after the first case whose status/outcome differs from its expectation",
    )
    parser.add_argument(
        "--dry-state",
        default=None,
        help="Dry-run state.json path reused for reservations in a fresh output dir",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = load_cases(Path(args.cases))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    state = RunState(out_dir / args.state_name)
    state.load()

    base_settings = fresh_settings(llm_enabled=False)
    if args.model is not None and args.model != base_settings.llm_rec_model:
        print(f"refusing: --model {args.model} != configured {base_settings.llm_rec_model}")
        return 2

    if not args.live and not args.rehearse and not args.rehearse_transport:
        from culinary_copilot.db import create_db_engine

        engine = create_db_engine(base_settings)
        try:
            for case in cases:
                settings = fresh_settings(llm_enabled=False, llm_recommendation_enabled=True)
                record = execute_dry_case(
                    case=case, settings=settings, engine=engine, out_dir=out_dir, state=state
                )
                print(
                    json.dumps(
                        {
                            "case": case.case_id,
                            "mode": "dry-run",
                            "provider_calls": 0,
                            "payload_bytes": record["payload_bytes"],
                        }
                    )
                )
        finally:
            engine.dispose()
        print("dry-run complete: zero provider calls, zero spend")
        return 0

    rehearse = bool(args.rehearse)
    rehearse_transport = bool(args.rehearse_transport)
    if rehearse_transport:
        # Transport rehearsal implies the rehearsal budget defaults.
        rehearse = True
    if rehearse or rehearse_transport:
        # Zero-network rehearsal: no key gate, default budget (overridable).
        ceiling_usd = args.ceiling_usd if args.ceiling_usd > 0 else 1.0
        prices = {
            "input": args.price_input_per_1m if args.price_input_per_1m is not None else 0.05,
            "output": args.price_output_per_1m if args.price_output_per_1m is not None else 0.40,
        }
    else:
        # Live mode gates.
        if args.ceiling_usd <= 0:
            print("refusing live run: --ceiling-usd must be positive")
            return 2
        if args.price_input_per_1m is None or args.price_output_per_1m is None:
            print("refusing live run: explicit pricing required (--price-input/output-per-1m)")
            return 2
        key = base_settings.openai_api_key.get_secret_value()
        if not key:
            print("refusing live run: OPENAI_API_KEY is not configured")
            return 2
        ceiling_usd = args.ceiling_usd
        prices = {"input": args.price_input_per_1m, "output": args.price_output_per_1m}

    import asyncio

    return asyncio.run(
        _run_async(
            cases=cases,
            out_dir=out_dir,
            state=state,
            base_settings=base_settings,
            prices=prices,
            ceiling_usd=ceiling_usd,
            dry_state=args.dry_state,
            retry_failed=args.retry_failed,
            rehearse=rehearse,
            rehearse_transport=rehearse_transport,
            rehearse_scenario=args.rehearse_scenario,
            stop_on_failure=args.stop_on_failure,
        )
    )


async def _run_async(
    *,
    cases: list[LiveCase],
    out_dir: Path,
    state: RunState,
    base_settings: Settings,
    prices: dict[str, float],
    ceiling_usd: float,
    dry_state: str | None,
    retry_failed: bool,
    rehearse: bool,
    rehearse_transport: bool,
    rehearse_scenario: str,
    stop_on_failure: bool = False,
) -> int:
    """Single event loop for the whole run (matches production).

    The provider is created, started, used, and closed on this loop, and
    every case's HTTP traffic runs here too. Sharing one provider across
    per-case TestClient portals reuses pooled SDK connections on dead
    loops (`RuntimeError: Event loop is closed` on alternating calls);
    this structure makes that impossible.
    """
    from culinary_copilot.db import create_db_engine

    engine = create_db_engine(base_settings)
    stub_server: Any = None
    stub_thread: Any = None
    if rehearse_transport:
        stub_server, stub_thread, base_url = _start_rehearsal_stub()
        provider = _transport_rehearsal_provider(
            fresh_settings(
                llm_enabled=False,
                llm_recommendation_enabled=True,
                llm_rec_model=base_settings.llm_rec_model,
            ),
            base_url,
        )
    elif rehearse:
        provider = _rehearsal_provider(
            fresh_settings(
                llm_enabled=False,
                llm_recommendation_enabled=True,
                llm_rec_model=base_settings.llm_rec_model,
            ),
            rehearse_scenario,
        )
    else:
        from culinary_copilot.llm.client import OpenAIApplicationProvider

        provider = OpenAIApplicationProvider(
            fresh_settings(
                llm_enabled=False,
                llm_recommendation_enabled=True,
                llm_rec_model=base_settings.llm_rec_model,
            )
        )
    await provider.start()
    spent = 0.0
    consecutive_failures = 0
    dry_payloads = load_dry_payloads(dry_state)
    run: dict[str, Any] = {"stop": None, "spent": 0.0}
    try:
        for case in cases:
            existing = state.case_record(case.case_id).get("live", {})
            if existing.get("status") == "submitting" and existing.get("final") is not True:
                # A previous run crashed between reservation and response:
                # delivery is unknown, so resubmission could silently pay
                # twice. Refuse without explicit human review (inspect the
                # OpenAI usage dashboard, then clear or finalize the entry).
                # --retry-failed does NOT clear this: it only re-runs unpaid
                # final records.
                print(
                    json.dumps(
                        {
                            "stop": "needs-review",
                            "case": case.case_id,
                            "detail": (
                                "previous run left status=submitting; "
                                "possible paid delivery without a record"
                            ),
                        }
                    )
                )
                run["stop"] = "needs-review"
                run["spent"] = spent
                write_summary(
                    out_dir=out_dir,
                    state=state,
                    stop="needs-review",
                    spent=spent,
                    ceiling_usd=ceiling_usd,
                    prices=prices,
                )
                return 7
            if existing.get("final") is True and (existing.get("paid") is True or not retry_failed):
                print(json.dumps({"case": case.case_id, "skipped": "already-recorded"}))
                spent += float(existing.get("spent_usd", 0.0))
                run["spent"] = spent
                continue
            settings = fresh_settings(llm_enabled=False, llm_recommendation_enabled=True)
            dry_bytes = dry_payloads.get(
                case.case_id,
                int(state.case_record(case.case_id).get("dry", {}).get("payload_bytes", 0)),
            )
            probe = probe_reservation(
                settings=settings, case=case, dry_bytes=dry_bytes, prices=prices
            )
            if spent + probe["cost_usd"] > ceiling_usd:
                print(
                    json.dumps(
                        {
                            "stop": "ceiling",
                            "spent_usd": spent,
                            "next_reserve_usd": probe["cost_usd"],
                        }
                    )
                )
                run["stop"] = "ceiling"
                run["spent"] = spent
                write_summary(
                    out_dir=out_dir,
                    state=state,
                    stop="ceiling",
                    spent=spent,
                    ceiling_usd=ceiling_usd,
                    prices=prices,
                )
                return 3
            record = await execute_live_case_async(
                case=case,
                settings=settings,
                engine=engine,
                provider=provider,
                out_dir=out_dir,
                state=state,
                prices=prices,
                dry_payload_bytes=dry_bytes,
            )
            spent += float(record.get("spent_usd", 0.0))
            run["spent"] = spent
            record["final"] = True
            state.case_record(case.case_id)["live"] = record
            state.save()
            print(
                json.dumps(
                    {
                        "case": case.case_id,
                        "status": record.get("status_code"),
                        "outcome": record.get("outcome"),
                        "spent_usd": round(spent, 6),
                    }
                )
            )
            if record.get("grading_error"):
                print(json.dumps({"stop": "grading-error", "detail": record["grading_error"]}))
                run["stop"] = "grading-error"
                write_summary(
                    out_dir=out_dir,
                    state=state,
                    stop="grading-error",
                    spent=spent,
                    ceiling_usd=ceiling_usd,
                    prices=prices,
                )
                return 4
            if record.get("unsupported_claim"):
                print(
                    json.dumps({"stop": "unsupported-claim", "detail": record["unsupported_claim"]})
                )
                run["stop"] = "unsupported-claim"
                write_summary(
                    out_dir=out_dir,
                    state=state,
                    stop="unsupported-claim",
                    spent=spent,
                    ceiling_usd=ceiling_usd,
                    prices=prices,
                )
                return 4
            if stop_on_failure and not record.get("expected_outcome_match"):
                print(json.dumps({"stop": "case-failed", "case": case.case_id}))
                run["stop"] = "case-failed"
                write_summary(
                    out_dir=out_dir,
                    state=state,
                    stop="case-failed",
                    spent=spent,
                    ceiling_usd=ceiling_usd,
                    prices=prices,
                )
                return 8
            fidelity = record.get("fidelity", {})
            if fidelity.get("checked") and not fidelity.get("match"):
                print(json.dumps({"stop": "fidelity-mismatch", "detail": fidelity}))
                run["stop"] = "fidelity-mismatch"
                write_summary(
                    out_dir=out_dir,
                    state=state,
                    stop="fidelity-mismatch",
                    spent=spent,
                    ceiling_usd=ceiling_usd,
                    prices=prices,
                )
                return 4
            if record.get("paid") and record.get("outcome") not in (
                "recommendation",
                "clarification",
                "insufficient_evidence",
            ):
                consecutive_failures += 1
            elif record.get("stop"):
                reason = str(record.get("error_reason") or "")
                stop_label = (
                    "internal-error" if reason == "provider_internal_error" else "provider-access"
                )
                print(json.dumps({"stop": stop_label, "reason": reason}))
                run["stop"] = stop_label
                write_summary(
                    out_dir=out_dir,
                    state=state,
                    stop=stop_label,
                    spent=spent,
                    ceiling_usd=ceiling_usd,
                    prices=prices,
                )
                return 5
            else:
                consecutive_failures = 0
            if consecutive_failures >= 3:
                print(json.dumps({"stop": "consecutive-failures"}))
                run["stop"] = "consecutive-failures"
                write_summary(
                    out_dir=out_dir,
                    state=state,
                    stop="consecutive-failures",
                    spent=spent,
                    ceiling_usd=ceiling_usd,
                    prices=prices,
                )
                return 6
    except KeyboardInterrupt:
        run["stop"] = run["stop"] or "interrupted"
        raise
    except Exception as exc:
        run["stop"] = run["stop"] or f"crashed: {type(exc).__name__}"
        raise
    finally:
        # Summary on EVERY exit path, including uncaught exceptions and
        # interrupts; the write itself never masks the original error.
        # The provider closes on the run loop it was created on.
        try:
            await provider.aclose()
        except Exception:
            pass
        if stub_server is not None:
            try:
                stub_server.shutdown()
            except Exception:
                pass
        engine.dispose()
        try:
            write_summary(
                out_dir=out_dir,
                state=state,
                stop=run["stop"],
                spent=run["spent"],
                ceiling_usd=ceiling_usd,
                prices=prices,
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
