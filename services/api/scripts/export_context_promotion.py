"""Export one completed Workbench evaluation as a deployable promotion bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from waypoint.context_promotion import PromotionStore, build_promotion_bundle
from waypoint.workbench_api import prepare_promotion_entries
from waypoint.workbench_jobs import WorkbenchJobStore


def export(job_db: Path, job_id: str, output: Path) -> tuple[str, int, int]:
    job = WorkbenchJobStore(job_db).get(job_id)
    evaluation = (job.result or {}).get("outputs", {}).get("evaluation") if job else None
    if (
        job is None
        or job.status != "completed"
        or job.request.get("workbench_mode") != "evaluate"
        or not isinstance(evaluation, dict)
    ):
        raise ValueError("export requires a completed evaluation job")
    raw_entries = job.request.get("catalog_override")
    feature_entries = job.request.get("feature_catalog_entries")
    context_version = str(job.request.get("catalog_version_id") or "")
    feature_version = str(job.request.get("feature_catalog_version_id") or "")
    if not isinstance(raw_entries, list) or not isinstance(feature_entries, list):
        raise TypeError("evaluation job is missing catalog inputs")
    if not context_version or not feature_version:
        raise ValueError("evaluation job is missing catalog version IDs")

    feature_keys = {
        str(entry.get("feature"))
        for entry in feature_entries
        if isinstance(entry, dict) and entry.get("feature")
    }
    entries, counts, _warnings = prepare_promotion_entries(raw_entries, feature_keys)
    counts["approved"] = int(
        job.request.get("catalog_approved_count", counts["approved"])
    )
    counts["pii_removed"] = int(
        job.request.get("catalog_pii_removed_count", counts["pii_removed"])
    )

    fingerprint = json.dumps(
        {"evaluation_job_id": job.id, "policy": "pii-first-canonical-v1"},
        sort_keys=True,
        separators=(",", ":"),
    )
    promotion_id = f"promotion-{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"
    bundle = build_promotion_bundle(
        entries,
        feature_catalog_entries=feature_entries,
        promotion_id=promotion_id,
        context_catalog_version_id=context_version,
        feature_catalog_version_id=feature_version,
        created_at=job.updated_at,
    )
    if not bundle["rules"]:
        raise ValueError("promotion requires an approved non-PII Include variable")
    counts["retained"] = len(bundle["rules"])
    counts["duplicates_merged"] = max(
        0, counts["approved"] - counts["pii_removed"] - counts["retained"]
    )
    bundle["counts"] = counts
    PromotionStore(output).promote(bundle)
    return promotion_id, len(bundle["rules"]), len(bundle["feature_catalog"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--job-db", type=Path, default=Path(".workbench/jobs.sqlite3"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/context-promotions")
    )
    args = parser.parse_args()
    identifier, rules, features = export(args.job_db, args.job_id, args.output)
    print(f"exported {identifier}: {rules} rules, {features} features")
