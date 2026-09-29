# Phase 3 live evaluation plan (RUNNER BUILT, NOT RUN)

Status: runner built (`evals/phase3_agent/live_run.py`), tested with
fakes on disposable databases only. **The live run is NOT authorized:
do not run it until the owner says "run it".** Re-verify pricing
(`EMBED_PRICING_VERSION`, `PRICING_VERSION`) immediately before any
live attempt; model and embedding prices change, and the figures below
go stale.

Owner checkpoint: review `REVIEW.md` (10 offline trajectories) and approve
or correct the budget and the database choice below before anything else
happens. No live call, embedding, download or application-database write
has been made for Phase 3, and none is authorized by this plan alone.
Each live run still needs its own explicit go-ahead with its share and
stop conditions (Checkpoint 0, budget item).

## 8 live scenarios (`evals/phase3_agent/live_scenarios.json`, frozen)

Chicken dinner to plan, yogurt ask-and-resume (frozen answer),
direct lentil request, vegetarian conflict, empty retrieval (asks
with a concrete lemon-dessert choice), Epicure unavailable
(per-scenario `epicure_enabled: false`, never `.env`), pure
technique question, roast pairing. Expectations updated for the new
Epicure policy: the direct request consults Epicure and may return
one recipe (`direct_dish_request`). no-progress is deliberately
absent: it may not trigger with a real model, and its coverage stays
with the offline failure-injection tests.

## Model and pricing (repo-recorded, re-verify before running)

- Model: `gpt-6-luna` (the only model in `src/culinary_copilot/llm/models.py`).
- Pricing, from `ModelPrices` in that file (`PRICING_VERSION
  "2026-09-24-luna-v1"`, `date_verified "2026-09-24"`, Standard tier,
  synchronous Responses):
  - input `$0.10` / 1M tokens (cached input `$0.01`, not counted: usage
    records do not capture cached-input counts, so every input token is
    priced at the list input rate — see
    `recommendations/pricing.py`);
  - output `$0.50` / 1M tokens (includes reasoning tokens).
- Source: `https://developers.openai.com/api/docs/pricing?latest-pricing=standard`
  (recorded in the registry entry).
- **Flagged for owner re-verification**: re-check the pricing page and
  bump `PRICING_VERSION` if anything changed before the first live call.

## Token ceilings (measured, with headroom)

Pre-turn input estimates count everything sent (items + offered tool
defs + directive schema), chars/4 — the repo has no token estimator.
Packet measurement (`generate.py`, scripted inputs):

| Measure | Max observed |
|---|---|
| Single turn input (full estimate) | 2,918 |
| Per-session input total | 10,959 (normal flow, 2 runs / 4 turns) |
| Per-session output total (estimated) | 349 |

Realistic session (same 4-turn shape, real recipe sizes from the
committed local corpus `data/recipe-import/normalized.jsonl` — small
3.3k chars / median 4.5k / large 6.3k chars; corpus p90 6.3k, max
34.2k; app DB never read): fixed part ~2,620/turn, session input
~11.1–11.3k. Recorded live structured outputs reach ~2k/call. The
enforced ceilings keep headroom over both:

- `AGENT_INPUT_TOKEN_CEILING=30000` (~2.6x measured; covers a full
  8-step session), `AGENT_OUTPUT_TOKEN_CEILING=12000` (~6x max recorded
  per-call output across a full session)
  (also the code defaults in `config.py`, documented in `.env.example`).
- The loop sums provider-reported usage from `session_events` (estimates
  only when a turn reports none) and stops with
  `agent_token_budget_exhausted` before a turn that could cross either
  ceiling. Each turn's output is hard-capped at `min(6500 per-turn max,
  tokens remaining)`; below 500 remaining the turn is not sent, and a
  response truncated by the cap stops the run the same way — overshoot
  is impossible since 8 steps bound totals by construction.
- Worst-case live session: `30000/1e6*0.10 + 12000/1e6*0.50 = $0.009`.
- Query embeddings for vector-mode turns are provider calls too and come
  out of the same bound (counted at the embedding model's recorded rate
  at run time; full-text turns cost nothing extra).

## Scope: at most 8 sessions

Budget exhaustion and wall-clock stops are covered offline, so they are
dropped from the live cases. The remaining packet cases go live:

1. normal full flow (recommend, select, plan),
2. yogurt ask-and-resume,
3. direct recipe request,
4. hard-constraint conflict,
5. empty retrieval,
6. tool failure (Epicure not configured),
7. Epicure skip,
8. no-progress.

Each session may be retried once (same ceilings): at most 2 attempts.

## Retry-inclusive dollar bound

- Bound: `8 sessions x 2 attempts x $0.009 = $0.144`, rounded to **$0.15**.
- **$0.15** fits inside the **$1.00 total Milestone 3 ceiling**
  (Checkpoint 0) and leaves **$0.85 for Phases 4, 5 and 7**.

## Target database (owner decision)

- Option A: session rows in the application database (needs the approved
  migration rehearsal first; Phase 3 itself adds no migration).
- Option B: a disposable copy (restore the pre-migration backup to a
  `*_check_*` database and run there; nothing touches the app DB).
- The owner picks A or B at the checkpoint. The command below shows
  option A; for B replace `--expect-db-name` with the disposable name.

## Exact live command (paste-safe: no comment lines)

```sh
HF_HUB_OFFLINE=1 uv run python evals/phase3_agent/live_run.py --live --yes --ceiling-usd 0.15 --expect-db-name culinary_copilot --expect-db-host localhost
```

This uses `DATABASE_URL` from `.env` (target the correct database per
the owner conditions before running). The runner refuses without
`--live --yes --ceiling-usd` (any ceiling above $0.15 refused),
without the DB guards, when `HF_HUB_OFFLINE` is not `1`, when the
model or pricing is unknown, or when the technique snapshot is
unverifiable (use option B then). `--fake` runs the full 8-scenario
pipeline against the fake provider on a disposable database (dropped
afterward); raw output goes under `data/phase3-live/` (git-ignored).

## Runner design (`evals/phase3_agent/live_run.py`)

Pre-call spending reservations: before every paid call the runner
reserves input tokens plus the maximum permitted output against the
$0.15 run ceiling, and refuses any call that does not fit (finishing
fewer scenarios is acceptable).

Input counting: conservative local bound — UTF-8 bytes/3 over the
full payload (input items, tools, schema), never below chars/4. A
Responses input-token endpoint exists and the installed SDK exposes
`client.responses.input_tokens.count`, but the official docs do not
confirm it is unbilled, so a counting call could itself cost money
and break the ledger. The local bound is deterministic, offline, and
testable; bytes/3 strictly dominates chars/4 for any UTF-8 text.

Ledger: `SpendLedger` records reserve → reconcile (reported usage
replaces the reservation, remainder released) or keep (ambiguous
failure stays spent; unsent calls release). Model turns go through
`LedgerModelProvider`; query embeddings (if vector mode ever runs)
through `LedgerEmbedProvider`. Dollars use
`recommendations/pricing.py` at the preflight-recorded
`PRICING_VERSION`.

Trial isolation: a fresh session per attempt (a retry never inherits
answers or evidence); ask-and-resume stays inside one session with
scripted answers frozen in `live_scenarios.json` (sha256 frozen
before any run); the runner keeps a manifest of every session it
created; pre/post snapshots (recipes, quarantine and technique
counts plus a checksum over `technique_documents` hashes) prove no
writes outside sessions/session_events. All verified by
`tests/test_phase3_live.py` on disposable databases. If the snapshot
is unverifiable at preflight, the runner refuses and tells the owner
to use option B.

Database A only with that verified isolation, otherwise B. The run
happens after the P3-A-01/02 fixes and before Phase 5.

## Stop conditions (live)

- Stop the whole evaluation when spend reaches `$0.15` (usage summed via
  `recommendations/pricing.py::estimate_cost_usd`), when 2 sessions in a
  row stop with `agent_no_progress` or `agent_validation_failed`, or on
  any `provider_auth` / `contact_operator` error.
- Abort a session run on any `unknown_session`, `stale_revision`
  (concurrent run), or token-ceiling breach.
- Every trajectory (stage events, tool outcomes, stop reason, final) is
  kept in `session_events` and exported to `live-results/` for the Phase 7
  comparison against fixed retrieval modes.
