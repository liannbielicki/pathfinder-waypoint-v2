"""Pure win-stay/lose-shift loop policy. No I/O; the pipeline owns persistence.

One reactive rule, one challenger per round: a win refines the same mechanism,
a loss (after PATIENCE tries) forces an untried one. Stop is mechanical.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

_KEYS = frozenset(
    {"MAX_ROUNDS", "MAX_NO_IMPROVE", "PATIENCE", "KEEP_DELTA_PP", "WIN_THRESHOLD_PP"}
)


@dataclass(frozen=True)
class LoopConfig:
    max_rounds: int
    max_no_improve: int
    patience: int
    keep_delta_pp: float
    win_threshold_pp: float

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
            win_threshold_pp=float(merged["WIN_THRESHOLD_PP"]),
        )
        if config.patience < 1:
            raise ValueError("PATIENCE must be >= 1")
        if (
            min(
                config.max_rounds,
                config.max_no_improve,
                config.keep_delta_pp,
                config.win_threshold_pp,
            )
            < 0
        ):
            raise ValueError("loop config values must be >= 0")
        if config.keep_delta_pp > config.win_threshold_pp:
            raise ValueError("KEEP_DELTA_PP must be <= WIN_THRESHOLD_PP")
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "MAX_ROUNDS": self.max_rounds,
            "MAX_NO_IMPROVE": self.max_no_improve,
            "PATIENCE": self.patience,
            "KEEP_DELTA_PP": self.keep_delta_pp,
            "WIN_THRESHOLD_PP": self.win_threshold_pp,
        }


DEFAULT_LOOP_CONFIG = LoopConfig(
    max_rounds=10,
    max_no_improve=3,
    patience=1,
    keep_delta_pp=0.5,
    win_threshold_pp=15.0,
)


@dataclass(frozen=True)
class LoopState:
    round: int = 0  # rounds completed
    best_score: float | None = None
    best_candidate_id: str | None = None
    current_mechanism: str | None = None
    tries_on_current: int = 0
    dry_mechanisms: int = 0
    tried_mechanisms: tuple[str, ...] = ()


def next_mode(state: LoopState, config: LoopConfig) -> Literal["stay", "shift"]:
    if state.current_mechanism is None:
        return "stay"  # cold start: nothing to shift away from
    return "shift" if state.tries_on_current >= config.patience else "stay"


def is_win(state: LoopState, score_pp: float | None, config: LoopConfig, floor_pp: float) -> bool:
    if score_pp is None:
        return False
    if state.best_score is None:
        return score_pp >= floor_pp
    return score_pp >= state.best_score + config.keep_delta_pp


def apply_round(
    state: LoopState,
    *,
    mechanism: str,
    candidate_id: str,
    score_pp: float | None,
    outcome: str,
    config: LoopConfig,
) -> LoopState:
    tried = state.tried_mechanisms
    if mechanism not in tried:
        tried = (*tried, mechanism)
    if outcome == "win":
        return replace(
            state,
            round=state.round + 1,
            best_score=score_pp,
            best_candidate_id=candidate_id,
            current_mechanism=mechanism,
            tries_on_current=0,
            dry_mechanisms=0,
            tried_mechanisms=tried,
        )
    same = mechanism == state.current_mechanism
    tries = state.tries_on_current + 1 if same else 1
    dry = state.dry_mechanisms + (1 if tries >= config.patience else 0)
    return replace(
        state,
        round=state.round + 1,
        current_mechanism=mechanism,
        tries_on_current=tries,
        dry_mechanisms=dry,
        tried_mechanisms=tried,
    )


def stop_reason(state: LoopState, config: LoopConfig) -> str | None:
    if state.best_score is not None and state.best_score > config.win_threshold_pp:
        return "win_threshold"
    if state.dry_mechanisms >= config.max_no_improve:
        return "no_improve_exhausted"
    if state.round >= config.max_rounds:
        return "round_cap"
    return None


class RoundLike(Protocol):
    mechanism: str
    candidate_id: str | None
    score_pp: float | None
    outcome: str


def replay(rounds: Sequence[RoundLike], config: LoopConfig) -> LoopState:
    """Rebuild loop state from the durable ledger — the one recovery code path."""
    state = LoopState()
    for row in rounds:
        state = apply_round(
            state,
            mechanism=row.mechanism,
            candidate_id=row.candidate_id or "",
            score_pp=row.score_pp,
            outcome=row.outcome,
            config=config,
        )
    return state
