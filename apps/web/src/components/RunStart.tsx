"use client";

import { useEffect, useRef, useState } from "react";
import {
  PENDING_AUDIENCE_QUERY,
  createRun,
  getFleetSettings,
  type RunCreateInput,
  type RunView,
} from "@/lib/api";

interface LoopField {
  key: string;
  label: string;
  help: string;
  min: number;
}

const LOOP_FIELDS: LoopField[] = [
  { key: "MAX_ROUNDS", label: "Max rounds per Pro",
    help: "Hard cap on evolve rounds for each Pro.", min: 0 },
  { key: "MAX_NO_IMPROVE", label: "Dry mechanisms before stopping",
    help: "Stop after this many mechanisms produce no new best.", min: 0 },
  { key: "PATIENCE", label: "Refine attempts per mechanism",
    help: "Tries a mechanism gets before the loop shifts to a new one.", min: 1 },
  { key: "KEEP_DELTA_REACTION", label: "Min improvement to keep (panel reaction)",
    help: "The main keep bar, on the 3-7 panel scale. The screening panel seats 3 personas, so its mean moves in steps of 1/3: one persona changing its mind by a point is 0.33, two is 0.67. This setting can only tighten — the bar never falls below two steps of the panel actually seated (0.67 on a full one, more on a degraded one), so one persona alone can never win and anything under 0.67 here has no effect.",
    min: 0 },
  { key: "KEEP_DELTA_PP", label: "Min improvement to keep (pp)",
    help: "Secondary guard: a challenger that clears the reaction bar must also beat the best by this margin in points.",
    min: 0 },
  { key: "WIN_THRESHOLD_PP", label: "Stop-early reduction (pp)",
    help: "A reduction above this ends the search as a success.", min: 0 },
  { key: "CANDIDATE_COUNT", label: "Ideas per round",
    help: "How many candidate ideas are generated and ranked each round.", min: 1 },
  { key: "TIE_MARGIN", label: "Near-tie evidence margin (0-1)",
    help: "Labels close ranker scores in the audit evidence; every candidate is persona-screened.",
    min: 0 },
  { key: "WARM_START_THRESHOLD", label: "Warm-start similarity (0-1)",
    help: "How similar a past validated winner's Pro must be before its mechanism seeds round 1.",
    min: 0 },
];

// Same vocabulary as waypoint.models.CHANNELS; the API rejects anything else.
const CHANNELS = ["sms", "email", "call"] as const;
type Channel = (typeof CHANNELS)[number];

export function RunStart({ onStarted }: { onStarted: (run: RunView) => void }) {
  const [proIds, setProIds] = useState("");
  // Autofilled with "now" (second precision, UTC) — the operator can overwrite
  // it when backfilling a run from an older audience pull.
  const [audienceRun, setAudienceRun] = useState(
    () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
  );
  const [channels, setChannels] = useState<Channel[]>([...CHANNELS]);
  const [journeyWindow, setJourneyWindow] = useState("churn_risk_open");
  const [contextSource, setContextSource] = useState<"standard" | "staging">("standard");
  const [includeFeaturesNotInCurrentPlan, setIncludeFeaturesNotInCurrentPlan] = useState(false);
  const [modelTier, setModelTier] = useState<"fast" | "deep">("deep");
  const [models, setModels] = useState({ fast: "fast", deep: "deep" });
  const [stagingAvailable, setStagingAvailable] = useState(false);
  const [stagingContext, setStagingContext] = useState<Awaited<ReturnType<typeof getFleetSettings>>["staging_context"]>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [defaults, setDefaults] = useState<Record<string, number> | null>(null);
  const [fleetCap, setFleetCap] = useState<number | null>(null);
  const [defaultsError, setDefaultsError] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [confirms, setConfirms] = useState<Record<string, string>>({});
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    let cancelled = false;
    getFleetSettings()
      .then((settings) => {
        if (cancelled) return;
        setDefaults(settings.loop_defaults);
        setFleetCap(settings.max_in_flight_llm_calls);
        setStagingAvailable(settings.staging_context_available);
        setStagingContext(settings.staging_context);
        setModels(settings.models);
        // Only fields the server actually advertises get a value. A key the
        // API does not know (an older API than this UI) would otherwise render
        // as the string "undefined" — an empty number input that reads as
        // edited, demands a confirm, and is then POSTed as an unknown key.
        setValues(Object.fromEntries(
          LOOP_FIELDS.filter((f) => f.key in settings.loop_defaults)
            .map((f) => [f.key, String(settings.loop_defaults[f.key])]),
        ));
      })
      .catch(() => {
        if (!cancelled) setDefaultsError(true);
      });
    return () => { cancelled = true; };
  }, []);

  const ids = proIds.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  // Fields this UI knows about but the API does not report a default for. They
  // are hidden rather than shown as dead inputs — but the operator is told, so
  // a silently shorter form is never mistaken for the whole set of controls.
  const omitted =
    defaults === null
      ? []
      : LOOP_FIELDS.filter((f) => !(f.key in defaults)).map((f) => f.key);
  // A field the server has no default for can never be "changed": there is
  // nothing to change it from, and sending it would be an unknown-key 422.
  const changed = (key: string) =>
    defaults !== null && key in defaults && Number(values[key]) !== defaults[key];

  function focusField(id: string) {
    formRef.current?.querySelector<HTMLElement>(`#${id}`)?.focus();
  }

  function validateLoopControls(): string | null {
    if (defaults === null) return null; // controls disabled; no overrides possible
    for (const field of LOOP_FIELDS) {
      if (!changed(field.key)) continue;
      const value = Number(values[field.key]);
      if (Number.isNaN(value) || value < field.min) {
        focusField(`loop-${field.key}`);
        return `${field.label} must be at least ${field.min}.`;
      }
      if (confirms[field.key] !== "confirm") {
        focusField(`confirm-${field.key}`);
        return `Type confirm to apply the new ${field.label}.`;
      }
    }
    const keepDelta = Number(values.KEEP_DELTA_PP ?? defaults.KEEP_DELTA_PP);
    const winThreshold = Number(
      values.WIN_THRESHOLD_PP ?? defaults.WIN_THRESHOLD_PP,
    );
    if (keepDelta > winThreshold) {
      focusField("loop-KEEP_DELTA_PP");
      return "Min improvement to keep (pp) must be at most the stop-early reduction.";
    }
    return null;
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    const invalid = validateLoopControls();
    if (invalid) {
      setError(invalid);
      return;
    }
    if (contextSource === "staging" && ids.some((id) => !/^\d{5,6}$/.test(id))) {
      focusField("pro-ids");
      setError("Staging context requires five- or six-digit organization IDs.");
      return;
    }
    setBusy(true);
    const overrides = Object.fromEntries(
      LOOP_FIELDS.filter((f) => changed(f.key))
        .map((f) => [f.key, Number(values[f.key])]),
    );
    try {
      // Async by default: the API returns 202 immediately; we navigate and poll.
      const run = await createRun({
        pro_ids: ids,
        // Sentinel until the n8n flow self-reports its query version during
        // the run; the pipeline overwrites it with the authoritative value.
        audience_query: PENDING_AUDIENCE_QUERY,
        audience_run: audienceRun,
        channels,
        journey_window: journeyWindow as RunCreateInput["journey_window"],
        context_source: contextSource,
        include_features_not_in_current_plan:
          contextSource === "staging" && includeFeaturesNotInCurrentPlan,
        model_tier: modelTier,
        ...(contextSource === "staging" && stagingContext
          ? {
              context_promotion_id: stagingContext.promotion_id,
            }
          : {}),
        ...(Object.keys(overrides).length ? { loop_config: overrides } : {}),
      });
      onStarted(run);
    } catch (e) {
      setError(`Run could not be started: ${e instanceof Error ? e.message : e}`);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="panel" ref={formRef} noValidate>
      <h2>Start a run</h2>
      <p>
        The supplied audience is already SQL-suppressed (DNC applied upstream).
        Waypoint validates identifiers and preserves lineage; it never sends.
      </p>

      <fieldset>
        <legend>Run inputs</legend>
        <label htmlFor="pro-ids">
          {contextSource === "staging"
            ? "Organization IDs (one per line)"
            : "Pro IDs (one per line)"}
        </label>
        <textarea
          id="pro-ids"
          value={proIds}
          onChange={(e) => setProIds(e.target.value)}
          rows={5}
          required
        />
        <p className="helper">
          Audience query version is tracked automatically: the n8n flow
          self-reports it during the run and it appears in the run status.
        </p>
        <label htmlFor="audience-run">Audience run timestamp</label>
        <input
          id="audience-run"
          value={audienceRun}
          onChange={(e) => setAudienceRun(e.target.value)}
          placeholder="2026-08-06T18:00:00Z"
          required
        />
        <div>
          <span>Channels (Waypoint picks per idea)</span>
          {CHANNELS.map((c) => (
            <label key={c} className="check-row" htmlFor={`channel-${c}`}>
              <input
                id={`channel-${c}`}
                type="checkbox"
                checked={channels.includes(c)}
                onChange={(e) =>
                  setChannels(e.target.checked
                    ? CHANNELS.filter((x) => x === c || channels.includes(x))
                    : channels.filter((x) => x !== c))
                }
              />
              Channel: {c}
            </label>
          ))}
        </div>
        <div>
          <span>Model</span>
          {(["deep", "fast"] as const).map((tier) => (
            <label key={tier} htmlFor={`model-${tier}`}>
              <input
                id={`model-${tier}`}
                type="radio"
                name="model-tier"
                checked={modelTier === tier}
                onChange={() => setModelTier(tier)}
              />
              {tier === "deep" ? "Deep" : "Fast"} ({models[tier]})
            </label>
          ))}
        </div>
        <p className="helper">
          Deep is the default for idea generation and evaluation; Fast is cheaper.
        </p>
        <label htmlFor="journey-window">Journey window</label>
        <select
          id="journey-window"
          value={journeyWindow}
          onChange={(e) => setJourneyWindow(e.target.value)}
        >
          <option value="churn_risk">churn risk (not using the app)</option>
          <option value="churn_risk_open">
            churn risk — everyone (no one is excluded)
          </option>
          <option value="onboarding">onboarding</option>
          <option value="upsell">upsell / expansion</option>
        </select>
        <p className="helper">
          The customer state this run optimizes a touch for. Touches are
          selected for return-to-app impact within this window.
        </p>
        <div>
          <span>Context source</span>
          <label htmlFor="context-standard">
            <input
              id="context-standard"
              type="radio"
              name="context-source"
              checked={contextSource === "standard"}
              onChange={() => setContextSource("standard")}
            />
            Standard context
          </label>
          <label htmlFor="context-staging">
            <input
              id="context-staging"
              type="radio"
              name="context-source"
              checked={contextSource === "staging"}
              disabled={!stagingAvailable}
              onChange={() => setContextSource("staging")}
            />
            Staging context
          </label>
        </div>
        <p className="helper">
          {contextSource === "staging"
            ? stagingContext
              ? <>Staging uses <strong>{stagingContext.context_catalog_name}</strong> (<code>{stagingContext.context_catalog_version_id}</code>) with <strong>{stagingContext.feature_catalog_name}</strong> (<code>{stagingContext.feature_catalog_version_id}</code>) · {stagingContext.included_variables} approved variables · {new Intl.DateTimeFormat("en-US", { timeZoneName: "short", year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true }).format(new Date(stagingContext.created_at))}.</>
              : "Staging is not ready because no active approved Workbench catalog is available."
            : "Standard uses today&apos;s production workflow."}
        </p>
        {contextSource === "staging" && (
          <>
            <label htmlFor="include-features-not-in-current-plan">
              <input
                id="include-features-not-in-current-plan"
                type="checkbox"
                checked={includeFeaturesNotInCurrentPlan}
                onChange={(event) => setIncludeFeaturesNotInCurrentPlan(event.target.checked)}
              />
              Include features not in the current plan
            </label>
            <p className="helper">
              Off excludes only features proven unavailable on the organization&apos;s
              current Core SaaS plan. On includes those features labeled as not included
              in the current plan. Add-ons and unknown plan coverage stay included and
              are labeled unknown.
            </p>
          </>
        )}
      </fieldset>

      <fieldset disabled={defaults === null}>
        <legend>Loop behavior</legend>
        {defaultsError && (
          <p className="helper">
            Loop defaults could not be loaded; controls are disabled and the run
            uses the server defaults.
          </p>
        )}
        {omitted.length > 0 && (
          <p className="helper">
            {`Not editable here — this API does not report a default for `}
            <span className="technical">{omitted.join(", ")}</span>
            {`. The run uses the server's own values. (The API is older than this UI.)`}
          </p>
        )}
        {LOOP_FIELDS.filter(
          (field) => defaults === null || field.key in defaults,
        ).map((field) => (
          <div key={field.key}>
            <label htmlFor={`loop-${field.key}`}>
              {field.label} <small className="technical">{field.key}</small>
            </label>
            <input
              id={`loop-${field.key}`}
              type="number"
              step="any"
              min={field.min}
              value={values[field.key] ?? ""}
              aria-describedby={`help-${field.key}`}
              onChange={(e) =>
                setValues((prev) => ({ ...prev, [field.key]: e.target.value }))
              }
            />
            <p className="helper" id={`help-${field.key}`}>{field.help}</p>
            {changed(field.key) && (
              <>
                <label htmlFor={`confirm-${field.key}`}>
                  {`Type "confirm" to apply the new ${field.label}`}
                </label>
                <input
                  id={`confirm-${field.key}`}
                  value={confirms[field.key] ?? ""}
                  onChange={(e) =>
                    setConfirms((prev) => ({
                      ...prev, [field.key]: e.target.value,
                    }))
                  }
                />
              </>
            )}
          </div>
        ))}
      </fieldset>

      <fieldset className="fleet-safety">
        <legend>Fleet safety</legend>
        <p>
          <strong>Max simultaneous model calls (fleet)</strong>{" "}
          <small className="technical">MAX_IN_FLIGHT_LLM_CALLS</small>{" "}
          <span className="fleet-cap-value">{fleetCap ?? "—"}</span> (read-only)
        </p>
        <p className="helper">
          Shared across all workers; limits API pressure, not total run cost.
        </p>
      </fieldset>

      <button type="submit" disabled={busy || ids.length === 0}>
        {busy ? "Starting run…" : `Start run (${ids.length} pros)`}
      </button>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </form>
  );
}
