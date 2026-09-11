import json

import httpx
import pytest

from waypoint.workbench import (
    ContextLayerClient,
    N8NContextClient,
    WorkbenchStage,
    build_product_index,
    redact,
    resolve_product_cards,
    scrub_pii,
    unwrap_source_payload,
)


def test_trace_stage_names_are_kept_in_execution_order():
    stages = [WorkbenchStage(name="input"), WorkbenchStage(name="raw_context")]
    assert [stage.name for stage in stages] == ["input", "raw_context"]


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
    with pytest.raises(ValueError, match="object"):
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
    assert "organization_name" not in clean
    assert "primary_contact_name" not in clean
    assert "best_email" not in clean
    assert "phone" not in clean
    assert "address" not in clean
    assert "city" not in clean
    assert "state" not in clean
    assert "zip" not in clean
    assert "salesforce_account_id" not in clean
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


def test_unwraps_n8n_context_envelope_before_pii_projection():
    payload = {"context": {"context_row": {"SEGMENT_ACTUAL": "1A"}, "event_context": {"calls_total_count": 2}}}
    assert unwrap_source_payload("snowflake", payload) == {"SEGMENT_ACTUAL": "1A", "calls_total_count": 2}


@pytest.mark.asyncio
async def test_workbench_never_places_fixture_pii_in_prompt(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_model(prompt, *, api_key, model, stage):
        assert "Karla" not in prompt
        assert "LaPointe" not in prompt
        return (
            '[{"title":"Try HCP AI","mechanism":"hcp_assist",'
            '"actions":["show the capability"],"pro_facing_concept":"help",'
            '"manager_rationale":"fit","channel":"email","risk":"low"}]',
            {"model": model, "input_tokens": 10, "output_tokens": 20, "cost_usd": 0.01},
        )

    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    async def fake_context(self, identifier, base_url, api_key):
        return {"primary_contact_name": "Karla Bravo LaPointe", "features": [{"name": "hcp_assist"}]}

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    result = await execute_run(WorkbenchRunRequest(identifier="org-123", source_mode="context_layer", ai_api_key="test-key"))
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
    result = await execute_run(WorkbenchRunRequest(identifier="org-123", source_mode="context_layer"))
    assert result["outputs"]["baseline"]["candidates"] == []


@pytest.mark.asyncio
async def test_authoring_mode_returns_review_only_catalog_draft(monkeypatch):
    from waypoint.workbench_api import WorkbenchRunRequest, execute_run

    async def fake_model(prompt, *, api_key, model, stage, **kwargs):
        assert "VARIABLE INVENTORY" in prompt
        assert "canonical_key" in prompt
        assert "usefulness_rank" in prompt
        assert "aggregate_prompt" in prompt
        return ('[{"key":"industry","label":"Industry","category":"profile","unit":null,"meaning":"Business type","why_it_matters":"Helps tailor context"}]', {"model": model})

    monkeypatch.setattr("waypoint.workbench_api.run_model", fake_model)
    async def fake_context(self, identifier, base_url, api_key):
        return {"rows": [{"VARIABLE_NAME": "industry", "VALUE": "HVAC"}, {"VARIABLE_NAME": "email", "VALUE": "hidden@example.com"}]}

    monkeypatch.setattr("waypoint.workbench_api.ContextLayerClient.fetch", fake_context)
    result = await execute_run(WorkbenchRunRequest(identifier="org-123", source_mode="context_layer", context_base_url="https://context.test", context_api_key="context-key", workbench_mode="authoring", ai_api_key="test-key"))
    assert result["outputs"]["authoring"]["status"] == "draft_only"
    assert result["outputs"]["authoring"]["total_keys"] == 2
    assert result["outputs"]["authoring"]["completed_keys"] == 1
    assert result["outputs"]["authoring"]["remaining_keys"] == 1
    assert "label" not in result["outputs"]["authoring"]["draft"][0]
    assert any(stage["name"] == "authoring_parse" and stage["status"] == "succeeded" for stage in result["stages"])
    assert all("hidden@example.com" not in str(stage) for stage in result["stages"])
