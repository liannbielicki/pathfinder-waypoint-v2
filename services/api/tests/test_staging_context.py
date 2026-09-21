import asyncio
import logging
from typing import Any

import pytest

from waypoint.n8n import ContextUnavailable
from waypoint.staging_context import WorkbenchStagingContextClient, compile_staging_brief


class FakeSnowflake:
    def __init__(self, payloads: dict[str, Any] | None = None, error: Exception | None = None):
        self.payloads = payloads or {}
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    async def fetch(self, organization_id: str, url: str, token: str) -> Any:
        self.calls.append((organization_id, url, token))
        if self.error:
            raise self.error
        return self.payloads[organization_id]


class FakeContextLayer:
    def __init__(self, payloads: dict[str, Any] | None = None, error: Exception | None = None):
        self.payloads = payloads or {}
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    async def fetch(self, organization_id: str, url: str, api_key: str) -> Any:
        self.calls.append((organization_id, url, api_key))
        if self.error:
            raise self.error
        return self.payloads[organization_id]


def promotion(*rules: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "promotion-approved",
        "rules": list(rules),
        "feature_catalog": [
            {
                "feature": "jobs",
                "Product Area": "Jobs",
                "Value Statement": "Manage job workflows.",
            },
            {
                "feature": "unused",
                "Product Area": "Other",
                "Value Statement": "Must not reach runtime.",
            },
        ],
    }


def context_payload(
    segment: Any = "1A", industry: Any = "HVAC"
) -> dict[str, Any]:
    return {"firmographics": {"segment": segment, "industry": industry}}


def make_client(
    *,
    bundle: dict[str, Any] | None,
    snowflake: Any,
    context_layer: Any,
    max_concurrent: int = 3,
) -> WorkbenchStagingContextClient:
    async def load_promotion() -> dict[str, Any] | None:
        return bundle

    return WorkbenchStagingContextClient(
        workbench_url="https://n8n.example/workbench",
        n8n_token="n8n-token",
        context_layer_url="https://context.example",
        context_layer_key="context-token",
        promotion_loader=load_promotion,
        max_concurrent=max_concurrent,
        snowflake=snowflake,
        context_layer=context_layer,
    )


def test_staging_applies_the_configured_n8n_timeout() -> None:
    async def load_promotion() -> dict[str, Any] | None:
        return promotion()

    client = WorkbenchStagingContextClient(
        workbench_url="https://n8n.example/workbench",
        n8n_token="n8n-token",
        context_layer_url="https://context.example",
        context_layer_key="context-token",
        promotion_loader=load_promotion,
        n8n_timeout=900,
    )

    assert client.snowflake._timeout_seconds == 900


def test_staging_filters_features_unavailable_on_the_current_core_plan() -> None:
    bundle = {
        "id": "promotion-approved",
        "rules": [{
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": ["checklists"],
        }],
        "feature_catalog": [{
            "feature": "checklists",
            "Product Area": "Operations",
            "Plans": "Core SaaS Essentials, Core SaaS MAX, Core SaaS MAX+",
            "Value Statement": "Create reusable job checklists.",
        }],
    }
    snowflake = [
        {
            "VARIABLE_NAME": "org_snapshot",
            "VALUE": {"CORE_SAAS_PLAN_LEVEL": "Basic"},
        },
        {"VARIABLE_NAME": "SAFE", "VALUE": 1},
    ]

    brief = compile_staging_brief("889901", snowflake, context_payload(), bundle)

    assert brief.curated_context == {
        "v": {
            "core_saas_plan": "Core SaaS Basic",
            "industry": "HVAC",
            "safe": 1,
            "segment": "1A",
        }
    }


def test_staging_toggle_keeps_features_not_in_the_current_plan() -> None:
    bundle = {
        "id": "promotion-approved",
        "rules": [{
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": ["checklists"],
        }],
        "feature_catalog": [{
            "feature": "checklists",
            "Product Area": "Operations",
            "Plans": "Core SaaS Essentials, Core SaaS MAX, Core SaaS MAX+",
            "Value Statement": "Create reusable job checklists.",
        }],
    }
    snowflake = [
        {
            "VARIABLE_NAME": "org_snapshot",
            "VALUE": {"CORE_SAAS_PLAN_LEVEL": "Basic"},
        },
        {"VARIABLE_NAME": "SAFE", "VALUE": 1},
    ]

    brief = compile_staging_brief(
        "889901",
        snowflake,
        context_payload(),
        bundle,
        include_features_not_in_current_plan=True,
    )

    assert brief.curated_context == {
        "v": {
            "core_saas_plan": "Core SaaS Basic",
            "industry": "HVAC",
            "safe": 1,
            "segment": "1A",
        },
        "f": {"safe": ["checklists"]},
        "pc": {
            "checklists": {
                "a": "Operations",
                "e": "not_in_current_plan",
                "p": [
                    "Core SaaS Essentials",
                    "Core SaaS MAX",
                    "Core SaaS MAX+",
                ],
                "v": "Create reusable job checklists.",
            }
        },
    }


def test_staging_keeps_non_core_features_without_guessing_plan_availability() -> None:
    bundle = {
        "id": "promotion-approved",
        "rules": [{
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": ["hcp_assist"],
        }],
        "feature_catalog": [{
            "feature": "hcp_assist",
            "Plans": "Non Core SaaS",
            "Value Statement": "AI assistance for office workflows.",
        }],
    }
    snowflake = [
        {
            "VARIABLE_NAME": "org_snapshot",
            "VALUE": {"CORE_SAAS_PLAN_LEVEL": "Basic"},
        },
        {"VARIABLE_NAME": "SAFE", "VALUE": 1},
    ]

    brief = compile_staging_brief("889901", snowflake, context_payload(), bundle)

    assert brief.curated_context["f"] == {"safe": ["hcp_assist"]}
    assert brief.curated_context["pc"] == {
        "hcp_assist": {
            "e": "unknown",
            "p": ["Non Core SaaS"],
            "v": "AI assistance for office workflows.",
        }
    }


def test_staging_keeps_mixed_plan_metadata_as_unknown() -> None:
    bundle = {
        "id": "promotion-approved",
        "rules": [{
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": ["campaigns"],
        }],
        "feature_catalog": [{
            "feature": "campaigns",
            "Plans": "Core SaaS Basic, Campaigns add-on",
            "Value Statement": "Run customer campaigns.",
        }],
    }
    snowflake = [
        {
            "VARIABLE_NAME": "org_snapshot",
            "VALUE": {"CORE_SAAS_PLAN_LEVEL": "Essentials"},
        },
        {"VARIABLE_NAME": "SAFE", "VALUE": 1},
    ]

    brief = compile_staging_brief("889901", snowflake, context_payload(), bundle)

    assert brief.curated_context["f"] == {"safe": ["campaigns"]}
    assert brief.curated_context["pc"]["campaigns"] == {
        "e": "unknown",
        "p": ["Core SaaS Basic", "Campaigns add-on"],
        "v": "Run customer campaigns.",
    }


def test_staging_recognizes_universal_plan_metadata_with_notes() -> None:
    bundle = {
        "id": "promotion-approved",
        "rules": [{
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": ["business_setup"],
        }],
        "feature_catalog": [{
            "feature": "business_setup",
            "Plans": "Universal (all orgs)\n\nAvailable without a paid Core SaaS plan.",
        }],
    }
    snowflake = [
        {
            "VARIABLE_NAME": "org_snapshot",
            "VALUE": {"CORE_SAAS_PLAN_LEVEL": "Basic"},
        },
        {"VARIABLE_NAME": "SAFE", "VALUE": 1},
    ]

    brief = compile_staging_brief("889901", snowflake, context_payload(), bundle)

    assert brief.curated_context["pc"]["business_setup"] == {
        "e": "available",
        "p": ["Universal (all orgs)"],
    }


@pytest.mark.parametrize(
    ("plan_rows", "expected_plan"),
    [
        ([], None),
        ([{"VARIABLE_NAME": "org_snapshot", "VALUE": {
            "CORE_SAAS_PLAN_LEVEL": "Basic",
        }}, {
            "VARIABLE_NAME": "CORE_SAAS_PLAN_LEVEL",
            "VALUE": "Essentials",
        }], None),
        ([{"VARIABLE_NAME": "org_snapshot", "VALUE": {
            "CORE_SAAS_PLAN_LEVEL": "MAX++",
        }}], "Core SaaS MAX++ [LEGACY]"),
        ([{"VARIABLE_NAME": "org_snapshot", "VALUE": {
            "CORE_SAAS_PLAN_LEVEL": "Future",
        }}], "UNMAPPED"),
    ],
)
def test_staging_marks_uncertain_current_plan_coverage_unknown(
    plan_rows: list[dict[str, Any]], expected_plan: str | None,
) -> None:
    bundle = {
        "id": "promotion-approved",
        "rules": [{
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": ["checklists"],
        }],
        "feature_catalog": [{
            "feature": "checklists",
            "Plans": "Core SaaS Basic",
        }],
    }

    brief = compile_staging_brief(
        "889901",
        [*plan_rows, {"VARIABLE_NAME": "SAFE", "VALUE": 1}],
        context_payload(),
        bundle,
    )

    assert brief.curated_context["f"] == {"safe": ["checklists"]}
    assert brief.curated_context["pc"]["checklists"] == {
        "e": "unknown",
        "p": ["Core SaaS Basic"],
    }
    if expected_plan is None:
        assert "core_saas_plan" not in brief.curated_context["v"]
    else:
        assert brief.curated_context["v"]["core_saas_plan"] == expected_plan


@pytest.mark.parametrize(
    "segment",
    [
        "1A", "1B", "1C", "1D",
        "2A", "2B", "2C", "2D",
        "3A", "3B", "3C", "3D",
        "4A", "4B", "4C", "4D",
    ],
)
async def test_staging_always_curates_authoritative_firmographics(
    segment: str,
) -> None:
    brief = (
        await make_client(
            bundle=promotion({
                "source_key": "SAFE",
                "source_table": "UNKNOWN",
                "canonical_key": "safe",
                "related_features": [],
            }),
            snowflake=FakeSnowflake({
                "889901": [{"VARIABLE_NAME": "SAFE", "VALUE": 1}]
            }),
            context_layer=FakeContextLayer({
                "889901": context_payload(
                    segment=f" {segment.lower()} ",
                    industry=" Heating and Air Conditioning ",
                )
            }),
        ).fetch(["889901"])
    ).organizations[0]

    assert brief.segment == segment
    assert brief.curated_context == {
        "v": {
            "industry": "Heating and Air Conditioning",
            "safe": 1,
            "segment": segment,
        }
    }


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({}, "segment"),
        ({"firmographics": {"industry": "HVAC"}}, "segment"),
        (context_payload(segment="   "), "segment"),
        (context_payload(segment="5A"), "segment"),
        (context_payload(segment=None), "segment"),
        ({"firmographics": {"segment": "1A"}}, "industry"),
        (context_payload(industry="   "), "industry"),
        (context_payload(industry=None), "industry"),
    ],
)
async def test_staging_rejects_missing_or_invalid_authoritative_firmographics(
    payload: dict[str, Any], field: str
) -> None:
    with pytest.raises(ContextUnavailable, match=rf"firmographics\.{field}"):
        await make_client(
            bundle=promotion(),
            snowflake=FakeSnowflake({"889901": []}),
            context_layer=FakeContextLayer({"889901": payload}),
        ).fetch(["889901"])


async def test_staging_context_layer_firmographics_override_catalog_collisions() -> None:
    snowflake = FakeSnowflake({
        "889901": [
            {"VARIABLE_NAME": "SEGMENT_ACTUAL", "VALUE": "4D"},
            {"VARIABLE_NAME": "INDUSTRY_SEGMENT", "VALUE": "Plumbing"},
            {"VARIABLE_NAME": "SAFE", "VALUE": 1},
        ]
    })
    bundle = promotion(
        {
            "source_key": "SEGMENT_ACTUAL",
            "source_table": "UNKNOWN",
            "canonical_key": "segment",
            "related_features": [],
        },
        {
            "source_key": "INDUSTRY_SEGMENT",
            "source_table": "UNKNOWN",
            "canonical_key": "industry",
            "related_features": [],
        },
        {
            "source_key": "context_layer.firmographics.segment",
            "source_table": "UNKNOWN",
            "canonical_key": "segment_alias",
            "related_features": [],
        },
        {
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": [],
        },
    )

    brief = (
        await make_client(
            bundle=bundle,
            snowflake=snowflake,
            context_layer=FakeContextLayer({
                "889901": context_payload(segment="2B", industry="HVAC")
            }),
        ).fetch(["889901"])
    ).organizations[0]

    assert brief.segment == "2B"
    assert brief.curated_context == {
        "v": {"industry": "HVAC", "safe": 1, "segment": "2B"}
    }
    assert "4D" not in str(brief.curated_context)
    assert "Plumbing" not in str(brief.curated_context)


async def test_staging_fetches_both_sources_then_compiles_only_approved_values() -> None:
    snowflake = FakeSnowflake({
        "889901": [
            {
                "VARIABLE_NAME": "JOBS_CREATED_T28",
                "VALUE": 12,
                "SOURCE_TABLE": "ANALYTICS.JOBS",
            },
            {"VARIABLE_NAME": "UNAPPROVED", "VALUE": "drop-me"},
        ]
    })
    context_layer = FakeContextLayer({
        "889901": {
            "firmographics": {
                "segment": "1A",
                "industry": "HVAC",
                "email": "drop@example.com",
            }
        }
    })
    bundle = promotion(
        {
            "source_key": "JOBS_CREATED_T28",
            "source_table": "ANALYTICS.JOBS",
            "canonical_key": "jobs_created_t28",
            "related_features": ["jobs"],
        },
        {
            "source_key": "MISSING_JOB_ALIAS",
            "source_table": "UNKNOWN",
            "canonical_key": "jobs_created_t28",
            "related_features": ["unused"],
        },
    )

    batch = await make_client(
        bundle=bundle, snowflake=snowflake, context_layer=context_layer
    ).fetch(["889901"])

    assert batch.audience_query_version == "workbench:promotion-approved"
    assert snowflake.calls == [("889901", "https://n8n.example/workbench", "n8n-token")]
    assert context_layer.calls == [
        ("889901", "https://context.example", "context-token")
    ]
    brief = batch.organizations[0]
    assert brief.pro_id == "889901"
    assert brief.segment == "1A"
    assert brief.curated_context == {
        "v": {"industry": "HVAC", "jobs_created_t28": 12, "segment": "1A"},
        "f": {"jobs_created_t28": ["jobs"]},
        "pc": {"jobs": {
            "a": "Jobs",
            "e": "unknown",
            "v": "Manage job workflows.",
        }},
    }
    assert "UNAPPROVED" not in str(brief.curated_context)
    assert "drop@example.com" not in str(brief.curated_context)


async def test_staging_matches_exact_lineage_and_preserves_nulls() -> None:
    snowflake = FakeSnowflake({
        "889901": [
            {"VARIABLE_NAME": "COUNT", "VALUE": 2, "SOURCE_TABLE": "RECENT"},
            {"VARIABLE_NAME": "COUNT", "VALUE": 99, "SOURCE_TABLE": "HISTORY"},
            {"VARIABLE_NAME": "OPTIONAL", "VALUE": None},
        ]
    })
    context_layer = FakeContextLayer({"889901": context_payload()})
    bundle = promotion(
        {
            "source_key": "COUNT",
            "source_table": "RECENT",
            "canonical_key": "recent_count",
            "related_features": [],
        },
        {
            "source_key": "OPTIONAL",
            "source_table": "UNKNOWN",
            "canonical_key": "optional_signal",
            "related_features": [],
        },
        {
            "source_key": "MISSING",
            "source_table": "UNKNOWN",
            "canonical_key": "missing_signal",
            "related_features": [],
        },
    )

    brief = (await make_client(
        bundle=bundle, snowflake=snowflake, context_layer=context_layer
    ).fetch(["889901"])).organizations[0]

    assert brief.curated_context == {
        "v": {"industry": "HVAC", "recent_count": 2, "segment": "1A"},
        "n": ["optional_signal"],
    }


async def test_staging_omits_ambiguous_and_conflicting_values() -> None:
    snowflake = FakeSnowflake({
        "889901": [
            {"VARIABLE_NAME": "COUNT", "VALUE": 2, "SOURCE_TABLE": "RECENT"},
            {"VARIABLE_NAME": "COUNT", "VALUE": 99, "SOURCE_TABLE": "HISTORY"},
            {"VARIABLE_NAME": "LEFT", "VALUE": "a"},
            {"VARIABLE_NAME": "RIGHT", "VALUE": "b"},
            {"VARIABLE_NAME": "SAFE", "VALUE": 1},
        ]
    })
    context_layer = FakeContextLayer({"889901": context_payload()})
    bundle = promotion(
        {
            "source_key": "COUNT",
            "source_table": "UNKNOWN",
            "canonical_key": "ambiguous_count",
            "related_features": [],
        },
        {
            "source_key": "LEFT",
            "source_table": "UNKNOWN",
            "canonical_key": "collision",
            "related_features": [],
        },
        {
            "source_key": "RIGHT",
            "source_table": "UNKNOWN",
            "canonical_key": "collision",
            "related_features": [],
        },
        {
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": [],
        },
    )

    brief = (await make_client(
        bundle=bundle, snowflake=snowflake, context_layer=context_layer
    ).fetch(["889901"])).organizations[0]

    assert brief.curated_context == {
        "v": {"industry": "HVAC", "safe": 1, "segment": "1A"}
    }


async def test_staging_requires_numeric_ids_an_active_promotion_and_a_match() -> None:
    source = FakeSnowflake({"889901": [{"VARIABLE_NAME": "OTHER", "VALUE": 1}]})
    context = FakeContextLayer({"889901": context_payload()})

    with pytest.raises(ContextUnavailable, match="numeric organization ID"):
        await make_client(bundle=promotion(), snowflake=source, context_layer=context).fetch(
            ["pro_abc"]
        )
    with pytest.raises(ContextUnavailable, match="promotion"):
        await make_client(bundle=None, snowflake=source, context_layer=context).fetch(
            ["889901"]
        )
    missing_id = promotion({
        "source_key": "OTHER",
        "source_table": "UNKNOWN",
        "canonical_key": "other",
        "related_features": [],
    })
    missing_id.pop("id")
    with pytest.raises(ContextUnavailable, match="promotion artifact has no id"):
        await make_client(
            bundle=missing_id, snowflake=source, context_layer=context
        ).fetch(["889901"])
    with pytest.raises(ContextUnavailable, match="zero approved variables"):
        await make_client(
            bundle=promotion({
                "source_key": "MISSING",
                "source_table": "UNKNOWN",
                "canonical_key": "missing",
                "related_features": [],
            }),
            snowflake=source,
            context_layer=context,
        ).fetch(["889901"])


@pytest.mark.parametrize("failed_source", ["snowflake", "context_layer"])
async def test_staging_source_failures_do_not_expose_values(failed_source: str) -> None:
    secret = "sensitive-source-value"
    snowflake = FakeSnowflake(
        {"889901": [{"VARIABLE_NAME": "SAFE", "VALUE": 1}]},
        ValueError(secret) if failed_source == "snowflake" else None,
    )
    context_layer = FakeContextLayer(
        {"889901": context_payload()},
        ValueError(secret) if failed_source == "context_layer" else None,
    )

    with pytest.raises(ContextUnavailable) as raised:
        await make_client(
            bundle=promotion({
                "source_key": "SAFE",
                "source_table": "UNKNOWN",
                "canonical_key": "safe",
                "related_features": [],
            }),
            snowflake=snowflake,
            context_layer=context_layer,
        ).fetch(["889901"])

    assert failed_source in str(raised.value)
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (ValueError("Snowflake/n8n returned HTTP 504"), "HTTP 504"),
        (TimeoutError("Snowflake/n8n timed out after 900 seconds"), "900 seconds"),
    ],
)
async def test_staging_preserves_safe_snowflake_failure_details(
    error: Exception, detail: str
) -> None:
    snowflake = FakeSnowflake(error=error)
    context_layer = FakeContextLayer({"889901": context_payload()})

    with pytest.raises(ContextUnavailable, match=detail):
        await make_client(
            bundle=promotion({
                "source_key": "SAFE",
                "source_table": "UNKNOWN",
                "canonical_key": "safe",
                "related_features": [],
            }),
            snowflake=snowflake,
            context_layer=context_layer,
        ).fetch(["889901"])


@pytest.mark.parametrize(
    ("snowflake_payload", "context_payload", "source"),
    [({}, {}, "snowflake"), ([], [], "context_layer")],
)
async def test_staging_rejects_malformed_source_contracts(
    snowflake_payload: Any, context_payload: Any, source: str
) -> None:
    snowflake = FakeSnowflake({"889901": snowflake_payload})
    context_layer = FakeContextLayer({"889901": context_payload})

    with pytest.raises(ContextUnavailable, match=source):
        await make_client(
            bundle=promotion({
                "source_key": "SAFE",
                "source_table": "UNKNOWN",
                "canonical_key": "safe",
                "related_features": [],
            }),
            snowflake=snowflake,
            context_layer=context_layer,
        ).fetch(["889901"])


async def test_staging_logs_only_promotion_and_counts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="waypoint.staging_context")
    raw_secret = "do-not-log-this"
    snowflake = FakeSnowflake({
        "889901": [
            {"VARIABLE_NAME": "SAFE", "VALUE": 1},
            {"VARIABLE_NAME": "EXTRA", "VALUE": raw_secret},
        ]
    })
    context_layer = FakeContextLayer({"889901": context_payload()})

    await make_client(
        bundle=promotion({
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": [],
        }),
        snowflake=snowflake,
        context_layer=context_layer,
    ).fetch(["889901"])

    assert "promotion-approved" in caplog.text
    assert "received=4" in caplog.text
    assert "matched=1" in caplog.text
    assert raw_secret not in caplog.text


async def test_staging_fetches_the_two_sources_concurrently() -> None:
    both_started = asyncio.Event()
    started = 0

    class CoordinatedSource:
        async def _wait(self) -> None:
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)

    class Snowflake(CoordinatedSource):
        async def fetch(self, organization_id: str, url: str, token: str) -> Any:
            await self._wait()
            return [{"VARIABLE_NAME": "SAFE", "VALUE": 1}]

    class ContextLayer(CoordinatedSource):
        async def fetch(self, organization_id: str, url: str, token: str) -> Any:
            await self._wait()
            return context_payload()

    await make_client(
        bundle=promotion({
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": [],
        }),
        snowflake=Snowflake(),
        context_layer=ContextLayer(),
    ).fetch(["889901"])


async def test_staging_bounds_multi_organization_concurrency() -> None:
    active = 0
    maximum = 0

    class SlowSnowflake:
        async def fetch(self, organization_id: str, url: str, token: str) -> Any:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return [{"VARIABLE_NAME": "SAFE", "VALUE": 1}]

    class EmptyContext:
        async def fetch(self, organization_id: str, url: str, token: str) -> Any:
            return context_payload()

    client = make_client(
        bundle=promotion({
            "source_key": "SAFE",
            "source_table": "UNKNOWN",
            "canonical_key": "safe",
            "related_features": [],
        }),
        snowflake=SlowSnowflake(),
        context_layer=EmptyContext(),
        max_concurrent=2,
    )

    batch = await client.fetch(["1", "2", "3", "4"])

    assert len(batch.organizations) == 4
    assert maximum == 2
