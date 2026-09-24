# Phase 4 streaming walkthrough (offline, `curl -N`)

Nothing before `final` is a recommendation. `stage` events are progress
only (counts and stable codes); recipe content appears only in `final`
when the outcome is `recommendation`, and never in `error`.

## Setup (no paid calls)

```sh
cp .env.example .env  # preserve an existing .env
# Generation stays disabled: the walkthrough below shows stage + error
# events with zero provider calls.
grep -E 'LLM_RECOMMENDATION_ENABLED|REC_STREAM' .env
uv sync --locked
docker compose up -d db
make dev
# New terminal:
curl --fail http://localhost:8000/health/ready
```

## 1. Create a clarification group (rule-only, offline)

```sh
GRP=$(curl -s -X POST http://localhost:8000/api/v1/clarification/groups \
  -H 'Content-Type: application/json' \
  -d '{"message": "chicken dinner", "dish": "chicken curry",
       "request": {"ingredients": ["chicken"]}, "use_llm": false}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["group_id"])')
echo "group: $GRP"
```

Answer until `ready_for_retrieval` is true (see
[clarification](clarification.md) for the answer flow), noting
`request_revision` / `group_revision` from each response.

## 2. Stream with generation disabled (stage + error, zero spend)

```sh
curl -N -X POST http://localhost:8000/api/v1/recommendations/stream \
  -H 'Content-Type: application/json' \
  -d "{\"group_id\": \"$GRP\", \"request_revision\": 1, \"group_revision\": 1}"
```

Expected (503 pre-stream, no events — disabled generation returns HTTP,
not SSE):

```text
HTTP 503 {"reason": "generation_disabled", ...}
```

To see SSE events without a paid call, enable generation with an
unreachable provider is NOT needed: run the bundled offline test that
drives the same endpoint with a fake provider instead:

```sh
uv run pytest tests/test_phase4_streaming_telemetry.py tests/test_phase4_review_fixes.py -q
```

## 3. Stream events (generation enabled, fake/offline provider)

With `LLM_RECOMMENDATION_ENABLED=true` and a fake/offline provider (tests
only — no key), a successful stream looks like:

```text
event: stage
data: {"v": "v1", "type": "stage", "seq": 0, ..., "stage": "accepted", "detail": {...}}

event: stage
data: {"v": "v1", "type": "stage", "seq": 1, ..., "stage": "readiness", ...}

event: stage
data: {"v": "v1", "type": "stage", "seq": 2, ..., "stage": "epicure", ...}

...

event: stage
data: {"v": "v1", "type": "stage", "seq": 5, ..., "stage": "provider_request", "detail": {"turn": 1}}

event: stage
data: {"v": "v1", "type": "stage", "seq": 6, ..., "stage": "provider_turn", "detail": {"turn": 1, "attempts": 1, "tool_calls": 0}}

...

event: final
data: {"v": "v1", "type": "final", "seq": 9, ..., "body": {"outcome": "recommendation", ...}}
```

A failure looks like stages followed by exactly one error and no final:

```text
event: stage
data: {..., "stage": "evidence", ...}

event: error
data: {"v": "v1", "type": "error", "seq": 6, "status": 502,
        "reason": "validation_rejected", "message": "...", "detail": {...}}
```

Mid-run edits (answer/replan in another terminal while streaming) end
with:

```text
event: error
data: {"v": "v1", "type": "error", "seq": 7, "status": 409,
        "reason": "stale_revision", ...}
```

## Notes

- `provider_request` (turn number) is sent before each provider turn, so a
  long wait is visibly "waiting on the provider"; `provider_turn`
  (turn/attempt/tool-call counts) follows when the turn completes. Neither
  carries model output.
- Nothing before `final` is a recommendation. Every stream ends with
  exactly one `final` or one `error`, including unexpected server errors
  (500 `internal_error`).
- `final` for `clarification` / `insufficient_evidence` outcomes is still
  a `final` (same body as non-streaming), not an error.
- Keep-alive comments (`: keep-alive`) may appear on idle streams.
- Limits: `REC_STREAM_MAX_EVENTS=100` (including the terminal event),
  `REC_STREAM_MAX_DURATION_S=120`, `REC_STREAM_KEEPALIVE_S=10` (see
  `.env.example`).
