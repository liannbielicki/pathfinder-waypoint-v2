const steps = [
  ["1", "Input", "Org ID and run settings"],
  ["2", "Fetch sources", "Context Layer and/or n8n Snowflake"],
  ["3", "Normalize", "Align both responses to one org context"],
  ["4", "PII gate", "Remove excluded fields and matched values"],
  ["5", "Build prompts", "Baseline and catalog-enriched proposed"],
  ["6", "Generate", "Ask the model for candidate JSON"],
  ["7", "Parse", "Validate and display candidates"],
  ["8", "Evaluate", "Judge baseline vs proposed and log result"],
];

export function WorkbenchFlow() {
  return <section className="card flow" aria-labelledby="flow-title">
    <h2 id="flow-title">What happens in a run</h2>
    <p className="helper">The arrows show execution order. Raw source values are never displayed or sent to the model before step 4.</p>
    <ol className="flow-list">
      {steps.map(([number, title, description]) => <li key={number}>
        <span className="flow-number">{number}</span><span><strong>{title}</strong><small>{description}</small></span>
      </li>)}
    </ol>
  </section>;
}
