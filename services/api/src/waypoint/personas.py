"""Deterministic, privacy-safe persona panels with labeled fallbacks."""

from typing import Any, Literal

from pydantic import BaseModel, Field

# The only features matching may see. Everything else on the Pro is ignored.
PERMITTED_MATCH_FEATURES = (
    "segment", "plan", "tenure_bucket", "org_size_bucket", "trade_bucket",
    "open_ar_band", "lifecycle_stage", "features_active_count",
    "booking_attached", "premium_reviews_attached", "sales_proposal_attached",
    "service_agreements_attached", "hcp_assist_attached", "quickbooks_attached",
    "voip_attached", "card_on_file_attached", "time_tracking_attached",
    "flat_rate_pricing_attached",
)

FIT_THRESHOLD = 0.5
COVERAGE_THRESHOLD = 0.6

# Segment and lifecycle anchor the persona; plan and known product usage carry
# more signal than the remaining business-state fields. Coverage is tracked
# separately so one matching field never looks like a complete match.
MATCH_WEIGHTS = {
    "segment": 2.0,
    "plan": 1.5,
    "lifecycle_stage": 2.0,
    **{
        key: 1.5
        for key in PERMITTED_MATCH_FEATURES
        if key.endswith("_attached")
    },
}


class InsufficientPanelFit(Exception):
    def __init__(self, size: int, available: int) -> None:
        super().__init__(
            f"panel of {size} needs more qualifying matches; only {available} available"
        )
        self.size = size
        self.available = available


class Persona(BaseModel):
    persona_id: str
    family: str
    label: str
    features: dict[str, Any]
    snapshot_version: str


class ProMatchInput(BaseModel):
    pro_id: str
    features: dict[str, Any]


class PanelItem(BaseModel):
    persona_id: str
    label: str
    family: str
    role: Literal[
        "closest", "counterweight", "backfill", "segment_fallback", "broad_fallback"
    ]
    fit_score: float
    coverage_score: float = 0.0
    rationale: str
    reused: bool = False


class PanelSelection(BaseModel):
    items: list[PanelItem]
    fit_threshold: float
    snapshot_version: str
    match_features: dict[str, Any]
    # Defaults keep old stored evidence readable while newer runs explain
    # low-coverage, fallback, short, and reused panels explicitly.
    requested_size: int = 0
    degraded: bool = False
    match_quality: Literal["strong", "segment_fallback", "broad_fallback"] = "strong"
    coverage_score: float = 0.0
    fallback_reason: str | None = None
    overlap_persona_ids: list[str] = Field(default_factory=list)


def match_features(pro: ProMatchInput) -> dict[str, Any]:
    return {k: pro.features[k] for k in PERMITTED_MATCH_FEATURES if k in pro.features}


def _fit(pro_features: dict[str, Any], persona: Persona) -> tuple[float, float, str]:
    """Return weighted exact-match fit and how much Pro state was comparable."""
    keys = [k for k in PERMITTED_MATCH_FEATURES if k in pro_features and k in persona.features]
    if not keys:
        return 0.0, 0.0, "no shared permitted features"
    matched = [k for k in keys if pro_features[k] == persona.features[k]]
    shared_weight = sum(MATCH_WEIGHTS.get(key, 1.0) for key in keys)
    pro_weight = sum(
        MATCH_WEIGHTS.get(key, 1.0) for key in PERMITTED_MATCH_FEATURES if key in pro_features
    )
    fit = sum(MATCH_WEIGHTS.get(key, 1.0) for key in matched) / shared_weight
    coverage = shared_weight / pro_weight if pro_weight else 0.0
    return fit, coverage, f"matches on {', '.join(matched) or 'nothing'}"


def select_panel(
    pro: ProMatchInput,
    personas: list[Persona],
    size: Literal[3, 5],
    *,
    exclude_ids: set[str] | frozenset[str] = frozenset(),
) -> PanelSelection:
    """Seat strong matches, then same-segment and broad fallbacks.

    Excluded personas are preferred last rather than forbidden: final validation
    stays held out when the pool permits and remains runnable when it does not.
    """
    if not personas:
        raise InsufficientPanelFit(size=size, available=0)

    closest_count = 2 if size == 3 else 3
    features = match_features(pro)
    scored: list[tuple[str, PanelItem]] = []
    for persona in personas:
        fit, coverage, rationale = _fit(features, persona)
        if fit >= FIT_THRESHOLD and coverage >= COVERAGE_THRESHOLD:
            tier = "strong"
        elif features.get("segment") == persona.features.get("segment"):
            tier = "segment_fallback"
        else:
            tier = "broad_fallback"
        scored.append((tier, PanelItem(
            persona_id=persona.persona_id,
            label=persona.label,
            family=persona.family,
            role="closest",
            fit_score=fit,
            coverage_score=coverage,
            rationale=rationale,
        )))

    items: list[PanelItem] = []
    used_families: set[str] = set()
    tiers_used: list[str] = []

    def seat(pool: list[tuple[str, PanelItem]], reused: bool) -> None:
        for tier in ("strong", "segment_fallback", "broad_fallback"):
            candidates = [item for candidate_tier, item in pool if candidate_tier == tier]
            candidates.sort(
                key=lambda item: (
                    -(item.fit_score * item.coverage_score),
                    -item.coverage_score,
                    -item.fit_score,
                    item.persona_id,
                )
            )
            while candidates and len(items) < size:
                if tier == "strong" and len([i for i in items if i.role == "closest"]) < closest_count:
                    chosen = candidates.pop(0)
                    role = "closest"
                else:
                    chosen = next(
                        (item for item in candidates if item.family not in used_families),
                        candidates[0],
                    )
                    candidates.remove(chosen)
                    role = (
                        "counterweight"
                        if tier == "strong" and chosen.family not in used_families
                        else tier if tier != "strong" else "closest"
                    )
                items.append(chosen.model_copy(update={"role": role, "reused": reused}))
                tiers_used.append(tier)
                used_families.add(chosen.family)

    seat([(tier, item) for tier, item in scored if item.persona_id not in exclude_ids], False)
    if len(items) < size:
        seat([(tier, item) for tier, item in scored if item.persona_id in exclude_ids], True)

    snapshot = personas[0].snapshot_version if personas else "unknown"
    quality: Literal["strong", "segment_fallback", "broad_fallback"] = (
        "broad_fallback"
        if "broad_fallback" in tiers_used
        else "segment_fallback" if "segment_fallback" in tiers_used else "strong"
    )
    overlap = sorted(item.persona_id for item in items if item.reused)
    if overlap:
        fallback_reason = "insufficient unseen personas"
    elif quality == "broad_fallback":
        fallback_reason = "insufficient strong or same-segment matches"
    elif quality == "segment_fallback":
        fallback_reason = "strong matches lacked fit or coverage"
    elif len(items) < size:
        fallback_reason = f"only {len(items)} personas available"
    else:
        fallback_reason = None
    return PanelSelection(
        items=items,
        fit_threshold=FIT_THRESHOLD,
        snapshot_version=snapshot,
        match_features=features,
        requested_size=size,
        degraded=len(items) < size or quality != "strong" or bool(overlap),
        match_quality=quality,
        coverage_score=sum(item.coverage_score for item in items) / len(items),
        fallback_reason=fallback_reason,
        overlap_persona_ids=overlap,
    )
