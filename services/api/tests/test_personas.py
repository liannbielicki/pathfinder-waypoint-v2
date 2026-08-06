import json
from pathlib import Path

import pytest

from waypoint.personas import (
    FIT_THRESHOLD,
    InsufficientPanelFit,
    Persona,
    ProMatchInput,
    match_features,
    select_panel,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "personas.json").read_text())
PERSONAS = [
    Persona(snapshot_version=FIXTURE["snapshot_version"], **p) for p in FIXTURE["personas"]
]

PRO_FIXTURE = ProMatchInput(
    pro_id="pro_1",
    features={
        "segment": "1A", "plan": "basic", "tenure_bucket": "0-3m",
        "org_size_bucket": "solo", "trade_bucket": "hvac",
        "open_ar_band": "low", "lifecycle_stage": "active",
        # Never allowed to influence matching:
        "name": "Jordan", "email": "x@example.com", "age": 44,
    },
)


def test_three_person_screen_is_two_closest_plus_related_counterweight() -> None:
    panel = select_panel(PRO_FIXTURE, PERSONAS, size=3)
    assert [item.role for item in panel.items] == ["closest", "closest", "counterweight"]
    assert panel.items[2].fit_score >= panel.fit_threshold
    assert panel.items[2].family not in {panel.items[0].family, panel.items[1].family}


def test_five_person_final_is_three_plus_two() -> None:
    panel = select_panel(PRO_FIXTURE, PERSONAS, size=5)
    assert [item.role for item in panel.items].count("closest") == 3
    assert [item.role for item in panel.items].count("counterweight") == 2
    closest_families = {i.family for i in panel.items if i.role == "closest"}
    counterweight_families = [i.family for i in panel.items if i.role == "counterweight"]
    assert len(set(counterweight_families)) == len(counterweight_families)
    for item in panel.items:
        if item.role == "counterweight":
            assert item.fit_score >= panel.fit_threshold
            assert item.family not in closest_families


def test_panel_persists_provenance() -> None:
    panel = select_panel(PRO_FIXTURE, PERSONAS, size=3)
    assert panel.snapshot_version == "personas_2026_07"
    assert panel.fit_threshold == FIT_THRESHOLD
    for item in panel.items:
        assert item.persona_id
        assert item.rationale
        assert 0 <= item.fit_score <= 1


def test_protected_traits_cannot_enter_match_features() -> None:
    assert set(match_features(PRO_FIXTURE)).isdisjoint(
        {"name", "email", "phone", "race", "gender", "age"}
    )


def test_insufficient_qualifying_matches_reports_low_panel_fit() -> None:
    distant = ProMatchInput(
        pro_id="pro_x",
        features={"segment": "9Z", "plan": "mystery", "tenure_bucket": "0-1d",
                  "org_size_bucket": "mega", "trade_bucket": "unknown",
                  "open_ar_band": "n/a", "lifecycle_stage": "frozen"},
    )
    with pytest.raises(InsufficientPanelFit):
        select_panel(distant, PERSONAS, size=3)


def test_counterweight_shortage_never_relaxes_the_threshold() -> None:
    same_family = [p for p in PERSONAS if p.family == "solo_operators"]
    with pytest.raises(InsufficientPanelFit):
        select_panel(PRO_FIXTURE, same_family, size=3)


def test_panel_selection_is_deterministic() -> None:
    first = select_panel(PRO_FIXTURE, PERSONAS, size=5)
    second = select_panel(PRO_FIXTURE, list(reversed(PERSONAS)), size=5)
    assert [i.persona_id for i in first.items] == [i.persona_id for i in second.items]
