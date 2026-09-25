"""Pure win-stay/lose-shift loop policy. No I/O; the pipeline owns persistence.

One reactive rule, one challenger per round: a win refines the same mechanism,
a loss (after PATIENCE tries) forces an untried one. Stop is mechanical.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

_KEYS = frozenset(
    {
        "MAX_ROUNDS",
        "MAX_NO_IMPROVE",
        "PATIENCE",
        "KEEP_DELTA_PP",
        "KEEP_DELTA_REACTION",
        "WIN_THRESHOLD_PP",
        "CANDIDATE_COUNT",
        "TIE_MARGIN",
        "WARM_START_THRESHOLD",
    }
)

# The loop is decided by the CHEAP SCREENING panel, not the held-out 5-persona
# final: pipeline seats 3 personas for the round screen. Personas answer on an
# integer 3-7 scale, so the screen's mean moves in steps of 1/3 ≈ 0.3333 and the
# widest conceivable improvement is 7 - 3. Everything about the win bar below is
# measured on THIS lattice; the final panel's 0.2 lattice never decides a round.
SCREEN_PANEL_SIZE: Literal[3] = 3
REACTION_SPAN = 4.0
# A mean of thirds has no exact binary form, so a bar arithmetic like
# best + delta lands a hair above the honest value and a bare >= rejects a real
# improvement. The bar sits on a lattice of ~0.33, so slack this far below one
# step only ever forgives representation error, never a real gap. The pp
# comparison deliberately gets NO such slack: pp is continuous, not a lattice,
# so there is no representation error to forgive and 1e-9 there would be a
# silent widening of the bar rather than a correction. The asymmetry is the
# point.
_REACTION_EPS = 1e-9

# Safety ceiling on the per-round idea batch: an operator typo (or a bad
# default merge) must never turn one round into an unbounded generation bill.
MAX_CANDIDATE_COUNT = 10


@dataclass(frozen=True)
class LoopConfig:
    max_rounds: int
    max_no_improve: int
    patience: int
    keep_delta_pp: float
    keep_delta_reaction: float
    win_threshold_pp: float
    candidate_count: int
    tie_margin: float
    warm_start_threshold: float

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LoopConfig:
        unknown = set(data) - _KEYS
        if unknown:
            raise ValueError(f"unknown loop config keys: {sorted(unknown)}")
        merged = {**DEFAULT_LOOP_CONFIG.to_dict(), **dict(data)}
        config = cls(
            max_rounds=int(merged["MAX_ROUNDS"]),
            max_no_improve=int(merged["MAX_NO_IMPROVE"]),
            patience=int(merged["PATIENCE"]),
            keep_delta_pp=float(merged["KEEP_DELTA_PP"]),
            keep_delta_reaction=float(merged["KEEP_DELTA_REACTION"]),
            win_threshold_pp=float(merged["WIN_THRESHOLD_PP"]),
            candidate_count=int(merged["CANDIDATE_COUNT"]),
            tie_margin=float(merged["TIE_MARGIN"]),
            warm_start_threshold=float(merged["WARM_START_THRESHOLD"]),
        )
        if config.patience < 1:
            raise ValueError("PATIENCE must be >= 1")
        if config.candidate_count < 1:
            raise ValueError("CANDIDATE_COUNT must be a positive integer")
        if config.candidate_count > MAX_CANDIDATE_COUNT:
            raise ValueError(f"CANDIDATE_COUNT must be <= {MAX_CANDIDATE_COUNT}")
        if not 0 <= config.tie_margin <= 1:
            raise ValueError("TIE_MARGIN must be between 0 and 1 (ranker scores are 0-1)")
        if not 0 <= config.warm_start_threshold <= 1:
            raise ValueError("WARM_START_THRESHOLD must be between 0 and 1 (similarity is 0-1)")
        if (
            min(
                config.max_rounds,
                config.max_no_improve,
                config.keep_delta_pp,
                config.keep_delta_reaction,
                config.win_threshold_pp,
            )
            < 0
        ):
            raise ValueError("loop config values must be >= 0")
        if config.keep_delta_pp > config.win_threshold_pp:
            raise ValueError("KEEP_DELTA_PP must be <= WIN_THRESHOLD_PP")
        # No lower bound beyond >= 0, deliberately. The structural minimum is
        # two steps of the SEATED panel, applied in is_win — a value below it is
        # inert, not dangerous, and the shipped default (0.4, below 2/3) is
        # itself such a value, so rejecting them would reject the default. This
        # class only raises; there is no warning channel to route an "inert
        # setting" notice through, and inventing one for a knob that cannot
        # loosen the bar is not worth the machinery. The UI help text says so
        # where the operator can actually read it.
        # The panel reaction scale is 3-7, so the widest possible improvement is
        # 4.0. A bar at or above the span can never be cleared: that is an
        # operator typo (0.4 -> 4.0), not a strict search, so reject it loudly.
        if config.keep_delta_reaction >= REACTION_SPAN:
            raise ValueError(f"KEEP_DELTA_REACTION must be < {REACTION_SPAN} (the 3-7 scale span)")
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "MAX_ROUNDS": self.max_rounds,
            "MAX_NO_IMPROVE": self.max_no_improve,
            "PATIENCE": self.patience,
            "KEEP_DELTA_PP": self.keep_delta_pp,
            "KEEP_DELTA_REACTION": self.keep_delta_reaction,
            "WIN_THRESHOLD_PP": self.win_threshold_pp,
            "CANDIDATE_COUNT": self.candidate_count,
            "TIE_MARGIN": self.tie_margin,
            "WARM_START_THRESHOLD": self.warm_start_threshold,
        }


DEFAULT_LOOP_CONFIG = LoopConfig(
    max_rounds=10,
    max_no_improve=3,
    patience=1,
    # Secondary guard only, since the pp scale cannot carry the primary bar:
    # what one reaction step is worth in pp depends on the operating point AND
    # the cell baseline (0.014pp to 1.791pp for a 0.2 step across the artifact's
    # 166 cells, a 128x spread; a 1/3 screening step is 1.42pp at a mean of 3.4
    # and 0.80pp at 5.4 on the global baseline alone), so NO fixed pp value
    # sits above the measurement's own
    # resolution. Operators still tune this and the UI renders it; it keeps a
    # win from being a rounding artifact once the reaction bar below is cleared.
    keep_delta_pp=0.6,
    # The real bar, stated on the lattice the measurement actually moves on. The
    # deciding panel seats SCREEN_PANEL_SIZE personas, so one step is 1/3 and
    # two is 2/3; 0.4 is chosen from that RANGE, not as a multiple of a step:
    # it rejects one persona moving one point (0.3333, which is noise) and
    # admits two (0.6667). test_loop pins it inside (1/3, 2/3] so this fails
    # loudly if the screen panel is ever resized past where that still holds.
    # NOTE this can only TIGHTEN: is_win floors the bar at two steps of the
    # actually-seated panel, so a full 3-persona screen decides at 0.667 and any
    # value below that (including this default) is inert. See is_win for why
    # that is deliberate. On a discrete lattice it costs nothing — 0.4 and 0.667
    # admit exactly the same improvements on a 3-panel.
    keep_delta_reaction=0.4,
    win_threshold_pp=15.0,
    candidate_count=3,
    tie_margin=0.05,
    warm_start_threshold=0.75,
)


@dataclass(frozen=True)
class LoopState:
    round: int = 0  # rounds completed
    best_score: float | None = None
    best_candidate_id: str | None = None
    current_candidate_id: str | None = None
    # The champion's screen-panel mean, carried so the next round can compare on
    # the lattice. None means "not known": either no champion yet, or the
    # champion was crowned by a pre-KEEP_DELTA_REACTION round whose ledger row
    # predates the value being persisted (see replay) — both fall back to pp.
    # ponytail: this rides in the evolve_rounds `ranking` JSON rather than a
    # column, because a migration was off the table. One reader
    # (_round_mean_reaction) with one documented fallback — but it is weaker
    # than a column, and any future writer of `ranking` must not drop the key.
    # Upgrade path: add a mean_reaction column and backfill from the JSON.
    best_reaction: float | None = None
    current_mechanism: str | None = None
    tries_on_current: int = 0
    dry_mechanisms: int = 0
    blocked_rounds: int = 0
    evaluation_unavailable: bool = False
    tried_mechanisms: tuple[str, ...] = ()


def mechanism_key(value: str) -> str:
    """Stable identity for model-authored mechanism labels."""
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def next_mode(state: LoopState, config: LoopConfig) -> Literal["cold", "refine", "shift"]:
    if state.current_mechanism is None:
        return "shift" if state.tried_mechanisms else "cold"
    return "shift" if state.tries_on_current >= config.patience else "refine"


def is_win(
    state: LoopState,
    score_pp: float | None,
    config: LoopConfig,
    floor_pp: float,
    mean_reaction: float | None = None,
    panel_size: int | None = None,
) -> bool:
    """A challenger dethrones the champion only on the measurement's own lattice.

    The pp figure is a calibrated transform of the panel mean, with a variable
    resolution: one screening step is worth anywhere from 0.014pp to 1.791pp
    depending on the operating point and the cell baseline. A fixed pp bar is
    therefore either below the noise floor (a single persona changing its mind
    crowns a champion) or absurdly strict, and no single value avoids both. So
    the primary bar is stated in reaction units, where the step size is a
    property of the panel, and pp is demoted to a support floor plus a
    secondary guard.

    `panel_size` is the number of personas ACTUALLY SEATED for this screen.
    select_panel degrades rather than raising: a thin persona pool seats fewer
    than SCREEN_PANEL_SIZE and only flags `degraded`, which produces a handoff
    disclaimer and nothing more. A 2-persona panel moves in steps of 0.5 and a
    1-persona panel in steps of 1.0 — both clear a 0.4 bar, so the exact
    single-persona regression this rule exists to close would return on the
    runs that are already the least trustworthy. Scaling the bar with the
    seated panel closes that instead of rejecting the run, which would turn
    every thin-segment Pro into an abstention: a product decision nobody made.
    """
    if score_pp is None:
        return False
    if state.best_score is None:
        # First win: nothing to improve on, so only the absolute support gate
        # applies — unchanged behaviour.
        return score_pp >= floor_pp
    if score_pp < floor_pp:
        # An unsupported score can never be a champion, however it compares to
        # the incumbent: the floor is about the evidence, not the delta.
        # Redundant under today's constants — best_score was itself set by a win
        # that cleared the floor, and keep_delta_pp is validated >= 0, so a
        # challenger beating it is already above the floor. Kept as a defensive
        # guard so a future floor RAISE cannot leave a grandfathered champion's
        # successors below the new floor.
        return False
    if mean_reaction is None or state.best_reaction is None:
        # Pre-change ledger row (or a score with no panel mean): the reaction
        # bar is unknowable, so fall back to the pp rule that crowned the
        # incumbent in the first place rather than guessing. NOT an edge case —
        # every run resumed across this change takes this path until its next
        # win re-establishes best_reaction.
        return score_pp >= state.best_score + config.keep_delta_pp
    # The bar has a STRUCTURAL minimum of two steps of the seated panel, and
    # KEEP_DELTA_REACTION can only tighten past it. This is one-directional on
    # purpose: "one persona-step never wins" is the invariant this whole rule
    # exists to express, and a config value that could switch it off — an
    # operator typing 0.3 on a 3-panel would re-admit a single 0.3333 step —
    # would be a footgun wearing the label of the thing it disables.
    #
    # So any value below 2 / panel_size is INERT BY DESIGN, not a bug. That
    # costs nothing in expressiveness: the lattice is discrete, so on a full
    # 3-panel every value in (1/3, 2/3] decides identically (the only reachable
    # improvements are 0.333, 0.667, 1.0) and the shipped 0.4 is exactly as
    # strict as 0.667 would be. The knob differs from the structural minimum
    # only above it, where it genuinely tightens the search.
    #
    # ponytail: the bar is a hardcoded constant lifted to the seated panel's
    # two-step size, not derived from panel size in general. Upgrade path: if
    # panel size becomes an operator knob, express the config in STEPS and
    # multiply by 1/panel_size here.
    bar = config.keep_delta_reaction
    if panel_size:
        bar = max(bar, 2.0 / panel_size)
    return (
        mean_reaction - state.best_reaction >= bar - _REACTION_EPS
        and score_pp >= state.best_score + config.keep_delta_pp
    )


def apply_round(
    state: LoopState,
    *,
    mechanism: str,
    candidate_id: str,
    score_pp: float | None,
    outcome: str,
    config: LoopConfig,
    mode: str,
    also_tried: Sequence[str] = (),
    mean_reaction: float | None = None,
) -> LoopState:
    # also_tried: the round's other generated mechanisms. They were proposed,
    # critiqued and ranked, so a later SHIFT must forbid them or it re-proposes
    # and re-pays for the same losers every round. Only `mechanism` drives the
    # win / patience / dry bookkeeping below.
    tried = state.tried_mechanisms
    for name in (mechanism, *also_tried):
        key = mechanism_key(name)
        if key and key not in tried:
            tried = (*tried, key)
    if outcome == "lose" and score_pp is None:
        outcome = "unavailable"  # legacy ledger rows without a usable screen
    if outcome in {"suppressed", "unavailable"}:
        # Nothing was selected this round. Preserve the actual current idea;
        # refining an arbitrary suppressed/unranked row corrupts live and replay
        # state. Neither result is evidence that a scored mechanism went dry.
        return replace(
            state,
            round=state.round + 1,
            blocked_rounds=state.blocked_rounds + (outcome == "suppressed"),
            evaluation_unavailable=outcome == "unavailable",
            tried_mechanisms=tried,
        )
    if outcome == "win":
        return replace(
            state,
            round=state.round + 1,
            best_score=score_pp,
            # None when replaying a row written before the value was persisted;
            # is_win then falls back to pp until the next win restores it.
            best_reaction=mean_reaction,
            best_candidate_id=candidate_id,
            current_candidate_id=candidate_id,
            current_mechanism=mechanism,
            tries_on_current=0,
            dry_mechanisms=0,
            evaluation_unavailable=False,
            tried_mechanisms=tried,
        )
    # The loop DECIDED this round's intent and told the model (see next_mode /
    # evolve_prompt). Re-deriving it by string-comparing the model's free-text
    # mechanism label is recovering information we already had, through the one
    # channel the model does not hold stable: it rewords the label every round,
    # so `same` was always False, tries pinned at 1, and dry never advanced.
    # A win resets tries below regardless, and a losing refine round spent an
    # attempt on this mechanism's neighborhood whichever candidate lost — so
    # the mode alone is sufficient and the winner's slot is irrelevant.
    tries = state.tries_on_current + 1 if mode == "refine" else 1
    dry = state.dry_mechanisms + (1 if tries >= config.patience else 0)
    return replace(
        state,
        round=state.round + 1,
        current_mechanism=mechanism,
        current_candidate_id=candidate_id,
        tries_on_current=tries,
        dry_mechanisms=dry,
        evaluation_unavailable=False,
        tried_mechanisms=tried,
    )


def stop_reason(state: LoopState, config: LoopConfig) -> str | None:
    if state.best_score is not None and state.best_score > config.win_threshold_pp:
        return "win_threshold"
    if state.evaluation_unavailable:
        return "evaluation_unavailable"
    if state.dry_mechanisms >= config.max_no_improve:
        return "no_improve_exhausted"
    if state.blocked_rounds >= config.max_no_improve:
        return "blocked_budget_exhausted"
    if state.round >= config.max_rounds:
        return "round_cap"
    return None


class RoundLike(Protocol):
    mechanism: str
    candidate_id: str | None
    score_pp: float | None
    outcome: str
    ranking: dict[str, Any]


def _round_mean_reaction(ranking: dict[str, Any]) -> float | None:
    """The challenger's panel mean, recovered from the round's audit trail.

    `evolve_rounds` persists score_pp but not mean_reaction, and a migration is
    off the table — so the value rides in the existing JSON `ranking` column,
    written by pipeline._ranking_evidence. Rows written before that change have
    no key: return None and let is_win fall back to the pp comparison.

    Do NOT reconstruct this by inverting score_pp. It is algebraically possible
    only while every Pro shares the one global baseline; the moment per-cell
    baselines resolve, the inverse silently returns a wrong reaction.
    """
    value = ranking.get("mean_reaction")
    return float(value) if isinstance(value, int | float) else None


def _round_also_tried(ranking: dict[str, Any]) -> list[str]:
    """The round's non-challenger mechanisms, recovered from its audit trail.
    Live execution feeds these to apply_round as `also_tried`; replay must too,
    or a resumed SHIFT re-proposes and re-pays for mechanisms already generated,
    critiqued and ranked. Suppressed ideas never reach `order`, but they are
    re-gated for free on re-proposal, so omitting them costs no paid call."""
    generated = ranking.get("generated_mechanisms")
    if isinstance(generated, list):
        return [str(mechanism) for mechanism in generated if mechanism]
    return [o["mechanism"] for o in ranking.get("order", ()) if o.get("mechanism")]


def replay(rounds: Sequence[RoundLike], config: LoopConfig) -> LoopState:
    """Rebuild loop state from the durable ledger — the one recovery code path."""
    state = LoopState()
    for row in rounds:
        ranking = getattr(row, "ranking", None) or {}
        # `mode` is a pure function of (state, config), computed identically at
        # the top of the live round loop — so replay reproduces it exactly and
        # the ledger needs no `mode` column and no migration.
        state = apply_round(
            state,
            mechanism=row.mechanism,
            candidate_id=row.candidate_id or "",
            score_pp=row.score_pp,
            outcome=row.outcome,
            config=config,
            mode=next_mode(state, config),
            also_tried=_round_also_tried(ranking),
            mean_reaction=_round_mean_reaction(ranking),
        )
    return state
