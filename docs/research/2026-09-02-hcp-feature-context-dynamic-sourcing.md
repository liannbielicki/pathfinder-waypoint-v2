# HCP Feature Context — Research & a Better (Dynamic) Way to Source It

Date: 2026-09-02
Branch: `V3-Improvements`
Jira: [AISOL-1718](https://housecall.atlassian.net/browse/AISOL-1718) — *"Research HCP product
features, current app vs web capabilities, marketing reasons. (dynamic?)"*
(Story under epic [AISOL-448 "Auto Research"](https://housecall.atlassian.net/browse/AISOL-448).)
Status: **research + recommendation** (no pipeline behavior changed by this doc).

---

## TL;DR

Waypoint's idea generator is fed a hand-curated, statically-packaged 47-row CSV
(`services/api/data/hcp_feature_catalog.csv`, loaded by `waypoint/catalog.py`). It
captures *what a feature is* (a description) and *which CTA URL rendered on which
surface in QA* (`works_on`). It does **not** cleanly capture the two things this
ticket names — **feature-level app-vs-web capability** and **marketing reasons /
value propositions** — and it has no **plan/entitlement** awareness. It also drifts:
the design doc that introduced it already warns "packaged CSV can drift from the docs
copy."

The research surfaced a decisive fact: **HCP is already building the dynamic source of
truth this ticket asks for.** The **Product Context Layer (PCL)** team's Pillar-2
**Feature and Plan Registry** is, almost verbatim, the three dimensions named here —
"canonical description, **value proposition by vertical**, prerequisites, **objection
handling**, related features, known limitations, available MCP tools, and plan mapping."
It renders **FAC (Feature Access Control)** — HCP's system of record for
feature→product→package→plan entitlement — into an agent-readable, *living* registry.

**Recommendation:** don't grow the CSV. Keep Waypoint's one-function interface
(`feature_context(brief, *, feasibility)`) and **swap its body to consume the canonical
sources**, each owning exactly one dimension:

| Dimension in the ticket | Canonical owner (already exists / emerging) |
| --- | --- |
| What features exist + can *this org* access it | **FAC** (system of record) + billing plan API |
| Marketing reasons (value prop by vertical, objections, limitations) | **PCL Feature & Plan Registry** (OCR "value statements" shipped; full registry on PCL roadmap) |
| App vs web capability | **No single clean owner yet** — assembled from Mobile-Only help collection, OCR entry-points/completion-steps (surface-specific), the AI-Actions destination registry, and captured `Context_Layer` KB facts |
| Adoption state (already have) | OCR adoption stages ↔ Waypoint `feature_<key>_state` |

Answer to **"(dynamic?)"**: **yes — staged**, and mostly by *consuming* work already in
flight rather than building our own registry. The one hard engineering constraint is
that Waypoint's evolve loop is **replay-deterministic**, so a dynamic source must be
**snapshotted at run creation** (resolved once, stored on the run), never fetched live
inside the replayable loop. Details and a staged migration are below.

---

## 1. What "this" is today

`catalog.py` loads the CSV once at import and exposes a single function that the evolve
loop appends to `org_context` (read identically by generation, critic, and ranker):

```python
def feature_context(brief: OrgBrief, *, feasibility: bool) -> str: ...
```

For each feature the brief already references — the union of every `feature_<key>_state`
field plus `top_unused_paid_feature` — it emits one line:

```
- online_booking (state: attached_unused): Lets a customer request or schedule a job
  themselves ... without calling the office.  [reachable on: web]   ← only if feasibility=on
```

The CSV columns are `feature, description, cta_id, label, url, works_on, notes`. Its
strengths are real and worth preserving: **select-the-relevant-thing** discipline (no RAG
dump), deterministic ordering, graceful skip of unresolvable keys, and a single shared
context string so the critic never blocks a grounded idea as "ungrounded."

### What it does *not* capture (mapped to the ticket)

1. **App vs web capability — absent at the feature level.** `works_on` is *not* a
   capability statement; it records which **CTA URL** happened to render on `web`/`ios`/
   `mobile` in manual QA, mixed with sentinels (`broken`, `no_cta`, `not_applicable`).
   A feature can be fully usable on both surfaces yet have only a `web` link that QA
   confirmed. Conversely, real capability gaps that matter for a recommendation
   (e.g. *"a custom job type / business unit can only be set on web; the mobile
   scheduling flow can't"* — a documented HCP fact) are nowhere in the data.
2. **Marketing reasons — essentially absent.** There is no `value_prop` field. Only two
   of ~26 features carry an ad-hoc pitch buried in free-text `notes` (`hcp_assist`:
   *"24/7 AI answering that turns missed calls into booked jobs"*; `voip`). The generator
   is told what a feature *is*, never *why a Pro in this vertical should adopt it* — which
   is exactly the persuasive substance a churn/upsell touch needs.
3. **Entitlement — absent.** Nothing knows whether a Pro's plan even *includes* a feature,
   whether it's a paid add-on, or the upgrade path. `top_unused_paid_feature` is the only
   entitlement-ish signal and it arrives pre-computed from the n8n brief.
4. **Freshness — manual and drift-prone.** Static, packaged, "changes rarely," QA'd by
   hand, and explicitly allowed to diverge from the `docs/` copy. When HCP ships, renames,
   or deprecates a feature, nothing updates the catalog.

---

## 2. Research findings

### 2.1 HCP product features — the canonical list is FAC, not a CSV

HCP has a system of record for what features exist and who can access them:
**Feature Access Control (FAC)** maintains the authoritative hierarchy **Features**
(granular capabilities like `reporting`, `jobs.signatures`) → **Products** → **Packages**
→ **Plans / subscriptions / trials**, plus **FAC Events** that fire when an org's
entitlements change. Seed data lives in `db/seeds/access_control/features_factory.rb`
(surfaced in several `WS-*` billing tickets). This is the reference set the CSV's ~26
hand-picked features are an ad-hoc subset of.

The **Onboarding Context Registry (OCR)** — already **shipped and in production** — has
also standardized a first tranche of features as structured objects with *metadata, value
statements, permissions, and completion steps*, mapping each to a `feature_key` in FAC.
Phase-1 OCR features: Business Setup, Automated Communications, Employees, Customers,
Pricing, Jobs, Payments, Reviews, and CSR AI Phone. Its four adoption stages — **Not
Attached → Attached → Activated → Engaged** — are the same shape as Waypoint's
`feature_<key>_state`, so they line up directly.

### 2.2 App vs web capability — real, role-dependent, and *not* one clean source yet

- **Role split is a hard capability boundary.** Field Techs can *only* use the mobile app;
  Admin/Office Staff use both web and mobile. So "reachable on this Pro's surface" depends
  on *who* the touch is aimed at, not just the feature.
- **HCP documents mobile-only capability directly.** The help center has a dedicated
  **"Mobile-Only Features"** collection (e.g. directions to jobs/estimates, on-site
  capture). There is no symmetric "web-only" collection, but web-only gaps are real and
  captured elsewhere: e.g. *custom job type and business unit can only be set on web —
  the mobile scheduling flow cannot add them* (captured HCP walkthrough fact). The mobile
  app is **not yet agentic/voice** and **authenticates against production only**.
- **The catalog's `works_on` ≠ capability.** It's a QA/link-rendering artifact. The clean
  capability signal we actually want ("this feature is usable on web / app / both, for
  this role") has to be assembled from: the Mobile-Only help collection, OCR **entry
  points** and **completion steps** (which are inherently surface-specific), the
  **AI-Actions** registry of curated in-app destinations (managed in the HCP Admin Panel),
  and captured `Context_Layer` knowledge. **This is the weakest-sourced of the three
  dimensions and the biggest genuine gap** (see §5).

### 2.3 Marketing reasons — owned by the Product Context Layer, not yet wired to us

The **Product Context Layer** exists precisely because "agents describe features
incorrectly because there's no canonical source of truth about our own product." Its
**Pillar 2 — Product Context**, **Phase 1: Feature and Plan Registry** promises, per
feature: *canonical description, **value proposition by vertical**, prerequisites,
**objection handling**, related features, known limitations, available MCP tools, and plan
mapping (from FAC)*; per plan: what's included, add-on availability, upgrade paths. PCL is
explicitly a **"living registries, not static docs"** and **"consumers, not owners, of
source data"** team — it renders FAC + billing + telemetry into agent-readable context at
read time.

That is a near-exact match for "HCP product features + marketing reasons," already
entitlement-aware, already meant to be consumed by agents like Waypoint. **Readiness
caveat:** the OCR (onboarding slice) is shipped; the *broader* Feature & Plan Registry is
on the PCL roadmap and its queryable interface/coverage should be confirmed with the PCL
team (William Giuliani's space) before we depend on it (see §5, §6).

### 2.4 The dynamic infrastructure that already exists

| System | What it authoritatively owns | Maturity (as of this research) |
| --- | --- | --- |
| **FAC** (Feature Access Control) | Feature list + product/package/plan hierarchy + per-org entitlement; FAC Events on change | Shipped, system of record |
| **PCL — OCR** | Structured features w/ value statements, permissions, completion steps, adoption stage; `feature_key`→FAC | Shipped, in production |
| **PCL — Feature & Plan Registry** | Value prop by vertical, objection handling, limitations, related features, plan mapping, MCP tools | Roadmap (confirm status) |
| **MCP Tool Catalog** | Which agent-callable API tools exist per feature | Exists; PCL references it |
| **AI Actions** | Curated in-app destinations (pages/modals/links), Admin-Panel-managed | Exists (HCP AI Web) |
| **`Context_Layer` KB** (this session's MCP) | Captured internal facts: app/web quirks, vertical value unlocks, limitations | Live, semantic search |
| **Product Mgmt Confluence + monthly Insights** | What shipped, how pros use it, what's possible today | Maintained by Product Ops |

The takeaway: **the org has already decided static-doc feature knowledge is the wrong
model and is centralizing it into living, entitlement-aware registries.** Waypoint
maintaining its own CSV is the exact anti-pattern PCL was formed to end.

---

## 3. Recommendation — consume canonical sources behind the existing interface

**Keep `feature_context(brief, *, feasibility)` exactly as the pipeline sees it. Swap its
body.** The pipeline, critic, ranker, determinism guarantees, and tests are untouched;
only where the feature facts *come from* changes. This is the same "replaceable body
behind one function" discipline the repo already uses for `warmstart.retrieve` and item
resolution.

### 3.1 Target feature record (the shape to resolve to)

Whether sourced from PCL live or an enriched local file, resolve each feature to:

```jsonc
{
  "feature_key": "service_agreements",          // FAC key — the join anchor
  "display_name": "Service Plans",
  "description": "Recurring maintenance/membership plans ...",   // first-sentence, as today
  "value_prop": {                                // NEW — the "marketing reason"
    "default": "Locks in recurring revenue and repeat visits from existing customers.",
    "by_vertical": { "hvac": "Turns one-off tune-ups into quarterly contracts ...", "...": "..." },
    "objections": ["Setup feels heavy — but enrollment is one flow off an existing job."]
  },
  "available_on": {                              // NEW — real capability, not link-QA
    "web": true, "mobile_app": true,
    "notes": "Plan setup is web-first; techs can enroll a customer from the app.",
    "role_scope": ["admin", "office"]            // Field Techs are mobile-only
  },
  "entitlement": {                               // NEW — from FAC for THIS org
    "included_in_plan": false, "is_addon": true,
    "org_has_access": null,                      // null until resolved per-org
    "upgrade_path": "Max plan or add-on"
  },
  "adoption_state": "attached_unused",           // from brief feature_<key>_state (unchanged)
  "cta": { "label": "Service plans", "url": "https://pro.housecallpro.com/app/service_agreements", "surface": "web" },
  "source": "pcl|local_fallback", "resolved_at": "2026-09-02T...", "version": "..."
}
```

`value_prop`, `available_on`, and `entitlement` are the three gaps closed. `source` +
`resolved_at` + `version` make provenance and staleness legible (and are what the honest
"no evidence"-style fallback keys off).

### 3.2 Determinism — the one non-negotiable constraint

Waypoint's evolve loop is replay-deterministic; the feature block is "additive and
deterministic" today *because the CSV is frozen at import*. A live registry is not frozen.
So: **resolve the feature context once at run creation, snapshot it onto the run/brief,
and have the replayable loop read only the snapshot.** Live calls happen at the
pre-spend/brief-assembly boundary (near `feasibility.py`), never inside generation/critic/
rank. This preserves ledger replay and recorded-call tests unchanged.

### 3.3 Entitlement → the feasibility gate (a bonus quality win)

FAC per-org entitlement plugs straight into the existing pre-spend gate
(`feasibility.py`): **don't spend LLM/persona budget generating a touch anchored on a
feature this org can't access** (unless the objective is explicitly an upgrade nudge, in
which case the `upgrade_path` becomes the pitch). Today the catalog can't know this;
FAC makes it a cheap, correct abstain — the same shape as the existing
`infeasible: <reason>` / `infeasible_channel` path.

---

## 4. Staged migration (thin, honest, reversible)

Each stage stands alone and ships value; none blocks on PCL being fully ready.

- **Stage 0 — enrich the local file, same loader (no external dep).** Add `available_on`,
  `value_prop`, and (optional) `entitlement_default` columns/rows to the packaged file
  (CSV→JSON is cleaner for nested value props). Author values from the Mobile-Only help
  collection, OCR value statements, and `Context_Layer` KB facts. *Immediate* win: the
  generator finally gets marketing reasons and real app/web capability, with zero pipeline
  or determinism change. This is the safe floor and the permanent fallback.
- **Stage 1 — adapter interface.** Refactor `catalog.py` so `feature_context` calls a
  `FeatureResolver` protocol. Ship `LocalFileResolver` (Stage 0 data) as default. No
  behavior change; pure seam for Stage 2.
- **Stage 2 — `PclResolver` behind a flag, snapshot at run creation.** Implement a resolver
  that reads the PCL Feature & Plan Registry (+ FAC entitlement for the run's org),
  resolves the run's referenced features **once**, stores the snapshot on the run, and
  falls back to `LocalFileResolver` on miss/timeout/flag-off. Gate with a Settings flag
  mirroring `CTA_FEASIBILITY_HINTS`. Determinism preserved by §3.2.
- **Stage 3 — entitlement-aware feasibility + freshness.** Wire FAC entitlement into
  `feasibility.py`; subscribe to (or poll) FAC Events / PCL versioning so snapshots reflect
  releases and deprecations. Retire hand-QA of the local file to fallback-only status.

**Recommended immediate action:** do **Stage 0 now** (it closes the two named gaps with no
risk), and **open a dependency conversation with the PCL team** (§6) to confirm the
Feature & Plan Registry's interface and timeline before committing to Stage 2.

---

## 5. Open gaps / risks (stated so nobody expects more)

- **App-vs-web has no single canonical owner.** FAC governs *entitlement*, not *surface
  capability*. Until a feature-level surface-capability field exists (ideally in PCL),
  Stage 0's `available_on` is human-assembled and will be partial. Worth proposing to PCL
  that `available_on` + `role_scope` become first-class registry fields — every agent
  needs it, not just Waypoint.
- **PCL Feature & Plan Registry readiness is unconfirmed.** OCR is shipped; the broader
  registry is roadmap. Do not hard-depend on it before confirming coverage/interface.
  Stage 0/1 deliberately don't.
- **Role targeting needs a brief field.** "Reachable for this Pro" is role-dependent
  (Field Tech = mobile-only). The brief has no per-recipient role/device field today; the
  prior design doc flagged the same limitation. Coarse channel↔surface reasoning is the
  ceiling until that lands.
- **Determinism regression risk** if a live call ever leaks into the replayable loop — the
  snapshot-at-creation rule (§3.2) is load-bearing and must be enforced by a test.
- **Vertical-specific value props** are only as good as PCL's `by_vertical` coverage; a
  missing vertical must fall back to `value_prop.default`, never fabricate.

---

## 6. Who owns the sources (for the dependency conversation)

- **Product Context Layer** (feature registry, value props, objection handling): William
  Giuliani's space — [Team Roadmap](https://housecall.atlassian.net/wiki/spaces/~61e5d714f0ed0400687041dd/pages/4049993881/Product+Context+Layer+Team+Roadmap).
  Platform team, no PM, roadmap driven by consumer need — Waypoint is a textbook consumer.
- **FAC / billing** (entitlement, plan mapping): [Feature Access Control](https://housecall.atlassian.net/wiki/spaces/BILL/pages/3582984296) (BILL space).
- **MCP Tool Catalog** (per-feature callable tools): [AI space](https://housecall.atlassian.net/wiki/spaces/AI/pages/3878912009).
- **Mobile-Only capability**: HCP Help Center "Mobile-Only Features" collection.
- **Living internal facts** (app/web quirks, vertical unlocks): the `Context_Layer` KB
  (this session's `search_knowledge`), and Product Ops' monthly Insights package.

---

## 7. Appendix — evidence pulled during this research

- FAC hierarchy + FAC Events + `feature_key` mapping; PCL "living registries / consumers
  not owners" framing; Pillar-2 Phase-1 registry field list — PCL Team Roadmap
  (Confluence 4049993881).
- OCR shipped scope, adoption stages, value statements, entry points/completion steps —
  same page (OCR MVP section) + linked OCR one-pager/spec.
- App/web capability facts — `Context_Layer` KB: "Mobile cannot set custom job type or
  business unit … only on web"; "mobile app is not yet voice-activated or agentic";
  "mobile app authenticates against production only." Help-center "Mobile-Only Features"
  collection; Field-Tech = mobile-only role split.
- Marketing/value-prop framing — `Context_Layer` KB: "service agreements … recurring
  business model and locked-in revenue" (mid-sized vertical unlock); PCL "value proposition
  by vertical / objection handling."
- Current catalog behavior — `services/api/src/waypoint/catalog.py`,
  `services/api/data/hcp_feature_catalog.csv`, and
  `docs/superpowers/specs/2026-08-24-feature-catalog-context-design.md` (drift limitation
  called out there).
