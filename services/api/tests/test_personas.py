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


def test_counterweight_shortage_degrades_with_a_flag_instead_of_abstaining() -> None:
    # Only one family qualifies: no counterweight exists. The panel runs
    # short-handed and says so, rather than abstaining the Pro entirely.
    same_family = [p for p in PERSONAS if p.family == "solo_operators"]
    panel = select_panel(PRO_FIXTURE, same_family, size=3)
    assert panel.degraded is True
    assert panel.requested_size == 3
    assert len(panel.items) == 2
    assert all(item.fit_score >= panel.fit_threshold for item in panel.items)


def test_full_panel_is_not_flagged_degraded() -> None:
    panel = select_panel(PRO_FIXTURE, PERSONAS, size=3)
    assert panel.degraded is False
    assert panel.requested_size == 3


# A pro and personas sharing exactly four permitted keys, so fit is k/4 by
# construction: 4/4 qualifies, 2/4 is exactly threshold, 1/4 is below the
# backfill floor. The 1/3 persona shares only three keys (fit 0.33): backfill.
_BACKFILL_PRO = ProMatchInput(
    pro_id="pro_b",
    features={"segment": "2B", "plan": "grow", "tenure_bucket": "1y+",
              "org_size_bucket": "solo"},
)


def _persona(pid: str, family: str, **features: object) -> Persona:
    return Persona(persona_id=pid, family=family, label=pid,
                   snapshot_version="v1", features=features)


_BACKFILL_POOL = [
    _persona("full_a", "fam_a", segment="2B", plan="grow",
             tenure_bucket="1y+", org_size_bucket="solo"),  # fit 1.0
    _persona("full_b", "fam_a", segment="2B", plan="grow",
             tenure_bucket="1y+", org_size_bucket="solo"),  # fit 1.0, same family
    _persona("near", "fam_b", segment="2B", plan="basic",
             tenure_bucket="0-3m"),  # shares 3 keys, matches 1 -> fit 0.33
    _persona("far", "fam_c", segment="9Z", plan="basic",
             tenure_bucket="0-3m", org_size_bucket="mega"),  # fit 0.0
]


def test_backfill_seats_next_closest_above_the_floor_never_random() -> None:
    panel = select_panel(_BACKFILL_PRO, _BACKFILL_POOL, size=3)
    assert [item.role for item in panel.items] == ["closest", "closest", "backfill"]
    # The backfill seat is the best sub-threshold persona from a NEW family,
    # carrying its real fit score — never the 0.0-fit stranger.
    assert panel.items[2].persona_id == "near"
    assert panel.items[2].family == "fam_b"
    assert 0.3 <= panel.items[2].fit_score < FIT_THRESHOLD
    # A full-size panel with a backfill seat is still flagged degraded.
    assert panel.degraded is True
    assert panel.requested_size == 3


def test_backfill_never_seats_below_the_floor() -> None:
    # Only the 0.0-fit persona is available to backfill: the seat stays empty.
    pool = [p for p in _BACKFILL_POOL if p.persona_id != "near"]
    panel = select_panel(_BACKFILL_PRO, pool, size=3)
    assert [item.role for item in panel.items] == ["closest", "closest"]
    assert panel.degraded is True


def test_backfill_never_substitutes_for_the_two_qualifying_minimum() -> None:
    # One qualifying match plus a floor-clearing backfill candidate is still
    # no panel: backfill supplements real signal, it never constitutes it.
    pool = [p for p in _BACKFILL_POOL if p.persona_id != "full_b"]
    with pytest.raises(InsufficientPanelFit):
        select_panel(_BACKFILL_PRO, pool, size=3)


def test_fewer_than_two_qualifying_matches_still_abstains() -> None:
    lone = [p for p in PERSONAS if p.family == "solo_operators"][:1]
    with pytest.raises(InsufficientPanelFit):
        select_panel(PRO_FIXTURE, lone, size=3)


def test_segment_is_enough_when_personas_are_flat() -> None:
    # Regression: real persona-cards items are flat (segment + usage booleans),
    # sharing ONLY `segment` with a Pro. With segment fed, fit is 1.0 for the
    # whole pool and a panel forms; without it, no key is shared -> 0 available.
    flat = [
        Persona(persona_id=f"p{i}", family=f"p{i}", label=f"p{i}",
                snapshot_version="v3",
                features={"segment": "2B", "booking_attached": bool(i % 2)})
        for i in range(5)
    ]
    with_segment = ProMatchInput(pro_id="pro_a", features={"segment": "2B", "plan": "grow"})
    panel = select_panel(with_segment, flat, size=3)
    assert [item.role for item in panel.items] == ["closest", "closest", "counterweight"]
    assert all(item.fit_score == 1.0 for item in panel.items)

    without_segment = ProMatchInput(pro_id="pro_b", features={"plan": "grow"})
    with pytest.raises(InsufficientPanelFit):
        select_panel(without_segment, flat, size=3)


def test_panel_selection_is_deterministic() -> None:
    first = select_panel(PRO_FIXTURE, PERSONAS, size=5)
    second = select_panel(PRO_FIXTURE, list(reversed(PERSONAS)), size=5)
    assert [i.persona_id for i in first.items] == [i.persona_id for i in second.items]
