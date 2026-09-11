"use client";

import { FormEvent, useEffect, useState } from "react";
import { runWorkbench, type WorkbenchTrace } from "@/lib/workbench";
import { readCatalogVersions, selectedCatalogVersionId, type CatalogVersion } from "@/lib/catalogVersions";

export function WorkbenchRunForm({ onRun, busy, onBusy, onModeChange }: { onRun: (trace: WorkbenchTrace) => void; busy: boolean; onBusy?: (busy: boolean) => void; onModeChange?: (mode: string) => void }) {
  const [identifier, setIdentifier] = useState("889901");
  const [mode, setMode] = useState("both");
  const [policy, setPolicy] = useState("compare");
  const [workbenchMode, setWorkbenchMode] = useState("runtime");
  const [versions, setVersions] = useState<CatalogVersion[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState("fresh");
  const [authoringPrompt, setAuthoringPrompt] = useState("Create a token-efficient, AI-only variable catalog. Preserve exact source keys, propose short stable canonical keys, assign a machine value category, map each variable only to the exact feature keys it most likely responds to, rank usefulness from 1 to 5 for product engagement, and request cohort-level aggregates only for ranks 4 or 5. Do not create human descriptions, time metadata, or invented relationships. Return JSON only.");
  const [featureCatalogCsv, setFeatureCatalogCsv] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const refresh = () => {
      const next = readCatalogVersions();
      setVersions(next);
      const selected = selectedCatalogVersionId();
      setSelectedVersionId(selected !== "fresh" && next.some((version) => version.id === selected) ? selected : next.at(-1)?.id ?? "fresh");
    };
    refresh();
    window.addEventListener("waypoint-catalog-updated", refresh);
    return () => window.removeEventListener("waypoint-catalog-updated", refresh);
  }, []);

  const selectedVersion = versions.find((version) => version.id === selectedVersionId);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    onBusy?.(true);
    try {
      const savedCatalog = selectedVersion?.entries ?? [];
      const request = {
        identifier,
        identifier_type: "organization_id",
        source_mode: mode,
        context_policy: policy,
        workbench_mode: workbenchMode,
        catalog_override: savedCatalog.length ? savedCatalog : undefined,
        catalog_version_id: selectedVersion?.id,
        catalog_version_name: selectedVersion?.name,
        catalog_version_saved_at: selectedVersion?.updated_at,
        model: "claude-sonnet-5",
        enrichment: "catalog",
        channels: ["email", "sms"],
        ...(workbenchMode === "authoring" ? { authoring_prompt: authoringPrompt, feature_catalog_csv: featureCatalogCsv || undefined } : {}),
      };
      const trace = await runWorkbench(request);
      onRun(trace);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Workbench run failed");
    } finally {
      onBusy?.(false);
    }
  }

  return (
    <form className="panel" onSubmit={submit}>
      <h1>Context Layer Workbench</h1>
      <p className="helper">Local, read-only inspection. Raw values never enter the model request.</p>
      <label htmlFor="identifier">Organization ID</label>
      <input id="identifier" value={identifier} onChange={(event) => setIdentifier(event.target.value)} />
      <label htmlFor="source-mode">Source mode</label>
      <select id="source-mode" value={mode} onChange={(event) => setMode(event.target.value)}>
        <option value="both">Both (recommended)</option><option value="context_layer">Context Layer API</option><option value="snowflake">Snowflake via n8n</option>
      </select>
      <p className="helper">Source credentials and webhook settings are loaded from <code>services/api/.env</code>.</p>
      <p className="helper">Anthropic settings are loaded from <code>services/api/.env</code>.</p>
      <label htmlFor="context-policy">Context policy</label>
      <select id="context-policy" value={policy} onChange={(event) => setPolicy(event.target.value)}>
        <option value="compare">Compare baseline and proposed</option><option value="baseline">Baseline only</option><option value="proposed">Proposed only</option>
      </select>
      <label htmlFor="workbench-mode">Workbench mode</label>
      <select id="workbench-mode" value={workbenchMode} onChange={(event) => { setWorkbenchMode(event.target.value); onModeChange?.(event.target.value); }}>
        <option value="runtime">Runtime trace</option><option value="authoring">Context authoring lab</option>
      </select>
      {workbenchMode === "authoring" && <>
        <label htmlFor="feature-catalog">Feature catalog CSV</label>
        <input id="feature-catalog" type="file" accept=".csv,text/csv" onChange={(event) => { const file = event.target.files?.[0]; if (file) file.text().then(setFeatureCatalogCsv); }} />
        <p className="helper">Optional. Used for this authoring run to improve feature descriptions.</p>
        <label htmlFor="authoring-prompt">Authoring prompt</label>
        <textarea id="authoring-prompt" rows={4} value={authoringPrompt} onChange={(event) => setAuthoringPrompt(event.target.value)} />
        <label htmlFor="catalog-version">Start from catalog version</label>
        <select id="catalog-version" value={selectedVersionId} onChange={(event) => { setSelectedVersionId(event.target.value); const version = versions.find((item) => item.id === event.target.value); if (version?.prompt) setAuthoringPrompt(version.prompt); }}>
          <option value="fresh">Fresh variable inventory</option>
          {versions.map((version) => <option key={version.id} value={version.id}>{version.name} · {new Date(version.updated_at).toLocaleString()}</option>)}
        </select>
        <p className="helper">Fresh starts at 0%. A saved version resumes from its entries and restores the prompt used to create it.</p>
        <p className="helper">Uses scrubbed facts to draft catalog metadata. Drafts require review before runtime use.</p>
      </>}
      {workbenchMode === "runtime" && <p className="helper">If a local catalog was saved, it is loaded when you press Run.</p>}
      {error && <p className="error" role="alert">{error}</p>}
      <button type="submit" disabled={busy}>{busy ? "Running…" : "Run workbench"}</button>
    </form>
  );
}
