import pytest
from pydantic import ValidationError

from waypoint.settings import Settings


def test_missing_required_runtime_values_fail_startup() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_all_runtime_names_are_short_and_descriptive() -> None:
    names = set(Settings.model_fields)
    assert all(len(name) < 20 for name in names)
    assert names == {
        "DATABASE_URL", "LLM_API_KEY", "N8N_CONTEXT_URL", "N8N_TOKEN",
        "PERSONA_URL", "PERSONA_TOKEN", "HANDOFF_URL", "HANDOFF_TOKEN",
        "BYPASS_TOKEN",
        "RUN_COST_USD", "DAY_COST_USD", "WORKER_COUNT", "MAX_LLM_IN_FLIGHT",
        "KILL_SWITCH", "MODEL_FAST", "MODEL_DEEP", "MODEL_RANKER", "APP_PASSWORD",
        "SESSION_KEY", "LOG_LEVEL",
    }


def test_max_llm_in_flight_is_optional_and_defaults_to_four() -> None:
    # Railway-tunable; unset keeps the prior fleet-wide cap.
    assert Settings.model_fields["MAX_LLM_IN_FLIGHT"].default == 4


def test_model_ranker_defaults_to_empty_meaning_use_model_fast() -> None:
    assert Settings.model_fields["MODEL_RANKER"].default == ""
