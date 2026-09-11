"use client";

import { useState, type FormEvent } from "react";
import type { WorkbenchTrace } from "@/lib/workbench";
import { newCatalogVersionId, readCatalogVersions, writeCatalogVersions, type CatalogVersion } from "@/lib/catalogVersions";

type Entry = { key?: string; canonical_key?: string; value_category?: string; related_features?: unknown; usefulness_rank?: number; aggregate_prompt?: string | null; [key: string]: unknown };

function draftFromTrace(trace: WorkbenchTrace): Entry[] {
  const value = (trace.outputs.authoring as { draft?: unknown } | undefined)?.draft;
  const list = Array.isArray(value) ? value : value && typeof value === "object" && Array.isArray((value as { entries?: unknown }).entries) ? (value as { entries: unknown[] }).entries : [];
  return list.filter((item): item is Entry => !!item && typeof item === "object");
}

export function AuthoringCatalog({ trace }: { trace: WorkbenchTrace }) {
  const [entries, setEntries] = useState<Entry[]>(() => draftFromTrace(trace));
  const [saved, setSaved] = useState(false);
  const authoring = trace.outputs.authoring as { new_entries?: number; existing_entries?: number; remaining_keys?: number; total_keys?: number; completed_keys?: number; output_tokens?: number; token_budget?: number; budget_reached?: boolean; catalog_version?: { id?: string | null; name?: string | null; saved_at?: string | null; prompt?: string | null } | null } | undefined;
  const [versionName, setVersionName] = useState(() => authoring?.catalog_version?.name || `AI catalog ${new Date().toLocaleDateString()}`);
  const progress = authoring?.total_keys ? Math.min(100, Math.round(((authoring.completed_keys ?? 0) / authoring.total_keys) * 100)) : 0;
  const columns = ["key", "canonical_key", "value_category", "related_features", "usefulness_rank", "aggregate_prompt"];
  const parseError = trace.stages.find((stage) => stage.name === "authoring_parse" && stage.status === "failed");
  function update(row: number, column: string, value: string) {
    setEntries((current) => current.map((entry, index) => index === row ? { ...entry, [column]: value } : entry));
  }
  function resize(event: FormEvent<HTMLTextAreaElement>) {
    const target = event.currentTarget; target.style.height = "auto"; target.style.height = `${target.scrollHeight}px`;
  }
  function resizeOnMount(target: HTMLTextAreaElement | null) {
    if (target) { target.style.height = "auto"; target.style.height = `${target.scrollHeight}px`; }
  }
  function save() {
    const now = new Date().toISOString();
    const id = authoring?.catalog_version?.id || newCatalogVersionId();
    const existing = readCatalogVersions();
    const previous = existing.find((version) => version.id === id);
    const version: CatalogVersion = { id, name: versionName.trim() || "Unnamed catalog", created_at: previous?.created_at || now, updated_at: now, prompt: authoring?.catalog_version?.prompt || "", entries };
    writeCatalogVersions([...existing.filter((item) => item.id !== id), version], id);
    setSaved(true);
  }
  function download() {
    const blob = new Blob([JSON.stringify(entries, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = "waypoint-context-catalog-draft.json"; link.click(); URL.revokeObjectURL(url);
  }
  return <section className="card authoring-catalog" aria-labelledby="catalog-title">
    <div className="catalog-header"><div><h2 id="catalog-title">Context catalog draft</h2><p className="helper">Edit machine fields, then save a named local version. Nothing becomes runtime-approved automatically.</p>{authoring && <><p className="helper">New: {authoring.new_entries ?? 0} · Already saved: {authoring.existing_entries ?? 0} · Remaining: {authoring.remaining_keys ?? 0} · Tokens: {authoring.output_tokens ?? 0}/{authoring.token_budget ?? 70000}{authoring.budget_reached ? " · budget reached" : ""}</p><div className="catalog-progress"><label htmlFor="catalog-progress">Catalog progress: {progress}% ({authoring.completed_keys ?? 0}/{authoring.total_keys ?? 0} variables)</label><progress id="catalog-progress" max={100} value={progress}>{progress}%</progress></div></>}</div><div className="catalog-actions"><label htmlFor="version-name">Version name</label><input id="version-name" value={versionName} onChange={(event) => setVersionName(event.target.value)} /><button type="button" onClick={save}>{saved ? "Saved ✓" : "Save version"}</button><button type="button" className="view-toggle" onClick={download}>Download JSON</button></div></div>
    {parseError && <p className="error">Authoring failed: {parseError.error ?? "the model did not return valid catalog JSON"}</p>}
    {entries.length === 0 && !parseError ? <p>No catalog entries were returned. Adjust the authoring prompt and run again.</p> : entries.length > 0 && <div className="catalog-table"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{entries.map((entry, row) => <tr key={String(entry.key ?? row)}>{columns.map((column) => <td key={column}><textarea ref={resizeOnMount} aria-label={`${column} row ${row + 1}`} rows={1} value={String(entry[column] ?? "")} onInput={resize} onChange={(event) => update(row, column, event.target.value)} /></td>)}</tr>)}</tbody></table></div>}
  </section>;
}
