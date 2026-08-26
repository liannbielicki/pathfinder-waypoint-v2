import json
from pathlib import Path

from waypoint.api import app

REPO_ROOT = Path(__file__).parents[3]
REGEN = (
    "cd services/api && uv run python -c 'import json; from waypoint.api import app; "
    'open("../../contracts/openapi.json", "w")'
    ".write(json.dumps(app.openapi(), indent=2, sort_keys=True))'"
)


def test_committed_contract_matches_live_schema() -> None:
    committed = json.loads((REPO_ROOT / "contracts" / "openapi.json").read_text())
    assert committed == app.openapi(), (
        f"contracts/openapi.json is stale. Regenerate it ({REGEN}), then rerun "
        "openapi-typescript in apps/web to refresh src/lib/api-types.ts."
    )
