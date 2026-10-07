# Typed tool layer (Milestone 3, Phase 2 — implemented)

Backend-only. Ten typed tools behind one registry
(`src/culinary_copilot/tools/`). No agent loop yet (Phase 3); no
technique corpus (Phase 4); no web provider (Phase 5). Tool outputs
are data, never instructions.

## Registry

Each tool has a name, pydantic argument and result schemas
(`extra="forbid"`), a server-set timeout (read by
`tools/registry.py::tool_timeout_s` on every call — never
caller-set), an idempotency flag, and a cost class
(`free` / `paid` / `network`).

Timeout precedence (owner decision 2026-10-03): every tool uses
`TOOL_TIMEOUT_S` (default 10 s per Checkpoint 0) except `search_web`,
which resolves explicit `SEARCH_WEB_TIMEOUT_S` first, then explicit
`TOOL_TIMEOUT_S`, then its 30 s default. So the 30 s search default
applies with no env var at all, while a deliberately set
`TOOL_TIMEOUT_S` still binds `search_web` unless the search setting
is set explicitly. The provider sub-request's own timeout is at most
the tool timeout: with defaults min(tool 30 s, `LLM_REC_TIMEOUT_S`
20 s) = 20 s.

Failures are typed error results — never exceptions into the caller —
each with a stable `reason` and `next_action`
(`domain/recommendations.py::next_action_for`). `error_type` stays
`"unavailable"` for both permanent and transient outages (schema);
the `reason` carries the distinction:

| `error_type` | `reason` | `next_action` | Meaning |
|---|---|---|---|
| `timeout` | `tool_timeout` | `retry` | exceeded `TOOL_TIMEOUT_S` |
| `invalid_arguments` | `tool_invalid_arguments` | `change_request` | bad args (also unknown ingredient/pair) |
| `unavailable` | `tool_not_configured` | `contact_operator` | **permanent**: stubs, `EPICURE_ENABLED=false`, missing/mismatched Epicure assets, vector without embeddings |
| `unavailable` | `tool_unavailable` | `retry` | **transient only**: DB or provider errors |
| `unavailable` | `tool_internal_error` | `contact_operator` | defect: an exception escaped an implementation |
| `permission_denied` | `tool_permission_denied` | `change_request` | `search_web` while permission is off |
| `unavailable` | `search_unverified` | `change_request` | `search_web` with no provider source evidence, or no parsed source matching it (outcomes `no_provider_sources` / `no_verified_sources`); same query fails the same way |
| `invalid_arguments` (scale) | `scale_missing_servings` | `change_request` | source servings unknown |
| `invalid_arguments` (convert) | `convert_unsupported_unit` | `change_request` | unknown or cross-group units; `count` never converts |

Sync vs async is decided before calling (`inspect.iscoroutinefunction`,
unwrapping `__wrapped__` and callable-object `__call__`): async
implementations run once under `wait_for`; sync ones run once via
`to_thread` under `wait_for`. A timed-out thread is abandoned, not
killed — it keeps running in the background and its late result is
discarded, so implementations must be side-effect free, idempotent,
or belong to a non-idempotent tool.

`tests/test_next_action.py` scans `tools/*.py` plus
`domain/recommendations.py`, so a new tool reason without a mapping fails.

Every call records a structured `tool_call` event: session id, call
id, tool, sha256 digest of the canonical args (never raw args),
outcome/error type, latency ms, cost class, and `mode_ran` where
applicable. With a session id the event is appended to
`session_events` (`PostgresSessionStore.append_event`, no revision
bump); standalone calls go to the logger.

## Tool table

| Name | Arguments | Cost | Timeout | Errors |
|---|---|---|---|---|
| `search_recipes` | `query` (1–500), `mode?` (`fulltext`\|`vector` when embeddings on, else `fulltext` only), `limit?` (1–50, default 5) | **mode that ran**: fulltext `free`, vector `paid` (one query embedding; full-text is zero-call) | `TOOL_TIMEOUT_S` | `invalid_arguments` (incl. vector when unconfigured), `tool_not_configured` (vector without embeddings: retry with `fulltext`; never falls back), transient `unavailable`, `timeout` |
| `get_recipe` | `dataset_id`, `source_id` (exact pair, never fallback) | `free` | `TOOL_TIMEOUT_S` | `invalid_arguments` (unknown dataset/pair, with the expected separate-id shape), transient `unavailable`, `timeout`; a repeat of a pair whose full output is still visible in the run's capped history returns a short duplicate pointer instead of the document (still counts against the tool budget) |
| `find_balanced_pairings` | `ingredient`, `k?` (1–20, default 5) | `free` (local CPU, cached assets only) | `TOOL_TIMEOUT_S` | `invalid_arguments` (unknown ingredient), `tool_not_configured` (disabled/missing asset), `timeout` |
| `find_conventional_pairings` | same as above (cooc) | `free` | `TOOL_TIMEOUT_S` | same as above |
| `find_flavor_pairings` | same as above (chem) | `free` | `TOOL_TIMEOUT_S` | same as above |
| `find_substitutions` | `ingredient`, `k?` | `free` | `TOOL_TIMEOUT_S` | `invalid_arguments`, `tool_not_configured`, `timeout`; every candidate `verification: "unverified"`, no dietary claim |
| `scale_recipe` | `dataset_id`, `source_id`, `target_servings` (>0) | `free` | `TOOL_TIMEOUT_S` | `scale_missing_servings` (source servings unknown), `invalid_arguments`, transient `unavailable`, `timeout`; unknown quantities listed, never scaled; qualitative units `approximate: true` |
| `convert_units` | `amount` (>0), `from_unit`, `to_unit` | `free` | `TOOL_TIMEOUT_S` | `convert_unsupported_unit` (unknown, cross-group, or any `count` conversion), `invalid_arguments`, `timeout`; every success states `unit_system` (`metric`/`us_customary`/`count`) |
| `search_techniques` | `query` (1–500, send 2–5 keywords), `mode?` (`fulltext`\|`vector` when embeddings on, else `fulltext` only), `limit?` (1–10, default 5) | **mode that ran**: fulltext `free`, vector `paid` (one query embedding; full-text is zero-call) | `TOOL_TIMEOUT_S` | `invalid_arguments` (incl. vector when unconfigured), `tool_not_configured` (006 tables missing; vector without embeddings/007 rows: retry with `fulltext`; never falls back), transient `unavailable`, `timeout`; full-text matches every term per chunk first, then any term (`match: all\|any` in result + event); every hit carries `attribution_text` + `licence_url` |
| `search_web` | `query` (1–500; session bound server-side, never a model arg) | `network` | `SEARCH_WEB_TIMEOUT_S` (default 30 s; explicit `TOOL_TIMEOUT_S` overrides; provider min(30, `LLM_REC_TIMEOUT_S` 20) = 20 s) | `permission_denied` (permission off, no slot), `search_budget_exhausted` (3/session), `search_not_performed` (no web_search_call), `tool_not_configured` (no provider/store), transient `unavailable`, `timeout`; atomic slot claim (FOR UPDATE + re-read + count + claim event) before dispatch |

## Retrieval wiring (ADR 0001 steps 1–2 and 5: done)

- Step 1 (wire the retrieval endpoint): `api/retrieval.py::build_router`
  now reads `RETRIEVAL_MODE`, `RETRIEVAL_VECTOR_CUTOFF`,
  `RETRIEVAL_FULLTEXT_GATE`, `RETRIEVAL_RRF_K`,
  `RETRIEVAL_VECTOR_CANDIDATES` plus the embedding model/dimension
  from `Settings` into `retrieve_for_group`. Code default stays
  `fulltext`, so default-settings behavior is unchanged (zero
  embedding calls).
- Step 2 (provider at startup): `api/app.py` builds the query
  embedding provider only when `EMBEDDINGS_ENABLED` is set, with a
  model/dimension check (`check_model_dimension`); otherwise `None`.
  The tool path uses the same factory (`search_tools.build_embed_provider`).
- Step 5 (tests): `tests/test_tools.py` proves omitted mode follows
  `RETRIEVAL_MODE`, full-text makes zero embedding calls, and vector
  without embeddings is `unavailable` with no fallback;
  `tests/test_tools_pg.py` covers the permission gate on a disposable DB.
- Step 3 (outage behaviour): unchanged — fail closed
  (`allow_fallback=False`); a disclosed fallback is not added.
- Step 4 (recommendations path): not done — `recommendations/service.py`
  keeps explicit full-text (no `retrieval_mode` reference; asserted in
  `test_recommendations_path_still_fulltext`). Separate owner decision.

`search_recipes` mode rules: `mode` arg wins; when omitted,
`RETRIEVAL_MODE` applies (code default `fulltext`; the tool supports
`fulltext`/`vector` only, no `hybrid`). Vector uses cutoff
`RETRIEVAL_VECTOR_CUTOFF` when set, else pinned `0.66` (Checkpoint 0
decision 2 / Phase 6 winner `vector_c`). The response carries
`mode_ran`, also logged.

`search_techniques` mode rules (Phase 4, implemented): `mode` arg
wins; when omitted, `TECHNIQUE_RETRIEVAL_MODE` applies (code default
`fulltext`; `fulltext`/`vector` only). Vector uses pinned cutoff
`0.66` (mirrors `search_recipes`). Hits are chunk excerpts with
`doc_id`, `chunk_id`, `section`, `title`, `url`, `licence`,
`licence_url`, `attribution_text`, and a bounded excerpt; the response
carries `mode_ran`, also logged. Technique references are a separate
evidence type from recipe identities: they may support a technique
claim in a plan/cook step but are never options, recipe sources, or
quantity evidence (see `docs/techniques.md` and `docs/agent.md`).

## Epicure verification (2026-09-28)

Same publisher (`Kaikaku`) and licence (`cc-by-4.0`, matching the
arXiv submission) for all three siblings; no download was refused.

| Repo | Licence | SHA (pinned in config) | `embeddings.safetensors` | Full file set |
|---|---|---|---|---|
| `Kaikaku/epicure-core` | `cc-by-4.0` | `d31ebb5af8e92bbaf5cb67381d5006d4ea8368b7` (pre-existing `EPICURE_REVISION`) | 2,148,088 B | ~4.67 MB |
| `Kaikaku/epicure-cooc` | `cc-by-4.0` | `03edd311adde6e39a2eb6f9f3fa78f7396be6b53` (`EPICURE_COOC_REVISION`) | 2,148,088 B | ~4.15 MB |
| `Kaikaku/epicure-chem` | `cc-by-4.0` | `2461ef3fbafab36d2b1111187a3df98721146861` (`EPICURE_CHEM_REVISION`) | 2,148,088 B | ~4.68 MB |

Total new download (`vocab.json` + `embeddings.safetensors` × 2, once
into `HF_HOME`): ~4.4 MB, far under the 5 GB ceiling. Command:

```sh
uv run python -c "
from pathlib import Path
from huggingface_hub import hf_hub_download
from culinary_copilot.config import Settings
s = Settings()
cache = str(Path(s.hf_home) / 'hub')
for rid, rev in [
    (s.epicure_cooc_model_id, s.epicure_cooc_revision),
    (s.epicure_chem_model_id, s.epicure_chem_revision),
]:
    for fn in ['vocab.json', 'embeddings.safetensors']:
        hf_hub_download(repo_id=rid, revision=rev, filename=fn, cache_dir=cache)
"
```

Files landed in
`.cache/huggingface/hub/models--Kaikaku--epicure-{cooc,chem}/snapshots/<sha>/`
(gitignored cache, not committed). Smoke check after download:
core `chicken` → pork/beef, cooc → garlic/onion, chem →
beef/pork — matching the model cards. The tool path is cache-only
(`EpicureCore(cache_only=True)` → `hf_hub_download(...,
local_files_only=True)`): it never downloads at query time, so no
network call is possible from any Epicure tool; a missing file or
revision mismatch returns `tool_not_configured`. Existing endpoints
keep `cache_only=False` (unchanged lazy-download). Tests stay offline
with fakes, plus a guarded-download test (empty `HF_HOME`,
`hf_hub_download` asserting `local_files_only`) covering all four
Epicure tools.

## Deterministic measure tools

- `scale_recipe(dataset_id, source_id, target_servings)`: fetches the
  stored document, refuses with `scale_missing_servings` when source
  servings are unknown (no factor can be derived). Scales only
  ingredients with a parseable amount **and** a known unit
  (`canonical_unit` from the closed vocabulary; bare `count` scales).
  Unknown/unparsed quantities are returned in `unknown_quantities`
  with their original text — never scaled, never dropped. Scaled items
  carry `approximate`: `true` for qualitative units (pinch, dash, ...),
  `false` for mass/volume/count.
- `convert_units(amount, from_unit, to_unit)`: mass↔mass via grams,
  volume↔volume via milliliters (`g/kg/mg/oz/lb`,
  `ml/cl/l/tsp/tbsp/fl_oz/cup/pint/quart/gallon`). Every success
  states `unit_system`: the output unit's system (`metric` for
  `mg/g/kg/ml/cl/l`, `us_customary` for the US units, `count` for the
  `count`→`count` identity). Unknown units, cross-group conversions
  (mass→volume, anything→qualitative) and any `count` conversion
  refuse with `convert_unsupported_unit`.

## Stubs (explicit, not silent)

- `search_web(query)`: Phase 5 part 2 (offline) replaces the stub with
  a server-bound implementation: the session id comes from
  `ToolContext.bound_session_id` (set per run from the path id; the
  args model has no session field and `extra="forbid"` rejects a
  spoofed one). Every call re-reads `internet_search_allowed` inside
  the atomic slot claim. Off → `permission_denied` (no slot); on →
  one bounded sub-request via `llm/client.py::complete_web_search`
  (hosted `web_search`, `search_context_size: low`, required
   tool choice, `max_tool_calls: 1`, sources include only,
   strict schema, `store: false`, retries zero), verified to have
   performed a search. No provider field carries text page content
   (`web_search_call.results` is image-only per docs), so model
   excerpts are never verified quotations. With defaults the
   provider's own 20 s timeout fires before the 30 s tool timeout
   and is recorded as outcome `error`; a tool-level cancellation of
   the provider call is recorded as outcome `cancelled` plus
   operations (no results, ledger unchanged) and still surfaces as
   a tool timeout    upstream.
- Citation provenance (Checkpoint B condition 1): accepted references
  come from the provider's own evidence (`url_citation` annotations
  and/or `web_search_call.action.sources`, collected in
  `llm/client.py`), never from URLs in the search model's generated
  JSON. A parsed source is kept only when its normalized URL is in
  the provider URL set; the rest are dropped before the agent or the
  session ever sees them. Normalization (`_provenance_key`, both
  sides): `minimize_url` (strips query and fragment — citation URLs
  often carry `?utm_source` parameters), lowercase scheme and host,
  no trailing slash except the root. No provider URLs at all, or no
  surviving source, is a typed failure with no evidence (no summary,
  no sources; slot stays spent; outcomes `no_provider_sources` /
  `no_verified_sources`, reason `search_unverified`). Titles prefer
  the citation title when one exists; `excerpt_model` stays labelled
  model text. `search_results_retrieved` carries audit fields:
  `provider_url_count`, `unverified_dropped`, `provider_urls`
  (minimized, at most 10).

## New settings (all read, none dead)

| Setting | Default | Read by |
|---|---|---|
| `TOOL_TIMEOUT_S` | `10` | `tools/registry.py::_timeout_for` on every call (explicit value also binds `search_web` unless `SEARCH_WEB_TIMEOUT_S` is set explicitly) |
| `SEARCH_WEB_TIMEOUT_S` | `30` (owner decision 2026-10-03, `search_web` only) | `tools/registry.py::tool_timeout_s` for `search_web` (explicit setting, then explicit `TOOL_TIMEOUT_S`, then this default); `tools/stub_tools.py` bounds the provider request at min(this, `LLM_REC_TIMEOUT_S` = 20 s) |
| `TECHNIQUE_RETRIEVAL_MODE` | `fulltext` | `tools/technique_tools.py::resolve_technique_mode` on every call |
| `EPICURE_COOC_MODEL_ID` / `EPICURE_COOC_REVISION` | `Kaikaku/epicure-cooc` / `03edd31…` | `tools/epicure_tools.py::build_epicure_variants` |
| `EPICURE_CHEM_MODEL_ID` / `EPICURE_CHEM_REVISION` | `Kaikaku/epicure-chem` / `2461ef3…` | same as above |

Previously dormant and now wired (ADR 0001): `EMBEDDINGS_ENABLED`,
`EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`, `RETRIEVAL_MODE`,
`RETRIEVAL_VECTOR_CANDIDATES`, `RETRIEVAL_RRF_K`,
`RETRIEVAL_VECTOR_CUTOFF`, `RETRIEVAL_FULLTEXT_GATE` — read by
`api/retrieval.py`, `api/app.py` and `tools/search_tools.py`.
