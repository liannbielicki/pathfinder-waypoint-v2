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
    # Vercel Deployment Protection guards the LCM intake app; this is the
    # separate "Protection Bypass for Automation" secret it issues, required
    # alongside HANDOFF_TOKEN on every request or the SSO wall 401s us first.
    BYPASS_TOKEN: SecretStr
    RUN_COST_USD: Decimal = Field(gt=0)
    DAY_COST_USD: Decimal = Field(gt=0)
    WORKER_COUNT: int = Field(ge=1)
    # Fleet-wide cap on concurrent provider calls (the real throttle on how hard
    # we hit Anthropic). Set from the model tier's rate limit, NOT the agent
    # count. Default 4 preserves prior behavior; raise it in Railway and watch
    # for *_rate_limited job failures — the value where those stop is the safe
    # ceiling.
    MAX_LLM_IN_FLIGHT: int = Field(default=4, ge=1)
    KILL_SWITCH: bool = False
    MODEL_FAST: str
    MODEL_DEEP: str
    APP_PASSWORD: SecretStr
    SESSION_KEY: SecretStr = Field(min_length=32)
    LOG_LEVEL: str = "INFO"

    @classmethod
    def load(cls) -> Settings:
        return cls()  # type: ignore[call-arg]  # values come from the environment
