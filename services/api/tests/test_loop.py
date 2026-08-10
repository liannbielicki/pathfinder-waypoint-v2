"""Pure win-stay/lose-shift policy tests. No DB, no I/O."""

import pytest

from waypoint.loop import (
    DEFAULT_LOOP_CONFIG,
    LoopConfig,
    LoopState,
    apply_round,
    is_win,
    next_mode,
    replay,
    stop_reason,
)

FLOOR = 1.0


def cfg(**overrides) -> LoopConfig:
    base = {
        "max_rounds": 10,
        "max_no_improve": 3,
        "patience": 1,
        "keep_delta_pp": 0.5,
        "win_threshold_pp": 15.0,
    }
    return LoopConfig(**{**base, **overrides})


# --- LoopConfig -------------------------------------------------------------


def test_defaults_match_the_spec() -> None:
    assert DEFAULT_LOOP_CONFIG == cfg()


def test_from_mapping_merges_partial_overrides_into_defaults() -> None:
    config = LoopConfig.from_mapping({"MAX_ROUNDS": 4})
    assert config.max_rounds == 4
    assert config.patience == 1
    assert config.keep_delta_pp == 0.5


def test_from_mapping_round_trips_to_dict() -> None:
    config = LoopConfig.from_mapping({"PATIENCE": 2})
    assert config.to_dict() == {
        "MAX_ROUNDS": 10,
        "MAX_NO_IMPROVE": 3,
        "PATIENCE": 2,
        "KEEP_DELTA_PP": 0.5,
        "WIN_THRESHOLD_PP": 15.0,
    }
    assert LoopConfig.from_mapping(config.to_dict()) == config


@pytest.mark.parametrize(
    "bad",
    [
        {"PATIENCE": 0},
        {"MAX_ROUNDS": -1},
        {"MAX_NO_IMPROVE": -1},
        {"KEEP_DELTA_PP": -0.1},
        {"WIN_THRESHOLD_PP": -1},
        {"KEEP_DELTA_PP": 20.0},  # > WIN_THRESHOLD_PP default 15
        {"UNKNOWN_KEY": 1},
    ],
)
def test_from_mapping_rejects_out_of_bounds(bad: dict) -> None:
    with pytest.raises(ValueError):
        LoopConfig.from_mapping(bad)


# --- win/lose rule ----------------------------------------------------------


def test_first_win_uses_the_floor() -> None:
    state = LoopState()
    assert is_win(state, 1.0, cfg(), FLOOR)
    assert not is_win(state, 0.9, cfg(), FLOOR)


def test_keep_delta_gates_improvement_over_best() -> None:
    state = LoopState(best_score=5.0)
    assert not is_win(state, 5.1, cfg(), FLOOR)  # +0.1 under a 0.5 delta
    assert is_win(state, 5.5, cfg(), FLOOR)


def test_missing_score_is_never_a_win() -> None:
    assert not is_win(LoopState(), None, cfg(), FLOOR)


# --- win-stay / lose-shift --------------------------------------------------


def test_cold_start_is_stay() -> None:
    assert next_mode(LoopState(), cfg()) == "stay"


def test_win_stays_on_the_same_mechanism() -> None:
    state = apply_round(
        LoopState(),
        mechanism="invoices",
        candidate_id="c1",
        score_pp=2.0,
        outcome="win",
        config=cfg(),
        floor_pp=FLOOR,
    )
    assert state.best_score == 2.0
    assert state.current_mechanism == "invoices"
    assert next_mode(state, cfg()) == "stay"


def test_lose_at_patience_one_shifts() -> None:
    state = apply_round(
        LoopState(),
        mechanism="invoices",
        candidate_id="c1",
        score_pp=0.2,
        outcome="lose",
        config=cfg(),
        floor_pp=FLOOR,
    )
    assert state.best_score is None
    assert state.dry_mechanisms == 1
    assert next_mode(state, cfg()) == "shift"


def test_patience_two_gets_a_second_try_before_shifting() -> None:
    config = cfg(patience=2)
    state = apply_round(
        LoopState(),
        mechanism="invoices",
        candidate_id="c1",
        score_pp=0.2,
        outcome="lose",
        config=config,
        floor_pp=FLOOR,
    )
    assert next_mode(state, config) == "stay"
    assert state.dry_mechanisms == 0
    state = apply_round(
        state,
        mechanism="invoices",
        candidate_id="c2",
        score_pp=0.3,
        outcome="lose",
        config=config,
        floor_pp=FLOOR,
    )
    assert next_mode(state, config) == "shift"
    assert state.dry_mechanisms == 1


def test_win_resets_tries_and_dry_counters() -> None:
    config = cfg()
    state = LoopState(
        best_score=2.0,
        current_mechanism="invoices",
        tries_on_current=1,
        dry_mechanisms=2,
        round=3,
        tried_mechanisms=("invoices", "reviews"),
    )
    state = apply_round(
        state,
        mechanism="reviews",
        candidate_id="c9",
        score_pp=3.0,
        outcome="win",
        config=config,
        floor_pp=FLOOR,
    )
    assert state.tries_on_current == 0
    assert state.dry_mechanisms == 0
    assert state.current_mechanism == "reviews"
    assert state.best_candidate_id == "c9"


@pytest.mark.parametrize("outcome", ["suppressed", "unavailable"])
def test_non_scored_outcomes_consume_patience_like_losses(outcome: str) -> None:
    state = apply_round(
        LoopState(),
        mechanism="invoices",
        candidate_id="c1",
        score_pp=None,
        outcome=outcome,
        config=cfg(),
        floor_pp=FLOOR,
    )
    assert state.dry_mechanisms == 1
    assert next_mode(state, cfg()) == "shift"


def test_tried_mechanisms_accumulate_ordered_and_deduped() -> None:
    state = LoopState()
    for mech, cid in (("a", "c1"), ("b", "c2"), ("a", "c3")):
        state = apply_round(
            state,
            mechanism=mech,
            candidate_id=cid,
            score_pp=0.0,
            outcome="lose",
            config=cfg(patience=5),
            floor_pp=FLOOR,
        )
    assert state.tried_mechanisms == ("a", "b")
    assert state.round == 3


# --- stop rules -------------------------------------------------------------


def test_no_stop_while_searching() -> None:
    assert stop_reason(LoopState(round=1, dry_mechanisms=1), cfg()) is None


def test_stop_on_win_threshold() -> None:
    assert stop_reason(LoopState(best_score=15.1), cfg()) == "win_threshold"
    assert stop_reason(LoopState(best_score=15.0), cfg()) is None  # strict >


def test_stop_on_dry_mechanisms() -> None:
    assert stop_reason(LoopState(dry_mechanisms=3), cfg()) == "no_improve_exhausted"


def test_stop_on_round_cap() -> None:
    assert stop_reason(LoopState(round=10), cfg()) == "round_cap"


# --- replay -----------------------------------------------------------------


class Row:
    def __init__(self, mechanism, candidate_id, score_pp, outcome):
        self.mechanism = mechanism
        self.candidate_id = candidate_id
        self.score_pp = score_pp
        self.outcome = outcome


def test_replay_reproduces_the_live_folded_state() -> None:
    config = cfg(patience=2)
    rounds = [
        Row("a", "c1", 2.0, "win"),
        Row("a", "c2", 2.1, "lose"),
        Row("a", "c3", None, "suppressed"),
        Row("b", "c4", 3.5, "win"),
    ]
    live = LoopState()
    for row in rounds:
        live = apply_round(
            live,
            mechanism=row.mechanism,
            candidate_id=row.candidate_id,
            score_pp=row.score_pp,
            outcome=row.outcome,
            config=config,
            floor_pp=FLOOR,
        )
    assert replay(rounds, config, FLOOR) == live
    assert live.best_score == 3.5
    assert live.best_candidate_id == "c4"
    assert live.round == 4
