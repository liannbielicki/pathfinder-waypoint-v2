#!/usr/bin/env bash
# Re-runnable Context Layer pull. Run via: vault run bash docs/context-layer/scripts/pull.sh [org_uuid] [staging|test]
# Key never printed. Requires CONTEXT_LAYER in env (vault run injects it).
set -euo pipefail
ORG="${1:-pro_4c4975d550554bc7ad8b279b14fa3891}"
case "${2:-prod}" in
  prod)    BASE="https://api.internal-success.housecall-internal.com/pro-data" ;;      # confirmed working 2026-09-03
  staging) BASE="https://staging.internal-success.housecall-internal-dev.com/pro-data" ;;
  test)    BASE="https://api.internal-success.housecall-internal-test.com/pro-data" ;;
  *) echo "env must be prod|staging|test"; exit 1 ;;
esac
: "${CONTEXT_LAYER:?run under 'vault run'}"
curl -s -w "\nHTTP %{http_code}\n" -H "Authorization: Bearer $CONTEXT_LAYER" \
  "$BASE/api/context_layer/$ORG"
