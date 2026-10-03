# Phase 3 live evaluation plan (RUN 2026-09-29/30; see LIVE_REVIEW.md)

Status (2026-09-30): the owner ran attempts 1–9. The results, findings
and recorded spend ($0.1249 of $0.15) are in `LIVE_REVIEW.md`. Any
further live run needs the owner's explicit go-ahead. The text below is
the original plan, kept as the historical record.

Original status: runner built (`evals/phase3_agent/live_run.py`), tested with
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

## 8 live scenarios (`evals/phase3_agent/live_scenarios_v2.json`, frozen; v1 kept byte-identical)

Chicken dinner to plan, yogurt ask-and-resume (frozen answer),
direct lentil request, vegetarian conflict, empty retrieval (asks
with a concrete lemon-dessert choice), Epicure unavailable
(per-scenario `epicure_enabled: false`, never `.env`), pure
technique question, roast pairing. Expectations updated for the new
Epicure policy: the direct request consults Epicure and may return
one recipe (`direct_dish_request`). no-progress is deliberately
absent: it may not trigger with a real model, and its coverage stays
with the offline failure-injection tests.

### v1 -> v2 diff (owner-approved 2026-09-30)

The runner defaults to v2 (`--scenarios-file` selects v1); both files
are hash-frozen and the summary records the file and its sha256. v2
changes only:
- scripted answers are self-contained sentences (yogurt: "No, I don't
  have yogurt."), sent as the answer to whatever question is pending;
- the fake-only `question_contains` ("lemon dessert") is removed;
- technique-question expects kind `technique_answer` with
  `epicure: skip:simple_technique_question` and `min_options` 0;
- roast-pairing expects `min_epicure_lines` 3;
- everything else is unchanged (v1 sha
  `2c9b067c45ba019800e2a7c1398a1e99d3338cf33fa28fb7b6b1c5a732cab302`).

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

## Known orphan session (2026-09-29, recorded not removed)

The first live attempt crashed before any model call
(`build_tool_context` called with positional args; fixed since, and
the runner now builds the provider and context before creating any
session row). Its first scenario session,
`ses-live-live-chicken-a1-` plus 6 random hex chars, exists in the
app DB with no model call and no spend. It is left in place
(`session_events` are append-only, enforced by a trigger), and the
next run's isolation baseline includes it: pre/post snapshots compare
counts, so a pre-existing row is neutral as long as nothing writes to
it.

## Retry-inclusive dollar bound

- Bound: `8 sessions x 2 attempts x $0.009 = $0.144`, rounded to **$0.15**.
- **$0.15** fits inside the **$1.00 total Milestone 3 ceiling**
  (Checkpoint 0) and leaves **$0.85 for Phases 4, 5 and 7**.
- Cumulative ledger (`data/phase3-live/spend-history.json`, gitignored):
  every run appends its entries (with the attempt number) and preflight
  refuses when the remainder cannot fit a first turn. Seeded with
  conservative recorded amounts — attempt 3 $0.0034661 (kept-ambiguous
  400), attempt 4 $0.0059122 (reconciled usage plus one kept-ambiguous
  400) and attempt 5 $0.0239167 — totalling **$0.033295** and leaving
  **$0.1167** of the $0.15 ceiling.

## Target database (owner decision)

- Option A: session rows in the application database (needs the approved
  migration rehearsal first; Phase 3 itself adds no migration).
- Option B: a disposable copy (restore the pre-migration backup to a
  `*_check_*` database and run there; nothing touches the app DB).
- The owner picks A or B at the checkpoint. The command below shows
  option A; for B replace `--expect-db-name` with the disposable name.

## Exact live command (paste-safe: no comment lines)

```sh
EPICURE_ENABLED=true EMBEDDINGS_ENABLED=true HF_HUB_OFFLINE=1 LLM_RECOMMENDATION_ENABLED=true uv run python evals/phase3_agent/live_run.py --live --yes --ceiling-usd 0.15 --expect-db-name culinary_copilot --expect-db-host localhost
```

This uses `DATABASE_URL` from `.env` (target the correct database per
the owner conditions before running). `EMBEDDINGS_ENABLED=true` is on
the command line because Checkpoint 0 lets the agent choose fulltext
or vector per call, which needs query embeddings. `EPICURE_ENABLED=true`
is on the command line because every live scenario except
`live-epicure-unavailable` expects Epicure (attempt 5 ran with the
`.env` default `false`, so every Epicure call failed). The runner refuses
without `--live --yes --ceiling-usd` (any ceiling above $0.15
refused), without the DB guards, when `HF_HUB_OFFLINE` is not `1`,
when the model or pricing is unknown, when retry settings are not
zero, when Epicure is disabled for any scenario but
`live-epicure-unavailable`, when the Epicure cache-only probe fails
(one `find_balanced_pairings("chicken", k=1)` per backend —
core, cooc, chem, plus substitutions — recorded in the preflight
JSON), when embeddings are enabled but no query-embedding provider
can be built, or when the technique snapshot is unverifiable (use
option B then). Preflight records vector availability (recipe and
technique embedding counts) plus the scenario keys and max attempts.
`--fake` runs the full 8-scenario
pipeline against the fake provider on a disposable database (dropped
afterward); raw output goes under `data/phase3-live/` (git-ignored).
`--scenarios key1,key2` runs a subset (default all; unknown keys
refused); `--max-attempts {1,2}` caps runner-level retries per
scenario (default 2). Both are recorded in the summary and the
preflight JSON, and each scenario record carries
`expected_stop_matched` (final stop vs `expected.stop_reason`).

## Runner design (`evals/phase3_agent/live_run.py`)

Pre-call spending reservations: before every paid call the runner
reserves input tokens plus the maximum permitted output against the
$0.15 run ceiling, and refuses any call that does not fit (finishing
fewer scenarios is acceptable).

Input counting: a true-upper-bound local count — the UTF-8 byte
length of the full serialized request the SDK will send (input
items, the tools array and the text.format JSON schema in its strict
converted form, as the SDK sends it), plus a
fixed per-item and per-request overhead for Responses-envelope
framing. Assumption (documented in `live_run.py`): one token spans
at least one UTF-8 byte of the serialized request, so the byte
length strictly dominates any chars-per-token heuristic. Query
embeddings use the same approach over the serialized texts. A
Responses input-token endpoint exists and the installed SDK exposes
`client.responses.input_tokens.count`, but the official docs do not
confirm it is unbilled, so a counting call could itself cost money
and break the ledger. The local bound is deterministic, offline, and
testable.

Breach behaviour: after every paid call the runner compares the
reported usage with the reservation (input tokens, output tokens
and USD). Any excess is recorded in the ledger as
`reservation_breach` (the call was billed; the bound, not the money,
is what failed) and the run stops at once with
`contact-operator`: the scenario is marked
`stopped: contact-operator` with no grade, and no further calls are
made — every remaining scenario is listed as
`not_run: contact-operator`.

Unacknowledged-breach preflight: preflight refuses when
`spend-history.json` contains a `reservation_breach` entry with no
matching acknowledgment. Acknowledging is a manual owner step: add
an object with the run's `run_utc`, the entry's `label`, and the
owner's name to the history file's top-level
`breach_acknowledgments` list, e.g.
`{"run_utc": "2026-09-30T12:00:00Z", "label": "model-turn-5",
"by": "owner", "note": "bound fixed in P3-L-14"}`. Never acknowledge
a breach to retry the same bound — fix the bound first.

Ledger: `SpendLedger` records reserve → reconcile (reported usage
replaces the reservation, remainder released) or keep (ambiguous
failure stays spent; unsent calls release). Model turns go through
`LedgerModelProvider` (the call's own `max_output_tokens` cap wins
when the loop passes one, otherwise the configured per-turn
maximum); query embeddings for both `search_recipes` and
`search_techniques` go through `LedgerEmbedProvider` (one shared
context provider serves both tools). Every entry is priced by its
own model — chat turns at the chat rate, embeddings at
`text-embedding-3-small` $0.02/1M from `embeddings/registry.py` —
and records the model, kind, and pricing version used. One campaign
ledger covers agent turns, search sub-requests, tool fees,
embeddings and retries, capped at $0.10 for Phase 5
(`evals/phase3_agent/live_scenarios_phase5.json`, prepare-only).

Per-search accounting (owner decision 4 — PROVISIONAL): call fee
($0.01) + content allowance (config default 128k tokens) +
byte-bound sub-request input + fixed output cap, about $0.025 each.
Status: provisional — not an established upper bound; the live check
needs an owner decision (either a supported bound, or an explicit
change from a hard guarantee to an estimate with acknowledged
overrun risk). Three searches cost $0.03 in call fees alone. The
runner completes what fits; it promises no session count. Preflight
refuses live mode while the reservation is provisional unless
`--acknowledge-provisional-reservation` records decision 4.

Retry policy: provider-internal retries are forced to zero on a
runner settings copy (`llm_app_max_retries=0`, `embed_max_retries=0`;
preflight refuses anything else), so one wrapped call equals exactly
one billed request. Retries happen only as runner-level scenario
attempts, each reserved separately. The embedding provider retries
internally by default, so it gets the same treatment (reserve and
keep per single attempt).

Worst-case reservation math (luna $0.10 in / $0.50 out per 1M):
per turn at most ~121,000 input tokens (a payload at the 30k
chars/4-token ceiling is ~120k chars, and the byte-length bound
counts every byte plus overhead) plus at most 6,500 output tokens:
$0.0121 + $0.00325 ≈ $0.0154. The bound is deliberately loose — it
is reconciled down to reported usage after each call — so a full 8
step worst-case session reserves up to ~$0.123 against the $0.15
ceiling; typical sessions (~11.2k input + ~0.35k output ≈ $0.0013)
put the planned 8 scenarios × 2 attempts (16 sessions ≈ $0.021)
comfortably inside the cap, with query embeddings negligible
(~$0.00002 for 16 queries).

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

## Stop conditions (live, as implemented in `live_run.py`)

- A reservation refusal ends the run cleanly via the dedicated
  `BudgetExhausted` error (never a provider error): the scenario is
  marked `not_completed: budget` with no further attempt and no grade,
  and every remaining scenario is listed as `not_run: budget`. The
  overrun guard is never counted as an agent or provider failure.
- Stop the whole evaluation (`stopped_early` with reason and
  after-scenario in the summary) on any `provider_auth` or
  `contact_operator` terminal, on a preflight-class failure mid-run,
  or after 2 sessions in a row stop with `agent_no_progress` or
  `agent_validation_failed` (budget stops do not feed the streak).
- Abort a session run on any `unknown_session`, `stale_revision`
  (concurrent run), or token-ceiling breach.
- Every trajectory (stage events, tool outcomes, stop reason, final) is
  kept in `session_events` and exported to `live-results/` for the Phase 7
  comparison against fixed retrieval modes.
