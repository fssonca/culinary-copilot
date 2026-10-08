"""Phase 6 minimal web UI tests (offline, no model calls, no database).

Covers the spec's Python-testable surface:
- GET / redirects to /ui/; /ui/ and demo.html are text/html; JS/CSS
  have the right content types; the CSP and sibling headers are
  present on /ui responses and absent from /api responses.
- A static guard over src/culinary_copilot/web (no innerHTML-style
  sinks, no external hosts in code, no inline script bodies or style
  attributes in the HTML).
- Fixture contract: demo fixtures validate against the backend models
  (FinishResult parts, AskQuestion) and the final payload keys.
- web_ref label enrichment, including the "unclassified" fallback.
- OUTCOMES coverage: every reason in outcomes.js exists as a backend
  reason or stop reason; every page-reachable backend REASON_* has an
  OUTCOMES entry or is explicitly listed as next_action-fallback
  covered.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from culinary_copilot.agent.loop import (
    AgentDirective,
    AskQuestion,
    FinishOption,
    FinishResult,
    PlanPayload,
    TechniqueAnswer,
    WebAnswer,
    WebRef,
    _handle_finish,
)
from culinary_copilot.agent.validate import (
    ALLERGEN_DISCLAIMER,
    session_web_sources,
    web_label_for,
)
from culinary_copilot.api.app import create_app
from culinary_copilot.config import Settings
from culinary_copilot.domain import recommendations as domain
from culinary_copilot.domain.recommendations import NEXT_ACTIONS, next_action_for
from culinary_copilot.domain.sessions import SessionState

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "culinary_copilot" / "web"
FIXTURES = json.loads((WEB_DIR / "fixtures" / "demo-finals.json").read_text(encoding="utf-8"))
OUTCOMES_JS = (WEB_DIR / "js" / "outcomes.js").read_text(encoding="utf-8")

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _client() -> TestClient:
    return TestClient(create_app(_settings(epicure_enabled=False)))


# --- routes, content types, headers -------------------------------------------


def test_root_redirects_to_ui() -> None:
    with _client() as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code in (301, 302, 303, 307, 308)
    assert response.headers["location"].endswith("/ui/")


def test_ui_and_demo_are_html() -> None:
    with _client() as client:
        for path in ("/ui/", "/ui/demo.html"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith("text/html"), path


def test_ui_js_and_css_content_types() -> None:
    with _client() as client:
        js = client.get("/ui/js/main.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]
        css = client.get("/ui/styles.css")
        assert css.status_code == 200
        assert css.headers["content-type"].startswith("text/css")
        for name in ("api.js", "render.js", "outcomes.js", "demo.js"):
            response = client.get(f"/ui/js/{name}")
            assert response.status_code == 200, name
            assert "javascript" in response.headers["content-type"], name


def test_security_headers_on_ui_absent_from_api() -> None:
    with _client() as client:
        for path in ("/ui/", "/ui/demo.html", "/ui/js/main.js", "/ui/styles.css"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers.get("content-security-policy") == EXPECTED_CSP, path
            assert response.headers.get("x-content-type-options") == "nosniff", path
            assert response.headers.get("referrer-policy") == "no-referrer", path
            # UI files revalidate on every load, so a UI change is never
            # hidden behind a cached module (2026-10-06).
            assert response.headers.get("cache-control") == "no-cache", path
        api = client.get("/api/v1/pairings?ingredient=chicken")
        assert "content-security-policy" not in api.headers
        assert "x-content-type-options" not in api.headers
        assert "referrer-policy" not in api.headers


# --- static guard --------------------------------------------------------------


def _code_files() -> list[Path]:
    return [
        path
        for path in sorted(WEB_DIR.rglob("*"))
        if path.is_file() and path.suffix in {".html", ".css", ".js"}
    ]


def test_no_dom_sinks_or_dynamic_code() -> None:
    banned = [
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ]
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} contains {token}"


def test_no_external_hosts_in_code() -> None:
    # Fixture JSON is data (citation URLs the page renders as links);
    # code files must reference no external host.
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"https?://([^\s\"'<>]+)", text):
            host = (urlsplit(match.group(0)).hostname or "").lower()
            assert host in ("", "localhost", "127.0.0.1"), (
                f"{path.name} references external host {host}"
            )


def test_no_inline_script_or_style_in_html() -> None:
    for path in sorted(WEB_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", text, re.IGNORECASE), (
            f"{path.name} has an inline script body"
        )
        assert not re.search(r"\sstyle\s*=", text, re.IGNORECASE), (
            f"{path.name} has a style attribute"
        )
        assert "<script" in text.lower(), f"{path.name} loads no script"


def test_demo_makes_no_api_calls() -> None:
    demo = (WEB_DIR / "js" / "demo.js").read_text(encoding="utf-8")
    assert "/api/v1" not in demo


# --- fixture contract ----------------------------------------------------------

WEB_LABELS = {"official_guidance", "research_publication", "culinary_source", "unclassified"}
CONSTRAINT_STATUSES = {
    "checked",
    "unverified",
    "violated",
    "not_checked",
    "no_listed_terms_found",
}


def test_fixtures_labelled_synthetic() -> None:
    assert FIXTURES["label"] == "synthetic demo data — not model output"


def test_fixture_options_validate() -> None:
    result = FIXTURES["options"]
    assert isinstance(result["options"], list) and 1 <= len(result["options"]) <= 4
    for option in result["options"]:
        FinishOption.model_validate(option)
    assert result["note_claims"] in ("verified", "unverified")
    assert isinstance(result.get("epicure_lines"), list) and result["epicure_lines"]
    assert isinstance(result.get("dropped_options"), list) and result["dropped_options"]
    for dropped in result["dropped_options"]:
        assert {"index", "title", "source_id", "error"} <= set(dropped)
    assert isinstance(result.get("constraint_check"), list) and result["constraint_check"]
    saw_disclaimer = False
    for entry in result["constraint_check"]:
        assert entry["status"] in CONSTRAINT_STATUSES, entry
        assert entry["value"]
        if "disclaimer" in entry:
            assert entry["disclaimer"] == ALLERGEN_DISCLAIMER, entry
            saw_disclaimer = True
    assert saw_disclaimer, "fixtures must carry the exact backend allergen disclaimer"


def test_fixture_plan_validates() -> None:
    result = FIXTURES["plan"]
    plan = dict(result["plan"])
    steps_source = plan.pop("steps_source")
    assert steps_source in ("source", "model_adaptation")
    PlanPayload.model_validate(plan)
    assert plan["mise_en_place"] and plan["steps"] and plan["plating"]
    assert plan["source"] == {"dataset_id": "demo/synthetic", "source_id": "demo-lentil-soup-1"}


def test_fixture_technique_answer_validates() -> None:
    result = FIXTURES["technique_answer"]
    assert result["note_claims"] in ("verified", "unverified")
    TechniqueAnswer.model_validate(
        {
            "text": result["technique_answer"]["text"],
            "technique_refs": result["technique_answer"]["technique_refs"],
        }
    )
    assert result["technique_answer"]["technique_refs"]
    for entry in result["technique_answer"]["attribution"]:
        assert entry["attribution_text"]
        parsed = urlsplit(entry["licence_url"])
        assert parsed.scheme in ("http", "https") and parsed.hostname


def test_fixture_web_answer_validates() -> None:
    result = FIXTURES["web_answer"]
    assert result["web_answer"]["evidence_class"] == "external"
    assert result.get("note_claims") == "unverified"
    refs = result["web_answer"]["web_refs"]
    assert 1 <= len(refs) <= 5
    for ref in refs:
        WebRef.model_validate({"url": ref["url"], "title": ref["title"]})
        assert ref["label"] in WEB_LABELS, ref
    WebAnswer.model_validate(
        {
            "text": result["web_answer"]["text"],
            "web_refs": [{"url": ref["url"], "title": ref["title"]} for ref in refs],
        }
    )


def test_fixture_question_validates() -> None:
    AskQuestion.model_validate(FIXTURES["question"]["question"])


def test_fixture_outcomes_and_error_match_outcomes_table() -> None:
    keys = set(re.findall(r"^  ([a-z_]+): \{", OUTCOMES_JS, re.MULTILINE))
    assert keys, "no OUTCOMES entries parsed"
    for outcome in FIXTURES["outcomes"]:
        assert outcome["reason"] in keys, outcome
        assert outcome["next_action"] in NEXT_ACTIONS, outcome
    error = FIXTURES["error"]
    assert error["reason"] in keys
    assert error["status"] and error["message"] and error["next_action"] in NEXT_ACTIONS


# --- web_ref label enrichment --------------------------------------------------


def _event(event_type: str, payload: dict[str, Any]) -> Any:
    return SimpleNamespace(event_type=event_type, payload=payload)


class _FakeStore:
    def __init__(self, state: SessionState, events: list[Any]) -> None:
        self._state = state
        self._events = list(events)
        self.appended: list[tuple[str, dict[str, Any]]] = []

    def get(self, session_id: str) -> SessionState | None:
        return self._state if session_id == self._state.id else None

    def append_event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> Any:
        self.appended.append((event_type, dict(payload)))
        return SimpleNamespace(seq=len(self.appended))

    def list_events(self, session_id: str) -> list[Any]:
        return list(self._events)

    def mutate(
        self,
        session_id: str,
        *,
        expected_revision: int,
        fn: Any,
        event_type: str,
        event_payload: dict[str, Any],
    ) -> SessionState:
        assert expected_revision == self._state.revision
        updated = fn(self._state)
        updated.revision += 1
        return updated


URL_OFFICIAL = "https://www.foodsafety.gov/demo/soup-basics"
URL_PLAIN = "https://unlabelled.example.net/a-post"


def test_session_web_sources_carries_recorded_classification() -> None:
    store = _FakeStore(
        SessionState(id="ses-demo", revision=1),
        [
            _event("search_results_retrieved", {"urls": [URL_OFFICIAL, URL_PLAIN]}),
            _event(
                "evidence_evaluated",
                {
                    "evaluations": [
                        {
                            "url": URL_OFFICIAL,
                            "classification": "official_guidance",
                            "decision": "kept",
                        }
                    ]
                },
            ),
        ],
    )
    sources = session_web_sources(store, "ses-demo")
    assert set(sources) == {URL_OFFICIAL, URL_PLAIN}
    assert sources[URL_OFFICIAL].get("classification") == "official_guidance"
    assert "classification" not in sources[URL_PLAIN]


def test_web_label_for_falls_back_to_unclassified() -> None:
    assert web_label_for({URL_OFFICIAL: {"url": URL_OFFICIAL}}, URL_PLAIN) == "unclassified"
    assert web_label_for({}, URL_OFFICIAL) == "unclassified"
    assert (
        web_label_for(
            {URL_OFFICIAL: {"url": URL_OFFICIAL, "classification": "bogus"}},
            URL_OFFICIAL,
        )
        == "unclassified"
    )
    assert (
        web_label_for(
            {URL_OFFICIAL: {"url": URL_OFFICIAL, "classification": "culinary_source"}},
            URL_OFFICIAL,
        )
        == "culinary_source"
    )


def test_handle_finish_labels_each_web_ref() -> None:
    events = [
        _event("search_results_retrieved", {"urls": [URL_OFFICIAL, URL_PLAIN]}),
        _event(
            "evidence_evaluated",
            {
                "evaluations": [
                    {
                        "url": URL_OFFICIAL,
                        "classification": "official_guidance",
                        "decision": "kept",
                    }
                ]
            },
        ),
    ]
    state = SessionState(id="ses-demo", revision=1, current_phase="discover")
    store = _FakeStore(state, events)
    deps = SimpleNamespace(settings=_settings(), request_text=None, on_stage=None)
    directive = AgentDirective(
        decision="finish",
        result=FinishResult(
            web_answer={
                "text": "These demo pages describe the dish in words for discovery.",
                "web_refs": [
                    {"url": URL_OFFICIAL, "title": "Demo Soup Basics"},
                    {"url": URL_PLAIN, "title": "An Unlabelled Post"},
                ],
            }
        ),
        note="demo note",
    )
    outcome = asyncio.run(
        _handle_finish(
            deps,  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
            "ses-demo",
            state,
            1,
            directive,
            lambda dataset_id, source_id: None,
            run_epicure_ok=False,
            pairing_lines=[],
            validation_retries=0,
        )
    )
    assert outcome.stop_reason == "agent_sufficient_evidence"
    refs = outcome.final["web_answer"]["web_refs"]
    assert refs == [
        {"url": URL_OFFICIAL, "title": "Demo Soup Basics", "label": "official_guidance"},
        {"url": URL_PLAIN, "title": "An Unlabelled Post", "label": "unclassified"},
    ]


# --- OUTCOMES coverage ---------------------------------------------------------


def _outcomes_keys() -> set[str]:
    return set(re.findall(r"^  ([a-z_]+): \{", OUTCOMES_JS, re.MULTILINE))


def _outcomes_actions() -> dict[str, str]:
    """reason -> action parsed from outcomes.js as text."""
    return dict(
        re.findall(
            r'^  ([a-z_]+): \{\n    title: ".*",\n    body: ".*",\n    action: "([a-z_]+)",',
            OUTCOMES_JS,
            re.MULTILINE,
        )
    )


def _backend_reasons() -> set[str]:
    reasons = {
        value
        for name, value in vars(domain).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    reasons |= {"stale_revision", "malformed", "internal_error"}
    return reasons


def test_every_outcomes_entry_is_a_backend_reason() -> None:
    backend = _backend_reasons()
    unknown = sorted(key for key in _outcomes_keys() if key not in backend)
    assert unknown == [], f"OUTCOMES keys with no backend reason: {unknown}"


def test_required_page_reasons_have_outcomes_entries() -> None:
    required = {
        "agent_max_steps",
        "agent_tool_budget_exhausted",
        "agent_token_budget_exhausted",
        "agent_wall_clock_exceeded",
        "agent_no_progress",
        "agent_validation_failed",
        "tool_timeout",
        "tool_permission_denied",
        "search_budget_exhausted",
        "search_not_performed",
        "search_unverified",
        "stale_revision",
        "unknown_session",
        "session_unavailable",
        "internal_error",
    }
    missing = sorted(reason for reason in required if reason not in _outcomes_keys())
    assert missing == [], f"missing OUTCOMES entries: {missing}"


def test_remaining_backend_reasons_are_fallback_covered() -> None:
    """Every backend REASON_* that can reach the page either has an
    OUTCOMES entry or is explicitly listed here as next_action-fallback
    covered (the page renders those from next_action: retry shows
    "Try again", change_request shows "Rephrase your request",
    refetch_and_retry reloads, contact_operator is a plain message)."""
    fallback_covered = {
        # Model-output problems: same request retried (next_action retry).
        "schema_failure",
        "validation_rejected",
        "truncated_incomplete_response",
        "empty_response",
        "turn_limit_exceeded",
        "invalid_tool_call",
        "input_budget_exceeded",
        # Transient provider/infra: retry.
        "provider_unavailable",
        "provider_rate_limited",
        "provider_timeout",
        "corpus_unavailable",
        "stream_duration_exceeded",
        "history_pairing_error",
        "tool_unavailable",
        # Request must change: rephrase.
        "provider_refusal",
        "provider_content_filter",
        "unknown_group",
        "invalid_phase_transition",
        "tool_invalid_arguments",
        "tool_permission_denied",
        "scale_missing_servings",
        "convert_unsupported_unit",
        "unknown_question",
        "unknown_option",
        "malformed",
        # Server configuration: plain operator message.
        "tool_not_configured",
        "tool_internal_error",
        "session_store_not_migrated",
        "provider_bad_request",
        "provider_request_error",
        "provider_internal_error",
        "provider_not_found",
        "provider_auth",
        "generation_disabled",
        "stream_event_limit_exceeded",
        "internal_error",
    }
    keys = _outcomes_keys()
    # Terminal success codes are finals, never OUTCOMES entries (same
    # exclusion as tests/test_next_action.py).
    terminal_success = {"agent_sufficient_evidence", "agent_needs_user_input"}
    uncovered = sorted(
        reason
        for reason in _backend_reasons()
        if reason not in keys and reason not in fallback_covered and reason not in terminal_success
    )
    assert uncovered == [], f"backend reasons with no OUTCOMES entry or fallback: {uncovered}"
    for reason in fallback_covered:
        assert next_action_for(reason) in NEXT_ACTIONS, reason


def test_session_budget_reasons_use_new_session_action() -> None:
    """Per-session budgets are spent: the card offers "Start a new
    session" (the header handler), never "Rephrase your request"."""
    actions = _outcomes_actions()
    assert actions, "no OUTCOMES actions parsed"
    for reason in (
        "agent_max_steps",
        "agent_tool_budget_exhausted",
        "agent_token_budget_exhausted",
        "search_budget_exhausted",
    ):
        assert actions.get(reason) == "new_session", (reason, actions.get(reason))


def test_outcomes_has_no_budget_number_claims() -> None:
    """Budgets are settings, so outcomes.js must not hard-code them:
    no digit next to steps, tool calls or second."""
    found = re.search(
        r"\d[^\n]{0,20}(steps?|tool calls?|seconds?)"
        r"|(steps?|tool calls?|seconds?)[^\n]{0,20}\d",
        OUTCOMES_JS,
        re.IGNORECASE,
    )
    assert found is None, f"hard-coded budget number in outcomes.js: {found.group(0)!r}"


# --- agent-stream disconnect with the /ui middleware ---------------------------


class _BlockingAgentProvider:
    """Provider that never answers: the run stays in its first turn."""

    def __init__(self) -> None:
        self.cancelled = False

    async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("should have been cancelled")


def _agent_app(store: Any, settings: Settings, provider: Any) -> Any:
    from fastapi import FastAPI

    from culinary_copilot.api.agent import build_router
    from culinary_copilot.tools.registry import ToolContext

    app = FastAPI()
    app.include_router(
        build_router(
            settings=settings,
            engine=None,
            session_store=store,
            provider=provider,
            tool_context=ToolContext(settings=settings, engine=None, session_store=store),
            recipe_resolver=lambda dataset_id, source_id: None,
        )
    )
    return app


async def _agent_stream_then_disconnect(app: Any, session_id: str, spec: str) -> list[bytes]:
    payload = b"{}"
    first_chunk = asyncio.Event()
    delivered = False
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await first_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            if first_chunk.is_set() and spec >= "2.4":
                raise OSError("client gone")
            chunks.append(message["body"])
            first_chunk.set()

    path = f"/api/v1/sessions/{session_id}/agent/stream"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("testclient", 1),
        "server": ("testserver", 80),
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=10)
    except Exception:  # noqa: S110 - a disconnect may surface several ways
        pass
    await asyncio.sleep(0.1)
    return chunks


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
@pytest.mark.parametrize("middleware", ["absent", "present"])
def test_agent_stream_disconnect_cancels_run(spec: str, middleware: str) -> None:
    """The agent stream the page uses cancels the run on disconnect,
    with or without the /ui security headers middleware installed."""
    from culinary_copilot.api.app import _ui_security_headers

    provider = _BlockingAgentProvider()
    state = SessionState(id="ses-disconnect", revision=1, current_phase="discover")
    store = _FakeStore(state, [])
    app = _agent_app(store, _settings(), provider)
    if middleware == "present":
        app.add_middleware(_ui_security_headers)
    chunks = asyncio.run(_agent_stream_then_disconnect(app, state.id, spec))
    text = b"".join(chunks).decode()
    assert "event: final" not in text and "event: error" not in text
    assert provider.cancelled is True


# --- diet selector (2026-10-05 demo finding) ------------------------------------


def test_diet_selector_values_are_checked_diets() -> None:
    """The page's diet values match api.js and are diets the validator checks.

    Free-text "vegetarian" never became a session constraint, so the
    vegetarian check did not run; the selector sends it as a hard
    constraint at session creation instead.
    """
    from culinary_copilot.agent.validate import check_dietary_option

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    api_js = (WEB_DIR / "js" / "api.js").read_text(encoding="utf-8")
    select = re.search(r'<select id="diet-select">(.*?)</select>', html, re.S)
    assert select is not None
    page_values = [v for v in re.findall(r'<option value="([^"]*)">', select.group(1)) if v]
    diets = re.search(r"export const DIETS = \[([^\]]*)\];", api_js)
    assert diets is not None
    js_values = re.findall(r'"([^"]+)"', diets.group(1))
    assert page_values == js_values == ["vegetarian", "vegan"]
    assert "dietary_constraints: [diet]" in api_js
    doc = {"ingredients": [{"canonical": "chicken", "quantity_text": "500 g"}]}
    for value in page_values:
        errors, entry = check_dietary_option(0, {"source_id": "x"}, doc, value)
        assert entry.get("status") != "not_checked", value
        assert errors, f"{value}: chicken must violate"


@pytest.mark.parametrize("enabled", [True, False])
def test_agent_router_wires_search_provider_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """WEB_SEARCH_ENABLED passes the app provider to search_web; off by default.

    Demo finding (2026-10-05): the server never wired a search provider,
    so with the toggle on every search reported tool_not_configured.
    """
    import culinary_copilot.tools as tools_pkg
    from culinary_copilot.api.agent import build_router

    captured: dict[str, Any] = {}
    real = tools_pkg.build_tool_context

    def _capture(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(tools_pkg, "build_tool_context", _capture)
    provider = object()
    build_router(
        settings=_settings(web_search_enabled=enabled, epicure_enabled=False),
        engine=None,
        session_store=SimpleNamespace(),  # type: ignore[arg-type]
        provider=provider,
    )
    assert captured["search_provider"] is (provider if enabled else None)
    assert _settings().web_search_enabled is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_quantities_show_improper_fractions_as_mixed_numbers() -> None:
    # 2026-10-07 live session: "11/2 lb" (an exact 5 1/2) was misread.
    script = (
        "import { mixedAmount } from './src/culinary_copilot/web/js/render.js';"
        "console.log(JSON.stringify(['11/2', '3/2', '1/2', '4/2', '2', null].map(mixedAmount)));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(out.stdout) == ["5 1/2", "1 1/2", "1/2", "2", "2", None]
