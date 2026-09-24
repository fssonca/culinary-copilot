# API request flows

Part of the [current architecture](README.md). These are the current code paths.

## 1. API surface

| Method and path | Behavior |
|---|---|
| `GET /health/live` | Process health |
| `GET /health/ready` | PostgreSQL connectivity |
| `GET /api/v1/recipes` | Combined full-text search; optional dataset filter |
| `GET /api/v1/recipes/{source_id}` | Canonical recipe document lookup |
| `GET /api/v1/pairings` | Optional local Epicure neighbors |
| `POST /api/v1/clarification/groups` | Create structured request state and initial questions |
| `GET /api/v1/clarification/groups/{group_id}` | Read state and group |
| `POST /api/v1/clarification/groups/{group_id}/answers` | Validate answers or edits, then atomically save |
| `POST /api/v1/clarification/groups/{group_id}/replan` | Explicit follow-up planning; at most five successful replans per request |
| `POST /api/v1/retrieval/search` | Revision-pinned bounded evidence summaries for a ready clarification group (Phase 1; see `docs/retrieval.md`) |
| `POST /api/v1/recommendations` | Revision-pinned grounded selection for a ready group: `recommendation` / `clarification` / `insufficient_evidence` (Phase 3; see `docs/recommendations.md`) |

Retrieval returns ranked bounded summaries. Recommendations fetch
complete documents by exact identity and server-render the selected
recipe; the model performs selection only. Neither generates recipes.

## 2. Recipe discovery

```mermaid
flowchart TD
    Request["GET recipes with query and filters"] --> Validate["Validate query, dataset, limit and time"]
    Validate --> Choice{"dataset_id supplied?"}
    Choice -->|"No"| All["search_all: both datasets"]
    Choice -->|"Yes"| One["search_recipes: selected dataset"]
    All --> SQL["Shared parameterized full-text query"]
    One --> SQL
    SQL --> Filters["Exact canonical ingredient filter; optional time ceiling"]
    Filters --> Rank["Rank by text score; ties by dataset and source ID"]
    Rank --> Results["Result summaries with dataset_id and source_id"]
```

Unknown durations are excluded by a time ceiling. Ingredient filtering is exact
canonical-name array containment, not semantic ingredient matching. Search does
not enforce cuisine, dietary restrictions, equipment or substitution compatibility.

Lookup with `dataset_id` matches that exact pair or returns 404. Without it,
legacy lookup tries Food.com first, then another dataset. Original IDs are
preserved exactly. Duplicate aliases are metadata, not independent lookup rows.

## 3. Creating questions

```mermaid
sequenceDiagram
    participant C as Client
    participant A as Clarification API
    participant P as Planning service
    participant R as Rule planner
    participant D as Recipe repository
    participant L as Application provider
    participant S as In-memory store
    C->>A: POST group with message and/or structured request
    A->>P: Initialize state and plan
    P->>R: Propose missing-information questions
    alt LLM enabled and requested
        P->>D: Optional bounded search in worker thread
        D-->>P: Up to 3 titles and canonical identities
        P->>L: Message, state, prior answers, rules and evidence metadata
        L-->>P: Structured proposed updates and questions
        P->>P: Validate, deduplicate and preserve uncertainty
        Note over P,L: Explicit-value changes become confirmations
    else Disabled or provider failure
        P->>P: Use rule-based questions
    end
    P-->>A: Bounded question group and readiness
    A->>S: Store authoritative state and questions
    A-->>C: Group, revisions, blockers and pending confirmations
```

The rule-only path operates on structured information. It cannot interpret arbitrary
free text as an LLM would. Provider failure degrades to rules with error metadata;
unknown information must remain unknown. Group size defaults to six, with a maximum
of twelve. The future UI may present those questions serially; no UI exists yet.

The evidence helper currently selects search-result titles and dataset/source IDs,
with empty snippets. It does not fetch full recipes or pass the request's time
ceiling into that auxiliary search. This context is insufficient to prove recipe
suitability or a substitution's safety. Evidence retrieval is also distinct from
returning ranked recipe results to the user after clarification.

## 4. Answers, edits and concurrent requests

```mermaid
sequenceDiagram
    participant C as Client
    participant A as Answer endpoint
    participant S as In-memory store
    participant V as Deterministic answer logic
    C->>A: Answers plus request/group revisions
    A->>S: Read deep-copy snapshot and known questions
    S-->>A: State, group and question registry
    A->>V: Validate types, selections, edits and confirmations
    V-->>A: Updated state, history, dependencies and readiness
    A->>S: Compare-and-swap commit using original revisions
    alt Revisions still match
        S-->>A: Commit succeeds
        A-->>C: Updated state and pending questions
    else Another update already won
        S-->>A: No write
        A-->>C: HTTP 409 - refetch current state
    end
```

**Compare-and-swap** means “save only if nobody has changed the state since this
snapshot.” Validation and revision comparison are protected at the commit boundary.
Reads return copies, preventing accidental mutation of stored objects.

Answer submission never calls the LLM. A registry preserves previously issued
questions, so a user can edit an earlier answer after it leaves the pending group.
Dependencies can invalidate answers; history remains. Explicit no preference,
unknown, skipped and conflicting are separate states.

Replanning takes a snapshot, releases the lock, calls the provider if enabled,
then commits only if the request revision still matches. Successful replans advance
that revision. A concurrent answer makes the stale replan fail with 409; no store
lock is held during network work. This protection applies within one process only.

## 5. Readiness and corrections

```mermaid
flowchart TD
    State["Updated cooking-request state"] --> Conflict{"Conflicting requirements?"}
    Conflict -->|"Yes"| Clarify["needs_clarification"]
    Conflict -->|"No"| Skipped{"Required information skipped?"}
    Skipped -->|"Yes"| Blocked["blocked with explanation"]
    Skipped -->|"No"| Enough{"Discovery information and task essentials met?"}
    Enough -->|"No"| Clarify
    Enough -->|"Yes"| Ready["ready_for_retrieval; optional questions may remain"]
```

For example, discovery may need only a dish or ingredients, while a scaling task
also requires portions. Ready does not mean allergy-safe, complete, scalable or
dietarily verified. Responses preserve `state.request`, field statuses,
`unenforced_constraints` and a `readiness_note` for future consumers.

Model-proposed changes to explicit answers create `keep_current` / `confirm_change`
questions. The stored value stays unchanged until confirmation. Updates to unknown
fields require adequate model confidence, a quote matching the current message, and
shape validation; those checks do not establish semantic truth.

Retrieval integration (Phase 1, implemented): `POST /api/v1/retrieval/search`
consumes the current clarification group plus expected revisions, translates
dish/pantry/time through the deterministic mapping in `retrieval/query.py`
(dish decides eligibility, pantry only ranks; pantry-only matches any
overlap), and returns bounded evidence summaries with all unresolved
constraints intact (see `docs/retrieval.md`). A superseded group gets a
controlled 409 directing the client to the current group; older groups stay
readable as history. The readiness flag alone still never implies safety,
nutrition, completeness, or scaling approval.

## 6. Retrieval for a ready group

```mermaid
sequenceDiagram
    participant C as Client
    participant A as Retrieval API
    participant S as In-memory store
    participant Q as Query mapping
    participant D as Recipe repository
    C->>A: group_id plus request/group revisions, limit, dataset
    A->>S: Read authoritative snapshot (brief lock, deep copies)
    S-->>A: State, group and revisions
    A->>A: Recompute readiness (never trust caller flags), 409 on stale revisions or superseded group
    alt Not ready
        A-->>C: 200 not_ready with reasons and would-be query (no search)
    else Ready
        A->>Q: Deterministic dish/pantry/time mapping (dish eligibility, pantry ranks only)
        Q-->>A: query_text, match mode, max_minutes, unsupported constraints
        A->>D: search_all/search_recipes in a worker thread (off the event loop)
        D-->>A: Ranked identities (bounded, duration-eligible only under a ceiling)
        A->>D: Exact-pair get_recipe per hit in a worker thread (full documents)
        D-->>A: Full source documents (bounded)
        A->>S: Re-read revisions and group currency (no lock was held across I/O)
        alt Revisions advanced or group superseded meanwhile
            A-->>C: HTTP 409 - refetch and retry
        else Still current
            A-->>C: 200 ready with revision-stamped bounded summaries and unknowns
        end
    end
```

Time ceilings are never relaxed to force a match; only finite positive
totals can satisfy a ceiling (zero, missing, negative, and non-finite are
unknown and excluded), and empty results carry an explanation naming the
datasets searched versus those represented. Pantry ingredients never gate
eligibility for dish queries. Unsupported constraints (cuisine, preferences,
dietary, equipment, substitutions) are preserved unchanged and reported as
unverified.

## 7. Recommendation for a ready group

```mermaid
sequenceDiagram
    participant C as Client
    participant A as Recommendations API
    participant S as In-memory store
    participant E as Epicure adapter
    participant D as Recipe repository
    participant L as Recommendation provider
    C->>A: group_id plus request/group revisions, limit, dataset, tool_mode
    A->>A: Fail closed when generation is disabled (503, zero provider calls)
    A->>S: Read authoritative snapshot (brief lock, deep copies)
    S-->>A: State, group and revisions
    A->>A: Recompute readiness (never trust caller flags), 409 on stale revisions or superseded group
    alt Not ready (incl. conflicts)
        A-->>C: 200 clarification with reasons and blockers (no search, no provider call)
    else Ready
        A->>E: Early consultation with canonical ingredients (worker thread)
        E-->>A: consulted / skip / disabled / unavailable / unmapped / insufficient context
        A->>D: Reused ranking search + exact-pair complete fetch (worker threads)
        D-->>A: Full source documents from recipes only, recipe_quarantine is never read (bounded)
        A->>A: Deterministic constraint assessment, exclude violated/unresolved-hard
        alt No eligible source passes structural admission (sections, no omissions, no recorded error-severity defect)
            A-->>C: 200 insufficient_evidence with reason + unverified discovery pointers (ready_to_cook false, why, source_defects)
        else Eligible evidence
            A->>A: Serialize the complete payload, reduce whole candidates until it fits the evidence/input budgets (never truncate), 200 insufficient_evidence when nothing fits, zero provider calls
            alt Default path
                A->>L: Complete evidence + selection-only prompt (one turn)
                L-->>A: Structured selection (label + refs + typed propositions)
            else tool_mode (opt-in, native function calling)
                A->>L: Candidate metadata only + strict get_recipe tool, forced tool choice
                L-->>A: One native function_call (name, arguments, call_id)
                A->>D: Allowlisted exact-pair fetch
                D-->>A: Complete source document
                A->>A: Fetched document must match the evidence snapshot (fingerprint)
                A->>L: Final-selection instructions + turn-1 user message + reasoning/function_call items + function_call_output (same call_id), budgeted whole, no tools
                L-->>A: Structured selection (label + refs + typed propositions)
            end
            A->>A: Deterministic validation (identity, refs, step order, injection, constraints, snapshot)
            A->>A: Proposition prerequisites, server wording, omissions recorded
            A->>S: Re-read revisions and group currency (no lock was held across I/O)
            alt Revisions advanced or group superseded meanwhile
                A-->>C: HTTP 409 - refetch and retry
            else Still current
                A-->>C: 200 recommendation with server-rendered recipe, propositions, source checks and usage
            end
        end
    end
```

Provider refusal is a controlled 502 (`provider_refusal`), never
insufficient evidence. Failed responses carry no recipe content. Epicure
suggestions are assessed after evidence (used as pairing notes only when
present in the selected source, otherwise deferred) and never enter the
rendered recipe.

## 8. Source checks and review decisions

```mermaid
flowchart TD
    Source["Stored source document"] --> Structural{"Sections present, no omissions or recorded blocking defect?"}
    Structural -->|"No"| Pointer["Incomplete discovery pointer: ready_to_cook false"]
    Structural -->|"Yes"| Constraints{"Hard constraints supported or not applicable?"}
    Constraints -->|"No"| Abstain["Insufficient evidence"]
    Constraints -->|"Yes"| Select["Model selects identity, refs and typed propositions"]
    Select --> Validate["Validate selection and proposition prerequisites"]
    Validate --> Render["Python renders source facts and accepted wording"]
    Render --> Disclosure["Source checks disclose ingredient-list consistency not checked"]
    Review["Offline review proposals"] -.-> Decision["Owner acceptance as review records only"]
    Decision -.-> Future["Separate authorization required to change stored data"]
```

Unknown quantities alone do not prove an ingredient-list inconsistency. Structural
admission does not detect ingredients mentioned only in instructions. Food.com
000322 remains presentable; ENR-03 records a defect proposal but has not changed
the database. The source review spec is never loaded as runtime policy.

Optional propositions that fail their prerequisites are omitted and recorded in
`rejected_propositions`; invalid selections or hard-constraint failures remain
controlled errors. No unrestricted model-authored explanation is published.
