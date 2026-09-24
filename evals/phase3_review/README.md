# Phase 3 source-level review packet

Execution plan §3 item 5: a small source-level review packet and separate
proposed enrichment fixtures.

**AI-assisted review accepted by the project owner on 2026-09-24; not
independently human-verified or culinarily validated.** See
[owner_decisions.json](owner_decisions.json). Original AI specs and packet
outputs remain immutable pre-acceptance snapshots; their pending labels are
superseded by that separate decision, not rewritten as human review.

## Files

| File | In Git | Contents |
|---|---|---|
| `evals/phase3_review/examples.json` | yes | Six examples: artifact and case references, exact identities, what was not recorded, four separate judgments, defects, unknowns, historical vs current-policy behavior |
| `evals/phase3_review/enrichment_proposals.json` | yes | Three proposals: identity, source fingerprint, proposed change or explicit abstention, evidence references with sha256 of the exact stored values, what the evidence does and does not establish |
| `owner_decisions.json` | yes | Owner acceptance, proposal decisions and hashes of accepted inputs |
| `scripts/recommendations_live/review_packet.py` | yes | Builder and validator |
| `evals/results/phase3_review/manifest.json` | yes | Hashes of inputs, artifacts, stored sources and outputs; no source text |
| `evals/results/phase3_review/packet.{json,md}` | no (ignored) | Resolved packet: complete ingredient and instruction lists, saved responses |
| `evals/results/phase3_review/enrichment_fixtures.{json,md}` | no (ignored) | Proposals with the exact excerpts resolved |

Source text and saved model responses stay out of Git (AGENTS.md). The saved
live artifacts the packet reads (`evals/results/phase3_live_run5/`,
`phase3_followup_live/`) are also untracked and contain recipe text and model
output; all Phase 3 live-result directories are ignored.

## Commands

```sh
# Offline: structure of the tracked spec (also run by tests/test_phase3_review_spec.py)
uv run python scripts/recommendations_live/review_packet.py check-spec
# Read-only DB + saved artifacts: write the ignored outputs and the manifest
uv run python scripts/recommendations_live/review_packet.py build
# Rebuild in memory and compare with the files on disk and the manifest
uv run python scripts/recommendations_live/review_packet.py validate
```

The builder makes no provider calls, runs no searches, and reads the database
through a read-only connection with the production exact-pair lookup. Outputs
have no timestamps, so a rebuild from unchanged inputs is byte-identical.
`validate` fails when a stored source, a saved artifact or the spec changed,
when a saved render no longer equals the current render, or when an excerpt
hash no longer matches.

## Rules the spec enforces

- Every example has execution, fidelity, usefulness and constraint-support
  judgments, and marks what the artifact did not record.
- Historical candidate sets are never reconstructed from a new search.
- Original proposal snapshots stay `status: proposed`, `owner_decision: pending`;
  acceptance lives in the separate owner decision. A proposed
  change may not set amounts, units, quantity text, servings, durations or
  yields; abstentions are explicit.
- No runtime code loads these files (checked by
  `tests/test_recommendations_source_consistency.py`).
