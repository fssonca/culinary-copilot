# Phase 7 owner decisions

Owner decisions for Milestone 3, Phase 7 (scenario evals and the bounded
live run). Each entry is recorded by an AI assistant and is not a
signature.

## Live run authorization (2026-10-04)

Recorded by an AI assistant; not a signature. The owner approved:

- **Run the Phase 7 live check** as prepared in
  `evals/phase7_agent/LIVE_PLAN.md`:
  - 7 scenarios from `evals/phase7_agent/live_scenarios.json`
    (sha256 `f202afb6…`);
  - one attempt each;
  - at most 1 web search in the whole run
    (`--search-max-per-live-session 1 --max-campaign-searches 1`);
  - hard run ceiling **$0.15**;
  - expected cost about $0.02;
  - run against the application database (`culinary_copilot` on
    `localhost`), as in the Phase 3 and Phase 5 live runs.
- **Phase 7 pool cap: $0.50** of the $1.00 Milestone 3 ceiling.
  Phase 3 used $0.1304 and Phase 5 $0.1015 before this run.
- **Commit Phase 7 part 1 first**, so the code that runs is recorded.

Known gap, accepted for this run: preflight checks for unacknowledged
search-estimate breaches only in the Phase 5 history, not in the
Phase 7 one. The in-run breach stop applies. Fix before any further
Phase 7 live run.
