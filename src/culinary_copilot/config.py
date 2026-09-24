from typing import Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://copilot:copilot_dev@localhost:5432/culinary_copilot"
    )
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5-nano"
    # Hybrid LLM ingestion (OpenAI Batch). Disabled by default; default tests
    # are key-free and offline.
    llm_ingestion_enabled: bool = False
    llm_extraction_model: str = "gpt-5-nano"
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
    llm_app_model: str = "gpt-5-nano"
    llm_app_timeout_s: float = 20.0
    llm_app_max_output_tokens: int = 1500
    llm_app_max_input_chars: int = 12000
    llm_app_max_retries: int = 1

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
