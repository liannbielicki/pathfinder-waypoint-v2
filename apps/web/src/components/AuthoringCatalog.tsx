"use client";

import { useMemo, useState } from "react";
import { newCatalogVersionId, readCatalogVersions, saveCatalogVersion, selectedCatalogVersionId, type CatalogVersion } from "@/lib/catalogVersions";
import { getWorkbenchJob, promoteContext, startWorkbenchJob, WORKBENCH_ACTIVE_JOB_KEY, type PromotionResult, type WorkbenchTrace } from "@/lib/workbench";

type Entry = {
  key?: string;
  canonical_key?: string;
  value_category?: string;
  related_features?: string[] | string;
  usefulness_rank?: number;
  disposition?: "include" | "deprioritize" | "exclude";
  aggregate_prompt?: string | null;
  review_status?: "draft" | "reviewed";
  confidence?: number;
  uncertainty_reason?: string | null;
  approval_status?: "draft" | "auto_approved" | "review_required" | "human_approved" | "excluded";
  exclusion_reason?: string | null;
  [key: string]: unknown;
};

type AuditItem = { key: string; observed_state?: string; observed_type?: string; source_query?: string; source_table?: string };
type EvaluationArm = { context?: unknown; prompt?: string; candidates?: Record<string, unknown>[]; metrics?: Record<string, unknown> };
type Evaluation = {
  baseline?: EvaluationArm;
  curated?: EvaluationArm;
  judge?: { winner?: "baseline" | "curated" | "tie"; reason?: string; suggested_changes?: unknown[] };
};
const JOB_VERSION_PREFIX = "waypoint-context-workbench-job-version:";

function linkedVersionId(trace: WorkbenchTrace): string | null {
  if (!trace.job_id || typeof window === "undefined") return null;
  try { return window.localStorage.getItem(`${JOB_VERSION_PREFIX}${trace.job_id}`); } catch { return null; }
}

function draftFromTrace(trace: WorkbenchTrace): Entry[] {
  const value = (trace.outputs.authoring as { draft?: unknown } | undefined)?.draft;
  return Array.isArray(value) ? value.filter((item): item is Entry => !!item && typeof item === "object") : [];
}

export function AuthoringCatalog({ trace, onCompiled }: { trace: WorkbenchTrace; onCompiled?: (trace: WorkbenchTrace) => void }) {
  const initialEntries = useMemo(() => {
    const linked = linkedVersionId(trace);
    const version = linked ? readCatalogVersions().find((item) => item.id === linked) : undefined;
    return (version?.entries as Entry[] | undefined) ?? draftFromTrace(trace);
  }, [trace]);
  const [entries, setEntries] = useState<Entry[]>(initialEntries);
  const catalogEntries = entries.map((entry) => entry.disposition === "deprioritize"
    && (entry.approval_status === "human_approved" || entry.review_status === "reviewed")
    ? { ...entry, disposition: "include" as const, approval_status: "auto_approved" as const, review_status: "draft" as const }
    : entry);
  const [activeDisposition, setActiveDisposition] = useState<NonNullable<Entry["disposition"]>>("include");
  const [query, setQuery] = useState("");
  const [versionName, setVersionName] = useState(`Context catalog ${new Date().toLocaleDateString()}`);
  const [savedVersionId, setSavedVersionId] = useState<string | null>(() => {
    const linked = linkedVersionId(trace);
    if (linked && readCatalogVersions().some((item) => item.id === linked)) return linked;
    const selected = selectedCatalogVersionId();
    const version = readCatalogVersions().find((item) => item.id === selected);
    return version && JSON.stringify(version.entries) === JSON.stringify(initialEntries) ? version.id : null;
  });
  const [compiling, setCompiling] = useState(false);
  const [compiled, setCompiled] = useState<WorkbenchTrace | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  const [evaluationTrace, setEvaluationTrace] = useState<WorkbenchTrace | null>(null);
  const [promoting, setPromoting] = useState(false);
  const [promotion, setPromotion] = useState<PromotionResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const authoring = trace.outputs.authoring as { total_keys?: number; completed_keys?: number; remaining_keys?: number; output_tokens?: number; token_budget?: number; confidence_threshold?: number; catalog_version?: { prompt?: string | null }; feature_catalog_version_id?: string | null } | undefined;
  const audit = trace.outputs.audit as { inventory?: AuditItem[]; total_variables?: number } | undefined;
  const auditByKey = useMemo(() => {
    const grouped = new Map<string, AuditItem[]>();
    for (const item of audit?.inventory ?? []) grouped.set(item.key, [...(grouped.get(item.key) ?? []), item]);
    return grouped;
  }, [audit]);
  const occurrences = new Map<string, number>();
  const rows = catalogEntries.map((entry, index) => {
    const key = String(entry.key ?? "");
    const occurrence = occurrences.get(key) ?? 0;
    occurrences.set(key, occurrence + 1);
    return { entry, index, observed: auditByKey.get(key)?.[occurrence] };
  });
  const entriesWithLineage = rows.map(({ entry, observed }) => ({
    ...entry,
    source_table: String(entry.source_table ?? observed?.source_table ?? ""),
  }));
  const visible = rows.filter(({ entry }) => entry.disposition === activeDisposition && `${entry.key} ${entry.canonical_key} ${entry.value_category}`.toLowerCase().includes(query.toLowerCase()));
  const dispositionCounts = {
    include: catalogEntries.filter((entry) => entry.disposition === "include").length,
    deprioritize: catalogEntries.filter((entry) => entry.disposition === "deprioritize").length,
    exclude: catalogEntries.filter((entry) => entry.disposition === "exclude").length,
  };
  const deprioritized = catalogEntries.filter((entry) => entry.disposition === "deprioritize").length;
  const included = dispositionCounts.include;
  const safeAudit = (audit?.inventory ?? []).filter((item) => item.observed_state !== "removed_pii");
  const uniqueAuditKeys = new Set(safeAudit.map((item) => item.key));
  const draftedKeys = new Set(catalogEntries.map((entry) => String(entry.key ?? "")));
  const duplicateSourceRows = safeAudit.length - uniqueAuditKeys.size;
  const undrafted = [...uniqueAuditKeys].filter((key) => !draftedKeys.has(key)).length;
  const parseError = trace.stages.find((stage) => stage.name === "authoring_parse" && stage.status === "failed");

  function update(index: number, values: Partial<Entry>) {
    setEntries((current) => current.map((entry, currentIndex) => currentIndex === index ? { ...entry, ...values } : entry));
    setSavedVersionId(null);
    setCompiled(null);
    setEvaluationTrace(null);
    setPromotion(null);
  }

  function changeDisposition(index: number, disposition: NonNullable<Entry["disposition"]>) {
    update(index, disposition === "include"
      ? { disposition, approval_status: "auto_approved", review_status: "draft", exclusion_reason: null }
      : disposition === "exclude"
        ? { disposition, approval_status: "excluded", review_status: "draft", exclusion_reason: "human_excluded" }
        : { disposition, approval_status: "review_required", review_status: "draft", exclusion_reason: null });
  }

  function approveDeprioritize(index: number) {
    changeDisposition(index, "include");
  }

  function save() {
    const now = new Date().toISOString();
    const version: CatalogVersion = {
      id: newCatalogVersionId(),
      name: versionName.trim() || "Unnamed context catalog",
      tag: "Edited",
      created_at: now,
      prompt: authoring?.catalog_version?.prompt ?? "",
      entries: entriesWithLineage,
      feature_catalog_version_id: authoring?.feature_catalog_version_id,
      confidence_threshold: authoring?.confidence_threshold ?? 0.8,
      excluded_keys: catalogEntries.filter((entry) => entry.approval_status === "excluded").map((entry) => String(entry.key ?? "")),
    };
    saveCatalogVersion(version);
    if (trace.job_id) window.localStorage.setItem(`${JOB_VERSION_PREFIX}${trace.job_id}`, version.id);
    setSavedVersionId(version.id);
    setCompiled(null);
    setEvaluationTrace(null);
    setPromotion(null);
  }

  async function compile() {
    if (!savedVersionId) return;
    const input = trace.stages.find((stage) => stage.name === "input")?.data;
    if (!input || typeof input !== "object") return;
    setCompiling(true);
    setEvaluationTrace(null);
    setPromotion(null);
    setError(null);
    try {
      let job = await startWorkbenchJob({
        ...(input as Record<string, unknown>),
        workbench_mode: "compile",
        catalog_override: entriesWithLineage,
        catalog_version_id: savedVersionId,
      });
      window.localStorage.setItem(WORKBENCH_ACTIVE_JOB_KEY, job.id);
      while (job.status === "queued" || job.status === "running") {
        await new Promise((resolveDelay) => setTimeout(resolveDelay, 1000));
        job = await getWorkbenchJob(job.id);
      }
      window.localStorage.removeItem(WORKBENCH_ACTIVE_JOB_KEY);
      if (job.status === "failed" || !job.result) throw new Error(job.error || "Compilation failed");
      const result = job.result;
      setCompiled(result);
      onCompiled?.(result);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Compilation failed");
    } finally {
      setCompiling(false);
    }
  }

  async function evaluate() {
    if (!savedVersionId || !completedOutput) return;
    const input = trace.stages.find((stage) => stage.name === "input")?.data;
    if (!input || typeof input !== "object") return;
    setEvaluating(true);
    setError(null);
    try {
      let job = await startWorkbenchJob({
        ...(input as Record<string, unknown>),
        workbench_mode: "evaluate",
        context_policy: "compare",
        catalog_override: entriesWithLineage,
        catalog_version_id: savedVersionId,
      });
      window.localStorage.setItem(WORKBENCH_ACTIVE_JOB_KEY, job.id);
      while (job.status === "queued" || job.status === "running") {
        await new Promise((resolveDelay) => setTimeout(resolveDelay, 1000));
        job = await getWorkbenchJob(job.id);
      }
      window.localStorage.removeItem(WORKBENCH_ACTIVE_JOB_KEY);
      if (job.status === "failed" || !job.result) throw new Error(job.error || "Context test failed");
      setEvaluationTrace({ ...job.result, job_id: job.id });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Context test failed");
    } finally {
      setEvaluating(false);
    }
  }

  async function promote() {
    if (!savedVersionId || !evaluationTrace) return;
    if (!evaluationTrace.job_id) {
      setError("The completed context test is missing its server job ID. Run the test again.");
      return;
    }
    setPromoting(true);
    setError(null);
    try {
      setPromotion(await promoteContext({
        evaluation_job_id: evaluationTrace.job_id,
      }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Promotion failed");
    } finally {
      setPromoting(false);
    }
  }

  const completedOutput = compiled?.outputs.compiled as { context?: unknown; metrics?: Record<string, unknown> } | undefined;
  const compiledRuleCount = Number(completedOutput?.metrics?.included_variables ?? 0);
  const estimatedTokens = Number(completedOutput?.metrics?.estimated_tokens ?? 0);
  const evaluation = evaluationTrace?.outputs.evaluation as Evaluation | undefined;
  const restoredOutput = trace.outputs.compiled as { context?: unknown; metrics?: Record<string, unknown> } | undefined;
  if (catalogEntries.length === 0 && restoredOutput) {
    return <section className="authoring-catalog compiled-preview"><p className="eyebrow">Step 4 · Compiled</p><h2>Approved context-packet baseline</h2><pre>{JSON.stringify(restoredOutput.context, null, 2)}</pre><p className="helper">{JSON.stringify(restoredOutput.metrics)}</p></section>;
  }
  return <section className="authoring-catalog" aria-labelledby="catalog-title">
    <div className="curation-header">
      <div><p className="eyebrow">Step 3 · Curate</p><h2 id="catalog-title">Review all context rules</h2><p className="helper">Every variable is retained. Approve any Deprioritized variable you want to promote to Include; leave the rest parked.</p></div>
      <div className="curation-count"><strong>{deprioritized === 0 ? "No deprioritized variables parked" : `${deprioritized} deprioritized parked`}</strong><span>{included} included · {catalogEntries.length} total</span></div>
    </div>
    <div className="disposition-tabs" aria-label="Filter variables by disposition">
      {(["include", "deprioritize", "exclude"] as const).map((disposition) => <button key={disposition} type="button" className={activeDisposition === disposition ? "active" : "secondary"} aria-pressed={activeDisposition === disposition} onClick={() => setActiveDisposition(disposition)}>{disposition[0].toUpperCase() + disposition.slice(1)} {dispositionCounts[disposition]}</button>)}
    </div>
    <div className="catalog-toolbar">
      <div><label htmlFor="variable-search">Search variables</label><input id="variable-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`Search ${activeDisposition} variables`} /></div>
      <div><label htmlFor="version-name">New immutable version name</label><input id="version-name" value={versionName} onChange={(event) => setVersionName(event.target.value)} /></div>
    </div>
    <p className="helper">Audited {audit?.total_variables ?? authoring?.total_keys ?? 0} source rows · Drafted {draftedKeys.size} unique rules · {duplicateSourceRows} duplicate source rows · Undrafted {undrafted} · Output tokens {authoring?.output_tokens ?? 0}/{authoring?.token_budget ?? 150000}</p>
    {parseError && <p className="error">Authoring failed: {parseError.error ?? "invalid model response"}</p>}
    <div className="compile-bar">
      <div className="compile-actions">
        <button type="button" className={savedVersionId ? "secondary" : undefined} aria-label="Save new immutable version" onClick={save} disabled={catalogEntries.length === 0 || Boolean(savedVersionId)}>{savedVersionId ? "Saved immutable version" : "Save immutable version"}</button>
        <button type="button" className={savedVersionId ? undefined : "secondary"} aria-label={completedOutput ? `Compiled ${compiledRuleCount} rule${compiledRuleCount === 1 ? "" : "s"}` : `Compile ${included} approved variable${included === 1 ? "" : "s"}`} onClick={() => void compile()} disabled={!savedVersionId || included === 0 || compiling || Boolean(completedOutput)}>{compiling ? "Compiling…" : completedOutput ? `Compiled ${compiledRuleCount} rule${compiledRuleCount === 1 ? "" : "s"}` : `Compile ${included} approved variable${included === 1 ? "" : "s"}`}</button>
        {savedVersionId && <span className="edited-tag">Edited</span>}
      </div>
      <span>{completedOutput ? `Baseline compiled successfully: ${included} approved selection${included === 1 ? "" : "s"} → ${compiledRuleCount} compiled rule${compiledRuleCount === 1 ? "" : "s"} · ${estimatedTokens} estimated tokens.${included > compiledRuleCount ? ` ${included - compiledRuleCount} selection${included - compiledRuleCount === 1 ? " was" : "s were"} blocked by validation; see the job warnings.` : ""}` : !savedVersionId ? `${deprioritized} deprioritized variable${deprioritized === 1 ? " is" : "s are"} parked. Save this edited version to continue.` : included === 0 ? "At least one rule must be Include." : `Next step: compile ${included} approved variable${included === 1 ? "" : "s"} into the deterministic baseline. This does not recollect Snowflake data or call AI.`}</span>
    </div>
    <div className="catalog-cards">
      {visible.map(({ entry, index, observed }) => {
        const key = String(entry.key ?? "");
        const related = Array.isArray(entry.related_features) ? entry.related_features.join(", ") : String(entry.related_features ?? "");
        return <details className="variable-card" key={`${key}:${index}`}>
          <summary>
            <div><strong>{key}</strong><span>{observed?.observed_state ?? "unavailable"} · {observed?.observed_type ?? "unavailable"}{observed?.source_query ? ` · ${observed.source_query}` : ""}</span></div>
            <div className="variable-badges"><span>{Math.round((entry.confidence ?? 0) * 100)}% confidence</span><span>rank {entry.usefulness_rank ?? 1}</span><span>{entry.approval_status?.replace("_", " ")}</span></div>
          </summary>
          <div className="variable-fields">
            {entry.uncertainty_reason && <p className="uncertainty wide">{entry.uncertainty_reason}</p>}
            <label>Canonical key<input value={entry.canonical_key ?? ""} onChange={(event) => update(index, { canonical_key: event.target.value })} /></label>
            <label>Category<input value={entry.value_category ?? ""} onChange={(event) => update(index, { value_category: event.target.value })} /></label>
            <label>Usefulness rank<select value={entry.usefulness_rank ?? 1} onChange={(event) => update(index, { usefulness_rank: Number(event.target.value) })}>{[1, 2, 3, 4, 5].map((rank) => <option key={rank}>{rank}</option>)}</select></label>
            <label>Disposition<select value={entry.disposition ?? "deprioritize"} onChange={(event) => changeDisposition(index, event.target.value as NonNullable<Entry["disposition"]>)}><option value="include">Include</option><option value="deprioritize">Deprioritize</option><option value="exclude">Exclude</option></select></label>
            <label className="wide">Exact related feature keys<input value={related} onChange={(event) => update(index, { related_features: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) })} /></label>
            <label className="wide">Cohort aggregate prompt<textarea rows={2} value={entry.aggregate_prompt ?? ""} onChange={(event) => update(index, { aggregate_prompt: event.target.value || null })} /></label>
            {entry.disposition === "deprioritize" && <div className="exception-actions wide">
              {entry.approval_status === "human_approved" || entry.review_status === "reviewed"
                ? <strong>Deprioritize approved</strong>
                : <button type="button" aria-label={`Approve deprioritize ${key}`} onClick={() => approveDeprioritize(index)}>Approve deprioritize</button>}
            </div>}
          </div>
        </details>;
      })}
    </div>
    {visible.length === 0 && <p className="helper">No {activeDisposition} variables match this search.</p>}
    {error && <p className="error">{error}</p>}
    {completedOutput && <section className="compiled-preview"><p className="eyebrow">Step 4 · Compiled</p><h3>Approved context-packet baseline</h3><button type="button" aria-label={evaluation ? "Test complete" : "Test curated context"} onClick={() => void evaluate()} disabled={evaluating || Boolean(evaluation)}>{evaluating ? "Testing baseline vs curated…" : evaluation ? "Test complete" : "Test curated context"}</button><p className="helper">Runs Waypoint twice for this organization. No outreach is sent.</p><details><summary>Inspect compiled contract</summary><pre>{JSON.stringify(completedOutput.context, null, 2)}</pre></details></section>}
    {evaluation && <section className="evaluation-results">
      <p className="eyebrow">Context test</p>
      <h3>{evaluation.judge?.winner === "curated" ? "Curated performed better" : evaluation.judge?.winner === "baseline" ? "Baseline performed better" : "The test was a tie"}</h3>
      <p>{String(evaluation.judge?.reason ?? "")}</p>
      <div className="evaluation-grid">
        {(["baseline", "curated"] as const).map((arm) => <article key={arm}>
          <h4>{arm === "baseline" ? "Baseline ideas" : "Curated ideas"}</h4>
          <p className="helper">{Number(evaluation[arm]?.metrics?.input_tokens ?? 0)} input · {Number(evaluation[arm]?.metrics?.output_tokens ?? 0)} output · {Number(evaluation[arm]?.metrics?.duration_ms ?? 0)} ms</p>
          <ul>{(evaluation[arm]?.candidates ?? []).map((candidate: Record<string, unknown>, index: number) => <li key={`${arm}:${index}`}><strong>{String(candidate.title ?? "Untitled idea")}</strong>{candidate.pro_facing_concept ? ` — ${String(candidate.pro_facing_concept)}` : ""}</li>)}</ul>
        </article>)}
      </div>
      {Array.isArray(evaluation.judge?.suggested_changes) && evaluation.judge.suggested_changes.length > 0 && <><h4>Small suggested changes</h4><ul>{evaluation.judge.suggested_changes.map((change: unknown, index: number) => <li key={index}>{String(change)}</li>)}</ul></>}
      <details><summary>Inspect exact inputs and prompts</summary><pre>{JSON.stringify({ baseline: { context: evaluation.baseline?.context, prompt: evaluation.baseline?.prompt }, curated: { context: evaluation.curated?.context, prompt: evaluation.curated?.prompt } }, null, 2)}</pre></details>
    </section>}
    {evaluation && <section className="promotion-panel">
      <p className="eyebrow">Final step · Promote</p>
      <h3>Use this context in Waypoint</h3>
      <p className="helper">Activates the approved context and selected feature catalog. It does not change n8n.</p>
      <button type="button" onClick={() => void promote()} disabled={promoting || Boolean(promotion)}>{promoting ? "Promoting…" : promotion ? "Promoted to Waypoint" : "Promote to Waypoint"}</button>
      {promotion && <>
        <p><strong>{promotion.id} is now active</strong> with {promotion.included_variables} approved variables.</p>
        <a className="download-link" href={`data:text/csv;charset=utf-8,${encodeURIComponent(promotion.csv)}`} download={`${promotion.id}-snowflake-handoff.csv`}>Download Snowflake handoff CSV</a>
      </>}
    </section>}
  </section>;
}
