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
        "RUN_COST_USD", "DAY_COST_USD", "WORKER_COUNT", "KILL_SWITCH",
        "MODEL_FAST", "MODEL_DEEP", "APP_PASSWORD", "SESSION_KEY", "LOG_LEVEL",
    }
