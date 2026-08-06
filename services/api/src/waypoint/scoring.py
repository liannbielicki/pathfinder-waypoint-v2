"""Canonical score, abstention, and no-action.

Ports the audited Exit-A calibration behavior: group-aware logit
``sigmoid(logit(baseline_g) + alpha + beta*(r - pivot))``, reduction measured
against the no-touch pivot in percentage points, CI from the cluster-robust
beta standard error. A missing artifact or a non-negative beta raises — the
caller abstains rather than fabricate a calibration.
"""

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_Z = 1.959963984540054
MIN_REDUCTION_FLOOR_PP = 1.0  # ported 1.0pp support gate


class BaselineCell(BaseModel):
    baseline: float
    n: int
    confidence: str


class Calibration(BaseModel):
    beta: float
    beta_se: float
    alpha: float
    pivot: float
    calibrated_range: tuple[float, float]
    global_baseline: float
    baselines: dict[str, BaselineCell]
    subtype_version: str


def load_calibration(path: Path) -> Calibration:
    calibration = Calibration.model_validate(json.loads(path.read_text()))
    if calibration.beta >= 0:
        raise ValueError(
            "calibration beta must be negative (higher reaction -> lower churn); "
            f"got {calibration.beta}"
        )
    return calibration


class CandidateScore(BaseModel):
    reduction_pp: float | None
    ci_lower_pp: float | None
    ci_upper_pp: float | None
    mean_reaction: float | None
    in_calibrated_range: bool
    baseline_confidence: str
    calibration_version: str
    abstained: bool = False
    abstain_reason: str | None = None


class Winner(BaseModel):
    candidate_id: str
    score: CandidateScore


class NoAction(BaseModel):
    reason: Literal["no_candidate_cleared_floor", "all_candidates_abstained"]


def _churn(baseline: float, alpha: float, beta: float, r: float, pivot: float) -> float:
    return 1.0 / (1.0 + math.exp(-(math.log(baseline / (1 - baseline)) + alpha
                                   + beta * (r - pivot))))


def score_candidate(
    reactions: list[float], cell: str, calibration: Calibration
) -> CandidateScore:
    version = calibration.subtype_version
    if not reactions:
        return CandidateScore(
            reduction_pp=None, ci_lower_pp=None, ci_upper_pp=None, mean_reaction=None,
            in_calibrated_range=False, baseline_confidence="none",
            calibration_version=version, abstained=True, abstain_reason="no_reactions",
        )
    baseline_cell = calibration.baselines.get(cell)
    if baseline_cell is not None:
        baseline, confidence = baseline_cell.baseline, baseline_cell.confidence
    else:
        baseline, confidence = calibration.global_baseline, "global"

    r = sum(reactions) / len(reactions)
    lo, hi = calibration.calibrated_range
    anchor = _churn(baseline, calibration.alpha, calibration.beta,
                    calibration.pivot, calibration.pivot)

    def reduction(beta: float) -> float:
        return (anchor - _churn(baseline, calibration.alpha, beta, r, calibration.pivot)) * 100

    # A more negative beta gives a larger reduction for r > pivot and a more
    # negative one for r < pivot, so the CI bounds must be ordered explicitly.
    bounds = sorted([
        reduction(calibration.beta + _Z * calibration.beta_se),
        reduction(calibration.beta - _Z * calibration.beta_se),
    ])
    return CandidateScore(
        reduction_pp=reduction(calibration.beta),
        ci_lower_pp=bounds[0],
        ci_upper_pp=bounds[1],
        mean_reaction=r,
        in_calibrated_range=lo <= r <= hi,
        baseline_confidence=confidence,
        calibration_version=version,
    )


def select_winner(scores: dict[str, CandidateScore]) -> Winner | NoAction:
    """Highest supported reduction wins; nothing supported is an honest no-action."""
    live = {cid: s for cid, s in scores.items() if not s.abstained}
    if not live:
        return NoAction(reason="all_candidates_abstained")
    supported = {
        cid: s for cid, s in live.items()
        if s.reduction_pp is not None and s.ci_lower_pp is not None
        and s.reduction_pp >= MIN_REDUCTION_FLOOR_PP and s.ci_lower_pp > 0
    }
    if not supported:
        return NoAction(reason="no_candidate_cleared_floor")
    best = max(supported, key=lambda cid: supported[cid].reduction_pp or 0.0)
    return Winner(candidate_id=best, score=supported[best])
