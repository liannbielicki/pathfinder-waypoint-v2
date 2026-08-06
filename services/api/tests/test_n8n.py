import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest_httpx import HTTPXMock

from waypoint.n8n import ContextUnavailable, N8NContextClient, OrgContextBatch

FIXTURE = Path(__file__).parent / "fixtures" / "n8n_context.json"

N8N_URL = "https://n8n.example/webhook/context"


def make_client(batch_size: int = 5) -> N8NContextClient:
    return N8NContextClient(url=N8N_URL, token="test-token", batch_size=batch_size)


def test_n8n_fixture_obeys_ai_egress_contract() -> None:
    batch = OrgContextBatch.model_validate_json(FIXTURE.read_text())
    assert batch.contract_version == "org_context_v1"
    serialized = batch.model_dump_json().lower()
    for forbidden in ("email", "phone", "first_name", "last_name", "address"):
        assert forbidden not in serialized


def test_contract_rejects_unknown_fields() -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["organizations"][0]["email"] = "leak@example.com"
    with pytest.raises(ValidationError):
        OrgContextBatch.model_validate(payload)


async def test_n8n_fetch_batches_ids(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=json.loads(FIXTURE.read_text()))
    client = make_client()
    result = await client.fetch(["pro_1", "pro_2"])
    assert result.organizations[0].open_due_usd == Decimal("430.25")
    request = httpx_mock.get_request()
    assert request is not None
    assert json.loads(request.content) == {"pro_ids": ["pro_1", "pro_2"]}
    assert request.headers["authorization"] == "Bearer test-token"


async def test_n8n_fetch_chunks_large_audiences(httpx_mock: HTTPXMock) -> None:
    fixture = json.loads(FIXTURE.read_text())
    httpx_mock.add_response(json=fixture)
    httpx_mock.add_response(json=fixture)
    client = make_client(batch_size=2)
    result = await client.fetch(["pro_1", "pro_2", "pro_3"])
    requests = httpx_mock.get_requests()
    assert [json.loads(r.content)["pro_ids"] for r in requests] == [
        ["pro_1", "pro_2"], ["pro_3"],
    ]
    assert len(result.organizations) == 4


async def test_n8n_refuses_redirects(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=302, headers={"location": "https://evil.example"})
    with pytest.raises(ContextUnavailable):
        await make_client().fetch(["pro_1"])


async def test_n8n_outage_is_explicit_not_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=503)
    with pytest.raises(ContextUnavailable):
        await make_client().fetch(["pro_1"])
