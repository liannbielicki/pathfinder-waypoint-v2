"""Live contract smoke check. Run only with approved production credentials:

    N8N_CONTEXT_URL=... N8N_TOKEN=... LIVE_TEST_PRO=... uv run pytest tests/test_n8n_live.py -q -m live
"""

import os

import pytest

from waypoint.n8n import N8NContextClient


@pytest.fixture
def live_client() -> N8NContextClient:
    url = os.environ.get("N8N_CONTEXT_URL")
    token = os.environ.get("N8N_TOKEN")
    if not url or not token or "replace-me" in url:
        pytest.skip("live n8n credentials not configured")
    return N8NContextClient(url=url, token=token)


@pytest.mark.live
async def test_existing_n8n_flow_matches_the_recorded_contract(
    live_client: N8NContextClient,
) -> None:
    batch = await live_client.fetch([os.environ["LIVE_TEST_PRO"]])
    assert batch.contract_version == "org-context-v2"
    assert batch.organizations
    assert "@" not in batch.model_dump_json()
