# n8n outcome ingestion

Closes Waypoint's learning loop: read what Iterable actually sent and what
Amplitude says the Pro did afterwards, and post both back to
`POST /api/outcomes`, where they become the evidence that steers future idea
generation.

> **Unverified.** Written without live Iterable or Amplitude credentials. The
> workflow JSON has never been imported into n8n and no request in it has been
> executed against a real API. Every field name marked ASSUMPTION below is a
> reading of the docs, not an observation. The JavaScript inside the Code nodes
> *is* tested — `node docs/n8n/check-code-nodes.js` extracts the real code out
> of the workflow JSON and asserts the correctness rules against fixtures — but
> that proves the logic, not the integration.

Nothing here writes to Iterable or Amplitude. Every call to them is a `GET`;
the only `POST`s in the file go to Waypoint's own API.

---

## The shape

Two halves, because sends are event-driven and returns are age-driven.

**Half 1 — hourly, 09:00–15:00 America/Denver.** Ask Waypoint what it shipped,
read Iterable's message events for the last 25 hours, and post one outcome row
per touch with `sent_at`, `delivered`, `clicked`, `unsubscribed`.

**Half 2 — nightly 03:00 UTC.** For each of the 7/14/30/90-day horizons,
re-read the Iterable sends from the single UTC day that aged past that horizon
yesterday, ask Amplitude whether each Pro came back inside the window, and post
`returned_7d` / `returned_14d` / `returned_30d` / `returned_90d`.

One touch is written about five times over 90 days. That is by design:
`/api/outcomes` is idempotent per `(recommendation_id, source)`, and
`_apply_flags` never lets a null erase a measured value.

## The work list

Both halves open by asking Waypoint what it actually shipped:

    GET /api/funnel/worklist?days=7      (hourly half)
    GET /api/funnel/worklist?days=120    (nightly sweep)

That returns bare `{run_id, pro_id}` pairs for winners that were handed off.
Every Iterable event whose `(lcmRun, userId)` is not on that list is dropped
before it is posted.

This is what makes the flow read as *here is what we sent, did it go out, did
they come back* rather than *here is everything Iterable did, which bits were
ours*. It also keeps the LCM app's other workstreams — testimonials, the email
A/B — out of `touch_outcomes`; they stamp `lcmRun` too, and without this filter
they would land as permanent unattributable rows.

**`days=120`, not 91.** The 90-day horizon is measured 90 days after the
*send*, and the send trails the run by LCM drafting plus human QA. At 91 the run
had already aged out of the window, the Pro vanished from the work list, and the
90-day return was discarded with no error. 120 leaves about a month of slack.

**`days=7` on the hourly half**, not 2, for the same reason at the other end: a
touch whose QA took longer than the window would lose its engagement row
entirely.

If the work list comes back empty the whole half is a no-op. That is fail-closed
and correct, but it is **silent** — check it after any Railway deploy.

## Attribution

`(lcmRun, userId)` is the key. `lcmRun` is the LCM app's intake batch, which is
Waypoint's `run_id`; `userId` is the `pro_uuid`. `uq_winners_run_pro` makes that
pair exactly one winner, so no Waypoint identifier is ever written into Iterable.

The LCM app stamps its attribution dimensions into Iterable `dataFields`, and
Iterable echoes them back verbatim on the message event as `transactionalData`
(a JSON **string** that must be parsed).

## Correctness rules

These are the rules that, if broken, silently poison the learning loop rather
than failing loudly. Each is asserted in `check-code-nodes.js`.

1. **Only a proven real-Pro send is evidence.** A guardrailed send carries a real
   Pro's context but is delivered to an internal inbox — and Amplitude would
   still truthfully report that Pro's organic activity, manufacturing a return
   for a touch nobody received. Routing is decided by the LCM app's `lcmRouting`
   stamp when present; otherwise by the recipient's **domain**. Any address on
   the internal domain is internal, rostered or not. An address we cannot
   classify claims *nothing* — an empty claim defers, whereas a spurious
   `guardrail` would merge to `conflict` against a proven send and permanently
   disqualify a real touch.
2. **Arm B is not our recommendation.** `lcmVariant` `A` is per-Pro copy; `B` is
   a single pre-selected control message sent to everyone. The filter is a
   positive `!== 'A'` check, so a missing, lowercase, whitespace-padded, or
   future arm name fails closed.
3. **No send event means never sent.** A row that failed human QA produces no
   outcome row at all, and above all never a `returned_*: false`. A touch that
   was never delivered is not a touch that failed.
4. **No dimensions, no row.** Events whose `transactionalData` is absent or
   unparseable are skipped, never timestamp-matched.
5. **Anchor on the true send time.** Horizon windows start at the Iterable send
   timestamp, never at Waypoint's handoff time — human QA puts up to three days
   between them.
6. **Timestamps with no offset are UTC.** `new Date("2026-08-20 10:00:00")` is
   valid in V8 and is read in the *process-local* zone, and n8n's
   `settings.timezone` governs cron expressions, not `new Date()` inside a Code
   node. Both parsers normalise the offset before parsing.
7. **A failed lookup is not a measured negative.** An Amplitude error, or a
   truncated activity page that does not reach back to the send, leaves the
   horizon NULL for a later night rather than writing a fabricated `false`.

## Auth

`POST /api/outcomes` and `GET /api/funnel/worklist` accept a scoped bearer token
(`OUTCOMES_TOKEN` in Railway). n8n uses that token via a Header Auth credential:

    Name:  Authorization
    Value: Bearer <OUTCOMES_TOKEN>

The token opens those two endpoints and nothing else — it cannot create runs,
read run detail, read the full funnel, or touch the kill switch. n8n therefore
never holds `APP_PASSWORD`, which is full operator access and which n8n persists
in plaintext execution history. A wrong Bearer header is refused outright rather
than falling back to the session cookie, so a leaked-token alarm cannot look
like a success.

The rich funnel (`GET /api/funnel`, with themes, mechanisms and org_ids) requires
**operator** auth. The machine gets `worklist`, which is bare id pairs, for the
same execution-history reason.

`WAYPOINT_API_BASE_URL` must be the **Railway** origin, not the Vercel one. The
browser reaches the API through Vercel's `/api/*` rewrite and is subject to Okta;
Railway is direct and is guarded by this token.

## Credentials

| n8n credential | Type | Fields | Allowed HTTP Request Domains |
|---|---|---|---|
| `waypoint-authorization` | Header Auth | Name `Authorization`, Value `Bearer <OUTCOMES_TOKEN>` | `pathfinder-waypoint-v2-staging.up.railway.app` |
| `waypoint-iterable-api` | Header Auth | Name `Api-Key`, Value your Iterable key (export/read — NOT the LCM send key) | `api.iterable.com` |
| `waypoint-amplitude-api` | Basic Auth | Username = Amplitude API Key, Password = Amplitude Secret Key | `amplitude.com` |

Set **Allowed HTTP Request Domains** to "Specific Domains" on all three. This is
the control that *enforces* the read-only boundary rather than documenting it:
n8n refuses to attach a credential to a request for any other host.

It does not, however, prevent writes to a host that is on the list — Iterable
serves reads and writes from `api.iterable.com` alike. **The Iterable key's own
permissions are the only thing preventing Iterable writes.** Amplitude is
different: its ingestion host is `api2.amplitude.com`, so pinning to
`amplitude.com` does block event writes.

Add the production host to `waypoint-authorization` at cutover. On Amplitude EU
data residency, use `analytics.eu.amplitude.com` here *and* in the two Amplitude
node URLs.

## No environment variables are required

Every value the Code nodes need is a named constant at the top of the node, and
every URL is literal. An n8n with `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` denies all
`$env` reads, and an expression in a URL field cannot be guarded against that.

Two constants you must set before the sweep will run:

| Where | Constant | Meaning |
|---|---|---|
| `Returns to outcomes` | `ACTIVE_USE_EVENTS` | Comma-separated Amplitude event names meaning "came back and used the app". **Empty by default and the node throws** — the canonical active-use contract is still undefined (TODOS.md), and a guessed proxy metric would quietly teach the loop the wrong thing. |
| `Build outcomes`, `Eligible touches` | `INTERNAL_DOMAIN` | The internal email domain, `housecallpro.com`. Any address on it is treated as a guardrailed send. |

Both nodes read an env var of the same name *if* one is readable, and fall back
to the constant otherwise, so either style works.

## Schedule

Sends go out once a day, roughly 50, inside business hours. Polling hourly
around the clock spent 24 runs re-reading a window where nothing had changed, so
the trigger fires **hourly 09:00–15:00 America/Denver** — 7 runs, covering the
send window plus the hours when opens and clicks arrive.

The consequence: the gap from the last fire of one day to the first of the next
is about 18 hours, so the lookback cannot match the cron interval. `Send window`
looks back **25 hours** on every run. Re-reading the same events six more times a
day costs nothing, and it means a missed or failed run self-heals on the next one.

The trigger's timezone is `America/Denver`, which follows DST, so "09:00 local"
stays 09:00 in summer. For a fixed UTC-7 year round, use `America/Phoenix` — one
word in the trigger node. Nothing about the horizon math moves either way: every
window inside the Code nodes is computed from `Date.now()` in UTC.

The horizon sweep cannot be event-driven — "did they come back within 7 days" is
unanswerable until day 7. Half 1 could become an Iterable webhook; keep the cron
as a backstop, since a dropped webhook is otherwise invisible.

## Amplitude rate limits

The Dashboard REST API (`usersearch`, `useractivity`) allows **10 concurrent
requests and 360 queries per hour**. The sweep makes two calls per touch, and on
a steady day four cohorts are in flight at once, so ~50 sends/day means **~400
calls a night**. Unthrottled that earns 429s — and a 429 becomes an *unmeasured*
row, so the sweep would fail safe but quietly measure nothing.

Both Amplitude nodes therefore run at **1 request per 10 seconds**,
sequentially. A full night takes roughly an hour, which is why the trigger is
03:00 and not just before business.

*ponytail: fixed throttle, no id cache. Ceiling: ~360 touches/night before the
sweep runs past the hour. Upgrade paths, in order — cache the `pro_uuid →
amplitude_id` mapping so `usersearch` runs once per Pro rather than once per
touch (halves the calls), or replace per-user lookups with one bulk
`/api/2/export` pull per day and match locally (turns ~400 calls into 1).*

## Failure modes

| What fails | What happens |
|---|---|
| Work list unreachable | Node retries 3×, then the execution aborts. Nothing is posted. Fail-closed but **silent** |
| Work list returns empty | The half is a no-op. Also silent — this is the one to watch |
| Iterable export 5xx | Node retries 3×, then the execution fails. The next run's 25h window re-covers the gap |
| n8n down > 25h (half 1) | Events in the gap are lost. Recover by raising `LOOKBACK_MIN` in `Send window` and running once manually |
| Amplitude `usersearch` no match | The IF node drops the item. No row — never `returned_*: false` |
| Amplitude `useractivity` 5xx | Retries 3×, then `onError: continueRegularOutput` lets the run continue; the item arrives carrying `error` instead of `events` and is **skipped**, not scored. `unmeasured_lookup_failures` is logged |
| Amplitude page truncated | If 1000 events came back and the oldest postdates the send, the window is only partly covered — skipped as unmeasured rather than scored `false` |
| `ACTIVE_USE_EVENTS` unset | Half 2 throws immediately, by design |
| Waypoint 422 | A malformed natural key would reject the whole batch, so bad events are dropped per-event in the Code nodes instead |
| Duplicate/overlapping runs | Harmless — ingestion is idempotent per `(recommendation_id, source)` |

Two log lines make the silent paths visible:
`eligible=N skipped_guardrail=N skipped_unprovable=N` from `Eligible touches`,
and `scored=N unmeasured_lookup_failures=N` from `Returns to outcomes`.

## Assumptions

Each of these is read from documentation, not observed. They are listed in rough
order of how much damage a wrong one does.

1. **`lcmVariant` is exactly `'A'` on our sends.** The filter requires it. If the
   LCM app stamps something else, every touch is dropped — fail-closed, but total
   and silent.
2. **`lcmRouting`, when stamped, uses the exact string `route-to-pro`.** A
   different spelling (`route_to_pro`) maps to `guardrail` in half 1 and skips
   every touch in half 2. Fail-closed, silent. **Not yet stamped at all** — the
   one-line change is requested from the LCM app; until it lands, the
   domain heuristic in rule 1 *is* the evidence gate.
3. **`lcmRun` is the Waypoint `run_id`** and `userId` is the `pro_uuid`.
4. Iterable's export API is `GET /api/export/data.json` with `dataTypeName`,
   `startDateTime`, `endDateTime`, returning NDJSON, authenticated by an
   `Api-Key` header.
5. Iterable event timestamps are `createdAt`, formatted
   `YYYY-MM-DD HH:mm:ss [+00:00]`, and the project's timezone is UTC.
6. Iterable message events carry `transactionalData` as a JSON string, `userId`,
   and `email` (email channel only — SMS carries `phoneNumber`).
7. Amplitude's `usersearch` takes `?user=` and returns `matches[]` with
   `user_id` and `amplitude_id`. We require exactly one *exact* `user_id` match:
   `usersearch` is a partial match, so `matches[0]` could be a different person.
8. Amplitude's `useractivity` takes `?user=<amplitude_id>&limit=`, returns
   `events[]` with `event_type` and `event_time`, in UTC.
9. Amplitude `user_id` equals `pro_uuid`. Measured across 53 live Pros by the
   LCM team: correct on 52. One Apple hide-my-email Pro resolves to a different
   profile.
10. The internal roster is `allison.torres`, `jake.fassora`, `liann.bielicki` on
    `housecallpro.com`. Rule 1 keys on the domain, so a *new* tester on that
    domain is handled correctly without a roster update.
11. Waypoint's `/api/outcomes` accepts a batch array and is idempotent per
    `(recommendation_id, source)`.

## Deploying it

1. In Railway, set `OUTCOMES_TOKEN` (`openssl rand -hex 32`) and redeploy.
2. Confirm the endpoints exist:
   `curl -i -X POST "$WP/api/outcomes" -H "authorization: Bearer $TOK" -H 'content-type: application/json' -d '[]'` → `202`
   `curl -i "$WP/api/funnel/worklist" -H "authorization: Bearer $TOK"` → `200`
3. Create the three n8n credentials above, with Allowed Domains set.
4. Import `outcome-ingestion.workflow.json`. Confirm `Build outcomes → Post
   outcomes` and `Returns to outcomes → Post returns` are connected — n8n has
   dropped these on import before, and without them the flow reads everything
   and writes nothing.
5. Set `ACTIVE_USE_EVENTS` in `Returns to outcomes`.
6. Swap the Railway host in all four Waypoint URLs when you leave staging.
7. Dry-run: disable `Post outcomes` and `Post returns`, execute both halves
   manually, and inspect what *would* have been posted. Check for no arm-B rows,
   `routing` present only where provable, and `sent_at` in UTC.
8. Set n8n's execution-data retention before this runs on real sends — the raw
   Iterable export contains recipient emails and phone numbers.
9. Enable the triggers.

## Open questions

1. **The canonical Amplitude active-use event contract** (TODOS.md). Blocks
   `ACTIVE_USE_EVENTS`, and therefore the whole sweep.
2. **Will the LCM app stamp `lcmRouting`?** One line in its
   `attributionFields()`. Until it does, routing is inferred from the recipient
   domain, which cannot classify SMS at all — so SMS touches are skipped as
   unprovable and no SMS horizon is ever measured. `skipped_unprovable` in the
   run log is the signal.
3. **Which Iterable key type can call the export API?** Their community notes a
   plain Read Only key cannot download user events. Create the most restricted
   key that works, and verify with one export call before trusting it.
4. **Amplitude region.** `amplitude.com` vs `analytics.eu.amplitude.com`.
5. **Retention on n8n execution data**, given raw exports carry PII.
