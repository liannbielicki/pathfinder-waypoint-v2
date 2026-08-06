import json
from pathlib import Path

import pytest

from waypoint.scoring import (
    CandidateScore,
    NoAction,
    Winner,
    load_calibration,
    score_candidate,
    select_winner,
)

ARTIFACT = Path(__file__).parents[1] / "data" / "reaction_churn_calibration_cards.json"
CALIBRATION = load_calibration(ARTIFACT)

CELL = "1A|basic|0-3m"


def test_calibration_loads_learned_artifact() -> None:
    assert CALIBRATION.beta == pytest.approx(-0.3590560839101312)
    assert CALIBRATION.subtype_version == "22cc4a1c89354327"
    assert CALIBRATION.baselines[CELL].baseline == pytest.approx(0.1922010128148487)


def test_calibration_rejects_stale_positive_direction(tmp_path: Path) -> None:
    data = json.loads(ARTIFACT.read_text())
    data["beta"] = abs(data["beta"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_calibration(bad)


def test_calibration_rejects_missing_artifact(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_calibration(tmp_path / "absent.json")


def test_score_reproduces_the_audited_fixture() -> None:
    score = score_candidate(reactions=[6.0], cell=CELL, calibration=CALIBRATION)
    assert score.reduction_pp == pytest.approx(9.532340011069316)
    assert score.ci_lower_pp == pytest.approx(8.385040736718983)
    assert score.ci_upper_pp == pytest.approx(10.607820148180641)
    assert score.in_calibrated_range is False  # 6.0 sits above [3.208, 5.472]


def test_score_is_zero_at_the_no_touch_pivot() -> None:
    score = score_candidate(reactions=[CALIBRATION.pivot], cell=CELL, calibration=CALIBRATION)
    assert score.reduction_pp == pytest.approx(0.0, abs=1e-12)


def test_higher_reaction_means_larger_reduction() -> None:
    low = score_candidate(reactions=[4.0], cell=CELL, calibration=CALIBRATION)
    high = score_candidate(reactions=[5.0], cell=CELL, calibration=CALIBRATION)
    assert high.reduction_pp > low.reduction_pp
    assert low.in_calibrated_range and high.in_calibrated_range


def test_unknown_cell_falls_back_to_global_baseline() -> None:
    score = score_candidate(reactions=[5.0], cell="9Z|mystery|0-1d", calibration=CALIBRATION)
    assert score.baseline_confidence == "global"
    assert score.reduction_pp > 0


def test_empty_reactions_abstain() -> None:
    score = score_candidate(reactions=[], cell=CELL, calibration=CALIBRATION)
    assert score.abstained is True
    assert score.reduction_pp is None


def test_select_winner_prefers_highest_supported_reduction() -> None:
    strong = score_candidate(reactions=[5.4], cell=CELL, calibration=CALIBRATION)
    weak = score_candidate(reactions=[4.6], cell=CELL, calibration=CALIBRATION)
    result = select_winner({"cand_a": weak, "cand_b": strong})
    assert isinstance(result, Winner)
    assert result.candidate_id == "cand_b"


def test_no_supported_candidate_resolves_to_no_action() -> None:
    # At the pivot the reduction is zero: nothing clears the floor.
    flat = score_candidate(reactions=[CALIBRATION.pivot], cell=CELL, calibration=CALIBRATION)
    result = select_winner({"cand_a": flat})
    assert isinstance(result, NoAction)
    assert result.reason == "no_candidate_cleared_floor"


def test_abstained_scores_never_win() -> None:
    abstained = score_candidate(reactions=[], cell=CELL, calibration=CALIBRATION)
    result = select_winner({"cand_a": abstained})
    assert isinstance(result, NoAction)


def test_negative_reaction_delta_scores_negative_not_clamped() -> None:
    below = score_candidate(reactions=[3.5], cell=CELL, calibration=CALIBRATION)
    assert below.reduction_pp is not None and below.reduction_pp < 0


def test_score_serializes_for_persistence() -> None:
    score = score_candidate(reactions=[5.0], cell=CELL, calibration=CALIBRATION)
    payload = score.model_dump()
    assert payload["calibration_version"] == "22cc4a1c89354327"
    assert isinstance(payload["reduction_pp"], float)
    assert isinstance(CandidateScore.model_validate(payload), CandidateScore)
