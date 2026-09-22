import json
from pathlib import Path

WORKFLOW = (
    Path(__file__).parents[3]
    / "n8n"
    / "waypoint-variable-audit-context-async-v1.json"
)
STANDARD_WORKFLOW = (
    Path(__file__).parents[3]
    / "n8n"
    / "waypoint-variable-audit-context-v1.json"
)


def _webhook_path(path: Path) -> str:
    workflow = json.loads(path.read_text())
    return next(
        node["parameters"]["path"]
        for node in workflow["nodes"]
        if node["type"] == "n8n-nodes-base.webhook"
    )


def test_async_workbench_webhook_never_collides_with_standard_context() -> None:
    assert _webhook_path(WORKFLOW) != _webhook_path(STANDARD_WORKFLOW)


def test_async_workbench_workflow_validates_before_acknowledging() -> None:
    workflow = json.loads(WORKFLOW.read_text())
    nodes = {node["name"]: node for node in workflow["nodes"]}

    normalize = nodes["Normalize staging request"]["parameters"]["jsCode"]
    assert "x-waypoint-organization-id" in normalize
    assert "x-waypoint-request-id" in normalize
    assert "x-waypoint-promotion-id" in normalize
    assert "x-waypoint-callback-mode" in normalize
    assert workflow["connections"]["Variable audit request"]["main"][0][0]["node"] == (
        "Normalize staging request"
    )
    assert workflow["connections"]["Normalize staging request"]["main"][0][0]["node"] == (
        "Acknowledge request"
    )
    callback = nodes["Prepare Waypoint callback"]["parameters"]["jsCode"]
    assert "$('Normalize staging request')" in callback
    assert "callback_mode" in callback
    delivery_url = nodes["Deliver context to Waypoint"]["parameters"]["url"]
    assert "/api/context-workbench/source-callback" in delivery_url
    assert "/api/context/staging/callback" in delivery_url

    org_snapshot_query = nodes["Part 1 - Org snapshot and metadata"]["parameters"]["query"]
    assert "waypoint_contact_pro" in org_snapshot_query
    assert "founding_pro" in org_snapshot_query
    payment_query = nodes["Addendum - Payment churn scores"]["parameters"]["query"]
    assert "addendum_channel_recommendation" in payment_query
