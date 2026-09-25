"""Single strict environment contract. Missing values fail startup loudly."""

from decimal import Decimal
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Runtime and the local Context Workbench intentionally share services/api/.env.
    # Ignore unrelated dotenv keys while still validating every declared runtime field.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: SecretStr
    LLM_API_KEY: SecretStr
    N8N_CONTEXT_URL: AnyHttpUrl
    # Deprecated compatibility value. Staging runtime uses the Workbench URL.
    N8N_CONTEXT_URL_STAGING: AnyHttpUrl | None = None
    N8N_CONTEXT_URL_WORKBENCH: AnyHttpUrl | None = None
    N8N_TOKEN: SecretStr
    CONTEXT_LAYER_BASE_URL: AnyHttpUrl | None = None
    CONTEXT_LAYER_API_KEY: SecretStr | None = None
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
    # Fleet-wide cap on LIVE Staging n8n executions (dispatched but not yet
    # called back). Independent of WORKER_COUNT: n8n returns 202 in
    # milliseconds, so worker concurrency caps outstanding HTTP requests, not
    # outstanding workflows — and each live workflow runs 9 sequential
    # Snowflake queries. Raise only as fast as Snowflake tolerates.
    STAGING_MAX_PENDING: int = Field(default=1, ge=1)
    # Fleet-wide cap on concurrent provider calls (the real throttle on how hard
    # we hit Anthropic). Set from the model tier's rate limit, NOT the agent
    # count. Default 4 preserves prior behavior; raise it in Railway and watch
    # for *_rate_limited job failures — the value where those stop is the safe
    # ceiling.
    MAX_LLM_IN_FLIGHT: int = Field(default=4, ge=1)
    # Hard ceiling on ONE provider HTTP attempt. The Anthropic SDK's own
    # default is a 600s read timeout, and three retry layers multiply around
    # every call (JSON_CALL_ATTEMPTS x retry_rate_limit x the SDK's own
    # max_retries), so the default let a single logical generation outlive the
    # worker's lease and get the job re-claimed and double-paid.
    # ponytail: one flat value for every stage, not a per-call budget derived
    # from max_tokens. The largest batch the pipeline can ask for is
    # _batch_max_tokens(MAX_CANDIDATE_COUNT + 1) = 13,200 tokens, which at a
    # slow generation rate could approach 300s; the normal operating point is
    # CANDIDATE_COUNT = 3 (+1 warm start) ~ 4,800 tokens, comfortably clear.
    # Named ceiling: raising CANDIDATE_COUNT toward MAX_CANDIDATE_COUNT makes
    # 300s a REAL cap that will start timing out big batches — raise this with
    # it, or size the timeout from max_tokens at that point.
    LLM_TIMEOUT_SECONDS: float = Field(default=300.0, gt=0)
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
    CTA_FEASIBILITY_HINTS: bool = True
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
    # Direct outcome pollers (iterable_source.py / amplitude_source.py). A
    # missing key disables that poller with one startup log line — the worker
    # runs fine with zero keys configured.
    ITERABLE_API_KEY: SecretStr | None = None
    AMPLITUDE_API_KEY: SecretStr | None = None
    AMPLITUDE_SECRET_KEY: SecretStr | None = None
    # Comma-separated Amplitude event_type names that count as a return
    # (first_return_at source) — matching ANY qualifies. HCP's Amplitude has
    # no session_start; the taxonomy's active-use events are "Loaded a Screen"
    # (mobile) and "Loaded a Page" (web). The canonical set is the data
    # owner's call (TODOS.md "Canonical Amplitude active-use event contract").
    AMPLITUDE_RETURN_EVENT: str = "Loaded a Screen,Loaded a Page"
    # Cadence for both outcome pollers.
    POLL_SECONDS: float = Field(default=300.0, gt=0)
    # Contact plan (docs/superpowers/specs/2026-09-25-contact-plan-design.md):
    # off = not computed; shadow = computed + recorded, behaviour unchanged;
    # enforce = the pinned (Pro, channel) drives ideation, follow-up and handoff.
    CONTACT_PLAN_MODE: Literal["off", "shadow", "enforce"] = "off"

    @property
    def amplitude_return_events(self) -> frozenset[str]:
        return frozenset(
            name.strip() for name in self.AMPLITUDE_RETURN_EVENT.split(",") if name.strip()
        )

    @field_validator(
        "ITERABLE_API_KEY",
        "AMPLITUDE_API_KEY",
        "AMPLITUDE_SECRET_KEY",
        "N8N_CONTEXT_URL_WORKBENCH",
        "CONTEXT_LAYER_BASE_URL",
        "CONTEXT_LAYER_API_KEY",
        mode="before",
    )
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        # Railway placeholder variables arrive as "" — that is "no key", and
        # must disable the poller, not enable it with blank credentials.
        return None if value == "" else value

    @field_validator("CONTACT_PLAN_MODE", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> object:
        # Railway values are hand-typed: "Shadow" or "" must not crash startup.
        if isinstance(value, str):
            return value.strip().lower() or "off"
        return value

    @classmethod
    def load(cls) -> Settings:
        return cls()  # type: ignore[call-arg]  # values come from the environment
