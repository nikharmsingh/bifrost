from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    default_model: str = "claude-sonnet-4-6"
    # Models API callers may request; empty means only default_model is allowed
    allowed_models: Annotated[list[str], NoDecode] = []
    # When set, the gateway requires a matching X-API-Key header
    gateway_api_key: str = ""
    log_level: str = "INFO"
    env: str = "development"
    allowed_origins: Annotated[list[str], NoDecode] = ["*"]

    langchain_api_key: str = ""
    langchain_tracing_v2: bool = False
    langchain_project: str = "bifrost"

    @field_validator("allowed_models", "allowed_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        # Same comma-separated format the Vercel apps use, so one env var works everywhere
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
