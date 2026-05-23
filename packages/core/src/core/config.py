from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    log_level: str = "INFO"
    env: str = "development"

    langchain_api_key: str = ""
    langchain_tracing_v2: bool = False
    langchain_project: str = "python-turborepo"


@lru_cache
def get_settings() -> Settings:
    return Settings()
