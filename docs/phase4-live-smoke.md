# Phase 4 live streaming smoke test (prepared, NOT run)

Optional, at most two cases. No live call is authorized by Phase 4.
Do not run without separate owner approval attaching a ceiling.

- Model (exact): `gpt-6-luna` (`LLM_REC_MODEL=gpt-6-luna`,
  `LLM_REC_REASONING_EFFORT=none`; the model does not accept `minimal`).
  This would be the first live run on `gpt-6-luna`: the 6500-token output
  cap and the Phase 3 results were measured on `gpt-5-nano`.
- Verified pricing (2026-09-24,
  <https://developers.openai.com/api/docs/pricing?latest-pricing=standard>,
  Standard, per 1M tokens): input $0.10, cached input $0.01, cache writes
  $0.125, output $0.50. For the reservation pass the cache-write rate as
  the input price (`--price-input-per-1m 0.125 --price-output-per-1m 0.50`)
  so cached-prompt writes cannot exceed it. Re-verify on run day; if
  unverifiable, do not submit.
- Reservation per case (conservative, same model as Phase 3 runner):
  input tokens ≤ UTF-8 bytes of the exact serialized payload;
  output ≤ `LLM_REC_MAX_OUTPUT_TOKENS` (6500) per attempt covering
  reasoning + text; attempts ≤ `REC_MAX_PROVIDER_TURNS ×
  (LLM_REC_MAX_RETRIES + 1)`. Reserve before submission; release on
  no-provider-call outcomes.
- Dollar ceiling: $0.10 aggregate for ≤2 cases (reservation-enforced,
  stop when the next reservation would exceed it). Illustrative
  reservation at $0.125 / $0.50, using the Phase 3 follow-up payload sizes
  (recompute from a fresh dry-run before any run): ordinary case
  2 attempts × (9058 bytes + 6500 output) ≈ $0.0088; tool case
  2 attempts × (2212 + 54500 bytes) + 4 × 6500 output ≈ $0.0272; total
  ≈ $0.036.
- Cases: 1× ordinary selection (default path), 1× `tool_mode: true`
  native `get_recipe` path. Both against the local corpus read-only.
- Stop rules: any unsupported claim, fidelity mismatch, 3 consecutive
  paid failures (n/a at 2 cases), provider access error, or spend
  projection breach — stop immediately, keep `summary.json`.
- Stream checks per case: stage ordering + gap-free seq, a
  `provider_request` stage before each provider turn and a
  `provider_turn` stage after it, exactly one `final` on success with
  identical body to non-streaming for the same revisions, exactly one
  `error` (409 mid-run if exercised) with no `final`, and one telemetry
  event showing model/effort/pricing version, per-turn usage, and
  estimated cost (None when any sent turn's usage is unknown). Compare
  telemetry cost with the runner's usage-derived cost: the telemetry
  prices input at $0.10, the reservation at $0.125.
- Command shape (illustrative; needs approval + key):

```sh
# Terminal 1: API with key + enabled generation (local only).
LLM_RECOMMENDATION_ENABLED=true OPENAI_API_KEY=... make dev
# Terminal 2: stream case 1 with curl -N (see walkthrough).
curl -N -X POST http://localhost:8000/api/v1/recommendations/stream \
  -H 'Content-Type: application/json' -d '{"group_id": "<grp>", ...}'
```

Record actual spend, request ids, and telemetry excerpts. This file is
the plan; no live traffic was sent for Phase 4.
