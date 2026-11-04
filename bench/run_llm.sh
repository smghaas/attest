#!/bin/bash
# The LLM part of the HW7 benchmark (C2 and C3, Sonnet). Run from the repo root with an API key:
#   ANTHROPIC_API_KEY=sk-ant-... bench/run_llm.sh
# About 270 episodes; roughly $5-10 and 20-40 minutes. Results go to bench/results/llm-*;
# commit and push them afterwards. Re-running overwrites those directories.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-python}
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY first}"
$PY -c "import anthropic, fastapi, cryptography" 2>/dev/null || { echo "pip install -r requirements.txt -r requirements-bench.txt"; exit 1; }

# 1. main suite: LLM supplier without Attest (C2) and with Attest (C3), 5 reps per case
$PY -m bench.run_bench --conditions C2_llm,C3_llm_attest --reps 5 --out llm-main
# 2. Part 4 at agent level: does the LLM supplier follow the issuer cap? Same case, cap off vs on
$PY -m bench.run_bench --suite sybil --conditions C3_llm_attest,C4_attest_only --reps 3 --cap 0 --out llm-sybil-cap0
$PY -m bench.run_bench --suite sybil --conditions C3_llm_attest,C4_attest_only --reps 3 --cap 1 --out llm-sybil-cap1

echo
echo "Done. Now: git add bench/results && git commit -m 'HW7 LLM benchmark results' && git push"
