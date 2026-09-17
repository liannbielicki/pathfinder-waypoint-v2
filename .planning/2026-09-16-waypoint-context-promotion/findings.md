# Findings

- Local V4 was five commits behind remote; the approved stash, fast-forward, and reapply preserved prior Workbench changes and added current Waypoint calls/CI work.
- Existing context and feature catalog versions live in browser localStorage, so the Waypoint worker cannot consume them without a server-side promotion artifact.
- The supplied feature CSV uses `Display Name` as the accepted exact feature key plus `Product Area` and `Value Statement` for compact runtime meaning.
- Workbench source rows can carry `SOURCE_TABLE`; query names are not treated as table lineage.
- Existing Waypoint uses a fixed `OrgBrief`, packaged feature CSV, and the existing `evolve_prompt`; the bridge must preserve that path as fallback.
