# Phase 3 evaluation procedure

Phase 3 is accepted as a bounded backend milestone. The final result and limitations
are in [the closure record](../docs/phase3-closure.md). This procedure is for future
explicitly authorized checks; it does not authorize new provider calls.

## Retained cases and evidence

- `cases/phase3_live_cases.json`: historical ten-case behavior suite, including
  synthetic injection, abstention and tool paths. Historical free-text outputs
  do not establish behavior of the current typed-proposition contract.
- `cases/phase3_followup_cases.json`: two-case repaired-contract check.
- Local `results/phase3_followup_live/`: final ordinary and native-tool evidence.
- Local `results/phase3_live_run*/`: historical failures and earlier successes.
- [Review packet specification](phase3_review/README.md): source comparisons and
  enrichment proposals. Source text and model outputs are ignored, not committed.

## Offline preparation

Use fresh output directories; never overwrite a historical run. These commands
read the local corpus but do not call a model provider. Cached Epicure assets must
already be available for its enabled path; keep downloads disabled.

```sh
HF_HUB_OFFLINE=1 uv run python scripts/recommendations_live/runner.py \
  --cases evals/cases/phase3_followup_cases.json \
  --out evals/results/phase3_new_dry

HF_HUB_OFFLINE=1 uv run python scripts/recommendations_live/runner.py \
  --cases evals/cases/phase3_followup_cases.json \
  --out evals/results/phase3_new_rehearsal \
  --dry-state evals/results/phase3_new_dry/state.json --rehearse-transport
```

The transport rehearsal uses a localhost stub and the real client path. It is not
live model-quality evidence. `--rehearse` provides SDK-stub scenarios as well;
see `runner.py --help` for options.

## Any future live run

Obtain task authorization for the exact cases, model and aggregate spend ceiling.
Verify current model parameters/prices and recompute reservations for serialized
inputs, schemas, continuation items, output caps and all permitted attempts.
Never reuse historical price assumptions as current verification.

Supply `--live`, `--model`, `--ceiling-usd`, `--price-input-per-1m`,
`--price-output-per-1m`, `--dry-state`, and a fresh `--out` directory.
Use `--stop-on-failure` for a focused repair check. Keep `HF_HUB_OFFLINE=1`.
Missing access, unknown configuration or an excessive reservation blocks submission.
A submitting record must be reconciled before any retry; never silently resubmit.

## Review and accounting

- Separate execution, source fidelity, usefulness and constraint support.
- Validate exact identity, source snapshot and server-rendered proposition wording.
- Record offered candidate identities/fingerprints and tool envelopes, not private
  reasoning, secrets or full prompts in public logs.
- Report known usage-derived cost separately from unknown-usage reservations.
- Zero provider calls cost zero. Preserve prior-turn accounting when a later turn fails.
- Stop on unsupported published claims, fidelity errors, access/configuration errors,
  budget exhaustion, or the configured failure threshold. Persist a summary on exit.
- Do not repair and repeat automatically; report the cause and evidence first.

## Archive

Superseded plans and redundant dry runs/rehearsals are checksum-preserved locally in
`data/phase3-archive/20260924/`. The current packet builder still uses retained live
artifacts at their original paths. Application data and backups were not altered.
