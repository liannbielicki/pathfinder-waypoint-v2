#!/usr/bin/env node
// Runnable check for the parsing/derivation Code nodes in
// outcome-ingestion.workflow.json (3 of 5 — the two window nodes are pure
// Date math against Date.now() and are checked separately below).
// It extracts their jsCode straight out of the workflow file and asserts the
// correctness rules that, if broken, would silently poison the learning loop.
//   node docs/n8n/check-code-nodes.js
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const wf = JSON.parse(fs.readFileSync(path.join(__dirname, 'outcome-ingestion.workflow.json'), 'utf8'));
const code = (name) => wf.nodes.find((n) => n.name === name).parameters.jsCode;
const RUN = 'run-abc';
// Every fixture Pro below is one WE shipped: verdict=winner and handed_off. The
// work-list filter is what makes the flow start from our own funnel instead of
// everything Iterable did, so the harness has to supply it.
const worklistPros = (ids) => ids.map((pro_id) => ({ run_id: RUN, pro_id, verdict: 'winner', handed_off: true }));
const ALL_FIXTURE_PROS = worklistPros([
  'pro-1', 'pro-2', 'pro-3', 'pro-4', 'pro-5', 'pro-6', 'pro:7', 'pro-x',
  'pro-utc', 'pro-us', 'pro-sms', 'pro-sms2', 'pro-qa', 'pro-lookalike',
]);
const worklist$ = (touch) => (name) => name && name.startsWith('Waypoint work list')
  ? { all: () => [{ json: { pros: ALL_FIXTURE_PROS } }] }
  : { itemMatching: () => ({ json: touch }), first: () => ({ json: touch }) };
const run = (name, items, env, $) =>
  new Function('items', '$env', '$', code(name))(items, env || {}, $ || worklist$());

const line = (userId, email, dims, createdAt) =>
  JSON.stringify({ userId, email, createdAt, ...(dims ? { transactionalData: JSON.stringify(dims) } : {}) });

const sends = [
  line('pro-1', 'owner@realco.com', { lcmRun: RUN, lcmVariant: 'A' }, '2026-08-20 10:00:00 +00:00'),
  line('pro-2', 'jake.fassora+x@housecallpro.com', { lcmRun: RUN, lcmVariant: 'A' }, '2026-08-20 10:00:00 +00:00'),
  line('pro-3', 'ctrl@realco.com', { lcmRun: RUN, lcmVariant: 'B' }, '2026-08-20 10:00:00 +00:00'),
  line('pro-4', 'old@realco.com', null, '2026-08-20 10:00:00 +00:00'),
  line('pro-5', 'g@realco.com', { lcmRun: RUN, lcmVariant: 'A', lcmRouting: 'guardrail' }, '2026-08-20 10:00:00 +00:00'),
  line('pro-6', '', { lcmRun: RUN, lcmVariant: 'A' }, '2026-08-20 10:00:00 +00:00'),
  line('pro:7', 'owner@realco.com', { lcmRun: RUN, lcmVariant: 'A' }, '2026-08-20 10:00:00 +00:00'),
].join('\n');

// --- Half 1 -----------------------------------------------------------------
const rows = run('Build outcomes', [
  { json: { dataTypeName: 'emailSend', data: sends } },
  { json: { dataTypeName: 'emailClick', data: line('pro-1', 'owner@realco.com', { lcmRun: RUN, lcmVariant: 'A' }, '2026-08-20 11:00:00 +00:00') } },
])[0].json.batch;
const by = Object.fromEntries(rows.map((r) => [r.pro_id, r]));

assert.strictEqual(rows.length, 4, 'arm B (rule 2) and un-stamped sends (rule 4) must be dropped');
assert.ok(!by['pro-3'], 'rule 2: lcmVariant B is the control message, not our recommendation');
assert.ok(!by['pro-4'], 'rule 4: no parseable transactionalData means no dimensions');
assert.strictEqual(by['pro-1'].routing, 'route-to-pro', 'proven external recipient');
assert.strictEqual(by['pro-2'].routing, 'guardrail', 'rule 1: internal roster is never route-to-pro');
assert.strictEqual(by['pro-5'].routing, 'guardrail', 'rule 1: an lcmRouting stamp wins');
assert.strictEqual(by['pro-1'].clicked, true);
assert.ok(!('clicked' in by['pro-2']), 'only ever assert TRUE — false would overwrite a later-window observation');
assert.strictEqual(by['pro-1'].sent_at, '2026-08-20T10:00:00.000Z', 'rule 5: true Iterable send timestamp, UTC');
assert.ok(rows.every((r) => r.source === 'iterable_n8n' && r.run_id && r.pro_id));
// outcomes.merge_routing: '' defers to what is stored, but two DIFFERENT claims fail
// closed to 'conflict'. An unprovable event must therefore claim NOTHING, or it would
// disqualify a touch an earlier window already proved was a real send.
assert.ok(!('routing' in by['pro-6']), 'unprovable recipient claims no routing at all');
assert.ok(!by['pro:7'], "a ':' in the natural key 422s the WHOLE batch — drop the one event, not the batch");
assert.strictEqual(by['pro-6'].delivered, true, 'the event itself is still recorded');

// --- Half 2 -----------------------------------------------------------------
const eligible = run('Eligible touches', [{ json: { horizon: 7, dataTypeName: 'emailSend', data: sends } }]);
assert.strictEqual(eligible.length, 1,
  'rule 1: only a PROVEN real-Pro send may be scored against Amplitude — internal, stamped-guardrail, unprovable, arm B and un-stamped sends are all skipped');
assert.strictEqual(eligible[0].json.pro_id, 'pro-1');

const touch = eligible[0].json;
const $ = worklist$(touch);
const env = { AMPLITUDE_ACTIVE_USE_EVENTS: 'app_open' };
const score = (evts) => run('Returns to outcomes', [{ json: { events: evts } }], env, $)[0].json.batch[0];

assert.strictEqual(score([{ event_type: 'app_open', event_time: '2026-08-22 09:00:00.000000' }]).returned_7d, true);
assert.strictEqual(score([{ event_type: 'app_open', event_time: '2026-09-22 09:00:00.000000' }]).returned_7d, false,
  'outside the horizon window anchored on sent_at');
assert.strictEqual(score([{ event_type: 'some_other_event', event_time: '2026-08-22 09:00:00.000000' }]).returned_7d, false,
  'only the named active-use events qualify');
assert.strictEqual(score([]).source, 'iterable_n8n', 'same source as half 1 so the row updates in place');
assert.throws(() => run('Returns to outcomes', [{ json: { events: [] } }], {}, $), /AMPLITUDE_ACTIVE_USE_EVENTS/,
  'must refuse rather than guess a qualifying event (TODOS.md)');

// A FAILED Amplitude lookup is not a measured negative. The activity node runs
// with onError=continueRegularOutput so one 5xx cannot abort the whole night —
// which means the errored item reaches this node, and scoring it as
// returned:false would write a fabricated measured fact.
for (const bad of [{ error: 'Amplitude 503' }, { events: null }, {}]) {
  assert.strictEqual(run('Returns to outcomes', [{ json: bad }], env, $).length, 0,
    `a failed lookup must produce NO row, not returned:false (${JSON.stringify(bad)})`);
}
assert.strictEqual(score([]).returned_7d, false,
  'a SUCCESSFUL lookup returning zero events is still a real measured negative');

// --- the blind spots a review found: these all used to pass while broken -----

// Rule 2 must fail CLOSED. `=== 'B'` leaked every one of these through as a real
// recommendation touch — the absent-stamp case being the realistic one.
for (const variant of ['b', 'B ', undefined, 2, 'control']) {
  const dims = { lcmRun: RUN };
  if (variant !== undefined) dims.lcmVariant = variant;
  const leaked = run('Build outcomes', [{ json: {
    dataTypeName: 'emailSend',
    data: line('pro-x', 'owner@realco.com', dims, '2026-08-20 10:00:00 +00:00'),
  } }]);
  assert.strictEqual(leaked.length, 0, `rule 2 must fail closed on lcmVariant=${JSON.stringify(variant)}`);
  assert.strictEqual(
    run('Eligible touches', [{ json: { horizon: 7, dataTypeName: 'emailSend',
      data: line('pro-x', 'owner@realco.com', dims, '2026-08-20 10:00:00 +00:00') } }]).length,
    0, `rule 2 must fail closed in half 2 too (lcmVariant=${JSON.stringify(variant)})`);
}

// Rule 6: a bare timestamp is UTC, not the n8n host's local zone. Every fixture
// above carries an explicit +00:00, which is exactly why this went unnoticed —
// so assert it under a non-UTC TZ as well as the ambient one.
{
  const bare = run('Build outcomes', [{ json: {
    dataTypeName: 'emailSend',
    data: line('pro-utc', 'owner@realco.com', { lcmRun: RUN, lcmVariant: 'A' }, '2026-08-20 10:00:00'),
  } }])[0].json.batch[0];
  assert.strictEqual(bare.sent_at, '2026-08-20T10:00:00.000Z',
    'rule 6: a timestamp with no offset is UTC — not the process-local zone');
  const micro = run('Build outcomes', [{ json: {
    dataTypeName: 'emailSend',
    data: line('pro-us', 'owner@realco.com', { lcmRun: RUN, lcmVariant: 'A' }, '2026-08-20 10:00:00.123456'),
  } }])[0].json.batch[0];
  assert.strictEqual(micro.sent_at, '2026-08-20T10:00:00.123Z', 'microsecond precision is still UTC');
}

// Rule 1, SMS: an SMS event carries phoneNumber, not email. Absence of an address
// is UNPROVABLE, not proof of a guardrail — reading it as guardrail silently
// dropped the entire SMS channel out of every horizon.
{
  const smsLine = JSON.stringify({
    userId: 'pro-sms', phoneNumber: '+15555550123', createdAt: '2026-08-20 10:00:00 +00:00',
    transactionalData: JSON.stringify({ lcmRun: RUN, lcmVariant: 'A', lcmRouting: 'route-to-pro' }),
  });
  const smsRow = run('Build outcomes', [{ json: { dataTypeName: 'smsSend', data: smsLine } }])[0].json.batch[0];
  assert.strictEqual(smsRow.channel, 'sms');
  assert.strictEqual(smsRow.routing, 'route-to-pro', 'a stamped SMS send is provable');
  assert.strictEqual(
    run('Eligible touches', [{ json: { horizon: 7, dataTypeName: 'smsSend', data: smsLine } }]).length,
    1, 'a stamped SMS touch MUST reach the Amplitude sweep');

  // Without the stamp it is unprovable, so it is skipped — fail-closed and correct,
  // but it means SMS horizons need lcmRouting. Pinned so the trade-off stays visible.
  const unstamped = JSON.stringify({
    userId: 'pro-sms2', phoneNumber: '+15555550124', createdAt: '2026-08-20 10:00:00 +00:00',
    transactionalData: JSON.stringify({ lcmRun: RUN, lcmVariant: 'A' }),
  });
  assert.ok(!('routing' in run('Build outcomes', [{ json: { dataTypeName: 'smsSend', data: unstamped } }])[0].json.batch[0]),
    'an unstamped SMS send claims no routing rather than a false guardrail');
  assert.strictEqual(
    run('Eligible touches', [{ json: { horizon: 7, dataTypeName: 'smsSend', data: unstamped } }]).length,
    0, 'unprovable SMS is skipped, not guessed');
}

// Rule 1, domains: matching the local part alone is wrong in BOTH directions.
{
  const at = (email, id) => line(id, email, { lcmRun: RUN, lcmVariant: 'A' }, '2026-08-20 10:00:00 +00:00');
  const out = run('Build outcomes', [{ json: { dataTypeName: 'emailSend', data: [
    at('jake.fassora.qa@housecallpro.com', 'pro-qa'),   // internal tester, NOT on the roster
    at('allison.torres@realco.com', 'pro-lookalike'),   // a real Pro who shares a local part
  ].join('\n') } }])[0].json.batch;
  const m = Object.fromEntries(out.map((r) => [r.pro_id, r]));
  // The DOMAIN decides. This previously asserted 'route-to-pro' — certifying a
  // live hole where any unrostered @housecallpro.com address was scored as a
  // proven real send, manufacturing cross-org evidence off an internal inbox.
  assert.strictEqual(m['pro-qa'].routing, 'guardrail',
    'ANY address on the internal domain is internal, rostered or not');
  assert.ok(!('routing' in m['pro-lookalike']),
    'a roster local part on an EXTERNAL domain is unprovable — never a false guardrail, which would conflict away a real touch');
}

// --- the window nodes (pure Date math, no fixtures to feed them) ------------
{
  const wins = run('Horizon windows', []);
  assert.strictEqual(wins.length, 8, '4 horizons x 2 channels');
  for (const { json: w } of wins) {
    const start = Date.parse(w.startDateTime.replace(' ', 'T') + 'Z');
    const end = Date.parse(w.endDateTime.replace(' ', 'T') + 'Z');
    assert.strictEqual(end - start, 86400000, 'each window is exactly one UTC day');
    assert.strictEqual(new Date(start).toISOString().slice(11), '00:00:00.000Z', 'floored to UTC midnight');
    // The window must be CLOSED when the cron reads it: every send in it aged
    // past the horizon at least an hour ago.
    const oldestUnmeasured = end + w.horizon * 86400000;
    assert.ok(oldestUnmeasured <= Date.now() - 3600000,
      `horizon ${w.horizon} window must be fully closed before it is measured`);
  }
  const send = run('Send window', [])[0].json;
  const s2 = Date.parse(send.startDateTime.replace(' ', 'T') + 'Z');
  const e2 = Date.parse(send.endDateTime.replace(' ', 'T') + 'Z');
  // 25h, not the cron interval: the trigger only fires 09:00-15:00 MT, so the
  // window has to span the ~18h overnight gap or events in it are lost.
  assert.strictEqual(e2 - s2, 25 * 60 * 60000, 'send window must span the overnight gap');
  assert.ok(e2 - s2 > 18 * 60 * 60000, 'longest gap between fires is 15:00 -> 09:00 next day');
  assert.strictEqual(run('Send window', []).length, 7, 'one item per Iterable event type');
}

// Option C: an event from another LCM workstream (or a Pro we never shipped)
// must be dropped here, not POSTed as an unattributable row.
{
  const foreign = run('Build outcomes', [{ json: {
    dataTypeName: 'emailSend',
    data: line('pro-not-ours', 'someone@realco.com', { lcmRun: 'someone-elses-batch', lcmVariant: 'A' }, '2026-08-20 10:00:00 +00:00'),
  } }]);
  assert.strictEqual(foreign.length, 0, 'an event we did not ship must never be POSTed');
  assert.strictEqual(
    run('Eligible touches', [{ json: { horizon: 7, dataTypeName: 'emailSend',
      data: line('pro-not-ours', 'someone@realco.com', { lcmRun: 'someone-elses-batch', lcmVariant: 'A' }, '2026-08-20 10:00:00 +00:00') } }]).length,
    0, 'and must never be measured against Amplitude');
}

// A truncated Amplitude page cannot prove a negative. If we got LIMIT events
// back and the oldest is newer than the send, the window is only partly covered
// — a `false` there is fabricated, not measured.
{
  const many = (n, t) => Array.from({ length: n }, () => ({ event_type: 'other', event_time: t }));
  assert.strictEqual(
    run('Returns to outcomes', [{ json: { events: many(1000, '2026-08-25 09:00:00') } }], env, $).length,
    0, 'a truncated page whose oldest event postdates the send must NOT score returned:false');
  const short = run('Returns to outcomes', [{ json: { events: many(10, '2026-08-25 09:00:00') } }], env, $);
  assert.strictEqual(short[0].json.batch[0].returned_7d, false,
    'an untruncated page with no qualifying event IS a real measured negative');
  const covered = run('Returns to outcomes', [{ json: { events: many(1000, '2026-08-01 09:00:00') } }], env, $);
  assert.strictEqual(covered[0].json.batch[0].returned_7d, false,
    'a truncated page that still reaches back before the send does cover the window');
}

console.log('docs/n8n/check-code-nodes.js: OK');
