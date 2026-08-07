import json

from pytest_httpx import HTTPXMock

from waypoint.settings import Settings
from waypoint.worker import PERSONA_PANEL_REQUEST, load_personas

SETTINGS = Settings(
    _env_file=None,
    DATABASE_URL="postgresql+asyncpg://localhost:5432/waypoint_test",
    LLM_API_KEY="test",
    N8N_CONTEXT_URL="https://n8n.example/webhook/context",
    N8N_TOKEN="test",
    PERSONA_URL="https://personas.example",
    PERSONA_TOKEN="persona-key",
    HANDOFF_URL="https://lcm.example/handoff",
    HANDOFF_TOKEN="lcm-token",
    RUN_COST_USD="25.00",
    DAY_COST_USD="500.00",
    WORKER_COUNT=1,
    MODEL_FAST="claude-haiku-4-5",
    MODEL_DEEP="claude-sonnet-5",
    APP_PASSWORD="operator-password",
    SESSION_KEY="0123456789abcdef0123456789abcdef",
)


async def test_load_personas_posts_panel_request(httpx_mock: HTTPXMock) -> None:
    # Flat item like the real service: the card's segment is under `segment_key`
    # (NOT `segment`), plus persona_id + usage booleans.
    httpx_mock.add_response(json={
        "panel_id": "panel_1",
        "subtype_version": "v3",
        "segment": "2A",
        "n_personas": 1,
        "personas": [
            {"persona_id": "p1", "segment_key": "2A", "booking_attached": False},
        ],
    })

    personas = await load_personas(SETTINGS, "2A")

    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "POST"
    assert str(request.url) == "https://personas.example/api/persona-cards"
    assert request.headers["X-API-Key"] == "persona-key"
    # segment is supplied per-Pro at call time, merged onto the base request.
    assert json.loads(request.content) == {**PERSONA_PANEL_REQUEST, "segment": "2A"}
    # subtype_version from the panel becomes each persona's snapshot_version.
    assert personas[0].snapshot_version == "v3"
    assert personas[0].persona_id == "p1"
    # family/label fall back to persona_id; flat fields become features.
    assert personas[0].family == "p1"
    assert personas[0].label == "p1"
    # `segment_key` is exposed as the `segment` match feature (the card has no
    # plain `segment`), so matching has its one shared key with the Pro.
    assert personas[0].features == {
        "segment_key": "2A", "booking_attached": False, "segment": "2A",
    }
