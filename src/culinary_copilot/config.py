from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://copilot:copilot_dev@localhost:5432/culinary_copilot"
    )
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5-nano"
    hf_token: SecretStr = SecretStr("")
    hf_home: str = ".cache/huggingface"
    epicure_enabled: bool = False
    epicure_model_id: str = "Kaikaku/epicure-core"
    epicure_revision: str = "d31ebb5af8e92bbaf5cb67381d5006d4ea8368b7"
