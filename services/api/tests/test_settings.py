import pytest
from pydantic import ValidationError

from waypoint.settings import Settings

# Minimal env covering every required (no-default) Settings field, so
# Settings.load() succeeds and tests can assert on defaults of other fields.
_MINIMAL_ENV = {
    "DATABASE_URL": "postgres://u:p@localhost/db",
    "LLM_API_KEY": "sk-test",
    "N8N_CONTEXT_URL": "https://n8n.example.com",
    "N8N_TOKEN": "n8n-token",
    "PERSONA_URL": "https://persona.example.com",
    "PERSONA_TOKEN": "persona-token",
    "HANDOFF_URL": "https://handoff.example.com",
    "HANDOFF_TOKEN": "handoff-token",
    "BYPASS_TOKEN": "bypass-token",
    "RUN_COST_USD": "1.00",
    "DAY_COST_USD": "10.00",
    "WORKER_COUNT": "1",
    "MODEL_FAST": "claude-fast",
    "MODEL_DEEP": "claude-deep",
    "APP_PASSWORD": "app-password",
    "SESSION_KEY": "x" * 32,
}


def test_missing_required_runtime_values_fail_startup() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_all_runtime_names_are_short_and_descriptive() -> None:
    names = set(Settings.model_fields)
    # The explicit Standard/Staging distinction is worth its 24-character name.
    assert all(len(name) <= 24 for name in names)
    assert names == {
        "DATABASE_URL", "LLM_API_KEY", "N8N_CONTEXT_URL", "N8N_CONTEXT_URL_STAGING",
        "N8N_TOKEN",
        "N8N_TIMEOUT_SECONDS", "N8N_MAX_CONCURRENT",
        "PERSONA_URL", "PERSONA_TOKEN", "HANDOFF_URL", "HANDOFF_TOKEN",
        "BYPASS_TOKEN",
        "RUN_COST_USD", "DAY_COST_USD", "WORKER_COUNT", "MAX_LLM_IN_FLIGHT",
        "KILL_SWITCH", "LEARNING_KILL_SWITCH", "CHECKPOINT_SECONDS", "CHECKPOINT_LIMIT",
        "CTA_FEASIBILITY_HINTS", "MODEL_FAST", "MODEL_DEEP", "MODEL_RANKER", "APP_PASSWORD",
        "OUTCOMES_TOKEN",
        "SESSION_KEY", "LOG_LEVEL",
        "ITERABLE_API_KEY", "AMPLITUDE_API_KEY", "AMPLITUDE_SECRET_KEY",
        "AMPLITUDE_RETURN_EVENT", "POLL_SECONDS",
    }


def test_staging_context_url_is_optional() -> None:
    assert Settings.model_fields["N8N_CONTEXT_URL_STAGING"].default is None


def test_max_llm_in_flight_is_optional_and_defaults_to_four() -> None:
    # Railway-tunable; unset keeps the prior fleet-wide cap.
    assert Settings.model_fields["MAX_LLM_IN_FLIGHT"].default == 4


def test_model_ranker_defaults_to_empty_meaning_use_model_fast() -> None:
    assert Settings.model_fields["MODEL_RANKER"].default == ""


def test_empty_poller_keys_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Railway placeholder variables arrive as "" — the poller must stay
    # disabled, not run with blank credentials.
    for key, val in _MINIMAL_ENV.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("ITERABLE_API_KEY", "")
    monkeypatch.setenv("AMPLITUDE_API_KEY", "")
    monkeypatch.setenv("AMPLITUDE_SECRET_KEY", "")
    settings = Settings.load()
    assert settings.ITERABLE_API_KEY is None
    assert settings.AMPLITUDE_API_KEY is None
    assert settings.AMPLITUDE_SECRET_KEY is None


def test_cta_feasibility_hints_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Provide the required env so load() succeeds; default of the new flag is what we assert.
    for key, val in _MINIMAL_ENV.items():
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("CTA_FEASIBILITY_HINTS", raising=False)
    assert Settings.load().CTA_FEASIBILITY_HINTS is False


def test_workbench_only_dotenv_keys_do_not_break_runtime_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, val in _MINIMAL_ENV.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("N8N_CONTEXT_WEBHOOK_URL", "https://workbench.example.com")
    monkeypatch.setenv("N8N_CONTEXT_WEBHOOK_TOKEN", "workbench-token")
    assert Settings.load().WORKER_COUNT == 1
