import json
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from waypoint.n8n import (
    ALLOWED_FIELDS,
    CONTRACT_VERSION,
    ContextConfigurationError,
    ContextUnavailable,
    N8NContextClient,
    OrgBrief,
    OrgContextBatch,
)

FIXTURE = Path(__file__).parent / "fixtures" / "n8n_context.json"

N8N_URL = "https://n8n.example/webhook/context"


def make_client(
    batch_size: int = 5,
    promotion_store=None,
    promotion_loader=None,
) -> N8NContextClient:
    # backoff 0 so retry tests don't sleep for real
    return N8NContextClient(
        url=N8N_URL,
        token="test-token",
        batch_size=batch_size,
        backoff_seconds=0.0,
        promotion_store=promotion_store,
        promotion_loader=promotion_loader,
    )


def _rows() -> list[dict]:
    """Wire format is a bare array of rows, each stamped with the contract."""
    batch = json.loads(FIXTURE.read_text())
    return [{"contract_version": CONTRACT_VERSION, **org} for org in batch["organizations"]]


def test_n8n_fixture_obeys_ai_egress_contract() -> None:
    batch = OrgContextBatch.model_validate_json(FIXTURE.read_text())
    assert batch.contract_version == CONTRACT_VERSION
    # Every field that crosses is on the allowlist (consent *state* bands like
    # email_consent_state are allowlisted; raw contact data is not).
    permitted = set(ALLOWED_FIELDS) | {"org_uuid", "pro_uuid", "org_id"}
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


def test_channel_recommendation_and_usage_states_reach_safe_match_context() -> None:
    brief = OrgBrief(
        org_uuid="pro_1",
        suggested_channel="EMAIL",
        feature_online_booking_state="attached_unused",
        feature_voip_state="not_attached",
    )
    assert brief.suggested_outreach_channel() == "email"
    assert brief.match_feature_map()["booking_attached"] is True
    assert brief.match_feature_map()["voip_attached"] is False


def test_unknown_feature_state_is_not_misrepresented_as_attached() -> None:
    features = OrgBrief(
        org_uuid="pro_1", feature_voip_state="unexpected_upstream_value"
    ).match_feature_map()
    assert "voip_attached" not in features


def test_invalid_channel_recommendation_is_not_treated_as_outreach() -> None:
    assert OrgBrief(org_uuid="pro_1", suggested_channel="push").suggested_outreach_channel() is None


async def test_unknown_fields_are_dropped_not_stored(httpx_mock: HTTPXMock) -> None:
    # A stray raw/PII column must never survive the allowlist projection.
    row = {**_rows()[0], "customer_email": "leak@example.com", "raw_due_usd": 4302}
    httpx_mock.add_response(json=[row])
    batch = await make_client().fetch(["pro_1"])
    dumped = batch.model_dump_json().lower()
    assert "leak@example.com" not in dumped
    assert "raw_due_usd" not in dumped
    assert batch.organizations[0].open_ar_band == "low"


async def test_active_promotion_retains_only_promoted_canonical_values(
    httpx_mock: HTTPXMock,
) -> None:
    bundle = {
        "rules": [{
            "source_key": "JOBS_CREATED_T28",
            "canonical_key": "jobs_created_t28",
            "related_features": ["jobs"],
        }],
        "feature_catalog": [{
            "feature": "jobs",
            "Product Area": "Jobs",
            "Value Statement": "Manage job workflows.",
        }],
    }

    class ActivePromotion:
        def read_active(self):
            return bundle

    row = {
        **_rows()[0],
        "JOBS_CREATED_T28": 12,
        "UNAPPROVED_VALUE": 99,
        "customer_email": "leak@example.com",
    }
    httpx_mock.add_response(json=[row])

    brief = (await make_client(promotion_store=ActivePromotion()).fetch(["pro_1"])).organizations[0]

    assert brief.curated_context == {
        "v": {"jobs_created_t28": 12},
        "f": {"jobs_created_t28": ["jobs"]},
        "pc": {"jobs": {"a": "Jobs", "v": "Manage job workflows."}},
    }
    assert "UNAPPROVED_VALUE" not in str(brief.curated_context)
    assert "customer_email" not in str(brief.curated_context)
    assert "curated_context" not in brief.model_dump()


async def test_staging_can_load_the_active_promotion_from_postgres_boundary(
    httpx_mock: HTTPXMock,
) -> None:
    async def load_promotion():
        return {
            "rules": [{
                "source_key": "JOBS_CREATED_T28",
                "canonical_key": "jobs_created_t28",
                "related_features": ["jobs"],
            }],
            "feature_catalog": [],
        }

    httpx_mock.add_response(json=[{**_rows()[0], "jobs_created_t28": 12}])

    brief = (
        await make_client(promotion_loader=load_promotion).fetch(["pro_1"])
    ).organizations[0]

    assert brief.curated_context == {
        "v": {"jobs_created_t28": 12},
        "f": {"jobs_created_t28": ["jobs"]},
    }


async def test_active_promotion_with_no_matching_values_fails_closed(
    httpx_mock: HTTPXMock,
) -> None:
    bundle = {
        "rules": [{
            "source_key": "NEW_QUERY_FIELD",
            "canonical_key": "new_query_field",
            "related_features": [],
        }],
        "feature_catalog": [],
    }

    class ActivePromotion:
        def read_active(self):
            return bundle

    httpx_mock.add_response(json=[_rows()[0]])

    brief = (await make_client(promotion_store=ActivePromotion()).fetch(["pro_1"])).organizations[0]

    assert brief.curated_context == {"v": {}}


async def test_active_promotion_drops_a_value_when_it_contains_pii(
    httpx_mock: HTTPXMock,
) -> None:
    bundle = {
        "rules": [{
            "source_key": "CUSTOMER_SEGMENT",
            "canonical_key": "customer_segment",
            "related_features": [],
        }],
        "feature_catalog": [],
    }

    class ActivePromotion:
        def read_active(self):
            return bundle

    httpx_mock.add_response(json=[{
        **_rows()[0],
        "CUSTOMER_SEGMENT": "unexpected@example.invalid",
    }])

    brief = (await make_client(promotion_store=ActivePromotion()).fetch(["pro_1"])).organizations[0]

    assert brief.curated_context == {"v": {}}
    assert "unexpected@example.invalid" not in str(brief.curated_context)


async def test_standard_client_never_applies_a_promotion(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=[{**_rows()[0], "JOBS_CREATED_T28": 12}])
    brief = (await make_client().fetch(["pro_1"])).organizations[0]
    assert brief.curated_context is None


async def test_staging_requires_canonical_aliases_instead_of_ambiguous_source_fallback(
    httpx_mock: HTTPXMock,
) -> None:
    bundle = {
        "rules": [
            {"source_key": "count", "canonical_key": "workflow_entries_count"},
            {"source_key": "count", "canonical_key": "workflow_progress_count"},
        ],
        "feature_catalog": [],
    }

    class ActivePromotion:
        def read_active(self):
            return bundle

    httpx_mock.add_response(json=[{
        **_rows()[0],
        "count": 99,
        "workflow_entries_count": 12,
    }])

    brief = (await make_client(promotion_store=ActivePromotion()).fetch(["pro_1"])).organizations[0]

    assert brief.curated_context == {"v": {"workflow_entries_count": 12}}


async def test_staging_fails_closed_when_promotion_artifact_is_missing(
    httpx_mock: HTTPXMock,
) -> None:
    class MissingPromotion:
        def read_active(self):
            return None

    httpx_mock.add_response(json=_rows())

    with pytest.raises(ContextUnavailable, match="promotion"):
        await make_client(promotion_store=MissingPromotion()).fetch(["pro_1"])


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
    # A persistent outage is retried, then surfaced explicitly — never as an
    # empty batch.
    for _ in range(3):
        httpx_mock.add_response(status_code=503)
    with pytest.raises(ContextUnavailable):
        await make_client().fetch(["pro_1"])
    assert len(httpx_mock.get_requests()) == 3


async def test_transient_504_is_retried_not_fatal(httpx_mock: HTTPXMock) -> None:
    # The incident shape: one gateway timeout from a slow Snowflake query must
    # not burn the job attempt when the next try succeeds.
    httpx_mock.add_response(status_code=504)
    httpx_mock.add_response(json=_rows())
    batch = await make_client().fetch(["pro_1"])
    assert len(batch.organizations) == 2
    assert len(httpx_mock.get_requests()) == 2


async def test_hard_errors_are_not_retried(httpx_mock: HTTPXMock) -> None:
    # A 4xx contract problem won't fix itself; retrying just hammers the flow.
    httpx_mock.add_response(status_code=400)
    with pytest.raises(ContextUnavailable):
        await make_client().fetch(["pro_1"])
    assert len(httpx_mock.get_requests()) == 1


async def test_async_202_is_reported_as_a_configuration_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(status_code=202, json={"status": "accepted"})
    with pytest.raises(ContextConfigurationError, match="synchronous Standard workflow"):
        await make_client().fetch(["pro_1"])
    assert len(httpx_mock.get_requests()) == 1


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


def test_calibration_cell_is_always_none() -> None:
    """Pinned: the live tenure vocabulary never overlaps the cards' bands, so
    composing a key would always miss. Disabled until the vocabularies are
    reconciled at the flow — see calibration_cell()'s docstring."""
    brief = OrgBrief(org_uuid="p", segment="1A", plan_tier="basic", tenure_band="0-3m")
    assert brief.calibration_cell() is None


async def test_each_context_retry_renews_the_callers_lease(httpx_mock: HTTPXMock) -> None:
    # A read timeout is an httpx.HTTPError, so a stalled flow costs the FULL
    # retry budget — 3 x N8N_TIMEOUT_SECONDS plus backoff, past the 1800s
    # lease — and nothing heartbeats around this call: it runs in run_job
    # before any stage handler. The hook renews the lease between attempts
    # rather than shrinking a budget the flow legitimately needs.
    beats: list[int] = []

    async def on_retry() -> None:
        beats.append(len(httpx_mock.get_requests()))

    httpx_mock.add_exception(httpx.ReadTimeout("flow still running"))
    httpx_mock.add_exception(httpx.ReadTimeout("flow still running"))
    httpx_mock.add_response(json=_rows())

    batch = await make_client().fetch(["pro_1"], on_retry)

    assert len(batch.organizations) == 2
    # One renewal before each RE-attempt, none before the first.
    assert beats == [1, 2]


async def test_the_context_retry_hook_is_optional(httpx_mock: HTTPXMock) -> None:
    # Every other caller (and every other test) passes nothing.
    httpx_mock.add_exception(httpx.ReadTimeout("flow still running"))
    httpx_mock.add_response(json=_rows())
    batch = await make_client().fetch(["pro_1"])
    assert len(batch.organizations) == 2
