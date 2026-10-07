import math
from typing import Any

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from culinary_copilot.embeddings.registry import (
    DEFAULT_EMBEDDING_MODEL,
    embedding_spec,
    require_supported_embedding_model,
)
from culinary_copilot.llm.models import DEFAULT_MODEL, model_spec, require_supported_model


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://copilot:copilot_dev@localhost:5432/culinary_copilot"
    )
    openai_api_key: SecretStr = SecretStr("")
    # Every model setting must name a model in llm/models.py (currently
    # gpt-6-luna only); anything else is refused at startup.
    openai_model: str = DEFAULT_MODEL
    # Hybrid LLM ingestion (OpenAI Batch). Disabled by default; default tests
    # are key-free and offline.
    llm_ingestion_enabled: bool = False
    llm_extraction_model: str = DEFAULT_MODEL
    # Responses reasoning effort (e.g. low/medium/high; model-dependent).
    # None omits the field (server default). Recorded in manifests, request
    # versions, cache keys and provenance whenever set.
    llm_reasoning_effort: str | None = None
    llm_batch_request_limit: int = 200
    llm_max_output_tokens: int = 8000
    llm_retry_limit: int = 2
    llm_audit_sample_rate: float = 0.0
    llm_audit_seed: int = 20260707
    # Spend ceiling (USD, batch-discounted estimate). Submit refuses when the
    # estimate exceeds it. None disables the check (still shows the estimate).
    llm_budget_usd: float | None = None
    # Per-1M-token prices (USD, synchronous list prices; the estimator applies
    # the documented 50% Batch discount). None = unknown pricing: estimates
    # report "unknown" and submit requires an explicit --limit.
    llm_price_input_per_1m: float | None = None
    llm_price_output_per_1m: float | None = None
    # Application-facing LLM (clarification planning). Separate master switch
    # from ingestion: LLM_INGESTION_ENABLED never enables application calls.
    # Default tests are key-free and offline; the disabled path must make
    # zero provider calls.
    llm_enabled: bool = False
    llm_app_model: str = DEFAULT_MODEL
    llm_app_timeout_s: float = 20.0
    llm_app_max_output_tokens: int = 1500
    llm_app_max_input_chars: int = 12000
    llm_app_max_retries: int = 1
    # Recommendation generation (Phase 3). Separate master switch from
    # clarification planning and ingestion: LLM_ENABLED never enables
    # recommendation calls. Disabled generation fails closed (503) before
    # any network access; default tests are key-free and offline.
    llm_recommendation_enabled: bool = False
    llm_rec_model: str = DEFAULT_MODEL
    llm_rec_timeout_s: float = 20.0
    # Recommendation-only reasoning effort (separate from ingestion's
    # LLM_REASONING_EFFORT switch). Must be a value the configured model
    # documents (llm/models.py). gpt-6-luna supports none/low/medium/high/
    # xhigh/max, not "minimal"; "none" is the lowest and matches the
    # zero reasoning tokens observed for selection in Phase 3 (then on
    # gpt-5-nano with "minimal"). Selection over bounded evidence is a
    # lightweight task. Sent explicitly on every recommendation call and
    # recorded in artifacts.
    llm_rec_reasoning_effort: str = "none"
    # Output cap derived from a measured bound (on gpt-5-nano, Phase 3; not
    # yet re-measured on gpt-6-luna), not guessed: the largest
    # schema-valid label selection under current input bounds serializes
    # to 5414 chars (server-issued 1-char label, evidence-bounded refs,
    # 5x200-char free text; tokens <= chars for this ASCII JSON), plus a
    # 1000-token reasoning allowance (docs: reasoning runs "a few hundred"
    # tokens at minimum; minimal effort produces "few or no" reasoning
    # tokens), rounded up: 5414 + 1000 -> 6500. Covers reasoning + text
    # together. LIVE-07 observed 224 output / 0 reasoning tokens: an
    # observation, not the bound.
    llm_rec_max_output_tokens: int = 6500
    llm_rec_max_input_chars: int = 12000
    llm_rec_max_retries: int = 1
    # Bounded recommendation workflow limits (conservative defaults; see
    # docs/recommendations.md for rationale).
    rec_candidate_count: int = 3
    rec_candidate_max: int = 5
    rec_evidence_max_chars: int = 6000
    rec_epicure_max_ingredients: int = 5
    rec_epicure_suggestion_count: int = 5
    rec_max_provider_turns: int = 2
    rec_max_tool_calls: int = 1
    # Streaming (Phase 4, SSE): one workflow, two transports. The
    # non-streaming endpoint keeps its behavior; the SSE endpoint shares
    # the same service via a stage hook. Limits are enforced in the
    # stream generator and documented in docs/recommendations.md.
    rec_stream_max_events: int = 100
    rec_stream_max_duration_s: float = 120.0
    rec_stream_keepalive_s: float = 10.0
    # Recipe-text embeddings (Phase 5, prepared offline). Separate registry
    # from Luna text generation (embeddings/registry.py). Query and corpus
    # vectors must share the same model/dimension; vector search filters on
    # both. No startup check exists yet, and no request path reads these
    # settings (see docs/adr/0001-retrieval-default.md). Disabled by
    # default: full-text mode makes zero embedding calls.
    embeddings_enabled: bool = False
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimension: int = 1536
    embed_timeout_s: float = 20.0
    embed_max_retries: int = 1
    embed_batch_inputs: int = 64
    embed_max_tokens_per_request: int = 300_000
    embed_budget_usd: float | None = None
    # Retrieval modes: fulltext (default) | vector | hybrid. Not yet wired:
    # the retrieval API and recommendations run full-text regardless of
    # these settings; mode/cutoff/gate are passed explicitly to
    # retrieve_for_group by the eval harness and tests. Vector/hybrid need
    # an embedding provider plus a pgvector-enabled database; otherwise
    # they fail closed (no silent full-text fallback unless the caller
    # explicitly requests it and the fallback is disclosed).
    retrieval_mode: str = "fulltext"
    retrieval_vector_candidates: int = 20
    retrieval_rrf_k: int = 60
    # Technique corpus retrieval (Milestone 3, Phase 4): fulltext
    # (default) | vector. search_techniques with an explicit mode wins;
    # omitted mode follows this setting. Vector needs EMBEDDINGS_ENABLED
    # plus a pgvector database with migration 007 rows, otherwise the
    # tool fails closed (no silent full-text fallback).
    technique_retrieval_mode: str = "fulltext"
    # Phase 6 tunable filter (None = no cutoff, current behaviour). Vector
    # mode keeps only vector results with cosine distance <= cutoff;
    # hybrid applies it to vector results before fusion, keeping full-text
    # exactly as they are. Full-text gate (hybrid only): add vector results
    # only when full-text returned at least one result.
    retrieval_vector_cutoff: float | None = None
    retrieval_fulltext_gate: bool = False
    # Agent sessions (Milestone 3, Phase 1, Checkpoint 0 budgets): 12 tool
    # calls per session; MAX_STEPS raised 8 -> 12 by the owner on 2026-10-04
    # (P3-L-12: recommend plus plan did not fit live; see
    # docs/phase7-owner-decisions.md). Phase 1 stores the remaining budgets
    # on the session row (defaults below); the bounded loop enforces them.
    session_max_tool_calls: int = 12
    session_max_steps: int = 12
    # Permission-gated web search (Milestone 3, Phase 5, part 2, offline).
    # Per-session search slots (owner decision 3: 3 in code, 2 in the live
    # check via runner flag). Enforced by an atomic slot claim in
    # services/session_store.py; no migration (existing session_events).
    search_max_per_session: int = 3
    # Strict sub-request output cap for the search summarizer turn.
    search_max_output_tokens: int = 1500
    # Provisional content allowance (tokens) for planning estimates only
    # (owner decision 4: NOT an established upper bound). Equals the
    # documented 128k search-context window cap; the live check must
    # resolve decision 4 before any live run.
    search_content_allowance_tokens: int = 128_000
    # Publisher-signal domain lists (owner decision 7). Comma-separated;
    # empty means unconfigured (all sources unclassified for that class).
    # Labels are publisher signals, never authority verdicts. usda.gov is
    # NOT listed here: it is official_guidance only on food-safety paths
    # (see search/labels.py), otherwise unclassified. Never list bare
    # nih.gov (only the NLM hosts below count as research_publication).
    web_official_guidance_domains: str = "fda.gov,fsis.usda.gov,foodsafety.gov,cdc.gov,who.int"
    web_research_domains: str = "pubmed.ncbi.nlm.nih.gov,pmc.ncbi.nlm.nih.gov"
    web_culinary_domains: str = ""
    # Search retention (owner decision 8, implemented): session search
    # events 90 days, process logs 30 days. Purge is owner-run
    # (scripts/search/purge_search.py + trigger-aware SQL), never automatic.
    search_event_retention_days: int = 90
    process_log_retention_days: int = 30

    @field_validator("llm_rec_reasoning_effort", mode="before")
    @classmethod
    def _validate_rec_effort(cls, value: Any) -> Any:
        # Values documented for reasoning.effort (Responses API reference +
        # reasoning guide); per-model support is server-enforced (400 on
        # unsupported values, surfaced as provider_bad_request).
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return "none"
            if value not in allowed:
                raise ValueError(f"LLM_REC_REASONING_EFFORT must be one of {sorted(allowed)}")
        return value

    @field_validator(
        "openai_model", "llm_extraction_model", "llm_app_model", "llm_rec_model", mode="after"
    )
    @classmethod
    def _supported_model(cls, value: str) -> str:
        return require_supported_model(value.strip())

    @field_validator("embedding_model", mode="after")
    @classmethod
    def _supported_embedding_model(cls, value: str) -> str:
        return require_supported_embedding_model(value.strip())

    @model_validator(mode="after")
    def _effort_supported_by_model(self) -> "Settings":
        # Per-model support is documented (llm/models.py); refuse at startup
        # rather than sending a value the server rejects with a 400.
        pairs = (
            ("LLM_REC_REASONING_EFFORT", self.llm_rec_model, self.llm_rec_reasoning_effort),
            ("LLM_REASONING_EFFORT", self.llm_extraction_model, self.llm_reasoning_effort),
        )
        for setting, model, effort in pairs:
            spec = model_spec(model)
            if effort is not None and spec is not None and effort not in spec.reasoning_efforts:
                raise ValueError(
                    f"{setting}={effort!r} is not supported by {model}; "
                    f"expected one of {list(spec.reasoning_efforts)}"
                )
        espec = embedding_spec(self.embedding_model)
        if espec is not None and self.embedding_dimension != espec.dimension:
            raise ValueError(
                f"EMBEDDING_DIMENSION={self.embedding_dimension} does not match "
                f"{self.embedding_model} registry dimension {espec.dimension}; "
                "query and corpus vectors must share one model/dimension"
            )
        if self.retrieval_mode not in {"fulltext", "vector", "hybrid"}:
            raise ValueError("RETRIEVAL_MODE must be one of fulltext, vector, hybrid")
        if self.technique_retrieval_mode not in {"fulltext", "vector"}:
            raise ValueError("TECHNIQUE_RETRIEVAL_MODE must be one of fulltext, vector")
        if self.retrieval_vector_cutoff is not None:
            cutoff = float(self.retrieval_vector_cutoff)
            if not math.isfinite(cutoff) or not 0 <= cutoff <= 2:
                raise ValueError("RETRIEVAL_VECTOR_CUTOFF must be None or within [0, 2]")
        if self.session_max_tool_calls < 1:
            raise ValueError("SESSION_MAX_TOOL_CALLS must be >= 1")
        if self.session_max_steps < 1:
            raise ValueError("SESSION_MAX_STEPS must be >= 1")
        if int(self.search_max_per_session) < 1:
            raise ValueError("SEARCH_MAX_PER_SESSION must be >= 1")
        if int(self.search_max_output_tokens) < 1:
            raise ValueError("SEARCH_MAX_OUTPUT_TOKENS must be >= 1")
        if int(self.search_content_allowance_tokens) < 1:
            raise ValueError("SEARCH_CONTENT_ALLOWANCE_TOKENS must be >= 1")
        if not math.isfinite(float(self.tool_timeout_s)) or float(self.tool_timeout_s) <= 0:
            raise ValueError("TOOL_TIMEOUT_S must be a finite positive number")
        if (
            not math.isfinite(float(self.search_web_timeout_s))
            or float(self.search_web_timeout_s) <= 0
        ):
            raise ValueError("SEARCH_WEB_TIMEOUT_S must be a finite positive number")
        if not math.isfinite(float(self.agent_wall_clock_s)) or float(self.agent_wall_clock_s) <= 0:
            raise ValueError("AGENT_WALL_CLOCK_S must be a finite positive number")
        if int(self.agent_input_token_ceiling) < 1:
            raise ValueError("AGENT_INPUT_TOKEN_CEILING must be >= 1")
        if int(self.agent_output_token_ceiling) < 1:
            raise ValueError("AGENT_OUTPUT_TOKEN_CEILING must be >= 1")
        for label, model_id, revision in (
            ("EPICURE_COOC", self.epicure_cooc_model_id, self.epicure_cooc_revision),
            ("EPICURE_CHEM", self.epicure_chem_model_id, self.epicure_chem_revision),
        ):
            if not model_id.strip() or not revision.strip():
                raise ValueError(f"{label}_MODEL_ID and {label}_REVISION must be non-empty")
        return self

    @field_validator(
        "llm_budget_usd",
        "llm_price_input_per_1m",
        "llm_price_output_per_1m",
        "llm_reasoning_effort",
        "embed_budget_usd",
        "retrieval_vector_cutoff",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        # .env convention: blank optional numbers mean "unconfigured".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    hf_token: SecretStr = SecretStr("")
    hf_home: str = ".cache/huggingface"
    epicure_enabled: bool = False
    epicure_model_id: str = "Kaikaku/epicure-core"
    epicure_revision: str = "d31ebb5af8e92bbaf5cb67381d5006d4ea8368b7"
    # Epicure siblings (Milestone 3, Phase 2, Checkpoint 0 decision 5):
    # cooc = recipe-context only, chem = chemistry only. Same publisher
    # (Kaikaku), same licence (CC BY 4.0) as epicure-core; verified
    # 2026-09-28 via HfApi + model cards (see docs/tools.md). Revisions
    # pinned the same way EPICURE_REVISION pins epicure-core.
    epicure_cooc_model_id: str = "Kaikaku/epicure-cooc"
    epicure_cooc_revision: str = "03edd311adde6e39a2eb6f9f3fa78f7396be6b53"
    epicure_chem_model_id: str = "Kaikaku/epicure-chem"
    epicure_chem_revision: str = "2461ef3fbafab36d2b1111187a3df98721146861"
    # Typed tool layer (Milestone 3, Phase 2, Checkpoint 0 budgets):
    # per-tool timeout 10 s. Read by tools/registry.py on every call.
    tool_timeout_s: float = 10.0
    # Hosted web search timeout (owner decision 2026-10-03: 30 s
    # permanent default for search_web only; the general TOOL_TIMEOUT_S
    # stays 10 s). Read by tools/registry.py for search_web calls only,
    # and passed to the search provider as its own request timeout at
    # most the tool timeout (see tools/stub_tools.py), so an abandoned
    # request cannot long outlive the tool that gave up on it. With
    # defaults the provider request is bounded at
    # min(tool 30 s, llm_rec_timeout_s 20 s) = 20 s.
    search_web_timeout_s: float = 30.0
    # Bounded agent loop (Milestone 3, Phase 3, Checkpoint 0 budgets):
    # wall clock 90 s per agent run. Read by agent/loop.py at run start.
    # Steps (8) and tool calls (12) are per-session budgets stored on the
    # session row (SESSION_MAX_STEPS / SESSION_MAX_TOOL_CALLS), not settings.
    agent_wall_clock_s: float = 90.0
    # Per-session token budgets (Phase 3 review, round 2): provider-reported
    # usage summed from session_events, no schema change. Full pre-turn
    # estimate counts items + offered tool defs + directive schema
    # (chars/4; the repo has no token estimator). Measured: fixed part
    # ~2600/turn, realistic 4-turn session ~11.3k in (real doc sizes from
    # data/recipe-import/normalized.jsonl), recorded live structured
    # outputs up to ~2k/call; the per-turn output cap (min 6500
    # configured max, 500 useful minimum) keeps single turns sane. Input
    # raised 30k -> 60k by the owner on 2026-10-04 with the 12-step
    # budget: the Phase 7 live run measured 3k-4.5k input per turn, so
    # 30k ran out after ~8 turns (docs/phase7-owner-decisions.md).
    # Output 12k covers ~6 max-recorded turns. See evals/phase3_agent/
    # and evals/phase7_agent/. Read by agent/loop.py before every turn.
    agent_input_token_ceiling: int = 60_000
    agent_output_token_ceiling: int = 12_000
    # Full trajectory recording (2026-10-05): off by default. When true,
    # the agent loop also records tool arguments (minimized), what each
    # tool returned to the model, every model directive (including
    # rejected answers) and each run's result as session_events, so a
    # session can be analyzed later. Scrubbed and bounded; stays in the
    # local database. Export: scripts/sessions/export_session.py.
    agent_record_trajectory: bool = False
    # Operator switch for real hosted web search from the app
    # (2026-10-05): off by default, so search_web stays
    # tool_not_configured. When true, the agent endpoints pass the
    # application provider as the search_web sub-request provider.
    # Each session still needs its own toggle on, and
    # SEARCH_MAX_PER_SESSION caps searches per session. Paid.
    web_search_enabled: bool = False
