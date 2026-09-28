"""Capture raw SSE from the recommendation stream endpoint (offline).

Drives the real endpoint with the Phase 4 test fakes: a fake model and a
stubbed recipe lookup (one "Chicken Curry" fixture). No paid calls, no
database. Run from the repository root:

    uv run python evals/phase4_review/capture_sse.py > evals/phase4_review/sse_transcript.txt
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

sys.path.insert(0, "tests")
import test_phase4_streaming_telemetry as t  # noqa: E402


def run(title: str, provider_factory: Callable[[Any, Any], Any]) -> None:
    store, state, group = t._store_with_group()
    app = t._app(store, t._settings(), provider_factory(store, group))
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    repo_search, repo_get = t._patched_repo()
    with repo_search, repo_get:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body)
    print(f"===== {title} =====")
    print(f"HTTP {resp.status_code} {resp.headers['content-type']}\n")
    print(resp.text)


class EditingProvider(t.FakeApplicationProvider):  # type: ignore[misc]
    """Bumps the stored revisions mid-run, like an edit from another tab."""

    def __init__(self, store: Any, group: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._store = store
        self._group = group

    async def complete_recommendation(self, *, system: Any, user: Any, response_model: Any) -> Any:
        state, group = self._store.get_snapshot(self._group.group_id)
        state.revision += 1
        group.revision += 1
        self._store.save_state(state)
        self._store.save_group(group)
        return await super().complete_recommendation(
            system=system, user=user, response_model=response_model
        )


def main() -> None:
    run(
        "1. success (fake model picks a real recipe)",
        lambda s, g: t.FakeApplicationProvider(script=[t._selection()]),
    )
    run(
        "2. model cites a recipe that was not retrieved (validation rejects it)",
        lambda s, g: t.FakeApplicationProvider(script=[t._selection(source_id="no-such-id")]),
    )
    run(
        "3. request edited in another tab while streaming",
        lambda s, g: EditingProvider(s, g, script=[t._selection()]),
    )


if __name__ == "__main__":
    main()
