from typing import Any

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
        return self

    @field_validator(
        "llm_budget_usd",
        "llm_price_input_per_1m",
        "llm_price_output_per_1m",
        "llm_reasoning_effort",
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
