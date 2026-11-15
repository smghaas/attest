#!/bin/sh
# Regenerate Town's catalog candidate for Attest from the gate's own OpenAPI document.
# Run from the Attest checkout, in an environment where both attest and nandatown are installed.
set -e
D="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
ATTEST_STATE_DIR="$TMP/state" ATTEST_LOG_DIR="$TMP/logs" \
  python -c "import json; from gate.server import app; print(json.dumps(app.openapi(), indent=1))" > "$TMP/openapi.json"
nandatown onramp "$TMP/openapi.json" --name attest --out "$TMP/out"
rm -rf "$D/attest" && cp -r "$TMP/out/attest" "$D/attest" && cp "$TMP/out/catalog.json" "$D/catalog.json"
cp "$TMP/openapi.json" "$D/attest/openapi.json"
rm -rf "$TMP"
