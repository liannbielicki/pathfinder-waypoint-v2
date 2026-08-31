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
    # The context flow (Snowflake + Iterable behind one webhook) can
    # legitimately run 10-15 minutes per call under load, and it degrades
    # badly when every worker loop hits it at once.
    N8N_TIMEOUT_SECONDS: float = Field(default=900.0, gt=0)
    N8N_MAX_CONCURRENT: int = Field(default=3, ge=1)
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
    # Independent V3 learning-loop kill switch: stops checkpoint resolution
    # and outcome-driven learning without stopping run processing.
    LEARNING_KILL_SWITCH: bool = False
    # Cadence and per-sweep bound for checkpoint resolution.
    CHECKPOINT_SECONDS: float = Field(default=300.0, gt=0)
    CHECKPOINT_LIMIT: int = Field(default=500, ge=1)
    # Feature-catalog CTA feasibility hints in idea context. Default OFF: today's
    # world is SMS-only and we do not yet trust channel<->works_on filtering.
    # Flip ON once multi-channel is live so ideas avoid web-only/broken links.
    CTA_FEASIBILITY_HINTS: bool = False
    MODEL_FAST: str
    MODEL_DEEP: str
    # The candidate ranker's model. Empty means "use MODEL_FAST". Every model
    # is validated against the price table at startup (Pricing refuses
    # unmeterable spend), so a typo fails loudly instead of billing blind.
    # Provider stays the single audited Anthropic gateway — a second provider
    # is a deliberate infra decision, not an env var.
    MODEL_RANKER: str = ""
    APP_PASSWORD: SecretStr
    # Machine token for the outcome automation (POST /api/outcomes and
    # GET /api/funnel/worklist) — nothing else, so the n8n flow never
    # holds APP_PASSWORD (which is full operator access, and which n8n would
    # persist in plaintext execution history). Unset => that endpoint stays
    # cookie-only, exactly as before. Generate: openssl rand -hex 32
    OUTCOMES_TOKEN: SecretStr | None = None
    SESSION_KEY: SecretStr = Field(min_length=32)
    LOG_LEVEL: str = "INFO"

    @classmethod
    def load(cls) -> Settings:
        return cls()  # type: ignore[call-arg]  # values come from the environment
