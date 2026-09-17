# Findings

- V4 was clean but 23 commits behind; it was fast-forwarded to `origin/V4-Improvements` at `ebb2b86`.
- The exact ignored `services/api/.env` is present and matches the previously configured Workbench env by SHA-256; no values were displayed.
- The experimental n8n response contract is a list of `QUERY_NAME`, `VARIABLE_NAME`, `VALUE`, and `METADATA` rows; 600-plus rows are intentional discovery input.
- The Context Layer exposes one full-org GET endpoint, not a per-feature route. Full-catalog coverage must be computed locally against that response.
- Current authoring loses observed type/state, accepts unverified mappings, and mixes broad source values into runtime context.
- Current browser versions overwrite an ID and do not version uploaded feature catalogs.
- Current UI defaults to both/runtime/compare and renders a six-column table, obscuring the intended authoring journey.
- Focused baseline: backend 14 tests passed; frontend 2 tests passed; focused ESLint passed. The `pnpm` wrapper hung, while direct local binaries worked.
