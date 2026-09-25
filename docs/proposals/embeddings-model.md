# Embedding model decision (Phase 5, checkpoint 3 — owner-approved 2026-09-24)

## Approval record

- Embedding model: `text-embedding-3-small`, native dimension `1536`, as the
  embedding model only. `gpt-6-luna` stays the only text-generation model.
- Standard price: **$0.02 per 1M input tokens**, verified 2026-09-24 on
  `https://developers.openai.com/api/docs/pricing?latest-pricing=standard`.
- Budget: **$2.00 ceiling** (`EMBED_BUDGET_USD=2.00`) against the $0.70
  reservation (recomputed with the byte bound: 35,163,880 tokens retry-x2,
  $0.7033 — fits).
- Activation image: `pgvector/pgvector:pg17-trixie` pinned by digest
  `sha256:724a4041afdb1750446e3f6b5cfa8f3b0ac5a2cf538ddfa6bfee4f94c2fa85c6`
  (verified on pull: Debian 13 trixie, glibc 2.41, PostgreSQL 17.11,
  pgvector 0.8.6).
- Registry separation (enforced in code and tested):
  - `llm/models.py` accepts only `gpt-6-luna` for text generation; an
    embedding model there is refused.
  - `embeddings/registry.py` accepts only embedding models
    (`text-embedding-3-small` 1536, `text-embedding-3-large` 3072,
    `text-embedding-ada-002` 1536); `gpt-6-luna` is refused.

## Proposed model

- `text-embedding-3-small`, native dimension `1536`, no shortening initially.
- Sources:
  - Models/dims/behavior: `https://developers.openai.com/api/docs/guides/embeddings` — 3-small default `1536`, 3-large `3072`; `dimensions` param supported on 3-series only; embeddings normalized to length 1; cosine similarity recommended; cosine and Euclidean rank identically on normalized vectors.
  - Request limits: `https://developers.openai.com/api/reference/resources/embeddings/methods/create` — per-input max `8192` tokens for all embedding models; aggregate max `300,000` tokens summed across inputs per request; max `2048` inputs per request; empty strings rejected; response carries per-index `index` mapping plus `usage.prompt_tokens/total_tokens`.
  - Pricing (Standard, per 1M input tokens): `https://developers.openai.com/api/docs/pricing?latest-pricing=standard` — 3-small `$0.02`, 3-large `$0.13`, ada-002 `$0.10`. No output/cached pricing for embeddings.
- Why 3-small: lowest verified input price, native 1536 dims with exact search, no post-hoc normalization risk from manual shortening. 3-large reserved if held-out comparison later justifies cost.

## Distance choice

- `<=>` cosine distance in Postgres via pgvector, matching the official OpenAI guidance.
- pgvector (`https://github.com/pgvector/pgvector`, v0.8.6): `<->` L2, `<=>` cosine, `<#>` inner product; exact search by default (no index); pinned image `pgvector/pgvector:pg17-trixie@sha256:724a...`. Normalized vectors make cosine/L2 rankings identical; inner-product equivalence is noted but not used initially to keep the documented semantic (`1 - cosine similarity`) explicit. No HNSW/IVFFlat until measurements justify it.

## Token handling

- Per-input cap 8192 tokens; aggregate cap 300,000 per request; batch bound is token-aware, never recipe-count-only. All three use `estimate_tokens_bytes()`.
- Reservation bound is the **UTF-8 byte length plus a framing constant**
  (`estimate_tokens_bytes` in `embeddings/rendering.py`): the project rule
  (docs/recommendations.md) is that every token spans at least one byte, so
  tokens never exceed bytes. Character counts underestimate non-ASCII text
  and are never used for reservations.
- Oversized recipes: deterministic single-chunk truncation at
  `EMBED_MAX_CHARS=7000` with `truncated: true` recorded per chunk job in the
  run; no silent multi-chunk split in v1 (schema supports chunks for later).

## Batch meaning

- `batch` in the CLI means multiple inputs in one synchronous `/v1/embeddings` request — not the asynchronous Batch API. Different execution/accounting: one HTTP round-trip, per-index mapping, single `usage` block per request. Retries are per-request with bounded attempts and timeouts. One worker per run-dir (pid claim); concurrent jobs use separate run-dirs.

## Cost basis

- Corpus + query embeddings both bill input tokens at `$0.02/1M` (Standard). Reservation = estimated tokens × price × `(max_retries + 1)`, recomputed from a fresh `--dry-run` before any run; cumulative spend accumulates in the ledger. A retrieval query (~21 chars) reserves at most ~29 tokens ($0.0000006); a 150-embedding comparison costs under $0.0001. Full-text mode makes zero embedding calls.
- Re-verify model, dims, limits, and prices on run day; if unverifiable, do not submit.
