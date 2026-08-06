"""Persistent persona-card snapshot: pin one persona set for a campaign's duration.

Riley regenerates her persona set on her own cadence (subtype_version changes).
Panels are a pure function of (segment, panel_size, seed) *within* a generation,
so a campaign spanning many processes has no consistency problem UNTIL she
regenerates mid-flight -- at which point some passes score against generation A
and the rest against generation B, inside one experiment.

Pinning via the request's subtype_version cannot fix this: her service hard-409s
a retired version rather than serving it. So we snapshot the raw payloads
ourselves and read from disk for the campaign's duration.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pathfinder.persona_cards_client import PersonaCardsClient, PersonaCardsError
from pathfinder.persona_card_snapshot import (
    ALL_SEGMENTS,
    RILEY_MIN_PANEL,
    MixedGenerationError,
    PersonaCardSnapshot,
    SnapshotCardsClient,
    capture_panels,
    snapshot_key,
)


def _panel_payload(segment="2B", version="v_A", panel_size=2, seed=0):
    return {
        "panel_id": f"p_{segment}_seed{seed}_{version}",
        "subtype_version": version,
        "segment": segment,
        "n_personas": panel_size,
        "personas": [
            # `backstory` is deliberately not modeled by PersonaCard; the reaction
            # prompt reads arbitrary card fields, so the snapshot must preserve it.
            {"persona_id": f"c{i}", "segment_key": segment, "trade": "HVAC",
             "backstory": f"long form {i}", "nps_score": 30 + i}
            for i in range(panel_size)
        ],
    }


def _exploding_transport(request):
    raise AssertionError("network must not be touched for a snapshot hit")


# --- snapshot store ---------------------------------------------------------

def test_snapshot_round_trips_full_payload(tmp_path):
    """Unmodeled card fields must survive the disk round-trip."""
    path = tmp_path / "snap.json"
    snap = PersonaCardSnapshot(path)
    snap.put(segment="2B", panel_size=2, seed=0, payload=_panel_payload())
    snap.save()

    panel = PersonaCardSnapshot(path).get_panel(segment="2B", panel_size=2, seed=0)
    assert panel.subtype_version == "v_A"
    assert [p.persona_id for p in panel.personas] == ["c0", "c1"]
    assert panel.personas[0].fields["backstory"] == "long form 0"


def test_snapshot_key_distinguishes_panel_size_and_seed():
    assert snapshot_key("2B", 8, 0) != snapshot_key("2B", 12, 0)
    assert snapshot_key("2B", 8, 0) != snapshot_key("2B", 8, 1)
    assert snapshot_key("2B", 8, 0) == snapshot_key("2B", 8, 0)


def test_missing_snapshot_file_is_empty_not_an_error(tmp_path):
    assert PersonaCardSnapshot(tmp_path / "absent.json").keys() == []


def test_subtype_versions_reports_every_generation_present(tmp_path):
    snap = PersonaCardSnapshot(tmp_path / "s.json")
    snap.put(segment="2B", panel_size=2, seed=0, payload=_panel_payload(version="v_A"))
    snap.put(segment="1A", panel_size=2, seed=0,
             payload=_panel_payload(segment="1A", version="v_B"))
    assert snap.subtype_versions() == {"v_A", "v_B"}


def test_verify_single_generation_raises_on_mixed_snapshot(tmp_path):
    """A snapshot spanning two generations is the exact bug this feature prevents."""
    snap = PersonaCardSnapshot(tmp_path / "s.json")
    snap.put(segment="2B", panel_size=2, seed=0, payload=_panel_payload(version="v_A"))
    snap.put(segment="1A", panel_size=2, seed=0,
             payload=_panel_payload(segment="1A", version="v_B"))
    with pytest.raises(MixedGenerationError, match="v_A"):
        snap.verify_single_generation()


def test_verify_single_generation_passes_on_uniform_snapshot(tmp_path):
    snap = PersonaCardSnapshot(tmp_path / "s.json")
    snap.put(segment="2B", panel_size=2, seed=0, payload=_panel_payload(version="v_A"))
    snap.put(segment="1A", panel_size=2, seed=0,
             payload=_panel_payload(segment="1A", version="v_A"))
    assert snap.verify_single_generation() == "v_A"


# --- strict (campaign) mode -------------------------------------------------

def test_strict_client_serves_from_snapshot_without_network(tmp_path):
    path = tmp_path / "s.json"
    snap = PersonaCardSnapshot(path)
    snap.put(segment="2B", panel_size=2, seed=0, payload=_panel_payload())
    snap.save()

    live = PersonaCardsClient("https://x", transport=_exploding_transport)
    client = SnapshotCardsClient(snapshot=PersonaCardSnapshot(path), live=live)
    panel = client.fetch_panel(segment="2B", panel_size=2, seed=0)
    assert panel.subtype_version == "v_A"


def test_strict_client_fails_closed_on_snapshot_miss(tmp_path):
    """Fail closed -> evaluator abstains, rather than silently pulling a new generation."""
    live = PersonaCardsClient("https://x", transport=lambda r: _panel_payload())
    client = SnapshotCardsClient(
        snapshot=PersonaCardSnapshot(tmp_path / "s.json"), live=live)
    with pytest.raises(PersonaCardsError, match="snapshot"):
        client.fetch_panel(segment="2B", panel_size=2, seed=0)


def test_strict_client_ignores_requested_subtype_version(tmp_path):
    """The snapshot IS the pin; a stale requested pin must not 409 us."""
    path = tmp_path / "s.json"
    snap = PersonaCardSnapshot(path)
    snap.put(segment="2B", panel_size=2, seed=0, payload=_panel_payload(version="v_A"))
    snap.save()

    live = PersonaCardsClient("https://x", transport=_exploding_transport)
    client = SnapshotCardsClient(snapshot=PersonaCardSnapshot(path), live=live)
    panel = client.fetch_panel(segment="2B", panel_size=2, seed=0, subtype_version="v_OLD")
    assert panel.subtype_version == "v_A"


# --- fill (refresh) mode ----------------------------------------------------

def test_fill_client_fetches_and_records_on_miss(tmp_path):
    path = tmp_path / "s.json"
    calls = []

    def transport(request):
        calls.append(request)
        return _panel_payload()

    live = PersonaCardsClient("https://x", transport=transport)
    snap = PersonaCardSnapshot(path)
    client = SnapshotCardsClient(snapshot=snap, live=live, allow_fetch=True)
    client.fetch_panel(segment="2B", panel_size=2, seed=0)
    snap.save()

    assert len(calls) == 1
    reloaded = PersonaCardSnapshot(path)
    assert reloaded.get_panel(segment="2B", panel_size=2, seed=0).subtype_version == "v_A"


def test_fill_client_does_not_refetch_a_hit(tmp_path):
    """Once captured, a panel is pinned even in fill mode."""
    calls = []

    def transport(request):
        calls.append(request)
        return _panel_payload()

    snap = PersonaCardSnapshot(tmp_path / "s.json")
    snap.put(segment="2B", panel_size=2, seed=0, payload=_panel_payload())
    client = SnapshotCardsClient(
        snapshot=snap, live=PersonaCardsClient("https://x", transport=transport),
        allow_fetch=True)
    client.fetch_panel(segment="2B", panel_size=2, seed=0)
    assert calls == []


# --- capture (the refresh CLI's core) ---------------------------------------

def test_capture_panels_captures_each_segment_once(tmp_path):
    seen = []

    def transport(request):
        seen.append((request["segment"], request["panel_size"]))
        return _panel_payload(segment=request["segment"], panel_size=2)

    snap = PersonaCardSnapshot(tmp_path / "s.json")
    result = capture_panels(
        snapshot=snap, live=PersonaCardsClient("https://x", transport=transport),
        segments=["1A", "2B"], panel_sizes=[24], seed=0)
    assert seen == [("1A", 24), ("2B", 24)]
    assert result["captured"] == 2 and result["skipped"] == 0
    assert result["subtype_version"] == "v_A"


def test_capture_panels_skips_entries_already_pinned(tmp_path):
    """Filling a gap must never disturb panels a campaign already runs against."""
    seen = []

    def transport(request):
        seen.append(request["segment"])
        return _panel_payload(segment=request["segment"], panel_size=2)

    snap = PersonaCardSnapshot(tmp_path / "s.json")
    snap.put(segment="1A", panel_size=24, seed=0,
             payload=_panel_payload(segment="1A", panel_size=2))
    result = capture_panels(
        snapshot=snap, live=PersonaCardsClient("https://x", transport=transport),
        segments=["1A", "2B"], panel_sizes=[24], seed=0)
    assert seen == ["2B"]
    assert result["captured"] == 1 and result["skipped"] == 1


def test_capture_panels_detects_regeneration_mid_capture(tmp_path):
    """If Riley regenerates while we capture, the snapshot is mixed -- say so."""
    versions = iter(["v_A", "v_B"])

    def transport(request):
        return _panel_payload(segment=request["segment"], panel_size=2,
                              version=next(versions))

    snap = PersonaCardSnapshot(tmp_path / "s.json")
    with pytest.raises(MixedGenerationError):
        capture_panels(
            snapshot=snap, live=PersonaCardsClient("https://x", transport=transport),
            segments=["1A", "2B"], panel_sizes=[24], seed=0)


# --- guards on the committed campaign artifact(s) ---------------------------
#
# Characterization guards, not red-green derived: they pin invariants the live
# campaign depends on so a later edit cannot quietly break them.

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FROZEN_DIR = _REPO_ROOT / "data/v1l/frozen"
_COMMITTED = sorted(_FROZEN_DIR.glob("persona_cards_snapshot_*.json"))


@pytest.mark.skipif(not _COMMITTED, reason="no committed persona-card snapshot")
@pytest.mark.parametrize("path", _COMMITTED, ids=lambda p: p.name)
def test_committed_snapshot_is_one_generation_and_covers_every_cell(path):
    snap = PersonaCardSnapshot(path)
    snap.verify_single_generation()  # raises if mixed
    missing = [s for s in ALL_SEGMENTS
               if snap.get_payload(segment=s, panel_size=RILEY_MIN_PANEL,
                                   seed=0) is None]
    assert not missing, f"{path.name} is missing cells: {missing}"


@pytest.mark.skipif(not _COMMITTED, reason="no committed persona-card snapshot")
@pytest.mark.parametrize("path", _COMMITTED, ids=lambda p: p.name)
def test_committed_snapshot_panels_are_full_and_parse(path):
    """Every pinned panel must hold a full slice-able panel of real cards."""
    snap = PersonaCardSnapshot(path)
    for segment in ALL_SEGMENTS:
        panel = snap.get_panel(segment=segment, panel_size=RILEY_MIN_PANEL, seed=0)
        assert len(panel.personas) >= RILEY_MIN_PANEL, (
            f"{path.name} {segment}: {len(panel.personas)} cards < "
            f"{RILEY_MIN_PANEL}; panel_size requests would slice short")
        assert all(c.persona_id for c in panel.personas)


@pytest.mark.skipif(not _COMMITTED, reason="no committed persona-card snapshot")
@pytest.mark.parametrize("path", _COMMITTED, ids=lambda p: p.name)
def test_committed_snapshot_matches_the_calibration_generation(path):
    """Pinned personas must be the generation the calibration was fit on.

    If this fails, someone adopted a new persona generation without re-fitting:
    scoring would run against personas the calibration never saw. Re-fit (see
    docs/superpowers/plans/2026-07-27-phase2-real-calibration.md) or re-pin.
    """
    calibration = json.loads(
        (_FROZEN_DIR / "reaction_churn_calibration_cards.json").read_text())
    assert PersonaCardSnapshot(path).verify_single_generation() == (
        calibration["subtype_version"])


def test_snapshot_records_capture_provenance(tmp_path):
    path = tmp_path / "s.json"
    snap = PersonaCardSnapshot(path)
    snap.put(segment="2B", panel_size=2, seed=0, payload=_panel_payload())
    snap.save()
    raw = json.loads(path.read_text())
    assert raw["panels"][snapshot_key("2B", 2, 0)]["subtype_version"] == "v_A"
    assert "captured_at" in raw["panels"][snapshot_key("2B", 2, 0)]


# --- segment-key vocabulary --------------------------------------------------
# The audience layer speaks cell codes ("2A"); the evaluator's candidate speaks
# persona keys ("service_a", via segment_code_to_persona_key). Both must resolve
# to the same snapshot entry or every runtime lookup misses (2026-07-31 outage).

def test_snapshot_key_treats_cell_code_and_persona_key_as_one_draw():
    assert snapshot_key("2A", 24, 0) == snapshot_key("service_a", 24, 0)


def test_capture_under_cell_code_serves_persona_key_lookup(tmp_path):
    snap = PersonaCardSnapshot(tmp_path / "s.json")
    snap.put(segment="2A", panel_size=24, seed=0,
             payload=_panel_payload(segment="2A", panel_size=2))
    client = SnapshotCardsClient(snapshot=snap, allow_fetch=False)
    panel = client.fetch_panel(segment="service_a", panel_size=24, seed=0)
    assert panel.subtype_version == "v_A"


def test_snapshot_file_with_raw_cell_keys_is_normalized_on_load(tmp_path):
    """A snapshot captured before key normalization must keep working."""
    path = tmp_path / "s.json"
    body = {"schema_version": 1, "panels": {"2A|24|0": {
        "subtype_version": "v_A", "captured_at": "2026-07-29T00:00:00+00:00",
        "payload": _panel_payload(segment="2A", panel_size=2),
    }}}
    path.write_text(json.dumps(body))
    snap = PersonaCardSnapshot(path)
    assert snap.get_panel(segment="service_a", panel_size=24, seed=0) is not None
