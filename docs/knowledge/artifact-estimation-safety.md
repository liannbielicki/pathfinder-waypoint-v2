# Artifact: Estimation & Safety Constants (verbatim extraction)

Copy-paste-ready reference values pulled verbatim from the Pathfinder codebase
for the rebuild spec. Every value carries a `file:line` citation. This is
extraction, not analysis — values are quoted exactly as they appear in-repo as
of branch `rebuild/planning-docs`. Nothing here is invented.

---

## SPINE A — Estimation

### A.1 Propensity clip bounds (THREE different values, three call sites)

There is no single clip constant. Three independent estimation paths use three
different bounds. Confirm which path a given number came from.

**Live evaluator path (config-driven gate) — `[0.05, 0.95]`**

`data_contracts/evaluator_gates.lock.yaml:35`
```yaml
positivity:
  propensity_clip: [0.05, 0.95]
```
Loaded as a tuple and applied in the diagnostics:

`src/pathfinder/propensity_diagnostics.py:118-119`
```python
    lo, hi = g.propensity_clip
    ps_c = np.clip(ps, lo, hi)
```

**AIPW doubly-robust estimator — `0.025` (symmetric, clipped to `[0.025, 0.975]`)**

`src/pathfinder/aipw_estimator.py:19`
```python
_PROP_CLIP = 0.025
```
`src/pathfinder/aipw_estimator.py:55`
```python
    e = np.clip(e, _PROP_CLIP, 1 - _PROP_CLIP)
```

**Legacy from-scratch AIPW influence fitter — `1e-6` (clipped to `[1e-6, 1-1e-6]`)**

`src/pathfinder/transform.py:34`
```python
    ps = np.clip(prop_clf.predict_proba(X)[:, 1], 1e-6, 1 - 1e-6)
```

> Confirmed live-path clip: the config-gated evaluator path clips propensity to
> **`[0.05, 0.95]`** (`evaluator_gates.lock.yaml:35` -> `propensity_diagnostics.py:118-119`).
> `0.025` is the AIPW estimator's own constant; `1e-6` is the older
> `transform.py` influence fitter. All three coexist.

### A.2 Positivity thresholds (0.20 / 0.50 / 0.10 / >=30)

Threshold values live in the pre-registered lock file, consumed by the gate.

`data_contracts/evaluator_gates.lock.yaml:34-37`
```yaml
positivity:
  propensity_clip: [0.05, 0.95]
  min_common_support_fraction: 0.50
  min_effective_sample_fraction: 0.10
  abstain_below_common_support: 0.20
```

Gate ordering that consumes them (severe -> abstain, then fallback):

`src/pathfinder/propensity_diagnostics.py:150-162`
```python
    # Severe positivity failure -> abstain.
    if d.common_support_fraction < g.abstain_below_common_support:
        return GateVerdict(
            "regression_adjusted", "positivity_violation",
            support_level="unsupported", exploration_reason="no_overlap",
        )

    # Positivity failure -> fallback.
    if (
        d.common_support_fraction < g.min_common_support_fraction
        or d.effective_sample_fraction < g.min_effective_sample_fraction
    ):
        return GateVerdict("regression_adjusted", "positivity_violation")
```

The `>=30` per-arm minimum is the **per-cell positivity gate** default (a
separate, coarser support check from the config gate above):

`src/pathfinder/positivity.py:12-13`
```python
def supported_arms_for_cell(extract_dir: str | Path, cell_id: str, *,
                            min_treated: int = 30, min_untreated: int = 30) -> list[dict]:
```
`src/pathfinder/positivity.py:28`
```python
        if n_treated >= min_treated and n_untreated >= min_untreated:
```

Note: the config gate's data-adequacy arm minimums are LARGER than 30:

`data_contracts/evaluator_gates.lock.yaml` (data_adequacy block)
```yaml
data_adequacy:
  min_treated_within_cell: 120
  min_untreated_within_cell: 240
  min_nondegenerate_covariates: 4            # PLACEHOLDER — calibrate before lock.
```

### A.3 AIPW influence-function form

Two implementations, identical DR influence-curve form.

`src/pathfinder/aipw_estimator.py:168-172` (cross-fit path)
```python
            psi[te_idx] = (
                (m1 - m0)
                + Ate * (Yte - m1) / e
                - (1 - Ate) * (Yte - m0) / (1 - e)
            )
```
Point estimate + influence-curve SE, z fixed at 1.959963984540054:

`src/pathfinder/aipw_estimator.py:175-177`
```python
    ate = float(np.mean(psi))
    se = float(np.std(psi, ddof=1) / np.sqrt(n)) if n > 1 else float("inf")
    z = 1.959963984540054
```

`src/pathfinder/transform.py:45-49` (legacy fitter, same form)
```python
    psi = (
        mu1 - mu0
        + t * (y - mu1) / ps
        - (1 - t) * (y - mu0) / (1 - ps)
    )
```

Estimator versioning:
`src/pathfinder/aipw_estimator.py:17` -> `_VERSION = "aipw_dr_001"`;
degraded (single in-sample fit) fallback tags `aipw_dr_001_degraded`
(`aipw_estimator.py:159`).

### A.4 The 9-covariate contract (exact list)

`src/pathfinder/covariate_contract.py:11-21`
```python
BUILDABLE_COVARIATES = [
    "mrr_at_enrollment",
    "features_active_count",
    "org_size_bucket",
    "trade_bucket",
    "action_count_7d",
    "action_count_30d",
    "email_open_rate_30d",
    "sms_response_rate_30d",
    "days_since_recent_human_touch",
]
```
Count = **9**.

Supporting sub-lists:

`src/pathfinder/covariate_contract.py:24`
```python
RATE_COVARIATES = ["email_open_rate_30d", "sms_response_rate_30d"]
```
`src/pathfinder/covariate_contract.py:27-31`
```python
MISSING_INDICATOR_COVARIATES = [
    "email_open_rate_30d",
    "sms_response_rate_30d",
    "days_since_recent_human_touch",
]
```
`src/pathfinder/covariate_contract.py:33`
```python
DROPPED_COVARIATES = ["tenure_months", "usage_trend_30d", "health_index"]
```

---

## SPINE B — Calibration / Sensitivity

### B.1 What "frozen" means (JSON artifact, loaded network-free)

`src/pathfinder/frozen_calibration.py:20-22`
```python
# repo root: frozen_calibration.py -> pathfinder -> src -> root
_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CALIBRATION_PATH = _ROOT / "data/v1l/frozen/reaction_churn_calibration.json"
```
`src/pathfinder/frozen_calibration.py:43-44`
```python
    path = Path(path) if path is not None else DEFAULT_CALIBRATION_PATH
    data = json.loads(path.read_text())
```
Missing artifact raises rather than fabricating a calibration — module docstring,
`frozen_calibration.py:9-10`: "A missing or unreadable artifact raises — the
caller then abstains (``reduction=None``) rather than fabricate a calibration."

### B.2 Holdout selection rule (seeded 50/50, most-negative ci_upper, winner's-curse over 12 checkpoints)

12-checkpoint winner's-curse framing — module docstring
`src/pathfinder/holdout_selection.py:1-8`
```python
"""Held-out selection (spec §2.E): pick the winner on one fold, estimate the
committed number on a disjoint fold — so the reported effect is not inflated by
selecting on the same data (winner's curse), compounded over 12 checkpoints.

Sign convention: churn reduction is a NEGATIVE churn effect; "better" = more
negative. We pick the cohort whose conservative bound (ci_upper, the bound
nearest the null) is most negative — i.e. even pessimistically still reduces churn.
"""
```
Seeded 50/50 split:

`src/pathfinder/holdout_selection.py:17-20`
```python
def split_panel(panel: pd.DataFrame, seed: int = 0, frac: float = 0.5):
    rng = np.random.default_rng(seed)
    mask = rng.random(len(panel)) < frac
    return panel[mask].copy(), panel[~mask].copy()
```
Most-negative-ci_upper winner selection:

`src/pathfinder/holdout_selection.py:32-36`
```python
        # churn reduction = negative effect; conservative bound nearest null is ci_upper.
        # "better" = more negative ci_upper, so score = -ci_upper (higher is better).
        score = -r["ci_upper"]
        if best is None or score > best[0]:
            best = (score, sig, r["ci_lower"])
```

### B.3 VanderWeele-Ding E-value formula

`src/pathfinder/sensitivity.py:16-21`
```python
def evalue_from_rr(rr: float) -> float:
    if rr <= 0:
        raise ValueError("rr must be positive")
    if rr < 1:
        rr = 1.0 / rr
    return rr + math.sqrt(rr * (rr - 1.0))
```
i.e. `E = rr + sqrt(rr * (rr - 1))`. Baseline risk constant:
`sensitivity.py:13` -> `_BASELINE_RISK = 0.5`. Conservative test uses the CI
bound nearest the null (`ci_upper`):

`src/pathfinder/sensitivity.py:30-37`
```python
def survives_sensitivity(mean_delta: float, ci_upper: float, threshold: float,
                         baseline: float = _BASELINE_RISK) -> bool:
    # Use the CI bound NEAREST the null (largest, i.e. least protective, churn delta).
    bound = ci_upper  # for a churn-reducing effect (<0), ci_upper is closest to 0
    if bound >= 0:
        return False  # CI touches/crosses null -> not robust
    rr = _delta_to_rr(bound, baseline)
    return evalue_from_rr(rr) >= threshold
```

### B.4 Sign-forcing line (shipped magnitude is untrustworthy)

`scripts/build_cards_reaction_churn_calibration.py:156`
```python
    directional_slope = -abs(transform.slope)
```
Rationale as written in-repo (`build_cards_reaction_churn_calibration.py:148-155`):
reaction<->churn has "corr ~ +0.11, predicts worse than the mean", the fitted
slope's sign is noise, so the sign is forced negative to make the RANKING
direction intuitive. The artifact self-labels magnitude as not trustworthy:

`scripts/build_cards_reaction_churn_calibration.py:164-165`
```python
        "slope_sign_forced_negative": True,
        "fitted_slope_before_sign_force": transform.slope,
```
`scripts/build_cards_reaction_churn_calibration.py:175-177`
```python
            "note": "P2.5 re-fit for the cards path. Slope SIGN forced negative "
                    "(directional prior) because reaction<->churn is uncorrelated here; "
                    "magnitude/CI are NOT trustworthy — directional/ranking use only.",
```
The frozen loader reads the flag through into the transform:
`src/pathfinder/frozen_calibration.py:45` -> `sign_forced = bool(data.get("slope_sign_forced_negative", False))`.

---

## SAFETY CONSTANTS

### S.1 ENV_ALLOWLIST (exact set)

`src/pathfinder/sandbox_child.py:46`
```python
ENV_ALLOWLIST = frozenset({"PATH", "PYTHONPATH", "LANG", "LC_ALL", "PF_SANDBOX"})
```
Credential-hint substrings (unconditional fail):

`src/pathfinder/sandbox_child.py:50-53`
```python
_CREDENTIAL_HINTS = (
    "SNOWFLAKE", "PASSWORD", "SECRET", "TOKEN", "KEY",
    "CRED", "AWS", "AZURE", "GOOGLE",
)
```

### S.2 Import-guard env check and its `startswith("__")` bypass

`src/pathfinder/sandbox_child.py:78-82`
```python
    for k in os.environ:
        if any(h in k.upper() for h in _CREDENTIAL_HINTS):
            _fail(result_path, f"credential_like_env_key:{k}")
        if k not in ENV_ALLOWLIST and not k.startswith("__"):
            _fail(result_path, f"env_key_not_allowlisted:{k}")
```
The `and not k.startswith("__")` clause is the bypass: any env key beginning
with `__` (e.g. macOS `__CF_USER_TEXT_ENCODING`) is admitted even though it is
not in `ENV_ALLOWLIST`. The credential-hint check on the line above still runs
unconditionally, so a `__`-prefixed key carrying a hint substring is still
blocked.

Static import guard re-run inside the child (defence in depth):

`src/pathfinder/sandbox_child.py:205-207`
```python
    violations = check_strategy_file(Path(strategy_file))
    if violations:
        _fail(result_path, "import_guard:" + ";".join(violations))
```

### S.3 `_scrubbed_env` (sandbox.py)

`src/pathfinder/sandbox.py:51-55`
```python
def _scrubbed_env() -> "dict[str, str]":
    """Build the child env: only allowlisted keys, plus the PF_SANDBOX marker."""
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env["PF_SANDBOX"] = "1"
    return env
```
Imports the same allowlist definition (`sandbox.py:30` ->
`from pathfinder.sandbox_child import ENV_ALLOWLIST`) and is passed to the child
via `env=_scrubbed_env()` at `sandbox.py:120`.

### S.4 policy_engine.py — fail-closed gate ordering / BLOCK precedence

Layer-2 gate returns BLOCK whenever eligibility cannot be positively confirmed.
Order of checks (first match wins):

`src/pathfinder/policy_engine.py:142-168`
```python
    # Fail-closed: no eligibility row ⇒ ineligible.
    if profile is None:
        return PolicyDecision("BLOCK", "no_eligibility_row")
    if profile.get("do_not_contact"):
        return PolicyDecision("BLOCK", "do_not_contact")
    if profile.get("opted_out_at_utc"):
        return PolicyDecision("BLOCK", "opted_out")

    # Multi-channel consent: check candidate's action.channel against profile.
    action_channel = candidate.get("action", {}).get("channel", "email")
    if action_channel == "none":
        # No-op candidate: no send, so consent/eligibility do not gate it.
        if candidate.get("approval", {}).get("requires_human"):
            return PolicyDecision("REVIEW_REQUIRED", "requires_human")
        return PolicyDecision("ALLOW", None)
    if action_channel != "email":
        return PolicyDecision("BLOCK", f"channel_{action_channel}_not_allowed_phase1")

    # Phase 1: email-only. Unknown/denied/missing consent ⇒ ineligible.
    if profile.get("email_consent_status") != "granted":
        return PolicyDecision("BLOCK", "email_consent_not_granted")
    if "email" not in profile.get("eligible_channels", []):
        return PolicyDecision("BLOCK", "channel_email_not_eligible")
    # Human-approval-required candidates are never auto-allowed (never sent).
    if candidate.get("approval", {}).get("requires_human"):
        return PolicyDecision("REVIEW_REQUIRED", "requires_human")
    return PolicyDecision("ALLOW", None)
```
Verdict type (`policy_engine.py:120`):
```python
PolicyVerdict = Literal["ALLOW", "REVIEW_REQUIRED", "BLOCK"]
```
Phase-1 allowed channel set (`policy_engine.py:31`):
```python
PHASE1_ALLOWED_CHANNELS: frozenset[str] = frozenset({"email"})
```

### S.5 secret_scrub — MULTIPLE implementations (note)

There are several scrub/redaction sites; they are not one shared helper.

1. `src/pathfinder/action_console/secret_scrub.py` — the primary value-based
   scrubber. Marker set `secret_scrub.py:25-35`:
   ```python
   SECRET_ENV_MARKERS: frozenset[str] = frozenset(
       {
           "SECRET",
           "TOKEN",
           "PASSWORD",
           "CREDENTIAL",
           "PRIVATE_KEY",
           "KEY",
           "SNOWFLAKE_",
       }
   )
   ```
   Placeholder `_PLACEHOLDER = "***"` (`secret_scrub.py:37`); min auto-discovered
   secret length `_MIN_SECRET_LEN = 8` (`secret_scrub.py:43`); public entry
   `def scrub(text, *, secrets=None)` (`secret_scrub.py:57`).

2. `src/pathfinder/sandbox.py:51` — `_scrubbed_env()` (env allowlist filter, see S.3).

3. `src/pathfinder/action_console/n8n_org_context.py:295` —
   `_scrub_message(text, *, token, url)`, which NESTS `scrub()` calls because
   `scrub(text, secrets=[...])` REPLACES the env sweep rather than adding to it
   (`n8n_org_context.py:299-307`).

4. `src/pathfinder/action_console/persona_response.py:254` —
   `_scrub_persona_content(text)`.

Caveat for the rebuild: `secret_scrub.scrub()`'s `secrets=` argument REPLACES
(does not augment) the environment sweep — every caller passing explicit secrets
must re-add the env sweep or silently lose env-marker scrubbing
(`n8n_org_context.py:299-301`).
