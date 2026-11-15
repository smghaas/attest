#!/bin/sh
# Start a public Attest sandbox. Seeds the demo world on first boot, then serves on $PORT.
set -e
export ATTEST_STATE_DIR="${ATTEST_STATE_DIR:-/data/state}"
export ATTEST_LOG_DIR="${ATTEST_LOG_DIR:-/data/logs}"
export ATTEST_SANDBOX="${ATTEST_SANDBOX:-1}"
export ATTEST_DEMO="${ATTEST_DEMO:-1}"
export ATTEST_TRUST_PROXY="${ATTEST_TRUST_PROXY:-1}"
mkdir -p "$ATTEST_STATE_DIR" "$ATTEST_LOG_DIR"
if [ ! -f "$ATTEST_STATE_DIR/agents/supplierco.json" ] && [ ! -f "$ATTEST_STATE_DIR/agents/rcv-supplierco.json" ]; then
  python setup_demo.py > /dev/null
fi
# one worker: the gate keeps its state in one file behind an in-process lock
exec uvicorn gate.server:app --host 0.0.0.0 --port "${PORT:-8402}" --workers 1 --proxy-headers
