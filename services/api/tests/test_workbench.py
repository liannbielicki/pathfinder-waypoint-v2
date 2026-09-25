import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from waypoint.workbench import (
    ContextLayerClient,
    N8NContextClient,
    WorkbenchStage,
    build_audit_inventory,
    build_product_index,
    compile_catalog_contract,
    compile_context,
    context_layer_coverage,
    org_uuid_from_n8n,
    parse_feature_catalog_csv,
    prioritize_review_exceptions,
    redact,
    resolve_product_cards,
    run_model,
    scrub_pii,
    unwrap_source_payload,
    validate_catalog_entries,
)
from waypoint.workbench_jobs import WorkbenchJobStore


def _completed_evaluation_job(
    path: Path,
    *,
    entries: list[dict[str, object]],
    feature_entries: list[dict[str, object]],
) -> str:
    store = WorkbenchJobStore(path)
    job = store.create({
        "identifier": "889901",
        "source_mode": "snowflake",
        "workbench_mode": "evaluate",
        "catalog_override": entries,
        "catalog_version_id": "context-one",
        "feature_catalog_entries": feature_entries,
        "feature_catalog_version_id": "features-one",
    })
    store.complete(
        job.id,
        {"stages": [], "warnings": [], "outputs": {"evaluation": {"judge": {"winner": "curated"}}}},
        status="completed",
    )
    return job.id


def test_trace_stage_names_are_kept_in_execution_order():
    stages = [WorkbenchStage(name="input"), WorkbenchStage(name="raw_context")]
    assert [stage.name for stage in stages] == ["input", "raw_context"]


@pytest.mark.asyncio
async def test_run_model_retries_without_effort_when_model_rejects_it(monkeypatch):
    import anthropic

    calls: list[dict[str, object]] = []

    class EffortUnsupported(Exception):
        status_code = 400

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.messages = self

        async def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            if "output_config" in kwargs:
                raise EffortUnsupported("This model does not support the effort parameter.")
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="[]")],
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
                stop_reason="end_turn",
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeClient)

    text, metrics = await run_model(
        "Return JSON",
        api_key="test-key",
        model="claude-haiku-4-5",
        stage="authoring_generation_1",
        effort="low",
    )

    assert text == "[]"
    assert calls[0]["output_config"] == {"effort": "low"}
    assert "output_config" not in calls[1]
    assert metrics["output_tokens"] == 2


def test_redact_masks_secrets_recursively():
    value = {"api_key": "secret", "nested": {"authorization": "Bearer secret", "ok": 1}}
    assert redact(value) == {
        "api_key": "[REDACTED]",
        "nested": {"authorization": "[REDACTED]", "ok": 1},
    }
    assert redact({"key": "T28_JOBS_CREATED", "private_key": "secret"}) == {
        "key": "T28_JOBS_CREATED", "private_key": "[REDACTED]"
    }


@pytest.mark.asyncio
async def test_context_layer_client_gets_org_payload_without_redirects():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json={"firmographics": {"industry": "HVAC"}, "features": []})

    client = ContextLayerClient(
        transport=httpx.MockTransport(handler),
        timeout=1,
    )
    result = await client.fetch("org-123", "https://context.test", "test-key")
    assert result["firmographics"]["industry"] == "HVAC"
    assert seen == {
        "method": "GET",
        "url": "https://context.test/api/context_layer/org-123",
        "authorization": "Bearer test-key",
    }


@pytest.mark.asyncio
async def test_n8n_context_client_posts_organization_id_with_bearer_token():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[{"VARIABLE_NAME": "SEGMENT_ACTUAL", "VALUE": "1A"}])

    client = N8NContextClient(transport=httpx.MockTransport(handler), timeout=1)
    result = await client.fetch("889901", "https://n8n.test/webhook/context", "test-token")
    assert result[0]["VARIABLE_NAME"] == "SEGMENT_ACTUAL"
    assert seen == {
        "method": "POST",
        "url": "https://n8n.test/webhook/context",
        "authorization": "Bearer test-token",
        "body": {"organization_id": "889901"},
    }


@pytest.mark.asyncio
async def test_n8n_context_client_starts_async_request_with_correlation_ids():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["headers"] = {
            "organization_id": request.headers["x-waypoint-organization-id"],
            "request_id": request.headers["x-waypoint-request-id"],
            "promotion_id": request.headers["x-waypoint-promotion-id"],
            "callback_mode": request.headers["x-waypoint-callback-mode"],
        }
        return httpx.Response(
            202,
            json={"status": "accepted", "request_id": "job-123"},
        )

    client = N8NContextClient(transport=httpx.MockTransport(handler), timeout=1)
    result = await client.start(
        "889901",
        "https://n8n.test/webhook/context",
        "test-token",
        request_id="job-123",
        promotion_id="promotion-456",
        callback_mode="workbench",
    )

    assert result == {"status": "accepted", "request_id": "job-123"}
    assert seen["body"] == {
        "organization_id": "889901",
        "request_id": "job-123",
        "promotion_id": "promotion-456",
        "callback_mode": "workbench",
    }
    assert seen["headers"] == {
        "organization_id": "889901",
        "request_id": "job-123",
        "promotion_id": "promotion-456",
        "callback_mode": "workbench",
    }


@pytest.mark.asyncio
async def test_n8n_context_client_defaults_waypoint_requests_to_staging_callback():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["callback_mode"] = request.headers["x-waypoint-callback-mode"]
        return httpx.Response(
            202,
            json={"status": "accepted", "request_id": "job-123"},
        )

    client = N8NContextClient(transport=httpx.MockTransport(handler), timeout=1)
    await client.start(
        "889901",
        "https://n8n.test/webhook/context",
        "test-token",
        request_id="job-123",
        promotion_id="promotion-456",
    )

    assert seen == {
        "body": {
            "organization_id": "889901",
            "request_id": "job-123",
            "promotion_id": "promotion-456",
            "callback_mode": "staging",
        },
        "callback_mode": "staging",
    }


@pytest.mark.parametrize(
    "payload",
    [
        [{"VARIABLE_NAME": "ORG_UUID", "VALUE": "cc962bf1-13bb-4eea-bf66-f3adc9e22192"}],
        [{
            "VARIABLE_NAME": "ORG_SNAPSHOT",
            "VALUE": {"ORG_UUID": "cc962bf1-13bb-4eea-bf66-f3adc9e22192"},
        }],
    ],
)
def test_context_layer_uuid_resolves_from_supported_n8n_rows(payload):
    assert org_uuid_from_n8n(payload) == "cc962bf1-13bb-4eea-bf66-f3adc9e22192"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [{"VARIABLE_NAME": "ORG_UUID", "VALUE": "not-a-uuid"}],
        [
            {"VARIABLE_NAME": "ORG_UUID", "VALUE": "cc962bf1-13bb-4eea-bf66-f3adc9e22192"},
            {"VARIABLE_NAME": "ORG_UUID", "VALUE": "6ee00fbd-3f85-4f44-a1f0-9bf4d3ed4fd5"},
        ],
    ],
)
def test_context_layer_uuid_fails_closed_when_missing_invalid_or_conflicting(payload):
    assert org_uuid_from_n8n(payload) is None


@pytest.mark.asyncio
async def test_n8n_context_client_rejects_a_mismatched_async_ack():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"status": "accepted", "request_id": "wrong"})

    client = N8NContextClient(transport=httpx.MockTransport(handler), timeout=1)
    with pytest.raises(ValueError, match="request ID"):
        await client.start(
            "889901",
            "https://n8n.test/webhook/context",
            "test-token",
            request_id="job-123",
            promotion_id="promotion-456",
        )


def test_n8n_context_client_allows_four_minutes_for_a_response():
    client = N8NContextClient()
    assert client._timeout.read == 240.0


@pytest.mark.asyncio
async def test_n8n_context_client_reports_when_the_response_times_out():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    client = N8NContextClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TimeoutError, match="Snowflake/n8n timed out after 240 seconds"):
        await client.fetch("889901", "https://n8n.test/webhook/context", "test-token")


@pytest.mark.asyncio
async def test_n8n_context_client_reports_connection_failures_without_the_webhook_url():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = N8NContextClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ConnectionError, match=r"Snowflake/n8n connection failed \(ConnectError\)"):
        await client.fetch("889901", "https://n8n.test/secret-webhook", "test-token")


@pytest.mark.asyncio
async def test_n8n_context_client_reports_invalid_json():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = N8NContextClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="Snowflake/n8n returned invalid JSON"):
        await client.fetch("889901", "https://n8n.test/webhook/context", "test-token")


def test_fixture_and_product_helpers_expose_candidates_without_raw_secret_fields():
    context = {"features": [{"name": "hcp_assist", "display_name": "HCP AI"}]}
    index = build_product_index({"hcp_assist": {"description": "AI assistance"}})
    cards, warnings = resolve_product_cards(
        [{"title": "Try HCP AI", "mechanism": "hcp_assist"}],
        {"hcp_assist": {"description": "AI assistance", "api_key": "secret"}},
    )
    assert context["features"][0]["display_name"] == "HCP AI"
    assert index[0]["product_key"] == "hcp_assist"
    assert cards[0]["product_key"] == "hcp_assist"
    assert cards[0]["card"]["api_key"] == "[REDACTED]"
    assert warnings == []


def test_context_layer_rejects_non_object_json():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(["not", "an", "object"]))

    client = ContextLayerClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TypeError, match="object"):
        import asyncio

        asyncio.run(client.fetch("org-123", "https://context.test", "test-key"))


def test_pii_gate_removes_sensitive_fields_and_identity_values_without_leaking_values():
    clean, ledger = scrub_pii(
        {
            "organization_name": "Mountain HVAC",
            "primary_contact_name": "Karla Bravo LaPointe",
            "best_email": "karla@example.com",
            "phone": "408-351-8919",
            "address": "10 Main Street",
            "city": "Rohnert Park",
            "state": "CA",
            "zip": "94928",
            "salesforce_account_id": "001-secret",
            "pain_points": "Karla wants fewer billing surprises",
            "industry": "Heating and Air Conditioning",
        }
    )
    assert clean["organization_name"] == "Mountain HVAC"
    assert "primary_contact_name" not in clean
    assert "best_email" not in clean
    assert "phone" not in clean
    assert "address" not in clean
    assert clean["city"] == "Rohnert Park"
    assert clean["state"] == "CA"
    assert "zip" not in clean
    assert clean["salesforce_account_id"] == "001-secret"
    assert clean["industry"] == "Heating and Air Conditioning"
    serialized = json.dumps(ledger)
    assert "Karla" not in serialized
    assert "karla@example.com" not in serialized
    assert all(set(item) == {"path", "category", "reason"} for item in ledger)


def test_pii_exclusion_preserves_non_pii_unknown_fields():
    clean, ledger = scrub_pii({"unknown_field": "some unclassified value"})
    assert clean == {"unknown_field": "some unclassified value"}
    assert ledger == []


def test_pii_exclusion_preserves_audit_metadata_fields():
    clean, ledger = scrub_pii({
        "query_name": "part_1_org_snapshot",
        "variable_name": "SEGMENT_ACTUAL",
        "value": "1A",
        "metadata": {"source_table": "marts.customer_base.agg_current_customer"},
    })
    assert clean["query_name"] == "part_1_org_snapshot"
    assert clean["variable_name"] == "SEGMENT_ACTUAL"
    assert clean["value"] == "1A"
    assert clean["metadata"]["source_table"] == "marts.customer_base.agg_current_customer"
    assert not ledger


def test_pii_exclusion_preserves_uppercase_audit_keys_and_dates():
    clean, ledger = scrub_pii({
        "QUERY_NAME": "part_3_health_index",
        "VARIABLE_NAME": "BIZDATE",
        "VALUE": "2026-09-09",
        "METADATA": {"MIN_DATE": "2026-01-01", "TABLE_NAME": "HEALTH_INDEX"},
    })
    assert clean["QUERY_NAME"] == "part_3_health_index"
    assert clean["VARIABLE_NAME"] == "BIZDATE"
    assert clean["VALUE"] == "2026-09-09"
    assert clean["METADATA"] == {"MIN_DATE": "2026-01-01", "TABLE_NAME": "HEALTH_INDEX"}
    assert ledger == []


def test_pii_exclusion_keeps_internal_identifiers_and_business_state_variables():
    clean, ledger = scrub_pii({
        "rows": [
            {"VARIABLE_NAME": "ORG_UUID", "VALUE": "org-secret"},
            {"VARIABLE_NAME": "FEATURE_VOIP_STATE", "VALUE": "attached_unused"},
        ]
    })
    assert clean["rows"] == [
        {"VARIABLE_NAME": "ORG_UUID", "VALUE": "org-secret"},
        {"VARIABLE_NAME": "FEATURE_VOIP_STATE", "VALUE": "attached_unused"}
    ]
    assert ledger == []
    inventory = build_audit_inventory({"snowflake": clean})
    assert [item["key"] for item in inventory] == ["ORG_UUID", "FEATURE_VOIP_STATE"]


def test_pii_exclusion_drops_sensitive_identifiers_but_keeps_internal_ids():
    clean, ledger = scrub_pii({
        "date_of_birth": "1990-01-01",
        "rows": [
            {"VARIABLE_NAME": "DATE_OF_BIRTH", "VALUE": "1990-01-01"},
            {"VARIABLE_NAME": "SSN_LAST_FOUR", "VALUE": "1234"},
            {"VARIABLE_NAME": "BANK_ROUTING_NUMBER", "VALUE": "021000021"},
            {"VARIABLE_NAME": "CUSTOMER_ID", "VALUE": "customer-123"},
            {"VARIABLE_NAME": "EMPLOYEE_ID", "VALUE": "employee-123"},
            {"VARIABLE_NAME": "ACCOUNT_KEY", "VALUE": "account-123"},
            {"VARIABLE_NAME": "BIZDATE", "VALUE": "2026-09-09"},
        ]
    })

    assert clean["rows"] == [
        {"VARIABLE_NAME": "CUSTOMER_ID", "VALUE": "customer-123"},
        {"VARIABLE_NAME": "EMPLOYEE_ID", "VALUE": "employee-123"},
        {"VARIABLE_NAME": "ACCOUNT_KEY", "VALUE": "account-123"},
        {"VARIABLE_NAME": "BIZDATE", "VALUE": "2026-09-09"},
    ]
    assert "date_of_birth" not in clean
    assert len(ledger) == 4


def test_pii_exclusion_keeps_company_geography_metrics_and_business_values():
    clean, ledger = scrub_pii({
        "rows": [
            {"VARIABLE_NAME": "COMPANY_CITY", "VALUE": "Rohnert Park"},
            {"VARIABLE_NAME": "COMPANY_STATE", "VALUE": "CA"},
            {"VARIABLE_NAME": "COMPANY_COUNTRY", "VALUE": "United States"},
            {"VARIABLE_NAME": "EMAIL_OPEN_RATE_T28", "VALUE": 0.42},
            {"VARIABLE_NAME": "SALESFORCE_CONTACT_COUNT", "VALUE": 8},
            {"VARIABLE_NAME": "LATEST_PLAN_NAME", "VALUE": "Essentials Package"},
            {"VARIABLE_NAME": "INTERNAL_SCORE", "VALUE": "94928"},
        ]
    })

    assert clean["rows"] == [
        {"VARIABLE_NAME": "COMPANY_CITY", "VALUE": "Rohnert Park"},
        {"VARIABLE_NAME": "COMPANY_STATE", "VALUE": "CA"},
        {"VARIABLE_NAME": "COMPANY_COUNTRY", "VALUE": "United States"},
        {"VARIABLE_NAME": "EMAIL_OPEN_RATE_T28", "VALUE": 0.42},
        {"VARIABLE_NAME": "SALESFORCE_CONTACT_COUNT", "VALUE": 8},
        {"VARIABLE_NAME": "LATEST_PLAN_NAME", "VALUE": "Essentials Package"},
        {"VARIABLE_NAME": "INTERNAL_SCORE", "VALUE": "94928"},
    ]
    assert ledger == []


def test_pii_exclusion_still_drops_zip_postal_and_direct_personal_contact_fields():
    clean, ledger = scrub_pii({
        "rows": [
            {"VARIABLE_NAME": "COMPANY_ZIP", "VALUE": "94928"},
            {"VARIABLE_NAME": "POSTAL_CODE", "VALUE": "94928-1234"},
            {"VARIABLE_NAME": "PRIMARY_CONTACT_NAME", "VALUE": "Karla Bravo"},
            {"VARIABLE_NAME": "CONTACT_EMAIL", "VALUE": "karla@example.com"},
            {"VARIABLE_NAME": "MOBILE_PHONE", "VALUE": "408-351-8919"},
            {"VARIABLE_NAME": "STREET_ADDRESS", "VALUE": "10 Main Street"},
            {"VARIABLE_NAME": "ORG_UUID", "VALUE": "6ee00fbd-3f85-4f44-a1f0-9bf4d3ed4fd5"},
        ]
    })

    assert clean["rows"] == [
        {"VARIABLE_NAME": "ORG_UUID", "VALUE": "6ee00fbd-3f85-4f44-a1f0-9bf4d3ed4fd5"}
    ]
    assert len(ledger) == 6


def test_unwraps_n8n_context_envelope_before_pii_projection():
    payload = {"context": {"context_row": {"SEGMENT_ACTUAL": "1A"}, "event_context": {"calls_total_count": 2}}}
    assert unwrap_source_payload("snowflake", payload) == {"SEGMENT_ACTUAL": "1A", "calls_total_count": 2}


def test_audit_inventory_preserves_every_experimental_row_and_observed_evidence():
    rows = [
        {
            "QUERY_NAME": f"query_{index % 4}",
            "VARIABLE_NAME": f"VARIABLE_{index}",
            "VALUE": None if index == 7 else index,
            "METADATA": {"DATA_TYPE": "NUMBER"},
        }
        for index in range(650)
    ]

    inventory = build_audit_inventory({"snowflake": {"rows": rows}})

    assert len(inventory) == 650
    assert inventory[7] == {
        "key": "VARIABLE_7",
        "source": "snowflake",
        "source_path": "rows[7].VALUE",
        "source_query": "query_3",
        "observed_state": "null",
        "observed_type": "null",
        "basis": "observed",
    }


def test_audit_inventory_excludes_contact_candidate_rows():
    rows = [
        {"VARIABLE_NAME": "SAFE", "VALUE": 1, "QUERY_NAME": "part_1_org_snapshot"},
        {
            "QUERY_NAME": "waypoint_contact_candidate",
            "VARIABLE_NAME": "contact_candidate",
            "VALUE": {"pro_uuid": "pro_aaa"},
        },
    ]

    inventory = build_audit_inventory({"snowflake": {"rows": rows}})

    assert [item["key"] for item in inventory] == ["SAFE"]


def test_feature_catalog_csv_requires_unique_exact_feature_keys():
    entries = parse_feature_catalog_csv(
        'feature,description,display_name\nonline_booking,"Book, online",Online Booking\nvoip,Calls,Voice\n'
    )
    assert [entry["feature"] for entry in entries] == ["online_booking", "voip"]
    assert entries[0]["description"] == "Book, online"

    with pytest.raises(ValueError, match="duplicate feature key"):
        parse_feature_catalog_csv("feature,description\nvoip,One\nvoip,Two\n")


def test_feature_catalog_csv_accepts_display_name_and_preserves_duplicate_columns():
    entries = parse_feature_catalog_csv(
        "Display Name,Product Area,Engaged,Engaged\n"
        "sms_number,SMS Text messaging,Activated,Sends messages\n"
    )

    assert entries == [
        {
            "feature": "sms_number",
            "Display Name": "sms_number",
            "Product Area": "SMS Text messaging",
            "Engaged": "Activated",
            "Engaged (2)": "Sends messages",
        }
    ]


def test_feature_catalog_csv_rejects_oversized_value_statement():
    statement = "x" * 1001

    with pytest.raises(
        ValueError,
        match="Value Statement for sms_number exceeds 1000 characters on line 2",
    ):
        parse_feature_catalog_csv(
            f"Feature Key,Value Statement\nsms_number,{statement}\n"
        )


def test_feature_context_preserves_complete_value_statement():
    from waypoint.workbench_api import _compact_feature_catalog

    statement = "Complete feature context. " * 18
    entry = {
        "feature": "sms_number",
        "Product Area": "Revenue",
        "Value Statement": statement,
    }

    assert len(statement) > 240
    assert _compact_feature_catalog([entry])[0]["description"] == statement.strip()

    compiled = compile_context(
        {"snowflake": {"rows": []}},
        [],
        feature_catalog_entries=[entry],
        feature_catalog_version_id="features-v1",
        context_catalog_version_id="context-v1",
    )
    assert compiled["context"]["pc"]["sms_number"]["v"] == statement.strip()


def test_audit_inventory_preserves_exact_source_table_lineage():
    inventory = build_audit_inventory({
        "snowflake": {
            "rows": [
                {
                    "VARIABLE_NAME": "JOBS_CREATED_T28",
                    "VALUE": 12,
                    "QUERY_NAME": "jobs_usage",
                    "SOURCE_TABLE": "ANALYTICS.JOBS_DAILY",
                },
                {
                    "VARIABLE_NAME": "HEALTH_GRADE",
                    "VALUE": "B",
                    "QUERY_NAME": "health",
                    "METADATA": {"TABLE_NAME": "ANALYTICS.HEALTH_INDEX"},
                },
            ],
        },
    })

    assert inventory[0]["source_table"] == "ANALYTICS.JOBS_DAILY"
    assert inventory[1]["source_table"] == "ANALYTICS.HEALTH_INDEX"


def test_context_layer_coverage_checks_every_catalog_feature():
    coverage = context_layer_coverage(
        {"features": [{"name": "voip"}, {"name": "unknown_live_key"}]},
        ["online_booking", "voip", "hcp_assist"],
    )
    assert coverage == {
        "total_catalog_features": 3,
        "present_count": 1,
        "absent_count": 2,
        "present_features": ["voip"],
        "absent_features": ["hcp_assist", "online_booking"],
        "unmatched_response_features": ["unknown_live_key"],
    }


def test_context_layer_fields_are_auditable_and_compilable():
    sources = {
        "context_layer": {
            "firmographics": {"industry": "HVAC", "budget_range": None},
            "features": [{"name": "voip", "rank": 2, "adopted": False}],
        }
    }
    inventory = build_audit_inventory(sources)
    assert [item["key"] for item in inventory] == [
        "context_layer.features.voip.adopted",
        "context_layer.features.voip.rank",
        "context_layer.firmographics.budget_range",
        "context_layer.firmographics.industry",
    ]
    assert inventory[2]["observed_state"] == "null"
    compiled = compile_context(
        sources,
        [{
            "key": "context_layer.features.voip.rank",
            "canonical_key": "voip_rank",
            "related_features": ["voip"],
            "disposition": "include",
            "review_status": "reviewed",
        }],
        feature_catalog_version_id="features-v1",
        context_catalog_version_id="context-v1",
    )
    assert compiled["context"] == {"v": {"voip_rank": 2}, "f": {"voip_rank": ["voip"]}}


def test_catalog_validation_removes_unverified_features_and_normalizes_decisions():
    entries, warnings = validate_catalog_entries(
        [
            {
                "key": "JOBS_CREATED",
                "canonical_key": "jobs_created",
                "value_category": "activity",
                "related_features": ["jobs", "invented"],
                "usefulness_rank": 5,
                "disposition": "include",
                "aggregate_prompt": "Calculate cohort percentiles for comparable Pros.",
            }
        ],
        {"jobs"},
    )
    assert entries[0]["related_features"] == ["jobs"]
    assert entries[0]["review_status"] == "draft"
    assert warnings == ["JOBS_CREATED: removed unverified feature key invented"]


def test_catalog_validation_rejects_individual_aggregate_calculations():
    entries, warnings = validate_catalog_entries(
        [{
            "key": "JOBS_CREATED",
            "usefulness_rank": 5,
            "aggregate_prompt": "Calculate this Pro's percentile from this value.",
        }],
        set(),
    )
    assert entries[0]["aggregate_prompt"] is None
    assert warnings == ["JOBS_CREATED: removed aggregate prompt that was not cohort-level"]


def _catalog_entry(key: str, *, confidence: float) -> dict[str, object]:
    return {
        "key": key,
        "canonical_key": key.casefold(),
        "value_category": "activity",
        "related_features": [],
        "usefulness_rank": 5,
        "disposition": "include",
        "aggregate_prompt": None,
        "confidence": confidence,
        "uncertainty_reason": "Variable name is ambiguous" if confidence < 0.8 else None,
    }


def test_catalog_validation_preserves_model_confidence_and_sets_approval():
    entries, warnings = validate_catalog_entries(
        [{
            **_catalog_entry("SMS_SENT", confidence=0.91),
            "canonical_key": "sms_sent",
            "value_category": "communication",
            "related_features": ["sms_number"],
        }],
        {"sms_number"},
    )

    assert warnings == []
    assert entries[0]["confidence"] == 0.91
    assert entries[0]["approval_status"] == "auto_approved"
    assert entries[0]["uncertainty_reason"] is None


def test_catalog_validation_promotes_approved_deprioritize_to_include():
    entries, warnings = validate_catalog_entries(
        [{
            **_catalog_entry("CALLS", confidence=0.6),
            "disposition": "deprioritize",
            "approval_status": "human_approved",
            "review_status": "reviewed",
        }],
        set(),
    )

    assert warnings == []
    assert entries[0]["disposition"] == "include"
    assert entries[0]["approval_status"] == "auto_approved"


def test_prioritize_review_exceptions_keeps_every_deprioritized_variable():
    entries, _ = validate_catalog_entries(
        [
            {**_catalog_entry(f"KEY_{index:02}", confidence=0.5), "disposition": "deprioritize"}
            for index in range(30)
        ],
        set(),
    )

    selected, output = prioritize_review_exceptions(entries)

    assert len(selected) == 30
    assert all(item["approval_status"] == "review_required" for item in selected)
    assert sum(item["approval_status"] == "excluded" for item in output) == 0


def test_canonical_key_collision_is_flagged_without_removing_non_pii_information():
    entries, warnings = validate_catalog_entries(
        [
            {**_catalog_entry("FIRST", confidence=0.9), "canonical_key": "same", "approval_status": "human_approved"},
            {**_catalog_entry("SECOND", confidence=0.9), "canonical_key": "same", "approval_status": "human_approved"},
        ],
        set(),
    )

    assert [entry["approval_status"] for entry in entries] == ["auto_approved", "auto_approved"]
    assert [entry["disposition"] for entry in entries] == ["include", "include"]
    assert warnings == ["canonical key same is used by multiple variables"]


def test_compile_context_attaches_the_full_selected_feature_catalog():
    entries, _ = validate_catalog_entries(
        [{
            **_catalog_entry("SMS_SENT", confidence=0.91),
            "canonical_key": "sms_sent",
            "value_category": "communication",
            "related_features": ["sms_number"],
        }],
        {"sms_number", "jobs"},
    )

    compiled = compile_context(
        {"snowflake": {"rows": [{"VARIABLE_NAME": "SMS_SENT", "VALUE": 0}]}},
        entries,
        feature_catalog_entries=[
            {
                "feature": "sms_number",
                "Product Area": "SMS",
                "Value Statement": "Customer texting number",
            },
            {
                "feature": "jobs",
                "Product Area": "Jobs",
                "Value Statement": "Job management",
            },
            {"feature": "feature_only"},
        ],
        feature_catalog_version_id="features-v1",
        context_catalog_version_id="context-v1",
    )

    assert compiled["context"]["pc"] == {
        "feature_only": {},
        "jobs": {"a": "Jobs", "v": "Job management"},
        "sms_number": {"a": "SMS", "v": "Customer texting number"},
    }


def test_compile_context_is_deterministic_and_uses_only_reviewed_includes():
    sources = {
        "snowflake": {
            "rows": [
                {"VARIABLE_NAME": "JOBS_CREATED", "VALUE": 12},
                {"VARIABLE_NAME": "NULL_METRIC", "VALUE": None},
                {"VARIABLE_NAME": "EXCLUDED", "VALUE": 999},
            ]
        }
    }
    entries = [
        {"key": "JOBS_CREATED", "canonical_key": "jobs", "related_features": ["jobs"], "disposition": "include", "review_status": "reviewed"},
        {"key": "NULL_METRIC", "canonical_key": "null_metric", "related_features": [], "disposition": "include", "review_status": "reviewed"},
        {"key": "EXCLUDED", "canonical_key": "excluded", "related_features": [], "disposition": "exclude", "review_status": "reviewed"},
        {"key": "NOT_REVIEWED", "canonical_key": "draft", "related_features": [], "disposition": "include", "review_status": "draft"},
    ]

    compiled = compile_context(
        sources,
        entries,
        feature_catalog_version_id="feature-v1",
        context_catalog_version_id="context-v1",
    )

    assert compiled["context"] == {
        "v": {"jobs": 12},
        "n": ["null_metric"],
        "f": {"jobs": ["jobs"]},
    }
    assert compiled["feature_catalog_version_id"] == "feature-v1"
    assert compiled["context_catalog_version_id"] == "context-v1"
    assert compiled["metrics"]["included_variables"] == 2
    assert compiled["metrics"]["estimated_tokens"] > 0


def test_compile_catalog_contract_uses_every_approved_include_without_org_values():
    compiled = compile_catalog_contract(
        [
            {
                "key": "JOBS_CREATED",
                "canonical_key": "jobs",
                "value_category": "activity",
                "related_features": ["jobs"],
                "usefulness_rank": 5,
                "disposition": "include",
                "approval_status": "auto_approved",
            },
            {
                "key": "CALLS",
                "canonical_key": "calls",
                "disposition": "deprioritize",
                "approval_status": "review_required",
            },
            {
                "key": "INTERNAL_ONLY",
                "canonical_key": "internal",
                "disposition": "exclude",
                "approval_status": "excluded",
            },
        ],
        feature_catalog_entries=[
            {"feature": "jobs", "Product Area": "Jobs", "Value Statement": "Job management"},
        ],
        feature_catalog_version_id="features-v1",
        context_catalog_version_id="context-v1",
    )

    assert compiled["context"] == {
        "r": [{"k": "JOBS_CREATED", "c": "jobs", "t": "activity", "u": 5, "f": ["jobs"]}],
        "pc": {"jobs": {"a": "Jobs", "v": "Job management"}},
    }
    assert compiled["metrics"]["included_variables"] == 1


def test_status_endpoint_identifies_env_file_without_exposing_values(monkeypatch):
    from waypoint.workbench_api import create_workbench_app

    monkeypatch.setenv("N8N_CONTEXT_URL_WORKBENCH", "https://secret.test/webhook")
    monkeypatch.setenv("N8N_TOKEN", "secret-token")
    response = TestClient(create_workbench_app()).get("/api/context-workbench/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["env_file"].endswith("services/api/.env")
    assert payload["configured"]["snowflake"] is True
    assert "secret.test" not in json.dumps(payload)
    assert "secret-token" not in json.dumps(payload)


def test_catalog_validate_endpoint_returns_immutable_content_version():
    from waypoint.workbench_api import create_workbench_app

    client = TestClient(create_workbench_app())
    body = {"name": "September", "filename": "features.csv", "csv_text": "feature,description\nvoip,Calls\n"}
    first = client.post("/api/context-workbench/catalog/validate", json=body)
    second = client.post("/api/context-workbench/catalog/validate", json=body)

    assert first.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["source_filename"] == "features.csv"
    assert first.json()["entries"][0]["feature"] == "voip"


def test_promotion_endpoint_uses_completed_server_evaluation_and_returns_exact_csv(tmp_path):
    from waypoint.workbench_api import create_workbench_app

    job_db = tmp_path / "jobs.sqlite3"
    client = TestClient(create_workbench_app(
        job_db_path=job_db,
        promotion_root=tmp_path / "promotions",
    ))
    evaluation_job_id = _completed_evaluation_job(
        job_db,
        entries=[{
            "key": "JOBS_CREATED_T28",
            "canonical_key": "jobs_created_t28",
            "source_table": "ANALYTICS.JOBS_DAILY",
            "related_features": ["jobs"],
            "usefulness_rank": 5,
            "disposition": "include",
            "approval_status": "auto_approved",
            "aggregate_prompt": "Calculate the cohort distribution.",
        }],
        feature_entries=[{
            "feature": "jobs",
            "Product Area": "Jobs",
            "Value Statement": "Manage job workflows.",
            "credential": "must-not-persist",
        }],
    )

    blocked = client.post(
        "/api/context-workbench/promotions",
        json={"evaluation_job_id": "does-not-exist"},
    )
    assert blocked.status_code == 404

    preview = client.post(
        "/api/context-workbench/promotions/preview",
        json={"evaluation_job_id": evaluation_job_id},
    )
    assert preview.status_code == 200
    assert not (tmp_path / "promotions" / "active.json").exists()

    promoted = client.post(
        "/api/context-workbench/promotions",
        json={"evaluation_job_id": evaluation_job_id},
    )
    assert promoted.status_code == 200
    payload = promoted.json()
    assert payload["included_variables"] == 1
    assert payload["counts"] == {
        "approved": 1,
        "pii_removed": 0,
        "duplicates_merged": 0,
        "retained": 1,
    }
    assert payload["csv"] == (
        "canonical_key,source_table,cohort_aggregate_prompt\n"
        "jobs_created_t28,ANALYTICS.JOBS_DAILY,Calculate the cohort distribution.\n"
    )
    assert set(payload) == {"id", "included_variables", "counts", "csv"}
    assert preview.json() == payload
    bundle = json.loads((tmp_path / "promotions" / f"{payload['id']}.json").read_text())
    assert bundle["feature_catalog"] == [{
        "feature": "jobs",
        "Product Area": "Jobs",
        "Value Statement": "Manage job workflows.",
    }]

    repeated = client.post(
        "/api/context-workbench/promotions",
        json={"evaluation_job_id": evaluation_job_id},
    )
    assert repeated.status_code == 200
    assert repeated.json() == payload


def test_promotion_endpoint_rejects_browser_supplied_catalog_and_pii_rules(tmp_path):
    from waypoint.workbench_api import create_workbench_app

    job_db = tmp_path / "jobs.sqlite3"
    evaluation_job_id = _completed_evaluation_job(
        job_db,
        entries=[{
            "key": "CUSTOMER_EMAIL",
            "canonical_key": "customer_email",
            "source_table": "RAW.CUSTOMERS",
            "disposition": "include",
            "approval_status": "auto_approved",
        }],
        feature_entries=[],
    )
    client = TestClient(create_workbench_app(
        job_db_path=job_db,
        promotion_root=tmp_path / "promotions",
    ))

    injected = client.post("/api/context-workbench/promotions", json={
        "evaluation_job_id": evaluation_job_id,
        "entries": [{"key": "INJECTED"}],
    })
    assert injected.status_code == 422

    pii = client.post(
        "/api/context-workbench/promotions",
        json={"evaluation_job_id": evaluation_job_id},
    )
    assert pii.status_code == 422
    assert "approved Include" in pii.json()["detail"]


def test_promotion_endpoint_preserves_source_table_for_each_duplicate_key_occurrence(tmp_path):
    from waypoint.workbench_api import create_workbench_app

    job_db = tmp_path / "jobs.sqlite3"
    evaluation_job_id = _completed_evaluation_job(
        job_db,
        entries=[
            {
                "key": "SHARED_METRIC",
                "canonical_key": "shared_metric_recent",
                "source_table": "ANALYTICS.RECENT",
                "disposition": "include",
                "approval_status": "auto_approved",
            },
            {
                "key": "SHARED_METRIC",
                "canonical_key": "shared_metric_history",
                "source_table": "ANALYTICS.HISTORY",
                "disposition": "include",
                "approval_status": "auto_approved",
            },
        ],
        feature_entries=[],
    )
    client = TestClient(create_workbench_app(
        job_db_path=job_db,
        promotion_root=tmp_path / "promotions",
    ))

    response = client.post(
        "/api/context-workbench/promotions",
        json={"evaluation_job_id": evaluation_job_id},
    )

    assert response.status_code == 200
    rows = response.json()["csv"].splitlines()
    assert "shared_metric_history,ANALYTICS.HISTORY," in rows
    assert "shared_metric_recent,ANALYTICS.RECENT," in rows


@pytest.mark.asyncio
async def test_both_sources_resolve_org_uuid_for_context_layer(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    seen = {}

    async def fake_n8n(self, identifier, webhook_url, token):
        return [
            {
                "QUERY_NAME": "part_1_org_snapshot",
                "VARIABLE_NAME": "org_snapshot",
                "VALUE": {"ORG_UUID": "cc962bf1-13bb-4eea-bf66-f3adc9e22192"},
            },
            {"QUERY_NAME": "usage", "VARIABLE_NAME": "JOBS_CREATED", "VALUE": 12},
        ]

    async def fake_context(self, identifier, base_url, api_key):
        seen["identifier"] = identifier
        return {"features": [{"name": "jobs"}]}

    async def fake_model(prompt, **kwargs):
        assert "cc962bf1-13bb-4eea-bf66-f3adc9e22192" not in prompt
        assert "\"VALUE\": 12" not in prompt
        return ('[{"key":"JOBS_CREATED","canonical_key":"jobs","value_category":"activity","related_features":["jobs","invented"],"usefulness_rank":5,"disposition":"include","aggregate_prompt":"Calculate cohort percentiles for comparable Pros.","confidence":0.9,"uncertainty_reason":null}]', {"output_tokens": 100})

    monkeypatch.setattr("waypoint.workbench_api.N8NContextClient.fetch", fake_n8n)
    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    result = await execute_run(WorkbenchRunRequest(
        identifier="889901",
        source_mode="both",
        workbench_mode="authoring",
        n8n_webhook_url="https://n8n.test",
        n8n_webhook_token="token",
        context_base_url="https://context.test",
        context_api_key="key",
        ai_api_key="ai-key",
        feature_catalog_entries=[{"feature": "jobs"}, {"feature": "voip"}],
        feature_catalog_version_id="features-v1",
    ))

    assert seen["identifier"] == "cc962bf1-13bb-4eea-bf66-f3adc9e22192"
    # Internal IDs remain available to the audit, but observed values stay out of authoring prompts.
    assert result["outputs"]["audit"]["total_variables"] == 2
    assert result["outputs"]["context_layer_coverage"]["total_catalog_features"] == 2
    assert result["outputs"]["context_layer_coverage"]["present_features"] == ["jobs"]
    assert result["outputs"]["authoring"]["draft"][0]["related_features"] == ["jobs"]
    assert any("invented" in warning for warning in result["warnings"])


def test_workbench_requires_a_five_or_six_digit_organization_id():
    from pydantic import ValidationError

    from waypoint.workbench_api import WorkbenchRunRequest

    assert WorkbenchRunRequest(identifier="31336").identifier == "31336"
    assert WorkbenchRunRequest(identifier="889901").identifier == "889901"
    for invalid in ("org-123", "1234", "1234567"):
        with pytest.raises(ValidationError, match="five- or six-digit organization ID"):
            WorkbenchRunRequest(identifier=invalid)


@pytest.mark.asyncio
async def test_compile_mode_never_calls_model(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fail_n8n(*args, **kwargs):
        raise AssertionError("compile must not recollect source data")

    async def fail_model(*args, **kwargs):
        raise AssertionError("compile must not call a model")

    monkeypatch.setattr("waypoint.workbench_api.N8NContextClient.fetch", fail_n8n)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fail_model)
    result = await execute_run(WorkbenchRunRequest(
        identifier="889901",
        source_mode="snowflake",
        workbench_mode="compile",
        n8n_webhook_url="https://n8n.test",
        n8n_webhook_token="token",
        catalog_version_id="context-v1",
        feature_catalog_version_id="features-v1",
        feature_catalog_entries=[{"feature": "jobs"}],
        catalog_override=[{
            "key": "JOBS_CREATED",
            "canonical_key": "jobs",
            "related_features": ["jobs"],
            "usefulness_rank": 5,
            "disposition": "include",
            "review_status": "reviewed",
        }],
    ))

    assert result["outputs"]["compiled"]["context"] == {
        "r": [{"k": "JOBS_CREATED", "c": "jobs", "u": 5, "f": ["jobs"]}],
        "pc": {"jobs": {}},
    }
    assert result["outputs"]["compiled"]["metrics"]["included_variables"] == 1


@pytest.mark.asyncio
async def test_evaluate_mode_compares_exact_waypoint_prompts_and_uses_fast_judge(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fail_n8n(*args, **kwargs):
        raise AssertionError("evaluation must resume from the scrubbed callback payload")

    calls = []

    async def fake_model(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if kwargs["stage"] == "evaluation_judge":
            return (
                json.dumps({
                    "winner": "curated",
                    "reason": "More grounded recommendations.",
                    "suggested_changes": ["Keep the strongest usage signals first."],
                }),
                {"model": kwargs["model"], "input_tokens": 30, "output_tokens": 20, "duration_ms": 4},
            )
        label = "curated" if '"jobs_created"' in prompt else "baseline"
        return (
            json.dumps([{
                "title": label,
                "mechanism": "jobs",
                "actions": ["help"],
                "pro_facing_concept": "help",
                "manager_rationale": "fit",
                "channel": "email",
                "risk": "low",
            }]),
            {"model": kwargs["model"], "input_tokens": 100, "output_tokens": 50, "duration_ms": 8},
        )

    monkeypatch.setattr("waypoint.workbench_api.N8NContextClient.fetch", fail_n8n)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    monkeypatch.setenv("MODEL_FAST", "claude-haiku-4-5")
    monkeypatch.delenv("WORKBENCH_MODEL", raising=False)

    result = await execute_run(WorkbenchRunRequest(
        identifier="889901",
        source_mode="both",
        workbench_mode="evaluate",
        ai_api_key="ai-key",
        model="claude-sonnet-5",
        feature_catalog_entries=[{
            "feature": "jobs",
            "Product Area": "Jobs",
            "Value Statement": "Create and manage jobs.",
        }],
        catalog_override=[{
            "key": "JOBS_CREATED",
            "canonical_key": "jobs_created",
            "value_category": "activity",
            "related_features": ["jobs"],
            "usefulness_rank": 5,
            "disposition": "include",
            "review_status": "reviewed",
        }],
    ), resume_state={
        "phase": "sources_ready",
        "scrubbed_sources": {
            "snowflake": {"rows": [
                {
                    "QUERY_NAME": "usage",
                    "VARIABLE_NAME": "JOBS_CREATED",
                    "VALUE": 12,
                    "METADATA": {"unused": "do-not-send"},
                },
                {"QUERY_NAME": "identity", "VARIABLE_NAME": "ORG_UUID", "VALUE": "secret"},
            ]},
            "context_layer": {"firmographics": {"segment": "1A", "industry": "HVAC"}},
        },
    })

    evaluation = result["outputs"]["evaluation"]
    assert len(calls) == 3
    assert all("You are running one round of an evolutionary search" in calls[index]["prompt"] for index in (0, 1))
    assert calls[0]["model"] == calls[1]["model"] == "claude-sonnet-5"
    assert calls[0]["prompt"] != calls[1]["prompt"]
    assert '"ORG_UUID": "secret"' in calls[0]["prompt"]
    assert "do-not-send" not in calls[0]["prompt"]
    assert evaluation["baseline"]["context"] == {
        "v": {
            "JOBS_CREATED": 12,
            "ORG_UUID": "secret",
            "context_layer.firmographics.industry": "HVAC",
            "context_layer.firmographics.segment": "1A",
        }
    }
    assert evaluation["baseline"]["candidates"][0]["title"] == "baseline"
    assert evaluation["curated"]["candidates"][0]["title"] == "curated"
    assert evaluation["curated"]["context"]["v"] == {"jobs_created": 12}
    assert evaluation["curated"]["context"]["pc"]["jobs"]["a"] == "Jobs"
    assert evaluation["judge"]["winner"] == "curated"
    assert calls[2]["model"] == "claude-haiku-4-5"
    assert "effort" not in calls[2]
    assert calls[2]["prompt"].count("secret") == 1
    assert '"context":' not in calls[2]["prompt"]


@pytest.mark.asyncio
async def test_all_source_failures_include_each_safe_reason(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fail_n8n(self, identifier, webhook_url, token):
        raise ValueError("Snowflake/n8n returned HTTP 503")

    monkeypatch.setattr("waypoint.workbench_api.N8NContextClient.fetch", fail_n8n)
    with pytest.raises(HTTPException) as caught:
        await execute_run(WorkbenchRunRequest(
            identifier="889901",
            source_mode="snowflake",
            workbench_mode="runtime",
            n8n_webhook_url="https://n8n.test",
            n8n_webhook_token="token",
            ai_api_key="test-key",
        ))

    assert caught.value.status_code == 502
    assert caught.value.detail == {
        "message": "All selected context sources failed",
        "sources": {"snowflake": "Snowflake/n8n returned HTTP 503"},
    }


@pytest.mark.asyncio
async def test_workbench_never_places_fixture_pii_in_prompt(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_model(prompt, *, api_key, model, stage):
        assert "Karla" not in prompt
        assert "LaPointe" not in prompt
        return (
            (
                '[{"title":"Try HCP AI","mechanism":"hcp_assist",'
                '"actions":["show the capability"],"pro_facing_concept":"help",'
                '"manager_rationale":"fit","channel":"email","risk":"low"}]'
            ),
            {"model": model, "input_tokens": 10, "output_tokens": 20, "cost_usd": 0.01},
        )

    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    async def fake_context(self, identifier, base_url, api_key):
        return {"primary_contact_name": "Karla Bravo LaPointe", "features": [{"name": "hcp_assist"}]}

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    result = await execute_run(WorkbenchRunRequest(
        identifier="889901", source_mode="context_layer", ai_api_key="test-key",
        context_base_url="https://context.example", context_api_key="test-context-key",
    ))
    pii_stage = next(stage for stage in result["stages"] if stage["name"] == "pii_gate")
    assert pii_stage["data"]["removed_count"] >= 1
    assert all("Karla" not in json.dumps(stage) for stage in result["stages"])


@pytest.mark.asyncio
async def test_workbench_uses_env_credentials_when_request_does_not_contain_keys(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic-test")

    async def fake_model(prompt, *, api_key, model, stage):
        assert api_key == "env-anthropic-test"
        return ('[]', {"model": model, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.01})

    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    async def fake_context(self, identifier, base_url, api_key):
        return {"industry": "HVAC"}

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setenv("CONTEXT_LAYER_BASE_URL", "https://context.test")
    monkeypatch.setenv("CONTEXT_LAYER_API_KEY", "context-test")
    result = await execute_run(WorkbenchRunRequest(identifier="889901", source_mode="context_layer"))
    assert result["outputs"]["baseline"]["candidates"] == []


@pytest.mark.asyncio
async def test_authoring_mode_returns_review_only_catalog_draft(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_model(prompt, *, api_key, model, stage, **kwargs):
        assert "VARIABLE INVENTORY" in prompt
        assert "canonical_key" in prompt
        assert "usefulness_rank" in prompt
        assert "aggregate_prompt" in prompt
        return (
            (
                '[{"key":"industry","canonical_key":"industry","value_category":"profile",'
                '"related_features":[],"usefulness_rank":2,"disposition":"deprioritize",'
                '"aggregate_prompt":null,"confidence":0.9,"uncertainty_reason":null,'
                '"label":"Industry"}]'
            ),
            {"model": model},
        )

    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    async def fake_context(self, identifier, base_url, api_key):
        return {"rows": [{"VARIABLE_NAME": "industry", "VALUE": "HVAC"}, {"VARIABLE_NAME": "email", "VALUE": "hidden@example.com"}]}

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    result = await execute_run(WorkbenchRunRequest(identifier="889901", source_mode="context_layer", context_base_url="https://context.test", context_api_key="context-key", workbench_mode="authoring", ai_api_key="test-key"))
    assert result["outputs"]["authoring"]["status"] == "draft_only"
    assert result["outputs"]["authoring"]["total_keys"] == 1
    assert result["outputs"]["authoring"]["completed_keys"] == 1
    assert result["outputs"]["authoring"]["remaining_keys"] == 0
    assert "label" not in result["outputs"]["authoring"]["draft"][0]
    assert any(stage["name"] == "authoring_parse" and stage["status"] == "succeeded" for stage in result["stages"])
    assert all("hidden@example.com" not in str(stage) for stage in result["stages"])


@pytest.mark.asyncio
async def test_authoring_selection_policy_survives_generation_retry_and_revision(
    monkeypatch,
):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_context(self, identifier, base_url, api_key):
        return {"firmographics": {"metric_t28": 1}}

    prompts: dict[str, str] = {}

    async def fake_model(prompt, *, stage, **kwargs):
        prompts[stage] = prompt
        if stage.startswith("authoring_generation_"):
            return "{", {"output_tokens": 10, "stop_reason": "end_turn"}
        if stage.startswith("authoring_repair_"):
            return "{", {"output_tokens": 10, "stop_reason": "end_turn"}
        confidence = 0.9 if stage.startswith("authoring_confidence_revision_") else 0.6
        return (
            json.dumps({
                "key": "context_layer.firmographics.metric_t28",
                "canonical_key": "metric_t28",
                "value_category": "activity",
                "related_features": [],
                "usefulness_rank": 4,
                "disposition": "include",
                "aggregate_prompt": "Calculate matched cohort percentiles.",
                "confidence": confidence,
                "uncertainty_reason": (
                    None if confidence >= 0.8 else "The metric family needs comparison."
                ),
            }),
            {"output_tokens": 10, "stop_reason": "end_turn"},
        )

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    await execute_run(WorkbenchRunRequest(
        identifier="889901",
        source_mode="context_layer",
        context_base_url="https://context.test",
        context_api_key="context-key",
        workbench_mode="authoring",
        ai_api_key="test-key",
        feature_catalog_entries=[],
    ))

    policy_prompts = [
        prompt
        for stage, prompt in prompts.items()
        if stage.startswith((
            "authoring_generation_",
            "authoring_single_",
            "authoring_confidence_revision_",
        ))
    ]
    assert len(policy_prompts) == 3
    for prompt in policy_prompts:
        assert "Prefer T28" in prompt
        assert "T1 and T7" in prompt
        assert "T90" in prompt
        assert "count and amount" in prompt


@pytest.mark.asyncio
async def test_authoring_requeues_variables_missing_required_metadata(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_context(self, identifier, base_url, api_key):
        return {"firmographics": {"alpha": 1, "beta": 2}}

    prompts = []

    async def fake_model(prompt, **kwargs):
        prompts.append(prompt)
        assert kwargs["max_tokens"] == 20_000
        assert kwargs["effort"] == "low"
        assert '"product_area":"SMS Text messaging"' in prompt
        assert "DO NOT REPEAT" not in prompt
        key = "context_layer.firmographics.beta"
        if len(prompts) == 1:
            return (
                json.dumps([
                    {
                        "key": "context_layer.firmographics.alpha",
                        "canonical_key": "alpha",
                        "value_category": "profile",
                        "related_features": [],
                        "usefulness_rank": 2,
                        "disposition": "deprioritize",
                        "aggregate_prompt": None,
                        "confidence": 0.9,
                        "uncertainty_reason": None,
                    },
                    {"key": key},
                ]),
                {"output_tokens": 100, "stop_reason": "end_turn"},
            )
        return (
            json.dumps([{
                "key": key,
                "canonical_key": "beta",
                "value_category": "profile",
                "related_features": [],
                "usefulness_rank": 2,
                "disposition": "deprioritize",
                "aggregate_prompt": None,
                "confidence": 0.9,
                "uncertainty_reason": None,
            }]),
            {"output_tokens": 50, "stop_reason": "end_turn"},
        )

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    result = await execute_run(WorkbenchRunRequest(
        identifier="889901",
        source_mode="context_layer",
        context_base_url="https://context.test",
        context_api_key="context-key",
        workbench_mode="authoring",
        ai_api_key="test-key",
        feature_catalog_entries=[{
            "feature": "sms_number",
            "Product Area": "SMS Text messaging",
            "Value Statement": "A short matching description.",
            "Attached": "DO NOT REPEAT",
        }],
    ))

    assert len(prompts) == 2
    assert "context_layer.firmographics.alpha" not in prompts[1]
    assert result["outputs"]["authoring"]["completed_keys"] == 2
    assert result["outputs"]["authoring"]["remaining_keys"] == 0
    assert result["outputs"]["authoring"]["token_budget"] == 150_000


@pytest.mark.asyncio
async def test_authoring_revises_low_confidence_once_and_checkpoints(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_context(self, identifier, base_url, api_key):
        return {"firmographics": {"alpha": 1}}

    prompts: list[str] = []
    checkpoints: list[dict[str, object]] = []

    async def fake_model(prompt, **kwargs):
        prompts.append(prompt)
        confidence = 0.88 if "REVISE LOW-CONFIDENCE" in prompt else 0.6
        return (
            json.dumps([{
                "key": "context_layer.firmographics.alpha",
                "canonical_key": "alpha",
                "value_category": "profile",
                "related_features": [],
                "usefulness_rank": 4,
                "disposition": "include",
                "aggregate_prompt": "Calculate cohort percentiles for comparable Pros.",
                "confidence": confidence,
                "uncertainty_reason": None if confidence >= 0.8 else "The variable name is broad.",
            }]),
            {"output_tokens": 50, "stop_reason": "end_turn"},
        )

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    result = await execute_run(
        WorkbenchRunRequest(
            identifier="889901",
            source_mode="context_layer",
            context_base_url="https://context.test",
            context_api_key="context-key",
            workbench_mode="authoring",
            ai_api_key="test-key",
        ),
        checkpoint=checkpoints.append,
    )

    entry = result["outputs"]["authoring"]["draft"][0]
    assert len(prompts) == 2
    assert entry["confidence"] == 0.88
    assert entry["approval_status"] == "auto_approved"
    assert result["outputs"]["authoring"]["revision_attempted"] == 1
    assert checkpoints[-1]["entries"][0]["confidence"] == 0.88
    assert checkpoints[-1]["revised_keys"] == ["context_layer.firmographics.alpha"]


@pytest.mark.asyncio
async def test_authoring_model_cannot_claim_human_approval(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_context(self, identifier, base_url, api_key):
        return {"firmographics": {"alpha": 1}}

    async def fake_model(prompt, **kwargs):
        return (
            json.dumps([{
                "key": "context_layer.firmographics.alpha",
                "canonical_key": "alpha",
                "value_category": "profile",
                "related_features": [],
                "usefulness_rank": 4,
                "disposition": "include",
                "aggregate_prompt": "Calculate cohort percentiles for comparable Pros.",
                "confidence": 0.1,
                "uncertainty_reason": "The variable is ambiguous.",
                "approval_status": "human_approved",
                "review_status": "reviewed",
            }]),
            {"output_tokens": 50, "stop_reason": "end_turn"},
        )

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    result = await execute_run(WorkbenchRunRequest(
        identifier="889901",
        source_mode="context_layer",
        context_base_url="https://context.test",
        context_api_key="context-key",
        workbench_mode="authoring",
        ai_api_key="test-key",
    ))

    entry = result["outputs"]["authoring"]["draft"][0]
    assert entry["approval_status"] == "auto_approved"
    assert entry["review_status"] == "draft"


@pytest.mark.asyncio
async def test_authoring_keeps_review_exception_when_revision_is_not_a_list(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_context(self, identifier, base_url, api_key):
        return {"firmographics": {"alpha": 1}}

    async def fake_model(prompt, **kwargs):
        if "REVISE LOW-CONFIDENCE" in prompt:
            return ("42", {"output_tokens": 1, "stop_reason": "end_turn"})
        return (
            json.dumps([{
                "key": "context_layer.firmographics.alpha",
                "canonical_key": "alpha",
                "value_category": "profile",
                "related_features": [],
                "usefulness_rank": 4,
                "disposition": "deprioritize",
                "aggregate_prompt": "Calculate cohort percentiles for comparable Pros.",
                "confidence": 0.6,
                "uncertainty_reason": "The variable name is broad.",
            }]),
            {"output_tokens": 50, "stop_reason": "end_turn"},
        )

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)

    result = await execute_run(WorkbenchRunRequest(
        identifier="889901",
        source_mode="context_layer",
        context_base_url="https://context.test",
        context_api_key="context-key",
        workbench_mode="authoring",
        ai_api_key="test-key",
    ))

    entry = result["outputs"]["authoring"]["draft"][0]
    assert entry["approval_status"] == "review_required"
    assert result["outputs"]["authoring"]["review_exception_count"] == 1


@pytest.mark.asyncio
async def test_authoring_resume_does_not_redraft_checkpointed_keys(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_context(self, identifier, base_url, api_key):
        return {"firmographics": {"done": 1, "todo": 2}}

    prompts: list[str] = []

    async def fake_model(prompt, **kwargs):
        prompts.append(prompt)
        assert "context_layer.firmographics.done" not in prompt
        return (
            json.dumps([{
                "key": "context_layer.firmographics.todo",
                "canonical_key": "todo",
                "value_category": "profile",
                "related_features": [],
                "usefulness_rank": 2,
                "disposition": "deprioritize",
                "aggregate_prompt": None,
                "confidence": 0.9,
                "uncertainty_reason": None,
            }]),
            {"output_tokens": 25, "stop_reason": "end_turn"},
        )

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    result = await execute_run(
        WorkbenchRunRequest(
            identifier="889901",
            source_mode="context_layer",
            context_base_url="https://context.test",
            context_api_key="context-key",
            workbench_mode="authoring",
            ai_api_key="test-key",
        ),
        resume_state={
            "entries": [{
                "key": "context_layer.firmographics.done",
                "canonical_key": "done",
                "value_category": "profile",
                "related_features": [],
                "usefulness_rank": 2,
                "disposition": "deprioritize",
                "aggregate_prompt": None,
                "confidence": 0.9,
                "uncertainty_reason": None,
                "approval_status": "auto_approved",
            }],
            "revised_keys": [],
        },
    )

    assert len(prompts) == 1
    assert result["outputs"]["authoring"]["completed_keys"] == 2


@pytest.mark.asyncio
async def test_authoring_resume_uses_checkpoint_inventory_and_cumulative_budget(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def source_must_not_run(*args, **kwargs):
        raise AssertionError("resume must not refetch the source")

    async def fake_model(prompt, **kwargs):
        return (
            json.dumps([{
                "key": "context_layer.firmographics.todo",
                "canonical_key": "todo",
                "value_category": "profile",
                "related_features": [],
                "usefulness_rank": 2,
                "disposition": "deprioritize",
                "aggregate_prompt": None,
                "confidence": 0.9,
                "uncertainty_reason": None,
            }]),
            {"output_tokens": 25, "stop_reason": "end_turn"},
        )

    inventory = [{
        "key": "context_layer.firmographics.todo",
        "source": "context_layer",
        "source_path": "firmographics.todo",
        "source_query": "",
        "observed_state": "present",
        "observed_type": "number",
        "basis": "observed",
    }]
    checkpoints: list[dict[str, object]] = []
    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", source_must_not_run)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    result = await execute_run(
        WorkbenchRunRequest(
            identifier="889901",
            source_mode="context_layer",
            workbench_mode="authoring",
            ai_api_key="test-key",
        ),
        resume_state={
            "inventory": inventory,
            "entries": [],
            "revised_keys": [],
            "output_tokens": 100,
            "attempts": {"context_layer.firmographics.todo": 1},
            "warnings": ["saved warning"],
        },
        checkpoint=checkpoints.append,
    )

    assert result["outputs"]["authoring"]["output_tokens"] == 125
    assert "saved warning" in result["warnings"]
    assert checkpoints[-1]["inventory"] == inventory
    assert checkpoints[-1]["attempts"]["context_layer.firmographics.todo"] == 2


@pytest.mark.asyncio
async def test_authoring_splits_and_retries_a_batch_that_hits_max_tokens(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_context(self, identifier, base_url, api_key):
        return {"firmographics": {key: index for index, key in enumerate("abcd")}}

    calls = []

    def complete(keys):
        return json.dumps([
            {
                "key": f"context_layer.firmographics.{key}",
                "canonical_key": key,
                "value_category": "profile",
                "related_features": [],
                "usefulness_rank": 2,
                "disposition": "deprioritize",
                "aggregate_prompt": None,
                "confidence": 0.9,
                "uncertainty_reason": None,
            }
            for key in keys
        ])

    async def fake_model(prompt, **kwargs):
        calls.append(kwargs)
        assert kwargs["effort"] == "low"
        if len(calls) == 1:
            return "", {"output_tokens": 20_000, "stop_reason": "max_tokens"}
        keys = "ab" if len(calls) == 2 else "cd"
        return complete(keys), {"output_tokens": 100, "stop_reason": "end_turn"}

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    checkpoints: list[dict[str, object]] = []
    result = await execute_run(
        WorkbenchRunRequest(
            identifier="889901",
            source_mode="context_layer",
            context_base_url="https://context.test",
            context_api_key="context-key",
            workbench_mode="authoring",
            ai_api_key="test-key",
            feature_catalog_entries=[],
        ),
        checkpoint=checkpoints.append,
    )

    assert len(calls) == 3
    assert result["outputs"]["authoring"]["completed_keys"] == 4
    assert result["outputs"]["authoring"]["remaining_keys"] == 0
    assert any("split" in warning for warning in result["warnings"])
    split_checkpoint = next(item for item in checkpoints if item["output_tokens"] == 20_000)
    assert len(split_checkpoint["pending_queue"]) == 4
    assert all(value == 1 for value in split_checkpoint["attempts"].values())


@pytest.mark.asyncio
async def test_authoring_requeues_single_variable_after_max_tokens(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_context(self, identifier, base_url, api_key):
        return {"firmographics": {"alpha": 1}}

    calls = 0

    async def fake_model(prompt, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "", {"output_tokens": 20_000, "stop_reason": "max_tokens"}
        return (json.dumps([{
            "key": "context_layer.firmographics.alpha",
            "canonical_key": "alpha",
            "value_category": "profile",
            "related_features": [],
            "usefulness_rank": 2,
            "disposition": "deprioritize",
            "aggregate_prompt": None,
            "confidence": 0.9,
            "uncertainty_reason": None,
        }]), {"output_tokens": 100, "stop_reason": "end_turn"})

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    result = await execute_run(WorkbenchRunRequest(
        identifier="889901",
        source_mode="context_layer",
        context_base_url="https://context.test",
        context_api_key="context-key",
        workbench_mode="authoring",
        ai_api_key="test-key",
        feature_catalog_entries=[],
    ))

    assert calls == 2
    assert result["outputs"]["authoring"]["completed_keys"] == 1
    assert result["outputs"]["authoring"]["remaining_keys"] == 0
