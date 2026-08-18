"""Pro-matched persona panels: 2+1 screen, 3+2 final check.

Matching uses permitted organizational/lifecycle/product-usage/financial/
behavioral features only. Identity, contact data, and protected traits never
enter matching. Counterweights are related (threshold-clearing) personas from
a different family — never generic dissenters. When qualifying matches run
short (minimum 2), the remaining seats are BACKFILLED with the next-closest
personas above a lower fit floor — ranked fit, never random, new families
first — and the panel is flagged as degraded. Below 2 qualifying matches we
report low panel fit instead of inventing representativeness.
"""

from typing import Any, Literal

from pydantic import BaseModel

# The only features matching may see. Everything else on the Pro is ignored.
PERMITTED_MATCH_FEATURES = (
    "segment", "plan", "tenure_bucket", "org_size_bucket", "trade_bucket",
    "open_ar_band", "lifecycle_stage", "features_active_count",
)

FIT_THRESHOLD = 0.5
# Smallest panel worth evaluating: with one persona there is no panel, just an
# opinion. Two qualifying matches run as a flagged, degraded panel.
MIN_PANEL_SIZE = 2
# Backfill seats admit the next-closest personas below FIT_THRESHOLD but above
# this floor. A 0.4-fit persona is weak evidence; below the floor it is noise
# wearing a persona costume — those seats stay empty instead.
BACKFILL_FIT_FLOOR = 0.3


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
    role: Literal["closest", "counterweight", "backfill"]
    fit_score: float
    rationale: str


class PanelSelection(BaseModel):
    items: list[PanelItem]
    fit_threshold: float
    snapshot_version: str
    match_features: dict[str, Any]
    # Degraded-panel provenance: a panel may run short-handed (>= MIN_PANEL_SIZE
    # qualifying matches) rather than abstain. Defaults keep old stored
    # evidence readable.
    requested_size: int = 0
    degraded: bool = False


def match_features(pro: ProMatchInput) -> dict[str, Any]:
    return {k: pro.features[k] for k in PERMITTED_MATCH_FEATURES if k in pro.features}


def _fit(pro_features: dict[str, Any], persona: Persona) -> tuple[float, str]:
    """Deterministic fit: fraction of permitted features that match exactly."""
    keys = [k for k in PERMITTED_MATCH_FEATURES if k in pro_features and k in persona.features]
    if not keys:
        return 0.0, "no shared permitted features"
    matched = [k for k in keys if pro_features[k] == persona.features[k]]
    return len(matched) / len(keys), f"matches on {', '.join(matched) or 'nothing'}"


def select_panel(
    pro: ProMatchInput, personas: list[Persona], size: Literal[3, 5]
) -> PanelSelection:
    closest_count = 2 if size == 3 else 3
    counter_count = size - closest_count
    features = match_features(pro)

    scored = []
    for persona in personas:
        fit, rationale = _fit(features, persona)
        scored.append(PanelItem(
            persona_id=persona.persona_id, label=persona.label, family=persona.family,
            role="closest", fit_score=fit, rationale=rationale,
        ))
    ranked = sorted(scored, key=lambda item: (-item.fit_score, item.persona_id))

    closest = [item for item in ranked[:closest_count] if item.fit_score >= FIT_THRESHOLD]

    used_families = {item.family for item in closest}
    counterweights: list[PanelItem] = []
    for item in ranked[closest_count:]:
        if len(counterweights) == counter_count:
            break
        # Each counterweight must clear the fit threshold and bring a family
        # not already on the panel (closest or prior counterweight).
        if item.fit_score >= FIT_THRESHOLD and item.family not in used_families:
            counterweights.append(item.model_copy(update={"role": "counterweight"}))
            used_families.add(item.family)

    items = [*closest, *counterweights]
    # The floor counts THRESHOLD-CLEARING members only: backfill supplements a
    # real panel, it never constitutes one. Below MIN_PANEL_SIZE, abstain.
    if len(items) < MIN_PANEL_SIZE:
        raise InsufficientPanelFit(size=size, available=len(items))

    if len(items) < size:
        # Backfill the empty seats with the next-closest personas above the
        # floor — ranked fit, never random, so every seat is evidence (however
        # weak, and visibly so: the seat keeps role="backfill" and its real
        # fit_score). New families first, restoring a dissenting voice.
        seated = {item.persona_id for item in items}
        pool = [
            item for item in ranked
            if item.persona_id not in seated and item.fit_score >= BACKFILL_FIT_FLOOR
        ]
        for prefer_new_family in (True, False):
            for item in pool:
                if len(items) == size:
                    break
                if item.persona_id in seated:
                    continue
                if prefer_new_family and item.family in used_families:
                    continue
                items.append(item.model_copy(update={"role": "backfill"}))
                seated.add(item.persona_id)
                used_families.add(item.family)

    snapshot = personas[0].snapshot_version if personas else "unknown"
    return PanelSelection(
        items=items,
        fit_threshold=FIT_THRESHOLD,
        snapshot_version=snapshot,
        match_features=features,
        requested_size=size,
        # Backfilled seats degrade the panel even when it reaches full size:
        # the flag means "not every seat cleared the threshold", not "short".
        degraded=len(items) < size or any(item.role == "backfill" for item in items),
    )
