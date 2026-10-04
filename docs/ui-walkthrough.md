# UI walkthrough (Phase 6 minimal page)

The owner runs the app. No model calls, web requests, or live runs are
needed for steps 2–5; step 6 names what stays deferred to Phase 7.

## 1. Start the app

The owner runs it (from the repo root):

```sh
uv run uvicorn culinary_copilot.api.app:create_app --factory
```

Then open `/ui/` in a browser. `/` redirects there.

## 2. Check every card on the demo page

Open `/ui/demo.html`. It renders only synthetic demo data (labelled
"synthetic demo data — not model output") through the same
`render.js` as the live page, and makes no API calls. Confirm each
section renders:

- Options: two cards with titles, `dataset_id / source_id` small
  text, quantities, an "adaptation"-labelled adaptation, constraint
  tiers with the exact backend disclaimer, Epicure used/rejected
  lines, a dropped-option note, a note with an unverified badge, and
  a "Choose this" button.
- Plan: source, mise en place, ordered steps with the model-adaptation
  badge, quantities, adaptations, technique references, plating.
- Technique answer: text, `doc_id · chunk N` references, attribution
  text with a licence link.
- Web answer: the "From the web" banner, text, and a numbered
  citation list with clickable titles, hosts, and source-label chips.
- Question: text plus options.
- Outcomes: one card per listed reason (budgets, timeouts, permission,
  search states, session errors). Session-budget cards (step, tool,
  reading, web-search budgets) carry a "Start a new session" button;
  the web-search card also notes you can keep going without web
  search.
- Error: the typed error card.
- Unsupported answer type: the neutral fallback naming the key.

## 3. Keyboard-only use

Tab through the page: the toggle, New session, the textarea, Send,
and every card button are native controls. The Activity log is a
native collapsible. Pressing Enter in the composer sends (Shift+Enter
for a new line). In answer mode, each question option is a button and
the free-text field stays available.

## 4. Phone width

Resize to ~380 px or open on a phone: the single column (max 760 px)
reflows with no horizontal scroll; citation links and option buttons
remain tappable.

## 5. Typed error without a model

With `LLM_ENABLED=false`, send any message from `/ui/`. The stream
returns a typed error and the page shows the matching outcome/error
card (for example the provider-unavailable card with its guidance),
not a blank transcript or a stack trace.

## 6. What needs a live run (deferred to Phase 7, not authorized now)

Real trajectories against the corpus and provider — option
selection resuming toward a plan (`POST …/select`, then streaming
with no message), answering a question resuming the run
(`POST …/answers`, then streaming with no message), the permission
toggle gating real web searches, and budget-exhaustion behaviour —
stay deferred to the Phase 7 scenario evals and bounded live run.
