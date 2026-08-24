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
    # CTA_FEASIBILITY_HINTS (21 chars) is the longest name today.
    assert all(len(name) < 22 for name in names)
    assert names == {
        "DATABASE_URL", "LLM_API_KEY", "N8N_CONTEXT_URL", "N8N_TOKEN",
        "N8N_TIMEOUT_SECONDS", "N8N_MAX_CONCURRENT",
        "PERSONA_URL", "PERSONA_TOKEN", "HANDOFF_URL", "HANDOFF_TOKEN",
        "BYPASS_TOKEN",
        "RUN_COST_USD", "DAY_COST_USD", "WORKER_COUNT", "MAX_LLM_IN_FLIGHT",
        "KILL_SWITCH", "CTA_FEASIBILITY_HINTS", "MODEL_FAST", "MODEL_DEEP", "MODEL_RANKER", "APP_PASSWORD",
        "SESSION_KEY", "LOG_LEVEL",
    }


def test_max_llm_in_flight_is_optional_and_defaults_to_four() -> None:
    # Railway-tunable; unset keeps the prior fleet-wide cap.
    assert Settings.model_fields["MAX_LLM_IN_FLIGHT"].default == 4


def test_model_ranker_defaults_to_empty_meaning_use_model_fast() -> None:
    assert Settings.model_fields["MODEL_RANKER"].default == ""


def test_cta_feasibility_hints_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Provide the required env so load() succeeds; default of the new flag is what we assert.
    for key, val in _MINIMAL_ENV.items():
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("CTA_FEASIBILITY_HINTS", raising=False)
    assert Settings.load().CTA_FEASIBILITY_HINTS is False
