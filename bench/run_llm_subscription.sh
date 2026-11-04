#!/bin/bash
# The LLM part of the HW7 benchmark (C2 and C3, Sonnet) on a Claude subscription, through
# headless Claude Code. Run from the repo root after logging in to Claude Code (`claude`, then /login):
#   bench/run_llm_subscription.sh --smoke     one episode per arm, to check the setup (do this first)
#   bench/run_llm_subscription.sh             the full run, about 270 episodes
# If the subscription's usage limit is reached the run stops; run the same command again later and
# it continues where it stopped. Afterwards commit and push bench/results.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-python}
command -v claude >/dev/null || { echo "Claude Code CLI not found: https://code.claude.com/docs"; exit 1; }
$PY -c "import mcp, fastapi, cryptography, httpx" 2>/dev/null || { echo "pip install -r requirements.txt -r requirements-bench.txt"; exit 1; }
claude auth status || { echo "Log in first: run 'claude' and use /login with your subscription account"; exit 1; }
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Note: ANTHROPIC_API_KEY is set in your shell; it is removed for these runs so your subscription is used."
fi
RUN="$PY -m bench.run_bench --via claude-code"

if [ "${1:-}" = "--smoke" ]; then
  $RUN --conditions C2_llm,C3_llm_attest --cases A9 --reps 1 --out smoke-claude-code
  echo
  echo "Smoke run done. Check above: C3 should show check_agent use; the transcripts are in"
  echo "bench/results/smoke-claude-code/transcripts. model_served is in episodes.jsonl:"
  grep -o '"model_served": "[^"]*"' bench/results/smoke-claude-code/episodes.jsonl | sort -u
  exit 0
fi

# 1. main suite: LLM supplier without Attest (C2) and with Attest (C3), 5 reps per case
$RUN --conditions C2_llm,C3_llm_attest --reps 5 --out llm-main
# 2. Part 4 at agent level: does the LLM supplier follow the issuer cap? Same case, cap off vs on
$RUN --suite sybil --conditions C3_llm_attest,C4_attest_only --reps 3 --cap 0 --out llm-sybil-cap0
$RUN --suite sybil --conditions C3_llm_attest,C4_attest_only --reps 3 --cap 1 --out llm-sybil-cap1

echo
echo "Done. Now: git add bench/results && git commit -m 'HW7 LLM benchmark results' && git push"
