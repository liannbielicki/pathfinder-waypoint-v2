import csv
import io
import json

from waypoint.context_promotion import (
    PACKAGED_PROMOTION_ROOT,
    PromotionStore,
    build_promotion_bundle,
    compile_promoted_context,
    promotion_csv,
)


def _entries() -> list[dict[str, object]]:
    return [
        {
            "key": "JOBS_CREATED_T28",
            "canonical_key": "jobs_created_t28",
            "source_table": "ANALYTICS.JOBS_DAILY",
            "related_features": ["jobs"],
            "disposition": "include",
            "approval_status": "auto_approved",
            "aggregate_prompt": "Calculate the T28 distribution for comparable cohorts.",
        },
        {
            "key": "CALLS_T7",
            "canonical_key": "calls_t7",
            "source_table": "",
            "related_features": ["voip"],
            "disposition": "include",
            "approval_status": "human_approved",
            "aggregate_prompt": None,
        },
        {
            "key": "NOISY_FIELD",
            "canonical_key": "noisy_field",
            "source_table": "ANALYTICS.NOISE",
            "disposition": "deprioritize",
            "approval_status": "review_required",
            "aggregate_prompt": "Do not export.",
        },
    ]


def _bundle() -> dict[str, object]:
    return build_promotion_bundle(
        _entries(),
        feature_catalog_entries=[
            {"feature": "jobs", "Product Area": "Jobs", "Value Statement": "Manage job workflows."},
            {"feature": "voip", "Product Area": "Phones", "Value Statement": "Manage customer calls."},
            {"feature": "unused", "Product Area": "Other", "Value Statement": "Must not reach runtime."},
            {"feature": "feature_only"},
        ],
        promotion_id="promotion-one",
        context_catalog_version_id="context-one",
        feature_catalog_version_id="features-one",
        created_at="2026-09-16T12:00:00Z",
    )


def test_promotion_keeps_only_approved_include_rules_and_exact_csv_columns():
    bundle = _bundle()

    assert [rule["canonical_key"] for rule in bundle["rules"]] == [
        "calls_t7",
        "jobs_created_t28",
    ]
    rows = list(csv.DictReader(io.StringIO(promotion_csv(bundle))))
    assert list(rows[0]) == [
        "canonical_key",
        "source_table",
        "cohort_aggregate_prompt",
    ]
    assert rows == [
        {
            "canonical_key": "calls_t7",
            "source_table": "UNKNOWN",
            "cohort_aggregate_prompt": "",
        },
        {
            "canonical_key": "jobs_created_t28",
            "source_table": "ANALYTICS.JOBS_DAILY",
            "cohort_aggregate_prompt": "Calculate the T28 distribution for comparable cohorts.",
        },
    ]


def test_runtime_compilation_keeps_promoted_values_and_referenced_feature_cards():
    context = compile_promoted_context(
        {
            "org_uuid": "secret-org-id",
            "jobs_created_t28": 12,
            "calls_t7": None,
            "unapproved": 99,
        },
        _bundle(),
    )

    assert context == {
        "v": {"jobs_created_t28": 12},
        "n": ["calls_t7"],
        "f": {
            "calls_t7": ["voip"],
            "jobs_created_t28": ["jobs"],
        },
        "pc": {
            "jobs": {"a": "Jobs", "v": "Manage job workflows."},
            "voip": {"a": "Phones", "v": "Manage customer calls."},
        },
    }
    assert "secret-org-id" not in str(context)


def test_runtime_compilation_unions_feature_mappings_for_one_canonical_value():
    bundle = _bundle()
    bundle["rules"].append({
        "source_key": "JOBS_ALIAS",
        "canonical_key": "jobs_created_t28",
        "related_features": ["voip"],
    })

    context = compile_promoted_context({"jobs_created_t28": 12}, bundle)

    assert context["f"] == {"jobs_created_t28": ["jobs", "voip"]}
    assert set(context["pc"]) == {"jobs", "voip"}


def test_promotion_store_keeps_immutable_versions_and_an_active_pointer(tmp_path):
    store = PromotionStore(tmp_path)
    bundle = _bundle()

    store.promote(bundle)

    assert store.read_active() == bundle
    assert store.read("promotion-one") == bundle


def test_promotion_preserves_distinct_non_pii_collisions_and_deduplicates_exact_rules():
    shared = {
        "key": "COUNT",
        "canonical_key": "workflow_count",
        "source_table": "ANALYTICS.WORKFLOWS",
        "disposition": "include",
        "approval_status": "auto_approved",
    }
    bundle = build_promotion_bundle(
        [
            {**shared, "related_features": ["jobs"], "aggregate_prompt": "Prompt A."},
            {**shared, "related_features": ["invoices"], "aggregate_prompt": "Prompt B."},
            {**shared, "canonical_key": "progress_count"},
        ],
        feature_catalog_entries=[],
        promotion_id="promotion-collisions",
        context_catalog_version_id="context-one",
        feature_catalog_version_id="features-one",
        created_at="2026-09-17T12:00:00Z",
    )

    assert [(rule["source_key"], rule["canonical_key"]) for rule in bundle["rules"]] == [
        ("COUNT", "progress_count"),
        ("COUNT", "workflow_count"),
    ]
    assert bundle["rules"][1]["related_features"] == ["invoices", "jobs"]
    assert bundle["rules"][1]["cohort_aggregate_prompt"] == "Prompt A. Prompt B."


def test_feature_catalog_removes_pii_values_without_removing_product_language():
    bundle = build_promotion_bundle(
        [],
        feature_catalog_entries=[{
            "feature": "email_marketing",
            "Product Area": "AI",
            "Value Statement": "Send campaigns; owner jane@example.com",
        }],
        promotion_id="promotion-features",
        context_catalog_version_id="context-one",
        feature_catalog_version_id="features-one",
        created_at="2026-09-17T12:00:00Z",
    )

    assert bundle["feature_catalog"] == [{
        "feature": "email_marketing",
        "Product Area": "AI",
    }]


def test_packaged_staging_promotion_is_the_approved_baseline() -> None:
    bundle = PromotionStore(PACKAGED_PROMOTION_ROOT).read_active()
    assert bundle is not None
    assert bundle["context_catalog_version_id"] == (
        "context-1789581748920-43479f5d-2f15-4bf0-89dd-fbf7e5bafe54"
    )
    assert bundle["feature_catalog_version_id"] == "features-649a8d942fd62ed9"
    assert bundle["counts"] == {
        "approved": 287,
        "pii_removed": 64,
        "duplicates_merged": 4,
        "retained": 219,
    }
    assert len(bundle["rules"]) == 219
    assert len(bundle["feature_catalog"]) == 181
    serialized = json.dumps(bundle).lower()
    assert "organization_id" not in serialized
    assert "@" not in serialized
