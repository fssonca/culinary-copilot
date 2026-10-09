# H8 owner review packet (Milestone 4, R0)

Prepared 2026-10-09 by an AI assistant for the owner's review; not a
review result. The verdict column is for the owner. Model output is
paraphrased here; the full transcripts are local only.

## Transcripts (local, not committed)

| Session | Attempt | Export |
|---|---|---|
| `h8c-allergy-sleepover` (egg allergy) | 3 | `data/h8-live/review/ses-live-h8c-allergy--a1-4868a8.md` (and `.json`) |
| `h8d-family-picnic` (party baking) | 4 | `data/h8-live/review/ses-live-h8d-family-p-a1-2e0ddb.md` (and `.json`) |

Exported read-only with `scripts/sessions/export_session.py`. Each
export holds every event: tool calls and what each tool returned to the
model, every model directive (including rejected ones), and each run's
result.

## Checklist against the predeclared criteria (`PLAN.md`)

The mechanical grades passed for both sessions. Points marked "owner"
need a judgement the grades cannot make.

### `h8c-allergy-sleepover`

| # | Criterion | What the record shows | Owner verdict |
|---|---|---|---|
| 1 | Asks which allergen before options | Asked first in the visible flow, but only after 16 steps of searches and pairing queries (the framing asks for the question before any search) | |
| 2 | Answer recorded and used | "Eggs" recorded; the next run used it | |
| 3 | Suitable options, all fetched, checked for the allergen | Three shortbread recipes; no egg term in their listed ingredients; the app's constraint check agrees, with its not-a-guarantee disclaimer. Owner: suitable for a sleepover bake? | |
| 4 | Plan for exactly the selected dish; quantities on the right ingredients; label matches | Plan for the selected brown-sugar shortbread, labelled as a model adaptation, four quantities from the source. Owner: check the amounts against the source and whether the adaptation label is fair | |
| 5 | Grounded technique answer, or an honest "not covered" | Used the recipe's own firm-to-the-touch cue, cited one returned chunk, said the sources give no further cookie-specific cue. Owner: is the cited chunk relevant? | |
| 6 | No budget stop | None; 30 steps in total | |

### `h8d-family-picnic`

| # | Criterion | What the record shows | Owner verdict |
|---|---|---|---|
| 1 | Questions judged for relevance (may not ask) | No question; options straight away | |
| 2 | Answers recorded and used | Not applicable (no question) | |
| 3 | Suitable options, all fetched | Three bar recipes (two raspberry oatmeal, one pecan pie). Owner: suitable for a picnic without a fridge? | |
| 4 | Plan for exactly the selected dish | Plan for the selected raspberry oatmeal bars, labelled as a model adaptation, written on the plan run's first turn. Owner: check quantities and label | |
| 5 | Grounded technique answer, or an honest "not covered" | Cited the FDA refrigeration chunk (2 hours, 1 hour above 90°F), said the sources set no time limit for these bars, invented none. Owner: is that the right level of caution? | |
| 6 | No budget stop | None; 10 steps in total | |

## Spend reconciliation

Recorded by the live runner's ledgers (reported usage, reconciled per
call):

| Pool | Runs | Recorded spend | Last run (UTC) |
|---|---:|---:|---|
| `phase3` | 9 | $0.1304 | 2026-10-03 |
| `phase5` | 5 | $0.1015 | 2026-10-03 |
| `phase7` | 2 | $0.0476 | 2026-10-04 |
| `h8` | 4 | $0.1133 | 2026-10-09 |
| **Total recorded** | | **$0.3928** | |

Not recorded by any ledger:

- **2026-10-07 five-session live evaluation**: about 736k input and 14k
  output tokens (`docs/phase7-owner-decisions.md`). At the registry
  prices for `gpt-6-luna` (standard tier, $0.10 per million input, $0.50
  per million output) that is about **$0.081**, an upper estimate since
  cached input is billed lower.
- **2026-10-06 `make demo` sessions** run by the owner: no token totals
  recorded; only provider billing can say.
- Query embeddings outside the ledgers: negligible at
  `text-embedding-3-small` prices, but not recorded.

Owner action: compare the total for 2026-09-30 to 2026-10-09 in the
provider's billing with the recorded total plus the estimate, and record
the result in `docs/phase7-owner-decisions.md`.
