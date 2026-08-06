# Preserved audit knowledge

This is the minimum implementation-facing audit packet. It preserves the organs of the old system without importing its architecture.

## Included

- `liann-synthesis.md` — adversarially verified current-state synthesis, keep/rewrite/drop decisions, and rebuild priorities.
- `live-loop-generator.md` — verified live-loop and generator behavior.
- `scoring-cost.md` — canonical scoring, cost, critic, and calibration behavior.
- `persona-contract.md` — persona-card and snapshot semantics, including known limits.
- `integrations.md` and `lcm-export.md` — consolidated external-boundary and handoff evidence.
- `calibration-verification.md` — authoritative correction for the learned calibration artifact and its remaining magnitude caveat.
- `artifact-prompts.md` — exact generator, critic, tool-schema, model, and pricing material worth preserving.
- `n8n/` — the audited `org-context-v2` workflow and operating contract.
- `contracts/` — legacy audience schema, fixture, and SQL boundary references. The production input contract requires separate upstream-clean lineage; these files document identity behavior and do not prove suppression.
- `legacy-code/` — narrowly selected read-only implementations for rubric, persona prompting, canonical scoring, context semantics, winner selection, and LCM payload behavior.
- `legacy-tests/` — focused executable references for context, persona snapshots, scoring, winner selection, and LCM behavior.
- `services/api/data/reaction_churn_calibration_cards.json` — frozen learned calibration artifact used by the verified legacy path.
- `services/api/data/persona_cards_snapshot_2026_07_29.json` — frozen legacy persona snapshot for contract and parity reference.

## Authority and provenance

The approved design and implementation plan override this packet wherever they differ. Raw audit reports remain historical evidence in the legacy repository and should not be copied wholesale.

- Legacy repository: `Codefied/Pathfinder-Waypoint`
- Audit branch: `liann/primed-rebuild-audit`
- Liann synthesis source commit: `e8d71e04`
- Jake audit packet source commit: `2476fe5a`
- Rebuild design base commit: `6ed8fbf5`
- Implementation plan base commit: `edc29825`

The governing design and plan were corrected during this V2 seed for audited integration facts and closed decisions. The initial commit of this repository, rather than the two base commits above, is authoritative for their seeded versions.

Use the legacy branch read-only if deeper provenance is required. Do not copy legacy application structure merely because a source file is referenced here.

## Governing cautions

- The live n8n contract is `org-context-v2`, accepts `org_uuids`, and caps each request at five. Its current operational and performance limits must be measured and addressed for the 200 Pros/day gate.
- The existing calibration was trained on the legacy segment/action-type panel distribution. The approved Pro-matched 3-person/5-person method changes that population. Until a matching-grain validation or refit exists, use the new panel for relative persona-reaction ranking only; do not present per-Pro churn direction, calibrated percentage-point, or causal-lift claims.
- The existing LCM payload is evidence for current behavior, not approval for V2's expanded measurement and lineage fields. Confirm the production payload and receipt fixture with Allison before launch.
