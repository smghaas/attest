"""Did the cold-start sessions stay inside their sandbox? Scans every tool call in a run's transcripts.

    /home/user/townenv/bin/python usability/check_isolation.py usability/runs/<id>

The sessions are told to touch only their own working directory, the drop-box folder and the installed attest
package. That cannot be fully enforced, so this script reports what they actually touched:

  repo         a tool call names the attest repository (/home/user/attest) - the docs-only premise is broken
  other_party  the buyer touched the seller's folder or the other way round (e.g. read the other side's keys)
  gate_files   a tool call names the gate's state or log files (evidence folder) directly
  broad        a filesystem-wide search (find /, grep -r /, ls /home ...) that could discover the repo
  outside      any other absolute path outside the allowed areas and ordinary system paths (a warning)
  exposure     a tool RESULT contained the repo path (e.g. a traceback); not a violation, listed for the grader

Allowed: the session's own working dir, the drop-box, the agents' venv (the installed package), /tmp, and
system paths (/usr, /bin, /lib, /etc, /dev, /proc, /opt/...).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = "/home/user/attest"
REPO_RE = re.compile(r"/home/user/attest(?![\w-])|~/attest(?![\w-])")
ABS_PATH_RE = re.compile(r"(?<![\w.~$}-])(/(?:[\w.@+-]+/?)+)")
BROAD_RE = re.compile(r"\bfind\s+/(\s|$)|\bfind\s+/home|\bfind\s+~|\bgrep\s+-\w*r\w*\s+.*\s/(\s|$)|\bls\s+(-\w+\s+)*/home\b|"
                      r"\blocate\s|\brg\s+.*\s/(\s|$)")
SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/dev", "/proc", "/sys", "/opt", "/var/lib", "/run")


def load_jsonl(p: Path) -> list:
    out = []
    for line in p.read_text().splitlines() if p.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def strings_of(tool_input: dict) -> list[str]:
    """The parts of a tool call that can name a location: paths, patterns and shell commands."""
    return [str(tool_input[k]) for k in ("file_path", "path", "pattern", "glob", "command", "notebook_path")
            if tool_input.get(k)]


def under(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots if r)


def check(run: Path) -> dict:
    run = Path(run)
    meta = json.loads((run / "meta.json").read_text())
    phases = json.loads((run / "phases.json").read_text()) if (run / "phases.json").exists() else []
    d = meta["dirs"]
    gate_dir = str(run / "gate")
    report, total = [], {"repo": 0, "other_party": 0, "gate_files": 0, "broad": 0, "outside": 0, "exposure": 0}
    for ph in phases:
        role = "buyer" if ph["phase"].startswith("buyer") else "seller"
        own, other = d[role], d["seller" if role == "buyer" else "buyer"]
        allowed = [own, d["exchange"], d["venv"], "/tmp"]
        findings = []
        for e in load_jsonl(run / ph["transcript"]):
            content = (e.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    for s in strings_of(c.get("input") or {}):
                        hit = None
                        if REPO_RE.search(s) and gate_dir not in s:
                            hit = "repo"
                        elif gate_dir in s:
                            hit = "gate_files"
                        elif other in s or re.search(rf"\.\./{Path(other).name}\b", s):
                            hit = "other_party"
                        elif BROAD_RE.search(s):
                            hit = "broad"
                        else:
                            bad = [p for p in ABS_PATH_RE.findall(s)
                                   if not under(p, allowed) and not p.startswith(SYSTEM) and p != "/"]
                            if bad:
                                hit = "outside"
                                s = f"{s}   [paths: {', '.join(sorted(set(bad))[:5])}]"
                        if hit:
                            findings.append({"kind": hit, "tool": c.get("name"), "text": s[:400]})
                elif c.get("type") == "tool_result":
                    text = c.get("content")
                    if isinstance(text, list):
                        text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
                    if text and REPO_RE.search(str(text)):
                        i = REPO_RE.search(str(text)).start()
                        findings.append({"kind": "exposure", "tool": "result", "text": str(text)[max(0, i - 150):i + 150]})
        for f in findings:
            total[f["kind"]] += 1
        report.append({"phase": ph["phase"], "role": role, "findings": findings})
    violations = total["repo"] + total["other_party"] + total["gate_files"]
    verdict = "violations" if violations else ("warnings" if total["broad"] + total["outside"] else "clean")
    return {"verdict": verdict, "totals": total, "phases": report}


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: check_isolation.py RUN_DIR")
    out = check(Path(sys.argv[1]))
    print(json.dumps(out, indent=1))
    print(f"\nisolation: {out['verdict']}  {out['totals']}", file=sys.stderr)


if __name__ == "__main__":
    main()
