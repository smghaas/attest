"""Run one C2/C3 episode through headless Claude Code (`claude -p`), so the LLM conditions
can run on a Claude subscription instead of API credits.

Same system prompt, kickoff, model, effort, tools and budget as the API harness. Claude
Code's own system prompt is replaced (--system-prompt), its built-in tools are off
(--tools ""), and the only tools are the desk's (bench/desk_mcp.py). ANTHROPIC_API_KEY is
removed from the child's environment so the subscription login is used.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .llm_supplier import EFFORT, KICKOFF, MODEL, system_prompt

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
if CLAUDE.lower().endswith((".cmd", ".bat")):
    # Windows npm shim: cmd.exe would mangle the multi-line --system-prompt argument
    sys.exit("Found the npm 'claude.cmd' wrapper. Install the native Claude Code build instead "
             "(PowerShell: irm https://claude.ai/install.ps1 | iex) or set CLAUDE_BIN to claude.exe.")
TIMEOUT_PER_ITEM_S = 240
TOOLS = ["inbox_next", "view_documents", "quote_price", "check_agent", "fulfil", "decline", "escalate"]
LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|too many requests|overloaded|\b429\b", re.I)


class UsageLimit(RuntimeError):
    pass


def cli_version() -> str:
    try:
        return subprocess.run([CLAUDE, "--version"], capture_output=True, encoding="utf-8", errors="replace",
                              timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def run_episode(case, rep: int, cond: str, cap, n_items: int) -> dict:
    """Returns {state (desk output), transcript, result event, model, wall_s}."""
    tmp = Path(tempfile.mkdtemp(prefix="attest-cc-"))
    try:
        out = tmp / "desk.json"
        args = [str(ROOT / "bench" / "desk_mcp.py"), "--case", case.id, "--rep", str(rep), "--cond", cond,
                "--out", str(out)] + (["--cap", str(cap)] if cap is not None else [])
        (tmp / "mcp.json").write_text(json.dumps({"mcpServers": {"desk": {"command": sys.executable, "args": args}}}))
        with_attest = cond == "C3_llm_attest"
        allowed = [f"mcp__desk__{t}" for t in TOOLS if with_attest or t != "check_agent"]
        work = tmp / "cwd"
        work.mkdir()
        cmd = [CLAUDE, "-p", KICKOFF, "--model", MODEL, "--effort", EFFORT,
               "--system-prompt", system_prompt(with_attest), "--tools", "",
               "--mcp-config", str(tmp / "mcp.json"), "--strict-mcp-config",
               "--allowedTools", ",".join(allowed), "--disable-slash-commands", "--no-session-persistence",
               "--output-format", "stream-json", "--verbose"]
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        if os.environ.get("ATTEST_CC_KEEP_API_ENV"):     # tests only: talk to a stub or fake endpoint
            env = dict(os.environ)
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=work, env=env, capture_output=True, encoding="utf-8", errors="replace",
                               timeout=TIMEOUT_PER_ITEM_S * n_items)
            stdout, stderr = p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            raw = e.stdout or ""
            stdout, stderr = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw, "timeout"
        wall = time.time() - t0
        events = []
        for line in stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
        text = json.dumps(result or {}) + stderr
        if (result is None or result.get("is_error")) and LIMIT_RE.search(text):
            raise UsageLimit((result or {}).get("result") or stderr.strip()[-300:])
        if result is None and not out.exists():
            raise RuntimeError(f"claude produced no result: {stderr.strip()[-500:]}")
        state = json.loads(out.read_text()) if out.exists() else {}
        transcript = [e for e in events if e.get("type") in ("assistant", "user")]
        return {"state": state, "transcript": transcript, "result": result or {}, "wall_s": wall,
                "model": init.get("model"), "tools_seen": init.get("tools"), "stderr": stderr.strip()[-500:]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
