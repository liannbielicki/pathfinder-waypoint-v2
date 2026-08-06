"""Single strict environment contract. Missing values fail startup loudly."""

from decimal import Decimal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

    DATABASE_URL: SecretStr
    LLM_API_KEY: SecretStr
    N8N_CONTEXT_URL: AnyHttpUrl
    N8N_TOKEN: SecretStr
    PERSONA_URL: AnyHttpUrl
    PERSONA_TOKEN: SecretStr
    HANDOFF_URL: AnyHttpUrl
    HANDOFF_TOKEN: SecretStr
    RUN_COST_USD: Decimal = Field(gt=0)
    DAY_COST_USD: Decimal = Field(gt=0)
    WORKER_COUNT: int = Field(ge=1)
    KILL_SWITCH: bool = False
    MODEL_FAST: str
    MODEL_DEEP: str
    APP_PASSWORD: SecretStr
    SESSION_KEY: SecretStr = Field(min_length=32)
    LOG_LEVEL: str = "INFO"

    @classmethod
    def load(cls) -> Settings:
        return cls()  # type: ignore[call-arg]  # values come from the environment
