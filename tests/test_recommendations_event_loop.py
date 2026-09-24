"""Event-loop handling regression tests (offline; runs 1/2/4 findings).

A localhost loopback stub server stands in for the Responses API (no
external network). Proves: the old per-case-TestClient pattern with a
shared provider alternates ok/fail with `RuntimeError: Event loop is
closed`; the single-loop path does not; the provider loop guard fires
with a clear controlled error; persisted internal messages are bounded
and redacted.
"""

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "recommendations_live"))

FOODCOM = "AkashPS11/recipes_data_food.com"
SELECTION = {
    "dataset_id": FOODCOM,
    "source_id": "000159",
    "ingredient_refs": ["ing-0"],
    "step_refs": ["step-0"],
    "reasons": [],
    "questions": [],
}


def _body() -> dict[str, Any]:
    return {
        "id": "resp-stub-1",
        "object": "response",
        "created_at": 1758720000,
        "model": "gpt-6-luna",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg-1",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(SELECTION),
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 700,
            "output_tokens": 121,
            "total_tokens": 821,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        payload = json.dumps(_body()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def stub_url() -> Any:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()
    thread.join(timeout=5)


def _settings() -> Any:
    from culinary_copilot.config import Settings

    return Settings(_env_file=None, llm_enabled=False, llm_recommendation_enabled=True)


def _provider_with_real_client(stub_url: str) -> Any:
    from openai import AsyncOpenAI

    from culinary_copilot.llm.client import OpenAIApplicationProvider

    provider = OpenAIApplicationProvider(_settings())
    provider._client = AsyncOpenAI(api_key="test-key", base_url=stub_url, max_retries=0)
    return provider


def _close_provider(provider: Any) -> None:
    """Close pooled stub connections (same loop not required for close)."""
    try:
        asyncio.run(provider.aclose())
    except Exception:
        pass


def test_sdk_level_alternation_across_loops(stub_url: str) -> None:
    """Mechanism proof without our wrapper: one shared AsyncOpenAI client
    used from a fresh event loop per call (the runner's old TestClient
    pattern) alternates success / `RuntimeError: Event loop is closed`,
    because the pooled keep-alive connection outlives its loop. This is
    what runs 1, 2 and 4 show at the transport layer."""
    from openai import AsyncOpenAI

    from culinary_copilot.domain.recommendations import SelectionProposal

    client = AsyncOpenAI(api_key="test-key", base_url=stub_url, max_retries=0)

    async def one_call() -> None:
        await client.responses.parse(
            model="gpt-6-luna",
            input=[{"role": "user", "content": "pick one"}],
            text_format=SelectionProposal,
            max_output_tokens=6500,
            reasoning={"effort": "none"},
        )

    results = []
    for _ in range(4):
        try:
            asyncio.run(one_call())
            results.append("ok")
        except Exception as exc:  # noqa: BLE001 -- mechanism characterization
            results.append(f"{type(exc).__name__}: {exc}"[:120])
    try:
        asyncio.run(client.close())
    except Exception:
        pass
    assert results[0] == "ok"
    failures = [r for r in results[1:] if "Event loop is closed" in r]
    assert failures, f"expected loop-reuse failures, got {results}"
    assert "ok" in results[1:], f"expected recovery after discard, got {results}"


def test_old_runner_pattern_fails_controlled(stub_url: str) -> None:
    """With the loop guard, the old per-case-TestClient pattern fails every
    cross-loop call with a clear controlled error (no opaque RuntimeError,
    no accidental billing ambiguity)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from culinary_copilot.domain.recommendations import SelectionProposal
    from culinary_copilot.llm.client import ProviderInternalError

    provider = _provider_with_real_client(stub_url)
    app = FastAPI()

    @app.post("/rec")
    async def rec() -> dict[str, Any]:
        try:
            await provider.complete_recommendation(
                system="s", user="u", response_model=SelectionProposal
            )
            return {"ok": True}
        except ProviderInternalError as exc:
            return {
                "ok": False,
                "type": type(exc).__name__,
                "message": str(exc)[:200],
                "request_sent": exc.request_sent,
            }

    results = [TestClient(app).post("/rec").json() for _ in range(3)]
    _close_provider(provider)
    assert results[0]["ok"] is True
    for failed in results[1:]:
        assert failed["ok"] is False
        assert failed["type"] == "ProviderInternalError"
        assert "event loop" in failed["message"].lower()
        assert failed["request_sent"] is False


def test_single_loop_path_all_succeed(stub_url: str) -> None:
    """The fix: one event loop for the whole run (ASGI transport) → no
    alternation, real SDK parsing, connection reuse on one loop."""
    import httpx
    from fastapi import FastAPI

    from culinary_copilot.domain.recommendations import SelectionProposal

    provider = _provider_with_real_client(stub_url)
    app = FastAPI()

    @app.post("/rec")
    async def rec() -> dict[str, Any]:
        outcome = await provider.complete_recommendation(
            system="s", user="u", response_model=SelectionProposal
        )
        assert outcome.parsed and outcome.parsed.get("source_id") == "000159"
        return {"ok": True}

    async def drive() -> list[bool]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://eval") as client:
            results = []
            for _ in range(4):
                resp = await client.post("/rec")
                results.append(resp.json()["ok"])
            return results

    try:
        assert asyncio.run(drive()) == [True, True, True, True]
    finally:
        _close_provider(provider)


def test_loop_guard_fires_on_foreign_loop(stub_url: str) -> None:
    """Calling a started provider on a different loop fails controlled."""
    from culinary_copilot.llm.client import ProviderInternalError

    provider = _provider_with_real_client(stub_url)

    async def start_on_first_loop() -> None:
        await provider.start()

    asyncio.run(start_on_first_loop())

    async def use_on_second_loop() -> Any:
        from culinary_copilot.domain.recommendations import SelectionProposal

        return await provider.complete_recommendation(
            system="s", user="u", response_model=SelectionProposal
        )

    with pytest.raises(ProviderInternalError) as exc_info:
        asyncio.run(use_on_second_loop())
    _close_provider(provider)
    assert "event loop" in str(exc_info.value).lower()
    assert exc_info.value.request_sent is False


def test_internal_message_bounded_and_redacted() -> None:
    from culinary_copilot.recommendations.service import _redact_error_message

    raw = "boom sk-abcdefghijklmnop Bearer secret-token-value api_key=ABCD1234 " + "x" * 500
    redacted = _redact_error_message(raw)
    assert len(redacted) <= 300
    assert "sk-abcdefghijklmnop" not in redacted
    assert "secret-token-value" not in redacted
    assert "ABCD1234" not in redacted
    assert "redacted" in redacted
