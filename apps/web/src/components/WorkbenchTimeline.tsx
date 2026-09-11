"use client";

import { useState } from "react";
import type { WorkbenchTrace } from "@/lib/workbench";

function formatted(data: unknown) {
  return typeof data === "string" ? data : JSON.stringify(data, null, 2);
}

const stageHelp: Record<string, string> = {
  input: "Request settings only; credentials are omitted.",
  context_layer_context: "Context Layer response shape; values stay hidden until the PII gate.",
  snowflake_context: "n8n/Snowflake response shape; values stay hidden until the PII gate.",
  raw_context: "Structure and counts only. Raw provider values are never shown in this trace.",
  normalized_context: "Both sources aligned into one organization-level context object.",
  pii_gate: "Fail-closed app-side exclusion. The ledger records paths and reasons, never original values.",
  scrubbed_context: "Safe retained values after PII removal. This is the first value-bearing context view.",
  current_context: "The scrubbed context sent to the baseline prompt.",
  proposed_context: "The scrubbed context plus the product catalog enrichment.",
};

function HumanValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span>—</span>;
  if (typeof value !== "object") return <span>{String(value)}</span>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span>None</span>;
    if (value.every((item) => item && typeof item === "object" && !Array.isArray(item))) {
      const columns = Array.from(new Set(value.flatMap((item) => Object.keys(item as Record<string, unknown>))));
      return <table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{value.map((item, index) => <tr key={index}>{columns.map((column) => <td key={column}><HumanValue value={(item as Record<string, unknown>)[column]} /></td>)}</tr>)}</tbody></table>;
    }
    return <ol>{value.map((item, index) => <li key={index}><HumanValue value={item} /></li>)}</ol>;
  }
  return <table><tbody>{Object.entries(value as Record<string, unknown>).map(([key, child]) => <tr key={key}><th>{key}</th><td><HumanValue value={child} /></td></tr>)}</tbody></table>;
}

export function WorkbenchTimeline({ trace }: { trace: WorkbenchTrace }) {
  const [humanViews, setHumanViews] = useState<Record<string, boolean>>({});
  return <section aria-label="Workbench execution trace">
    {trace.stages.map((stage, index) => {
      const pii = stage.name === "pii_gate";
      const removed = pii && typeof stage.data === "object" && stage.data !== null && "removed_count" in stage.data
        ? String((stage.data as { removed_count: number }).removed_count) : null;
      return <details className="card" key={`${stage.name}-${index}`} open={pii || stage.name === "scrubbed_context"}>
        <summary><strong>{stage.name}</strong> <span className="pill">{stage.status}</span>{stage.duration_ms != null && <span className="pill">{stage.duration_ms}ms</span>}</summary>
        {removed != null && <p><strong>PII gate:</strong> removed {removed} fields. Original values are never shown; open <code>scrubbed_context</code> for retained safe values.</p>}
        {stageHelp[stage.name] && <p>{stageHelp[stage.name]}</p>}
        {stage.summary && <p>{stage.summary}</p>}
        {stage.error && <p className="error">{stage.error}</p>}
        {stage.metrics && Object.keys(stage.metrics).length > 0 && <pre>{formatted(stage.metrics)}</pre>}
        <button className="view-toggle" type="button" onClick={() => setHumanViews((current) => ({ ...current, [stage.name + index]: !current[stage.name + index] }))}>
          {humanViews[stage.name + index] ? "Show JSON" : "Human view"}
        </button>
        {humanViews[stage.name + index] ? <div className="human-view"><HumanValue value={stage.data} /></div> : <pre>{formatted(stage.data)}</pre>}
      </details>;
    })}
    {trace.warnings.length > 0 && <div className="card"><strong>Warnings</strong><pre>{formatted(trace.warnings)}</pre></div>}
  </section>;
}
