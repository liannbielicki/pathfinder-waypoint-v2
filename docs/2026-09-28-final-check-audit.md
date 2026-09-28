# Final-check audit: eight reported rejections

Source: the eight run summaries supplied on 2026-09-28. The linked Slack PDF
was visible as a file reference but its panel pages were unavailable to this
audit. These are selected failures, not a denominator for a failure rate.

| Org | Winning round | Final reduction (pp) | Implied five-person mean reaction |
|---|---:|---:|---:|
| 16647 | 1 | -0.2 | 4.2 |
| 33607 | 3 | 0.5 | 4.4 |
| 366426 | 1 | -1.7 | 3.8 |
| 426126 | 1 | -0.2 | 4.2 |
| 518117 | 1 | -0.2 | 4.2 |
| 727167 | 1 | -2.5 | 3.6 |
| 845617 | 1 | 0.5 | 4.4 |
| 865027 | 2 | -0.9 | 4.0 |

The implied means reproduce the pasted scores with the committed global
calibration (`reaction_churn_calibration_cards.json`). Every final is below the
1.0 pp support floor; `no_candidate_cleared_floor` is therefore the correct
decision for the evidence supplied. All eight champions arose by round 3,
and every run continued to six or more scored rounds without replacing the
champion. All summaries report zero pre-evaluation blocks and zero unavailable
evaluations. This is a screen-to-final disagreement, not a blocked-idea or
missing-evaluation failure.

The code provides a plausible systematic contributor: every screen uses the
same top-fit three-person panel, while final prefers five previously unseen
personas. Screen selection and final selection therefore sample different
parts of the ordered pool. The supplied summaries do not include persona IDs,
fit/coverage, or reactions, so they cannot establish that the held-out members
were lower fit, that a particular segment caused the drop, or that changing
panel selection would raise real performance. The displayed CI varies the
calibration slope only; it does not quantify persona or judge variation.

Do not lower the final floor, reuse screen personas in final, or optimize
against final reactions from the same run: each would weaken held-out
confirmation. Before changing panel policy, compare screen and final IDs,
fit/coverage, families, individual reactions, and cached model tier for these
runs, then replay an independent set of runs with the proposed policy.
