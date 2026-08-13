import json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from waypoint.n8n import (
    ALLOWED_FIELDS,
    CONTRACT_VERSION,
    ContextUnavailable,
    N8NContextClient,
    OrgBrief,
    OrgContextBatch,
)

FIXTURE = Path(__file__).parent / "fixtures" / "n8n_context.json"

N8N_URL = "https://n8n.example/webhook/context"


def make_client(batch_size: int = 5) -> N8NContextClient:
    return N8NContextClient(url=N8N_URL, token="test-token", batch_size=batch_size)


def _rows() -> list[dict]:
    """Wire format is a bare array of rows, each stamped with the contract."""
    batch = json.loads(FIXTURE.read_text())
    return [{"contract_version": CONTRACT_VERSION, **org} for org in batch["organizations"]]


def test_n8n_fixture_obeys_ai_egress_contract() -> None:
    batch = OrgContextBatch.model_validate_json(FIXTURE.read_text())
    assert batch.contract_version == CONTRACT_VERSION
    # Every field that crosses is on the allowlist (consent *state* bands like
    # email_consent_state are allowlisted; raw contact data is not).
    permitted = set(ALLOWED_FIELDS) | {"org_uuid"}
    for org in batch.organizations:
        assert set(org.model_dump()) <= permitted
    # No raw email/phone values leak: an address would carry an "@".
    assert "@" not in batch.model_dump_json()


def test_segment_reaches_match_features() -> None:
    # segment is the only key a Pro and a (flat) persona can share; it must
    # survive the v2 -> match-feature mapping or every panel abstains at 0 fit.
    brief = OrgBrief(org_uuid="pro_1", segment="1A", plan_tier="basic")
    features = brief.match_feature_map()
    assert features["segment"] == "1A"
    assert features["plan"] == "basic"


async def test_unknown_fields_are_dropped_not_stored(httpx_mock: HTTPXMock) -> None:
    # A stray raw/PII column must never survive the allowlist projection.
    row = {**_rows()[0], "customer_email": "leak@example.com", "raw_due_usd": 4302}
    httpx_mock.add_response(json=[row])
    batch = await make_client().fetch(["pro_1"])
    dumped = batch.model_dump_json().lower()
    assert "leak@example.com" not in dumped
    assert "raw_due_usd" not in dumped
    assert batch.organizations[0].open_ar_band == "low"


async def test_audience_query_version_is_captured_not_stored_on_orgs(
    httpx_mock: HTTPXMock,
) -> None:
    # The flow's SQL code node stamps its own version; the client surfaces it
    # as batch metadata while the allowlist keeps it off the org briefs.
    row = {**_rows()[0], "audience_query_version": "audience_v8"}
    httpx_mock.add_response(json=[row])
    batch = await make_client().fetch(["pro_1"])
    assert batch.audience_query_version == "audience_v8"
    assert "audience_v8" not in batch.organizations[0].model_dump_json()


async def test_missing_audience_query_version_degrades_to_none(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(json=_rows())
    batch = await make_client().fetch(["pro_1"])
    assert batch.audience_query_version is None


async def test_wrong_contract_version_is_refused(httpx_mock: HTTPXMock) -> None:
    row = {**_rows()[0], "contract_version": "org-context-v1"}
    httpx_mock.add_response(json=[row])
    with pytest.raises(ContextUnavailable):
        await make_client().fetch(["pro_1"])


async def test_missing_org_uuid_is_refused(httpx_mock: HTTPXMock) -> None:
    row = {k: v for k, v in _rows()[0].items() if k != "org_uuid"}
    httpx_mock.add_response(json=[row])
    with pytest.raises(ContextUnavailable):
        await make_client().fetch(["pro_1"])


async def test_non_object_rows_are_refused_not_crashed(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=["not-an-object"])
    with pytest.raises(ContextUnavailable):
        await make_client().fetch(["pro_1"])


async def test_n8n_fetch_posts_generic_ids(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_rows())
    client = make_client()
    result = await client.fetch(["pro_1", "pro_2"])
    assert result.organizations[0].plan_tier == "basic"
    request = httpx_mock.get_request()
    assert request is not None
    assert json.loads(request.content) == {"id": ["pro_1", "pro_2"]}
    assert request.headers["authorization"] == "Bearer test-token"


async def test_n8n_fetch_chunks_large_audiences(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_rows())
    httpx_mock.add_response(json=_rows()[:1])
    client = make_client(batch_size=2)
    result = await client.fetch(["pro_1", "pro_2", "pro_3"])
    requests = httpx_mock.get_requests()
    assert [json.loads(r.content)["id"] for r in requests] == [
        ["pro_1", "pro_2"], ["pro_3"],
    ]
    assert len(result.organizations) == 3


async def test_n8n_refuses_redirects(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=302, headers={"location": "https://evil.example"})
    with pytest.raises(ContextUnavailable):
        await make_client().fetch(["pro_1"])


async def test_n8n_outage_is_explicit_not_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=503)
    with pytest.raises(ContextUnavailable):
        await make_client().fetch(["pro_1"])


async def test_rows_rekeyed_to_submitted_id_format(httpx_mock: HTTPXMock) -> None:
    # The flow accepts numeric org ids, pro_<hex> ids, and dashed uuids, but
    # always answers keyed by the dashed org_uuid. Briefs must come back keyed
    # by the id the caller submitted, or pipeline matching abstains every pro.
    row = {**_rows()[0], "org_uuid": "7f8a05b2-ec02-4c07-8bbe-ccfa9000abfb"}
    httpx_mock.add_response(json=[row])
    submitted = "pro_7F8A05B2EC024C078BBECCFA9000ABFB"
    batch = await make_client().fetch([submitted])
    assert batch.organizations[0].pro_id == submitted


async def test_pro_uuid_is_a_distinct_id_space_and_still_matches(httpx_mock: HTTPXMock) -> None:
    # Live contract: pro_<hex> is an Iterable user id, NOT the org_uuid with a
    # prefix. The flow resolves it and echoes it back as pro_uuid on each row
    # (seen in execution 36657272). Matching must use those echoed ids.
    row = {
        **_rows()[0],
        "org_uuid": "144fea96-4526-44ac-92e0-31d956fafa72",
        "pro_uuid": "pro_f05fd57012f343f59f3bc3f6c575e7ec",
        "organization_id": 920618,
    }
    httpx_mock.add_response(json=[row])
    submitted = "pro_f05fd57012f343f59f3bc3f6c575e7ec"
    batch = await make_client().fetch([submitted])
    assert batch.organizations[0].pro_id == submitted


async def test_numeric_org_id_matches_via_organization_id(httpx_mock: HTTPXMock) -> None:
    row = {
        **_rows()[0],
        "org_uuid": "144fea96-4526-44ac-92e0-31d956fafa72",
        "pro_uuid": "pro_f05fd57012f343f59f3bc3f6c575e7ec",
        "ORGANIZATION_ID": 920618,
    }
    httpx_mock.add_response(json=[row])
    batch = await make_client().fetch(["920618"])
    assert batch.organizations[0].pro_id == "920618"


async def test_unrequested_rows_keep_their_own_uuid(httpx_mock: HTTPXMock) -> None:
    row = {**_rows()[0], "org_uuid": "11111111-2222-3333-4444-555555555555"}
    httpx_mock.add_response(json=[row])
    batch = await make_client().fetch(["pro_7f8a05b2ec024c078bbeccfa9000abfb"])
    assert batch.organizations[0].org_uuid == "11111111-2222-3333-4444-555555555555"
