import csv
import io

from waypoint.context_promotion import (
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


def test_runtime_compilation_keeps_promoted_values_and_full_feature_catalog():
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
            "feature_only": {},
            "jobs": {"a": "Jobs", "v": "Manage job workflows."},
            "unused": {"a": "Other", "v": "Must not reach runtime."},
            "voip": {"a": "Phones", "v": "Manage customer calls."},
        },
    }
    assert "secret-org-id" not in str(context)


def test_promotion_store_keeps_immutable_versions_and_an_active_pointer(tmp_path):
    store = PromotionStore(tmp_path)
    bundle = _bundle()

    store.promote(bundle)

    assert store.read_active() == bundle
    assert store.read("promotion-one") == bundle
