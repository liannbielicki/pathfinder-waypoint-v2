"""Pro-matched persona panels: 2+1 screen, 3+2 final check.

Matching uses permitted organizational/lifecycle/product-usage/financial/
behavioral features only. Identity, contact data, and protected traits never
enter matching. Counterweights are related (threshold-clearing) personas from
a different family — never generic dissenters. If enough qualifying matches do
not exist, we report low panel fit instead of inventing representativeness.
"""

from typing import Any, Literal

from pydantic import BaseModel

# The only features matching may see. Everything else on the Pro is ignored.
PERMITTED_MATCH_FEATURES = (
    "segment", "plan", "tenure_bucket", "org_size_bucket", "trade_bucket",
    "open_ar_band", "lifecycle_stage", "features_active_count",
)

FIT_THRESHOLD = 0.5


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
    role: Literal["closest", "counterweight"]
    fit_score: float
    rationale: str


class PanelSelection(BaseModel):
    items: list[PanelItem]
    fit_threshold: float
    snapshot_version: str
    match_features: dict[str, Any]


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
    if len(closest) != closest_count:
        raise InsufficientPanelFit(size=size, available=len(closest))

    used_families = {item.family for item in closest}
    counterweights = [
        item.model_copy(update={"role": "counterweight"})
        for item in ranked[closest_count:]
        if item.fit_score >= FIT_THRESHOLD and item.family not in used_families
    ][:counter_count]
    if len(counterweights) != counter_count:
        raise InsufficientPanelFit(size=size, available=len(closest) + len(counterweights))

    snapshot = personas[0].snapshot_version if personas else "unknown"
    return PanelSelection(
        items=[*closest, *counterweights],
        fit_threshold=FIT_THRESHOLD,
        snapshot_version=snapshot,
        match_features=features,
    )
