"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import {
  compareFeatureCatalogVersions,
  clearLocalCatalogData,
  contextVersionForServer,
  contextVersionFromShared,
  featureVersionForServer,
  featureVersionFromShared,
  readCatalogVersions,
  readFeatureCatalogVersions,
  selectCatalogVersion,
  selectFeatureCatalogVersion,
  selectedCatalogVersionId,
  selectedFeatureCatalogVersionId,
  type CatalogVersion,
  type FeatureCatalogVersion,
} from "@/lib/catalogVersions";
import {
  getWorkbenchStatus,
  getWorkbenchJob,
  listWorkbenchCatalogs,
  resumeWorkbenchJob,
  saveWorkbenchCatalog,
  startWorkbenchJob,
  validateFeatureCatalog,
  WORKBENCH_ACTIVE_JOB_KEY,
  type WorkbenchJob,
  type WorkbenchStatus,
  type WorkbenchTrace,
} from "@/lib/workbench";

const DEFAULT_PROMPT = "Create a token-efficient variable catalog for identifying a Pro's core issues and useful HCP solutions. Preserve exact source keys, infer compact categories, use only exact verified feature keys, rank usefulness from 1 to 5, choose include/deprioritize/exclude, and request cohort aggregates only when they materially improve a rank 4 or 5 decision. Return JSON only.";

function restoreSavedEntries(entries: CatalogVersion["entries"]): CatalogVersion["entries"] {
  return entries.map((entry) => {
    if (entry.disposition === "exclude") return { ...entry, approval_status: "excluded" };
    if (entry.disposition === "deprioritize") {
      const approved = entry.approval_status === "human_approved" || entry.review_status === "reviewed";
      if (approved) return { ...entry, disposition: "include", approval_status: "auto_approved", review_status: "draft" };
      return {
        ...entry,
        approval_status: "review_required",
        confidence: Number(entry.confidence ?? 0),
        uncertainty_reason: entry.uncertainty_reason || "Legacy saved rule needs review.",
      };
    }
    return { ...entry, disposition: "include", approval_status: "auto_approved" };
  });
}

export function WorkbenchRunForm({
  onRun,
  busy,
  onBusy,
  onStartFresh,
  completed = false,
}: {
  onRun: (trace: WorkbenchTrace) => void;
  busy: boolean;
  onBusy?: (busy: boolean) => void;
  onStartFresh?: () => void;
  completed?: boolean;
}) {
  const [identifier, setIdentifier] = useState("889901");
  const [addContextLayer, setAddContextLayer] = useState(false);
  const [status, setStatus] = useState<WorkbenchStatus | null>(null);
  const [featureVersions, setFeatureVersions] = useState<FeatureCatalogVersion[]>([]);
  const [featureVersionId, setFeatureVersionId] = useState("");
  const [contextVersions, setContextVersions] = useState<CatalogVersion[]>([]);
  const [contextVersionId, setContextVersionId] = useState(() => selectedCatalogVersionId());
  const [catalogDiff, setCatalogDiff] = useState<{ added: string[]; removed: string[]; changed: string[] } | null>(null);
  const [authoringPrompt, setAuthoringPrompt] = useState(DEFAULT_PROMPT);
  const [error, setError] = useState<string | null>(null);
  const [activeJobId, setActiveJobId] = useState(() => typeof window === "undefined" ? null : window.localStorage.getItem(WORKBENCH_ACTIVE_JOB_KEY));
  const [activeJob, setActiveJob] = useState<WorkbenchJob | null>(null);
  const deliveredJob = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const refreshStatus = async () => {
      try {
        const nextStatus = await getWorkbenchStatus();
        if (!cancelled) {
          setStatus(nextStatus);
          setError(null);
        }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Backend is unavailable");
      }
    };
    void refreshStatus();
    const statusTimer = window.setInterval(() => void refreshStatus(), 5000);
    const refresh = async () => {
      try {
        let [sharedContexts, sharedFeatures] = await Promise.all([
          listWorkbenchCatalogs("context"),
          listWorkbenchCatalogs("feature"),
        ]);
        const contextIds = new Set(sharedContexts.map((item) => item.id));
        const sharedFeatureById = new Map(sharedFeatures.map((item) => [item.id, item]));
        const sharedContextById = new Map(sharedContexts.map((item) => [item.id, item]));
        const localContexts = readCatalogVersions().filter((item) => {
          const shared = sharedContextById.get(item.id);
          return !shared || shared.details.recovered_metadata === true;
        });
        const localFeatures = readFeatureCatalogVersions().filter((item) => {
          const shared = sharedFeatureById.get(item.id);
          return !shared || shared.details.recovered_metadata === true;
        });
        if (localContexts.length || localFeatures.length) {
          await Promise.all([
            ...localContexts.map((item) => saveWorkbenchCatalog(contextVersionForServer(item))),
            ...localFeatures.map((item) => saveWorkbenchCatalog(featureVersionForServer(item))),
          ]);
          [sharedContexts, sharedFeatures] = await Promise.all([
            listWorkbenchCatalogs("context"),
            listWorkbenchCatalogs("feature"),
          ]);
          clearLocalCatalogData();
        }
        if (cancelled) return;
        const features = sharedFeatures.map(featureVersionFromShared);
        const contexts = sharedContexts.map(contextVersionFromShared);
        setFeatureVersions(features);
        setContextVersions(contexts);
        setFeatureVersionId(selectedFeatureCatalogVersionId() || features.at(0)?.id || "");
        const selectedContext = selectedCatalogVersionId();
        setContextVersionId(contexts.some((version) => version.id === selectedContext) ? selectedContext : "fresh");
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Catalog history is unavailable");
      }
    };
    void refresh();
    const refreshListener = () => void refresh();
    window.addEventListener("waypoint-catalog-updated", refreshListener);
    return () => {
      cancelled = true;
      window.clearInterval(statusTimer);
      window.removeEventListener("waypoint-catalog-updated", refreshListener);
    };
  }, []);

  useEffect(() => {
    if (!activeJobId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const job = await getWorkbenchJob(activeJobId);
        if (cancelled) return;
        setActiveJob(job);
        setError(job.status === "failed" ? job.error || "Workbench job failed" : null);
        if (job.status === "completed" || job.status === "needs_review") {
          onBusy?.(false);
          if (job.result && deliveredJob.current !== job.id) {
            deliveredJob.current = job.id;
            onRun({ ...job.result, job_id: job.id });
          }
          return;
        }
        if (job.status === "failed") {
          onBusy?.(false);
          return;
        }
        onBusy?.(true);
      } catch (cause) {
        if (cancelled) return;
        setError(cause instanceof Error ? `${cause.message} Reconnecting to saved job ${activeJobId}.` : "Reconnecting to saved Workbench job.");
      }
      timer = setTimeout(() => void poll(), 2000);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeJobId, onBusy, onRun]);

  const featureVersion = featureVersions.find((version) => version.id === featureVersionId);
  const contextVersion = contextVersions.find((version) => version.id === contextVersionId);

  function loadSavedVersion(version: CatalogVersion) {
    window.localStorage.removeItem(WORKBENCH_ACTIVE_JOB_KEY);
    deliveredJob.current = null;
    setActiveJobId(null);
    setActiveJob(null);
    onBusy?.(false);
    const entries = restoreSavedEntries(version.entries);
    onRun({
      stages: [{
        name: "input",
        status: "succeeded",
        summary: "restored from an immutable saved context catalog",
        data: {
          identifier,
          identifier_type: "organization_id",
          source_mode: addContextLayer ? "both" : "snowflake",
          workbench_mode: "authoring",
          authoring_prompt: version.prompt || authoringPrompt,
          feature_catalog_entries: featureVersion?.entries,
          feature_catalog_version_id: version.feature_catalog_version_id ?? featureVersion?.id,
        },
      }],
      warnings: ["Loaded from a saved context catalog without rerunning Snowflake or AI."],
      outputs: {
        audit: {
          total_variables: entries.length,
          inventory: entries.map((entry) => ({
            key: String(entry.key ?? ""),
            observed_state: "unavailable",
            observed_type: "unavailable",
            source_query: "saved catalog",
          })),
        },
        authoring: {
          draft: entries,
          total_keys: entries.length,
          completed_keys: entries.length,
          remaining_keys: 0,
          output_tokens: 0,
          token_budget: 150000,
          confidence_threshold: version.confidence_threshold ?? 0.8,
          review_exception_count: entries.filter((entry) => entry.approval_status === "review_required").length,
          catalog_version: {
            id: version.id,
            name: version.name,
            saved_at: version.created_at,
            prompt: version.prompt,
          },
          feature_catalog_version_id: version.feature_catalog_version_id ?? featureVersion?.id,
        },
      },
    });
  }

  function startFresh() {
    window.localStorage.removeItem(WORKBENCH_ACTIVE_JOB_KEY);
    deliveredJob.current = null;
    setActiveJobId(null);
    setActiveJob(null);
    setContextVersionId("fresh");
    selectCatalogVersion("fresh");
    setError(null);
    onBusy?.(false);
    onStartFresh?.();
  }

  async function uploadCatalog(file: File) {
    setError(null);
    try {
      const previous = featureVersion;
      const validated = featureVersionFromShared(await validateFeatureCatalog({
        name: file.name.replace(/\.csv$/i, ""),
        filename: file.name,
        csv_text: await file.text(),
      }));
      setFeatureVersions((current) => [validated, ...current.filter((item) => item.id !== validated.id)]);
      setFeatureVersionId(validated.id);
      selectFeatureCatalogVersion(validated.id);
      setCatalogDiff(compareFeatureCatalogVersions(previous, validated));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Feature catalog validation failed");
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    onBusy?.(true);
    try {
      const job = await startWorkbenchJob({
        identifier,
        identifier_type: "organization_id",
        source_mode: addContextLayer ? "both" : "snowflake",
        workbench_mode: "authoring",
        authoring_prompt: authoringPrompt,
        feature_catalog_entries: featureVersion?.entries,
        feature_catalog_version_id: featureVersion?.id,
        feature_catalog_csv: featureVersion?.csv_text,
        catalog_override: contextVersion?.entries,
        catalog_version_id: contextVersion?.id,
        catalog_version_name: contextVersion?.name,
        catalog_version_saved_at: contextVersion?.created_at,
      });
      window.localStorage.setItem(WORKBENCH_ACTIVE_JOB_KEY, job.id);
      deliveredJob.current = null;
      setActiveJob(job);
      setActiveJobId(job.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Workbench run failed");
      onBusy?.(false);
    }
  }

  async function resume() {
    if (!activeJobId) return;
    setError(null);
    onBusy?.(true);
    try {
      const job = await resumeWorkbenchJob(activeJobId);
      deliveredJob.current = null;
      setActiveJob(job);
      setActiveJobId(job.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not resume saved job");
      onBusy?.(false);
    }
  }

  return <form className="panel workbench-start" onSubmit={submit}>
    <p className="eyebrow">Context Workbench</p>
    <h1>Build Waypoint context</h1>
    <p className="lede">Start with every experimental variable. Audit first, curate second, compile only reviewed context.</p>

    <ol className="workbench-steps" aria-label="Workbench stages">
      <li className="active">1 Connect</li><li>2 Collect & audit</li><li>3 Curate</li><li>4 Compile</li>
    </ol>

    <section className="connection-card" aria-label="Environment status">
      <div><strong>Environment</strong><code>{status?.env_file ?? "Checking backend…"}</code></div>
      <div className="status-row">
        <span className={status?.configured.snowflake ? "ready" : "not-ready"}>Snowflake {status ? (status.configured.snowflake ? "configured" : "not configured") : "checking"}</span>
        <span className={status?.configured.context_layer ? "ready" : "not-ready"}>Context Layer {status ? (status.configured.context_layer ? "configured" : "not configured") : "checking"}</span>
        <span className={status?.configured.ai ? "ready" : "not-ready"}>AI {status ? (status.configured.ai ? "configured" : "not configured") : "checking"}</span>
      </div>
    </section>
    {status?.activity === "waypoint" && <p className="error" role="alert">A Waypoint run is active. Context Workbench will unlock when it finishes.</p>}

    <div className="form-grid">
      <div><label htmlFor="identifier">Organization ID</label><input id="identifier" value={identifier} onChange={(event) => setIdentifier(event.target.value)} required /></div>
      <div><label htmlFor="feature-version">Feature catalog version</label><select id="feature-version" value={featureVersionId} onChange={(event) => { setFeatureVersionId(event.target.value); selectFeatureCatalogVersion(event.target.value); }}><option value="">Built-in catalog</option>{featureVersions.map((version) => <option key={version.id} value={version.id}>{version.name} · {version.entries.length} features</option>)}</select></div>
    </div>

    <label className="check-row"><input type="checkbox" checked={addContextLayer} onChange={(event) => setAddContextLayer(event.target.checked)} /> Add Context Layer API coverage</label>
    <p className="helper">The experimental n8n/Snowflake source is always collected. This page never edits its queries.</p>

    <div className="catalog-upload">
      <label htmlFor="feature-upload">Upload feature catalog CSV</label>
      <input id="feature-upload" aria-label="Upload feature catalog CSV" type="file" accept=".csv,text/csv" onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadCatalog(file); }} />
      {catalogDiff && <p className="helper">Compared with prior version: +{catalogDiff.added.length} added · −{catalogDiff.removed.length} removed · {catalogDiff.changed.length} changed</p>}
    </div>

    <label htmlFor="context-version">Start from prior context catalog</label>
    <select id="context-version" value={contextVersionId} onChange={(event) => {
      const id = event.target.value;
      if (id === "fresh") {
        startFresh();
        return;
      }
      setContextVersionId(id);
      selectCatalogVersion(id);
      const version = contextVersions.find((item) => item.id === id);
      if (version) loadSavedVersion(version);
    }}><option value="fresh">Fresh audit — use all source variables</option>{contextVersions.map((version) => <option key={version.id} value={version.id}>{version.tag ? `${version.tag} · ` : ""}{version.name} · {version.entries.length} rules</option>)}</select>
    {contextVersion && <button type="button" className="secondary" onClick={() => loadSavedVersion(contextVersion)}>Open selected saved run</button>}

    <details className="advanced"><summary>Advanced authoring instructions</summary><label htmlFor="authoring-prompt">Authoring prompt</label><textarea id="authoring-prompt" rows={5} value={authoringPrompt} onChange={(event) => setAuthoringPrompt(event.target.value)} /></details>
    {!completed && activeJob && <section className="job-progress" aria-label="Workbench job progress">
      <strong>Job {activeJob.status.replace("_", " ")}</strong>
      <span>{String(activeJob.state.phase ?? "collecting")} · {Number(activeJob.state.pending_keys ?? 0)} variables pending</span>
      {activeJob.updated_at && <span>Checkpoint {new Date(activeJob.updated_at).toLocaleTimeString()}</span>}
      {activeJob.status === "failed" && <button type="button" className="secondary" onClick={() => void resume()}>Resume saved job</button>}
    </section>}
    {!completed && error && <p className="error" role="alert">{error}</p>}
    {completed
      ? <div className="next-step-note">
          <p className="helper">Collection is complete. Continue to curation below, or clear this view to collect a new run.</p>
          <button type="button" className="secondary" onClick={startFresh}>Start a fresh run</button>
        </div>
      : <button type="submit" disabled={busy || status?.activity === "waypoint" || status?.configured.snowflake === false || status?.configured.ai === false}>{busy ? "Collecting and curating…" : "Collect and curate all variables"}</button>}
  </form>;
}
