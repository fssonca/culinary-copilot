# Phase 5 web search: owner decisions

Prepared 2026-10-02 with AI assistance, from
`docs/proposals/phase5-web-search.md` (the agent's part 1 proposal) and
a review of it against the code and the official OpenAI pages. The
decisions below belong to the owner. Nothing in this document is
authorized until the owner answers.

**Answering:** each decision ends with the choices. A one-line reply is
enough, for example "adopt the recommendations" or "1 yes, 2 b, 5 $0.05,
…". There is a quick answer sheet at the end.

**What the answers authorize:** part 2 only, which is offline
implementation with fake providers and disposable databases. The paid
live check, any migration on the application database and any logging
of real users each still need their own go-ahead.

## Background

### What Phase 5 is for

Milestone 3 lets the agent search the internet, but only when the user
has switched it on. The learning plan's completion criteria include:
- "Disabled internet search cannot be invoked through the backend";
- "Searches produce source references, operational logs and actionable
  gap records".

Checkpoint 0 (2026-09-28) already chose the provider: OpenAI's built-in
`web_search` tool, using the same API key as the agent. It also set the
rule that the backend enforces permission by leaving the tool out of the
request when permission is off. Checkpoint B, at the end of Phase 5,
asks the owner to:
- approve the provider, the per-session limit and the dollar ceiling;
- review sample logs and the gap queue.

### Where the project stands

| Area | State |
|---|---|
| Phase 3 agent loop | Live evaluation accepted 2026-09-30 as a diagnostic with reservations. Live ask-and-resume (P3-L-13) is still open. |
| Pre-Phase 5 controls | Done offline: the dietary-constraint check, claim grounding, plan evidence, truncation, and true-upper-bound spending reservations (P3-L-07 to 10 and 14). |
| Phase 4 technique corpus | Complete. Owner label spot-check recorded 2026-09-29. |
| `search_web` today | A stub. It checks the session's permission on every call; permission off returns "denied", permission on returns "not configured". It makes no network call, and the agent is only offered it when permission is on. |
| Milestone 3 spend | About $0.13 recorded of the $1.00 ceiling. The Phase 3 cap's remaining ~$0.025 stays unspent. |

### What the official docs say (verified 2026-10-01, spot-checked 2026-10-02)

- **Tool:** use `{"type": "web_search"}`. `web_search_preview` is
  legacy.
- **Price:** "$10.00 / 1k calls + Search content tokens billed at model
  rates". That is $0.01 per search, plus the retrieved page text billed
  as input at `gpt-6-luna` rates ($0.10 per 1M tokens).
- **How much page text one search adds:** not documented. The only limit
  given is that "the search context window is limited to 128k" tokens.
- **Sources:** the full list of URLs a search consulted needs
  `include: ["web_search_call.action.sources"]`. Inline citations
  (`url_citation`) carry only the URL, title and position, with no
  excerpt or date.
- **Capping searches per request:** `max_tool_calls` caps built-in tool
  calls per response. Its default and maximum are not documented.
- **Combining tools:** the docs neither confirm nor forbid using web
  search together with our function tools and strict structured output
  in one request.

## Decision 1: accept the documentation check as the baseline

**Context.** The proposal's section 1 quotes the official pages with URLs
and the date read, and marks everything the docs don't state as
"unknown". I re-checked the pricing row, the tool name, the sources
`include` option and the 128k limit, and they match. The unknowns
matter: the per-search page-text size and whether all the tools combine
are not documented, so the design avoids depending on them.

**Choices:** yes / no (state the correction).
**Recommendation:** yes.

## Decision 2: how web search plugs into the agent

**Context.** There are two ways to use the built-in tool.

- **(a) Inside the agent's own turns.** Add `web_search` next to the
  agent's other tools and let the model search whenever it likes.
  Problems:
  - the number of searches per turn can't be reliably capped (the cap's
    default is undocumented);
  - cost can't be reserved per search;
  - raw web content flows straight into the agent's context, which is
    where planted instructions are most dangerous;
  - every test of the agent loop would have to fake web-search output.
- **(b) Keep `search_web` as our own tool (recommended).** When the agent
  calls it, our code checks permission, checks the session's search
  limit, reserves the cost, and then makes one separate, narrow request
  to OpenAI with the web search tool (at most one search). That request
  returns a strict, short result: a summary plus up to 5 sources, each
  with URL, title, excerpt and date. The agent only ever sees that
  checked result, never raw pages. The cost is one small extra model
  call per search.

With (b), the tool is still left out of every request when permission
is off, as checkpoint 0 requires, and permission is re-checked just
before every search.

**A fix that is included either way.** Today the stub checks permission
on a session ID that the model passes in as an argument, and nothing
replaces it with the real one. The loop stops offering the tool once
permission is off, so the risk is small. But within a turn, a model or a
planted instruction could name a different session that has permission
on, and the search limit and logs would then be charged to the wrong
session. Part 2 will take the session ID from the server's run context
instead.

**Choices:** a / b.
**Recommendation:** b.

## Decision 3: how many searches per session

**Context.** Searches cost real money, and an agent that keeps searching
is a known failure mode. The proposal limits searches per session by
counting the session's existing event records, so no database change is
needed. Every attempt counts, including failures. Once the limit is
reached the tool answers "search budget exhausted; start a new
session", the same way the step and tool-call limits behave.

**Choices:** 3 per session in code and 2 in the live check (proposed) /
other numbers.
**Recommendation:** 3 and 2.

## Decision 4: how much money to reserve per search

**Context.** After Phase 3 (finding P3-L-14), every paid call must
reserve a true upper bound on its cost before it is sent. If actual
usage ever exceeds the reservation, the run stops. For one search the
proposal reserves:
- the $0.01 call fee;
- page text up to the documented 128k-token limit ($0.0128);
- the request itself, measured by its byte size (about $0.0004);
- the capped answer of 1,500 output tokens ($0.00075).

That totals about **$0.025 per search**. After the call, the ledger is
corrected down to what OpenAI actually reports, which should normally be
much less. The weak point is that the docs don't promise 128k is a limit
*per search*. The existing breach check covers this: if a search ever
reports more, the run stops and preflight refuses to start again until
you acknowledge it.

**Choices:** yes / correct the formula.
**Recommendation:** yes.

## Decision 5: Phase 5's share of the $1.00 Milestone 3 budget

**Context.** About $0.13 is recorded so far. The proposal caps Phase 5's
live runs at **$0.10**. Because each search reserves its worst case,
fewer sessions fit in the reservation than would actually cost that
much. The runner runs what fits and stops early rather than exceed the
cap. Expected real spend for a 3–4 session check is under $0.03. Even
at the full cap, Milestone 3 would be at about $0.23, leaving about
$0.77 for Phase 7 and contingency.

**Choices:** $0.10 / another amount.
**Recommendation:** $0.10, treated as a cap rather than a target.

## Decision 6: rules for web evidence

**Context.** The milestone says web pages are external evidence, outputs
cite URLs, and authoritative sources are kept separate from anecdotes.
The proposal's rules:
1. **Bounded evidence:** a summary of at most 1,000 characters, at most
   5 sources, and excerpts of at most 500 characters each. Full pages
   are never stored.
2. **Classification:** a fixed domain list (decision 7) marks a source
   as authoritative. Everything else is "unclassified, treat as
   anecdote". The model cannot change a classification.
3. **Citation check:** every web URL cited in an answer must have been
   returned by a successful search in the same session, the same check
   technique references already have.
4. **Claim check:** times and temperatures attributed to web sources must
   appear in the returned excerpts, using the same matching that already
   handles "165°F" vs "165 °F".
5. **Labelling:** web-backed content is marked external in what the user
   sees, and is never presented as coming from a recipe.

**One gap the proposal leaves open: a web-only answer.** Rule 3 also
says web evidence never establishes a recipe's identity or quantities,
which is right. But it leaves the agent no way to answer when the web is
all it has. Take "dragonfruit soufflé glacé" with search on: recipe
options must be recipes from our own corpus, so what the agent found has
nowhere to go. The proposed addition is a `web_answer` final, like the
`technique_answer` added in Phase 3:
- a short text with at least 1 web reference;
- every reference must come from this session's search results;
- time and temperature claims are checked against the excerpts;
- it is labelled external;
- any quantities are shown as coming from the web page, never as facts
  from a recipe source;
- it also creates a `missing_recipe` gap record, so the dish can be
  considered for the corpus later.

**Choices:** rules 1–5 yes / correct; `web_answer` yes / no.
**Recommendation:** yes to both.

## Decision 7: which domains count as authoritative

**Context.** The proposed starting list is `fda.gov`, `fsis.usda.gov`,
`cdc.gov`, `nih.gov` and `who.int`, all public-health bodies. Matching
should be by domain suffix, so subdomains count: `fsis.usda.gov`
matches `usda.gov`. I suggest adding `usda.gov` and `foodsafety.gov`.
In practice every recipe site will show as unclassified. That is
deliberate and honest: recipe blogs are useful, but they are not
authorities.

**Choices:** approve the list with suffix matching, adding `usda.gov` and
`foodsafety.gov` / supply a different list.
**Recommendation:** approve with the two additions.

## Decision 8: logging, redaction, retention and access

**Context.** The learning plan asks for search events to be logged, with
no secrets stored and sensitive query details redacted. Retention and
access rules must be written down before any real-user data exists. The
proposal:
- **Events,** stored in the session's existing append-only event log:
  - search requested (redacted query, reason, information gap, phase,
    permission state);
  - results retrieved (URLs, titles, times);
  - evidence evaluated (sources used or rejected, each with a reason);
  - outcome (was the gap resolved? what is still uncertain?);
  - operations (latency, tokens, cost reserved vs actual, errors).
- **Redaction before anything is written:** email addresses, phone
  numbers, street addresses, and names after "my" (narrowly: "my" plus
  one or two capitalised words). The raw query is kept only as a hash.
  Process logs get no query text at all.
- **Retention:** 90 days for search events and 30 days for process logs,
  on development databases. Deleting from the append-only log needs a
  documented, owner-approved procedure with a backup.
- **Access:** the owner only, through review exports that stay out of
  git. **Nothing is stored for real users until the owner approves.**
  The search toggle stays off by default.

**Choices:** yes / correct any item.
**Recommendation:** yes.

## Decision 9: where gap records live

**Context.** Gap records are the "actionable" output of search: things
our own data lacked. The five kinds come from the learning plan:
missing recipe, missing ingredient alias, retrieval miss, missing
technique, unreliable metadata. The proposal triggers them from signals
that already exist, with no model judgement:
- searches with zero results;
- an Epicure "unknown ingredient" with suggestions;
- technique searches with zero hits;
- options dropped by source checks;
- web search used because local retrieval was empty.

A review export lists each candidate with its provenance, and nothing is
ever imported automatically. Two ways to store them:
- **(i) No new table (recommended).** Gaps are derived from the
  session's existing event records and exported to a git-ignored file.
  There is no database change on the application database, nothing to
  back up, and it is fully reversible. That is enough for the 3–4
  session live check.
- **(ii) A new migration 008 now,** with a dedicated gap table. It would
  be cleaner long-term, but it needs a backup and a separate apply to
  the application database. The proposal would draft it and test it
  only on a disposable database.

**Choices:** (i) events plus export / (ii) migration 008 now.
**Recommendation:** (i). Revisit (ii) if Phase 6 or 7 needs a review
interface.

## Decision 10: start part 2

**Context.** Part 2 builds everything above offline:
- settings, the separate search request, the real `search_web` behind
  (b) with the server-bound session ID, and `web_answer` if approved;
- evidence checks and labels, the five events with redaction, the
  per-search reservation, and the gap export;
- tests with a fake search provider: permission denied, permission
  switched off mid-session, instructions planted in a page, complete
  logs, the search limit, and the reservation and breach behaviour.

It ends with a report for you to review. The live check comes after,
with its own go-ahead and a short scenario list. That list can include
the still-open ask-and-resume check (P3-L-13), to avoid a second paid
run.

**Choices:** yes / no.
**Recommendation:** yes.

## Quick answer sheet

| # | Decision | Recommended answer |
|---|---|---|
| 1 | Documentation check as the baseline | yes |
| 2 | Integration | b (with the session ID taken from the server) |
| 3 | Searches per session | 3 in code, 2 in the live check |
| 4 | Reservation per search | yes (about $0.025, true upper bound) |
| 5 | Phase 5 live share | $0.10 cap |
| 6 | Evidence rules + `web_answer` | yes + yes |
| 7 | Authoritative domains | approve with suffix matching, adding usda.gov and foodsafety.gov |
| 8 | Logging, redaction, retention, access | yes (90 / 30 days, owner-only) |
| 9 | Gap storage | (i) events + export |
| 10 | Start part 2 (offline) | yes |

## Owner answers (2026-10-02)

The owner replied "adopt" to an owner-supplied review of this document.
That review is recorded below as the owner's decision. Where it differs
from the recommendations above, it wins. Recorded by an AI assistant;
not a signature.

**Overall.** Proceed with offline implementation (part 2), with the
cost guarantee, evidence rules, authority labels and logging design
revised first. Adopted:
- integration B;
- 3 searches per session, 2 in the live check;
- the $0.10 allocation, conditional on decision 4;
- `web_answer`;
- gap storage as events plus export.

Decisions 1, 4, 6, 7 and 8 are amended as follows. Live spending stays
pending until the reservation issue (decision 4) is resolved.

1. **Documentation baseline: accepted with corrections.**
   - The tool name, citation and source mechanisms, and published
     pricing are supported.
   - The claim that native web search cannot be capped is incorrect:
     setting `max_tool_calls` explicitly caps built-in calls per
     response. An undocumented default does not prevent setting a limit
     ([API reference](https://developers.openai.com/api/reference/python/resources/responses/methods/create)).
   - Statements are labelled one of three ways: "documented", "not
     documented", or "not yet tested in our integration". Fake tests
     cannot establish live API compatibility.
2. **Integration: B, with the server-bound session ID.**
   - B's advantages stand without describing option A as inherently
     unbounded.
   - The search sub-request requires web-tool invocation (`tool_choice`
     required, not `auto`), and the code checks that a successful search
     actually occurred
     ([web-search guide](https://developers.openai.com/api/docs/guides/tools-web-search)).
   - Summaries and excerpts are untrusted input too. A separate
     summarization request reduces exposure but can still pass on
     malicious instructions or false claims, and structured JSON is not
     a security guarantee
     ([OWASP prompt injection](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)).
3. **Search limits: 3 per session, 2 in the live check, with slots
   reserved before dispatch.**
   - Counting completed `tool_call` events is not enough: concurrent
     calls can see the same remaining allowance, and the registry
     tolerates event-write failures.
   - Use an atomic permission check and a durable search-slot
     reservation before sending, with appropriate locking on existing
     tables and no new migration
     ([PostgreSQL locking](https://www.postgresql.org/docs/17/explicit-locking.html)).
   - If the reservation cannot be recorded, do not search.
   - Failed dispatched attempts count, conservatively. Permission-denied
     calls are counted separately.
   - Toggle-off blocks newly authorized searches. It cannot undo a
     request already dispatched.
4. **Per-search reservation: $0.025 is not approved as a proven upper
   bound.**
   - The arithmetic is reasonable under its assumptions. But the 128k
     figure is a context limit, not a documented bound on total billable
     input for the hosted operation.
   - Stopping after a breach detects overspending; it does not prevent
     it.
   - $0.025 is kept as a provisional planning estimate, and the
     reservation accounting is built and tested offline.
   - Before live approval, either a supported bound is established, or
     a change from a hard guarantee to a spending estimate with
     acknowledged overrun risk is explicitly proposed. The existing
     hard-cap decision is not silently weakened.
5. **Phase 5 allocation: $0.10 cap, conditional on decision 4.**
   - One campaign ledger covers agent turns, search sub-requests, tool
     fees, embeddings and retries.
   - Forecast corrected: three actual searches cost $0.03 in search fees
     alone, before model tokens; four cost $0.04
     ([pricing](https://developers.openai.com/api/docs/pricing)).
   - The runner completes what fits; it does not promise a session
     count.
6. **Evidence rules and `web_answer`: yes, with stronger provenance
   distinctions.**
   - A model-written "excerpt" is not a verified quotation. Provider
     citation metadata, generated summaries and source text actually
     obtained are kept apart. Matching a generated claim against another
     generated field does not verify it.
   - `retrieved_at` is set by the server. `published_at` is separate
     and nullable.
   - Claims are matched against the specific cited source and its
     context. "165°F" appearing somewhere in an excerpt does not show it
     applies to the food being discussed.
   - Without reliable source text, only a cited discovery answer
     pointing to the page is allowed. It never claims verified
     quantities or safety instructions.
   - Citations stay clickable in the user-facing answer.
   - `web_answer` stays separate from corpus recipe identities.
   - A `missing_recipe` candidate is created only when the interaction
     actually concerns a recipe gap.
7. **Authoritative domains: the classification is revised, not just the
   list.**
   - A domain is a publisher signal, not an automatic authority verdict.
   - Food-safety guidance from FDA, FSIS, FoodSafety.gov, CDC and
     relevant WHO pages counts. USDA counts, with attention to the
     page's purpose.
   - Not all of `nih.gov`: PubMed and PMC host third-party research
     ([NLM disclaimer](https://pmc.ncbi.nlm.nih.gov/about/disclaimer/)).
   - Labels: official guidance, research publication, culinary source,
     unclassified. "Unclassified" does not mean "anecdotal".
   - Hostname matching is exact equality or a dot-delimited subdomain
     match, never a raw string suffix.
8. **Logging: the events and owner-only development scope are approved,
   with redaction and retention enforcement revised.**
   - The name and address patterns are narrow heuristics, not a
     complete privacy control.
   - Minimization is applied before provider submission as well as
     before storage. It covers tool arguments (including the
     trajectory-recording path), errors, URLs, summaries and exports.
   - 90 and 30 days are reasonable for development, provided a real
     expiry procedure covers exports and backup copies
     ([OWASP logging](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)).
   - `store: false` is set on the search sub-request. It does not remove
     provider abuse-monitoring retention; local and OpenAI retention
     are separate
     ([data controls](https://developers.openai.com/api/docs/guides/your-data)).
9. **Gap storage: events plus export, with provisional
   classifications.**
   - Triggers identify investigation candidates, not proven causes.
   - Each record stores the observed signal, the proposed category, its
     provenance and a review status.
   - Only review confirms a cause. Automatic import stays disabled.
10. **Start part 2: yes, with these corrections.**
    - Offline tests cover:
      - concurrent quota claims;
      - logging failures;
      - cross-session spoofing;
      - permission changes;
      - fabricated excerpts;
      - incorrect numeric context;
      - sensitive data in alternate logging paths.
    - The report identifies the provider behaviour still unresolved for
      the later authorized probe.
    - The outstanding ask-and-resume scenario (P3-L-13) is included, and
      it verifies that the agent asks, receives the answer and resumes.

## Owner answers, follow-up (2026-10-02)

Recorded by an AI assistant from the owner's answers; not a signature.

**Decision 4 (reservation): option A, an estimate with a stepwise live
check.** The owner explicitly accepts that paid web searches are
budgeted with an estimate (about $0.025 per search), not a proven upper
bound, with acknowledged overrun risk. This applies only to searches.
Agent turns and embeddings keep hard, true-upper-bound reservations.
Conditions:
- **At most 4 paid searches** across the whole Phase 5 live campaign,
  counted across runs.
- **Stepwise:** the first live run makes exactly **one** search and
  stops. Its reported usage (fee, content and input tokens, cost) is
  compared with the estimate before any further search is authorized.
- Any search whose reported cost exceeds its estimate stops the
  campaign, and preflight refuses until the owner acknowledges it.
- The $0.10 Phase 5 cap applies (decision 5). Each live run still needs
  its own "run it".

**P3-L-13 (live ask-and-resume): run separately now, without web
search.** One scenario, one attempt, hard reservations. It is charged to
the Phase 3 cap's remaining ~$0.025, not to the Phase 5 share.

## Live runs and an authorization overrun (2026-10-03)

Recorded by an AI assistant. The owner asked the assistant to run the
commands.

- **P3-L-13 rerun** (Phase 3 pool, $0.0025): the agent asked which
  allergy, recorded the answer ("peanuts"), and resumed using it. It
  then asked a reasonable follow-up about tree nuts (the recipe it found
  contains walnuts), so the session ended on a question, not options.
  The ask, record and resume mechanism is shown live. The final
  allergen-checked options were not reached.
- **Search run, step 2** (Phase 5 pool, $0.0355): the owner authorized
  at most 2 searches; the assistant intended 1. **3 searches were
  made.** The flags `--max-campaign-searches 1` and
  `--search-max-per-live-session 1` were not enforced during the run:
  the per-session limit stayed at the code default of 3, and the
  campaign limit was checked only in preflight. The assistant did not
  verify that enforcement before running. Each search cost about $0.011
  (within the $0.025 estimate).
- **Consequence:** the decision 4 campaign cap of 4 searches is used up
  (1 in step 1, 3 here). Phase 5 pool: $0.0489 of $0.10. No further
  search may run without a new owner decision. Enforcement of the
  run-level search limits must be fixed and tested first.

## Owner answers (2026-10-03)

Recorded by an AI assistant; not a signature.

- **P3-L-13 is closed.** Asking, recording the answer and resuming are
  shown live (the agent asked which allergy, recorded "peanuts" and
  resumed using it). Reaching allergen-checked options after resuming
  becomes a Phase 7 scenario.
- **One more search.** The decision 4 campaign cap is raised from 4 to 5
  for one live `web_answer` check: a single missing-dish session, with
  `--max-campaign-searches 1 --search-max-per-live-session 1`, both now
  enforced during the run. Expected cost is about $0.012. The search
  estimate terms of decision 4 are otherwise unchanged.
- **The proposed rule** (reject `ask_user` when local search found
  nothing and a web search succeeded for a named dish) is **not
  implemented for now**. The digest and framing fixes are tested first.

## The web_answer check run (2026-10-03)

Recorded by an AI assistant. The run was owner-authorized.

- The limits held during the run: one slot (`slots_max` 1) was claimed
  and dispatched, and a second search was refused with
  `search_budget_exhausted` and no provider call.
- **The dispatched search timed out** at the 10-second per-tool limit.
  Earlier searches took 4.1–7.4 s. The agent received no web results and
  asked whether a Japanese cabbage salad would do instead.
  **`web_answer` is still not shown live.**
- **A ledger defect:** the timed-out search stayed `reserved` at $0 and
  was never resolved, so the run reported $0.0023. The request was sent
  and may have been billed. Counted conservatively, with the unclear
  search at its $0.025 reservation, the run cost up to $0.0273 and the
  Phase 5 pool stands at about **$0.0762 of $0.10**. The campaign is at
  5 of 5 searches.

## Owner answers, one more web_answer attempt (2026-10-03)

Recorded by an AI assistant; not a signature.

- **One more attempt** at a live `web_answer`. The assistant's option
  text wrongly said the pool would stay at or below $0.10. With $0.0238
  left and a $0.025 search reservation, the attempt does not fit, and
  the owner was told so.
- **The Phase 5 live cap is raised from $0.10 to $0.13**, and the
  campaign search cap from 5 to 6. One missing-dish session, one search.
- **`SEARCH_WEB_TIMEOUT_S=30` for this run only**, set as an environment
  variable on the command, with `.env` and the code default (10 s)
  unchanged. The provider request is bounded at
  min(30, `llm_rec_timeout_s` 20) = 20 s. Approving 30 s as the
  permanent search timeout is still open.

## The last web_answer attempt and the root cause (2026-10-03)

Recorded by an AI assistant.

- The run cost $0.0130 (the search $0.01114, reconciled). Phase 5 pool:
  $0.0892 of $0.13. Campaign: 6 of 6 searches.
- The model **did attempt a `web_answer`** this time. It was correctly
  rejected because it cited site home pages
  (`https://japan-food.jetro.go.jp/`) instead of the exact returned URLs.
  It then tried to search again, was refused by the limit, and the run
  stopped on "no progress".
- **The root cause of every failed `web_answer` attempt:** the loop's
  model-facing summary of tool results (`_summarize_result`,
  `agent/loop.py`) has no `search_web` branch. The model received only
  the status of each search, never its summary, sources or URLs. The
  evidence digest showed only host names and titles. The earlier
  diagnosis, that the model received the full results, was wrong. The
  fake tests missed it because the fake model hard-codes its URLs.

## Owner answer, final web_answer attempt (2026-10-03)

Recorded by an AI assistant; not a signature. After the root-cause fix,
the owner approved **one final attempt**. The campaign search cap is
raised from 6 to 7, with one search and the same command as before
(`SEARCH_WEB_TIMEOUT_S=30` for the run only, with in-run limits). It
fits the remaining Phase 5 pool, about $0.041 of $0.13. If it fails,
Phase 5 moves to checkpoint B regardless.

## The final web_answer attempt succeeded (2026-10-03)

Recorded by an AI assistant.

- With the model-facing search summary fixed, the agent produced a
  `web_answer` in 4 turns with one search. It cited the exact returned
  URLs (MAFF, Just One Cookbook, JETRO), labelled the answer external
  and as a discovery answer, and stated that no verified quantities or
  directions were available. Its note claims are marked `unverified`.
  The scenario passed and reached its expected stop.
- Cost: $0.0123 (search $0.01113). Phase 5 pool: **$0.1015 of $0.13**.
  Campaign: **7 of 7** searches. Every search cost about $0.011, within
  the $0.025 estimate.
- Phase 5's live checks are complete. Next: checkpoint B (logs, gap
  records, and the open search-timeout proposal).
