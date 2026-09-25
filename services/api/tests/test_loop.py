"""Pure win-stay/lose-shift policy tests. No DB, no I/O."""

import pytest

from waypoint.loop import (
    DEFAULT_LOOP_CONFIG,
    MAX_CANDIDATE_COUNT,
    SCREEN_PANEL_SIZE,
    LoopConfig,
    LoopState,
    apply_round,
    is_win,
    mechanism_key,
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
        "keep_delta_pp": 0.6,
        "keep_delta_reaction": 0.4,
        "win_threshold_pp": 15.0,
        "candidate_count": 3,
        "tie_margin": 0.05,
        "warm_start_threshold": 0.75,
    }
    return LoopConfig(**{**base, **overrides})


# --- LoopConfig -------------------------------------------------------------


def test_defaults_match_the_spec() -> None:
    assert DEFAULT_LOOP_CONFIG == cfg()


def test_from_mapping_merges_partial_overrides_into_defaults() -> None:
    config = LoopConfig.from_mapping({"MAX_ROUNDS": 4})
    assert config.max_rounds == 4
    assert config.patience == 1
    assert config.keep_delta_pp == 0.6


def test_from_mapping_round_trips_to_dict() -> None:
    config = LoopConfig.from_mapping({"PATIENCE": 2})
    assert config.to_dict() == {
        "MAX_ROUNDS": 10,
        "MAX_NO_IMPROVE": 3,
        "PATIENCE": 2,
        "KEEP_DELTA_PP": 0.6,
        "KEEP_DELTA_REACTION": 0.4,
        "WIN_THRESHOLD_PP": 15.0,
        "CANDIDATE_COUNT": 3,
        "TIE_MARGIN": 0.05,
        "WARM_START_THRESHOLD": 0.75,
    }
    assert LoopConfig.from_mapping(config.to_dict()) == config


# --- CANDIDATE_COUNT / TIE_MARGIN -------------------------------------------


def test_candidate_count_defaults_to_three() -> None:
    assert DEFAULT_LOOP_CONFIG.candidate_count == 3


@pytest.mark.parametrize("value", [1, 2, 5, MAX_CANDIDATE_COUNT])
def test_candidate_count_accepts_explicit_values_up_to_the_ceiling(value: int) -> None:
    config = LoopConfig.from_mapping({"CANDIDATE_COUNT": value})
    assert config.candidate_count == value


@pytest.mark.parametrize("value", [0, -1, "abc", MAX_CANDIDATE_COUNT + 1])
def test_candidate_count_rejects_bad_values(value: object) -> None:
    with pytest.raises(ValueError):
        LoopConfig.from_mapping({"CANDIDATE_COUNT": value})


def test_warm_start_threshold_defaults_to_point_seven_five() -> None:
    assert DEFAULT_LOOP_CONFIG.warm_start_threshold == 0.75


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_warm_start_threshold_accepts_the_full_similarity_range(value: float) -> None:
    assert LoopConfig.from_mapping({"WARM_START_THRESHOLD": value}).warm_start_threshold == value


@pytest.mark.parametrize("value", [-0.1, 1.5, "abc"])
def test_warm_start_threshold_rejects_out_of_bounds(value: object) -> None:
    with pytest.raises(ValueError):
        LoopConfig.from_mapping({"WARM_START_THRESHOLD": value})


def test_tie_margin_defaults_to_point_zero_five() -> None:
    assert DEFAULT_LOOP_CONFIG.tie_margin == 0.05


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_tie_margin_rejects_out_of_bounds(value: float) -> None:
    with pytest.raises(ValueError):
        LoopConfig.from_mapping({"TIE_MARGIN": value})


@pytest.mark.parametrize(
    "bad",
    [
        {"PATIENCE": 0},
        {"MAX_ROUNDS": -1},
        {"MAX_NO_IMPROVE": -1},
        {"KEEP_DELTA_PP": -0.1},
        {"WIN_THRESHOLD_PP": -1},
        {"KEEP_DELTA_PP": 20.0},  # > WIN_THRESHOLD_PP default 15
        {"KEEP_DELTA_REACTION": -0.1},
        {"KEEP_DELTA_REACTION": 4.0},  # the whole 3-7 span: unclearable
        {"KEEP_DELTA_REACTION": "abc"},
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


def test_keep_delta_pp_still_gates_when_no_reaction_is_known() -> None:
    """A champion crowned before KEEP_DELTA_REACTION existed has no best_reaction
    in replayed state, so the pp comparison is all there is."""
    state = LoopState(best_score=5.0)
    assert not is_win(state, 5.1, cfg(), FLOOR, 4.6)  # +0.1 under a 0.6 delta
    assert is_win(state, 5.6, cfg(), FLOOR, 4.6)


def test_keep_delta_pp_remains_a_secondary_guard_on_the_improvement() -> None:
    """Two persona-steps that barely move pp is still not an improvement worth
    a champion swap — the pp delta stays as the second half of the AND."""
    state = LoopState(best_score=5.0, best_reaction=4.4)
    assert not is_win(state, 5.2, cfg(), FLOOR, 4.8)  # reaction clears, pp does not
    assert is_win(state, 5.7, cfg(), FLOOR, 4.8)


def test_missing_score_is_never_a_win() -> None:
    assert not is_win(LoopState(), None, cfg(), FLOOR)


# The loop is decided by the 3-persona SCREENING panel, so one persona moving
# one point moves the mean by 1/3 — not the 0.2 of the held-out 5-persona final.
# What that step is WORTH in pp swings with the operating point, so a fixed pp
# bar cannot sit above it everywhere. Each row is (mean reaction, pp for one
# 1/3 step from it), measured against the shipped artifact on the global
# baseline — reproduce with scoring.score_candidate on data/
# reaction_churn_calibration_cards.json. EVERY row exceeds the old 0.6pp bar,
# which is why that bar could not hold.
STEP = 1 / SCREEN_PANEL_SIZE
ONE_STEP_PP = [(3.4, 1.424), (4.0, 1.212), (4.25, 1.130), (4.6, 1.022), (5.4, 0.804)]


def test_the_default_bar_sits_between_one_and_two_screening_steps() -> None:
    """The load-bearing assertion. The bar must REJECT one persona moving one
    point and ADMIT two — on the panel that actually decides the round. This
    fails at 0.3 (which would admit a single 0.3333 step, i.e. the exact bug
    this task exists to close), fails at 0.7, and fails again the day the
    screening panel is resized to a size where 0.4 no longer sits in that
    window (e.g. 6 personas, where two steps is only 0.333)."""
    assert 1 / SCREEN_PANEL_SIZE < DEFAULT_LOOP_CONFIG.keep_delta_reaction <= 2 / SCREEN_PANEL_SIZE


@pytest.mark.parametrize(("reaction", "step_pp"), ONE_STEP_PP)
def test_a_single_persona_point_cannot_crown_a_champion(reaction: float, step_pp: float) -> None:
    """THE regression this rule exists to close. A single screening step is
    worth well over 0.6pp at every operating point on the calibrated range, so
    the pp-only bar promoted it; the reaction bar rejects it because one step < the bar."""
    config = cfg()
    state = LoopState(
        round=1,
        best_score=2.4,
        best_reaction=reaction,
        current_mechanism="m",
        best_candidate_id="c1",
    )
    assert is_win(state, 2.4 + step_pp, config, FLOOR, reaction + STEP) is False
    # the old bar's blind spot: pp alone would have called every one of these a win
    assert 2.4 + step_pp >= state.best_score + config.keep_delta_pp


@pytest.mark.parametrize(("reaction", "step_pp"), ONE_STEP_PP)
def test_two_persona_steps_win(reaction: float, step_pp: float) -> None:
    config = cfg()
    state = LoopState(round=1, best_score=2.4, best_reaction=reaction, best_candidate_id="c1")
    assert is_win(state, 2.4 + 2 * step_pp, config, FLOOR, reaction + 2 * STEP) is True


@pytest.mark.parametrize(("panel_size", "step"), [(1, 1.0), (2, 0.5), (3, 1 / 3)])
def test_a_degraded_panel_raises_the_bar_to_its_own_lattice(
    panel_size: int, step: float
) -> None:
    """select_panel seats what it can on a thin persona pool and only flags
    `degraded` — nothing rejects a short panel. A 2-persona panel steps by 0.5
    and a 1-persona panel by 1.0, both clearing a 0.4 bar, so the single-persona
    regression would come back on exactly the runs that are least trustworthy.
    The bar rises to two steps of the SEATED panel."""
    config = cfg()
    state = LoopState(round=1, best_score=2.4, best_reaction=4.0, best_candidate_id="c1")
    assert is_win(state, 9.0, config, FLOOR, 4.0 + step, panel_size=panel_size) is False
    assert is_win(state, 9.0, config, FLOOR, 4.0 + 2 * step, panel_size=panel_size) is True


def test_a_full_screen_panel_decides_at_two_of_its_own_steps() -> None:
    """The configured value is a FLOOR, not the effective bar: on a full
    3-persona screen the lattice lifts it from 0.4 to 0.667, so an improvement
    between the two (which no 3-persona panel can actually produce) is not a
    win. Only a bar ABOVE two steps would bind."""
    state = LoopState(round=1, best_score=2.4, best_reaction=4.0, best_candidate_id="c1")
    assert is_win(state, 9.0, cfg(), FLOOR, 4.5, panel_size=SCREEN_PANEL_SIZE) is False
    assert is_win(state, 9.0, cfg(keep_delta_reaction=1.5), FLOOR, 4.7, panel_size=3) is False


def test_an_unknown_panel_size_keeps_the_configured_bar() -> None:
    """replay never passes a panel size — it folds row.outcome and re-decides
    nothing — so the absent value must simply mean "use the config"."""
    state = LoopState(round=1, best_score=2.4, best_reaction=4.0, best_candidate_id="c1")
    assert is_win(state, 9.0, cfg(), FLOOR, 4.4, panel_size=None) is True


def test_the_reaction_bar_survives_float_representation_of_fifths() -> None:
    """Panel means are fifths: 4.4 + 0.4 == 4.800000000000001 in IEEE754, so a
    bare >= would reject a genuine two-step improvement."""
    state = LoopState(best_score=2.0, best_reaction=4.4, best_candidate_id="c1")
    assert is_win(state, 3.0, cfg(), FLOOR, 4.8) is True


def test_reaction_improvement_below_the_support_floor_never_wins() -> None:
    """The floor is about evidence, not deltas: an unsupported score cannot be a
    champion however far its panel mean moved."""
    state = LoopState(round=1, best_score=-2.0, best_reaction=3.2, best_candidate_id="c1")
    assert is_win(state, 0.9, cfg(), FLOOR, 4.0) is False
    assert is_win(state, 1.0, cfg(), FLOOR, 4.0) is True


def test_first_win_needs_only_the_floor_not_the_reaction_bar() -> None:
    assert is_win(LoopState(), 1.0, cfg(), FLOOR, 3.0) is True
    assert is_win(LoopState(), 1.0, cfg(), FLOOR, None) is True
    assert is_win(LoopState(), 0.9, cfg(), FLOOR, 7.0) is False


# --- win-stay / lose-shift --------------------------------------------------


def test_cold_start_is_explicit() -> None:
    assert next_mode(LoopState(), cfg()) == "cold"


def test_win_stays_on_the_same_mechanism() -> None:
    state = apply_round(
        LoopState(),
        mechanism="invoices",
        candidate_id="c1",
        score_pp=2.0,
        outcome="win",
        config=cfg(),
        mode="cold",
    )
    assert state.best_score == 2.0
    assert state.current_mechanism == "invoices"
    assert next_mode(state, cfg()) == "refine"


def test_lose_at_patience_one_shifts() -> None:
    state = apply_round(
        LoopState(),
        mechanism="invoices",
        candidate_id="c1",
        score_pp=0.2,
        outcome="lose",
        config=cfg(),
        mode="cold",
    )
    assert state.best_score is None
    assert state.current_candidate_id == "c1"
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
        mode="cold",
    )
    assert next_mode(state, config) == "refine"
    assert state.dry_mechanisms == 0
    state = apply_round(
        state,
        mechanism="invoices",
        candidate_id="c2",
        score_pp=0.3,
        outcome="lose",
        config=config,
        mode="refine",
    )
    assert next_mode(state, config) == "shift"
    assert state.dry_mechanisms == 1


def test_mechanism_identity_ignores_case_spacing_and_punctuation() -> None:
    """`mechanism_key` normalization — not `tries_on_current` bookkeeping, which
    is mode-driven since Task 1 and asserted separately — is what's under test
    here: two differently-cased/punctuated labels for the same idea must
    collapse to one entry in `tried_mechanisms`."""
    assert mechanism_key(" Invoice  Reminder! ") == mechanism_key("invoice-reminder")
    state = apply_round(
        LoopState(),
        mechanism="Invoice Reminder!",
        candidate_id="c1",
        score_pp=0.2,
        outcome="lose",
        config=cfg(patience=2),
        mode="cold",
    )
    state = apply_round(
        state,
        mechanism="invoice-reminder",
        candidate_id="c2",
        score_pp=0.3,
        outcome="lose",
        config=cfg(patience=2),
        mode="refine",
    )
    assert state.tried_mechanisms == ("invoice-reminder",)


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
        mode="refine",
    )
    assert state.tries_on_current == 0
    assert state.dry_mechanisms == 0
    assert state.current_mechanism == "reviews"
    assert state.best_candidate_id == "c9"


@pytest.mark.parametrize("outcome", ["suppressed", "unavailable"])
def test_non_scored_outcomes_do_not_count_as_scored_dry_mechanisms(outcome: str) -> None:
    state = apply_round(
        LoopState(),
        mechanism="invoices",
        candidate_id="c1",
        score_pp=None,
        outcome=outcome,
        config=cfg(),
        mode="cold",
    )
    assert state.dry_mechanisms == 0
    assert state.blocked_rounds == (1 if outcome == "suppressed" else 0)
    assert stop_reason(state, cfg()) == ("evaluation_unavailable" if outcome == "unavailable" else None)
    assert state.current_candidate_id is None
    assert state.current_mechanism is None
    assert next_mode(state, cfg()) == "shift"


@pytest.mark.parametrize("outcome", ["suppressed", "unavailable"])
def test_non_scored_round_preserves_the_actual_current_selection(outcome: str) -> None:
    current = LoopState(
        best_score=2.0,
        best_candidate_id="winner",
        current_candidate_id="winner",
        current_mechanism="invoices",
    )
    state = apply_round(
        current,
        mechanism="arbitrary-row",
        candidate_id="discarded",
        score_pp=None,
        outcome=outcome,
        config=cfg(),
        mode="refine",
    )
    assert state.current_candidate_id == "winner"
    assert state.current_mechanism == "invoices"
    assert next_mode(state, cfg()) == "refine"


def test_tried_mechanisms_accumulate_ordered_and_deduped() -> None:
    config = cfg(patience=5)
    state = LoopState()
    for mech, cid in (("a", "c1"), ("b", "c2"), ("a", "c3")):
        state = apply_round(
            state,
            mechanism=mech,
            candidate_id=cid,
            score_pp=0.0,
            outcome="lose",
            config=config,
            mode=next_mode(state, config),
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


def test_suppressed_rounds_use_separate_bounded_budget_and_replay() -> None:
    config = cfg(max_no_improve=3, max_rounds=10)
    rows = [Row(f"idea-{i}", f"c{i}", None, "suppressed") for i in range(3)]
    state = replay(rows, config)
    assert state.dry_mechanisms == 0
    assert state.blocked_rounds == 3
    assert stop_reason(state, config) == "blocked_budget_exhausted"


def test_mixed_scored_and_blocked_rounds_keep_separate_counters() -> None:
    config = cfg(max_no_improve=3, max_rounds=10)
    state = replay([
        Row("a", "c1", 0.2, "lose"),
        Row("b", "c2", None, "suppressed"),
        Row("c", "c3", 0.3, "lose"),
    ], config)
    assert state.dry_mechanisms == 2
    assert state.blocked_rounds == 1
    assert stop_reason(state, config) is None


def test_blocked_refine_does_not_spend_scored_patience_on_replay() -> None:
    config = cfg(patience=2, max_no_improve=2)
    state = replay([
        Row("a", "c1", 0.2, "lose", {"mode": "cold"}),
        Row("b", "c2", None, "suppressed", {"mode": "refine"}),
        Row("c", "c3", 0.3, "lose", {"mode": "refine"}),
    ], config)
    assert state.blocked_rounds == 1
    assert state.dry_mechanisms == 1
    assert stop_reason(state, config) is None


def test_legacy_loss_without_numeric_score_is_unavailable_not_scored_dry() -> None:
    state = replay([Row("a", "c1", None, "lose")], cfg())
    assert state.dry_mechanisms == 0
    assert stop_reason(state, cfg()) == "evaluation_unavailable"


def test_stop_on_round_cap() -> None:
    assert stop_reason(LoopState(round=10), cfg()) == "round_cap"


# --- replay -----------------------------------------------------------------


class Row:
    def __init__(self, mechanism, candidate_id, score_pp, outcome, ranking=None):
        self.mechanism = mechanism
        self.candidate_id = candidate_id
        self.score_pp = score_pp
        self.outcome = outcome
        self.ranking = ranking or {}


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
            mode=next_mode(live, config),
        )
    assert replay(rounds, config) == live
    assert live.best_score == 3.5
    assert live.best_candidate_id == "c4"
    assert live.round == 4


def test_replay_carries_mean_reaction_and_reproduces_live_state() -> None:
    """The ledger has no mean_reaction column and gets no migration, so the
    value rides in the ranking JSON. Replay must fold it back in or a resumed
    run judges its next challenger against a different bar than the live run."""
    config = cfg()
    rounds = [
        Row("a", "c1", 2.0, "win", ranking={"mean_reaction": 4.2}),
        Row("a", "c2", 2.5, "lose", ranking={"mean_reaction": 4.4}),
        Row("b", "c3", 3.0, "win", ranking={"mean_reaction": 4.6}),
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
            mode=next_mode(live, config),
            mean_reaction=row.ranking.get("mean_reaction"),
        )
    replayed = replay(rounds, config)
    assert replayed == live
    assert replayed.best_reaction == 4.6
    # and the resumed run judges the next challenger identically
    assert is_win(replayed, 3.6, config, FLOOR, 5.0) is True
    assert is_win(replayed, 3.6, config, FLOOR, 4.8) is False


def test_replay_of_a_pre_change_row_falls_back_to_the_pp_comparison() -> None:
    """Rows written before mean_reaction was persisted have no key. That is a
    real state, not an edge case: replay must not crash and must not invent a
    reaction (inverting score_pp only works while one global baseline holds)."""
    state = replay([Row("a", "c1", 2.0, "win", ranking={"order": [{"mechanism": "a"}]})], cfg())
    assert state.best_reaction is None
    assert state.best_score == 2.0
    assert is_win(state, 2.6, cfg(), FLOOR, 4.8) is True  # pp rule alone
    assert is_win(state, 2.5, cfg(), FLOOR, 4.8) is False


def test_replay_ignores_a_non_numeric_mean_reaction() -> None:
    state = replay([Row("a", "c1", 2.0, "win", ranking={"mean_reaction": None})], cfg())
    assert state.best_reaction is None


def test_replay_recovers_non_challenger_mechanisms_from_ranking() -> None:
    """A resumed SHIFT must forbid the batch's other mechanisms, not just the
    round's challenger — they cost paid critic/ranker calls to evaluate. They
    live in the round's ranking audit trail (order[].mechanism)."""
    config = cfg()
    ranking = {"order": [{"mechanism": "a"}, {"mechanism": "sibling"}]}
    state = replay([Row("a", "c1", 2.0, "win", ranking=ranking)], config)
    assert set(state.tried_mechanisms) == {"a", "sibling"}


def test_replay_recovers_suppressed_generated_mechanisms() -> None:
    ranking = {
        "order": [{"mechanism": "ranked"}],
        "generated_mechanisms": ["ranked", "critic-suppressed"],
    }
    state = replay([Row("ranked", "c1", None, "suppressed", ranking=ranking)], cfg())
    assert set(state.tried_mechanisms) == {"ranked", "critic-suppressed"}


# --- mode drives patience, not the model's prose ----------------------------


def test_refine_round_advances_patience_even_when_the_label_is_reworded() -> None:
    """Regression: the model rewords its own mechanism every round. Patience
    must track the loop's INTENT, not the model's prose."""
    config = cfg(patience=2, max_no_improve=5)
    state = LoopState(
        round=1,
        best_score=2.4,
        best_candidate_id="c1",
        current_mechanism="Simplify payment collection workflow",
        tries_on_current=0,
    )
    state = apply_round(
        state,
        mechanism="Streamline payment collection to reduce friction",
        candidate_id="c2",
        score_pp=1.4,
        outcome="lose",
        config=config,
        mode="refine",
    )
    assert state.tries_on_current == 1
    state = apply_round(
        state,
        mechanism="Make collecting payment effortless",
        candidate_id="c3",
        score_pp=1.4,
        outcome="lose",
        config=config,
        mode="refine",
    )
    assert state.tries_on_current == 2
    assert state.dry_mechanisms == 1
    assert next_mode(state, config) == "shift"


def test_shift_round_restarts_the_patience_count() -> None:
    config = cfg(patience=2)
    state = LoopState(round=3, current_mechanism="payment collection", tries_on_current=2)
    state = apply_round(
        state,
        mechanism="online booking adoption",
        candidate_id="c9",
        score_pp=0.3,
        outcome="lose",
        config=config,
        mode="shift",
    )
    assert state.tries_on_current == 1


def test_a_win_resets_patience_whatever_the_mode() -> None:
    config = cfg(patience=2)
    state = LoopState(round=2, best_score=1.4, current_mechanism="payments", tries_on_current=1)
    state = apply_round(
        state,
        mechanism="payments reworded",
        candidate_id="c4",
        score_pp=3.3,
        outcome="win",
        config=config,
        mode="refine",
    )
    assert state.tries_on_current == 0
    assert state.dry_mechanisms == 0
    assert state.current_mechanism == "payments reworded"


def test_dry_mechanisms_exhaust_and_stop_the_loop() -> None:
    """The end-to-end point of Task 1: a Pro that stops improving now STOPS."""
    config = cfg(max_rounds=15, max_no_improve=5, patience=2, keep_delta_pp=0.6)
    state = LoopState()
    rounds = 0
    for index in range(15):
        if stop_reason(state, config) is not None:
            break
        state = apply_round(
            state,
            mechanism=f"payment friction variant {index}",
            candidate_id=f"c{index}",
            score_pp=1.4,
            outcome="win" if state.best_score is None else "lose",
            config=config,
            mode=next_mode(state, config),
        )
        rounds += 1
    assert stop_reason(state, config) == "no_improve_exhausted"
    assert rounds < 15, "loop must stop before the round cap once refinement dries up"
