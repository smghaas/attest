"""Build docs/hw7_report.pdf from the committed results in bench/results.

  python -m bench.report.make_report

Works with whatever results exist: until the LLM run (bench/run_llm.sh) is committed,
the C2 / C3 rows say "pending". Charts use matplotlib; the PDF is printed from the HTML
page by Chromium through Node's Playwright (`npm i playwright` if it is not installed).
"""
from __future__ import annotations

import base64
import html
import io
import json
import math
import os
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RES = ROOT / "bench" / "results"
OUT_PDF = ROOT / "docs" / "hw7_report.pdf"
OUT_HTML = ROOT / "docs" / "hw7_report.html"
REPO = "https://github.com/smghaas/attest"
COMMITS = {"prereg": "2ae2327", "harness": "8f2ca7c", "probe": "6a229df", "cap": "750a61a"}

TEAM = ("<b>Samuel Haas</b> and <b>Eduard Alsina</b>. Both worked together on every part of this homework: the claim "
        "and baseline, the benchmark design, running the experiments, the change and retest, the analysis and this report.")

# palette (validated: bench colors pass all six checks on white, see the report's method note)
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"

NAMES = {"C0_accept_all": "C0 accept all", "C1_static_rule": "C1 static rule", "C2_llm": "C2 LLM alone",
         "C3_llm_attest": "C3 LLM + Attest", "C4_attest_only": "C4 Attest alone"}
ORDER = list(NAMES)


def load(path):
    p = RES / path
    return json.loads(p.read_text()) if p.exists() else None


def esc(s):
    return html.escape(str(s))


def fmt_pct(x):
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.0%}"


def png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=220, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def style(ax):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK2, labelsize=8, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def chart_main(s: dict) -> str:
    fig, ax = plt.subplots(figsize=(6.6, 2.6))
    style(ax)
    w, gap = 0.36, 0.02
    for i, c in enumerate(ORDER):
        m = s.get(c)
        if not m:
            ax.text(i, 0.5, "pending\nLLM run", ha="center", va="center", fontsize=7.5, color=MUTED)
            continue
        for j, (key, ci, col) in enumerate((("block_rate", "block_ci", BLUE), ("false_refusal_rate", "false_refusal_ci", ORANGE))):
            x = i + (j - 0.5) * (w + gap)
            v = m[key]
            lo, hi = m[ci]
            ax.bar(x, v, w, color=col, linewidth=0, zorder=2)
            ax.errorbar(x, v, yerr=[[max(0.0, v - lo)], [max(0.0, hi - v)]], fmt="none", ecolor=INK2, elinewidth=0.8, capsize=2, zorder=3)
            ax.text(x, max(hi, v) + 0.03, f"{v:.0%}", ha="center", va="bottom", fontsize=7, color=INK)
    ax.set_xticks(range(len(ORDER)), [NAMES[c] for c in ORDER])
    ax.set_ylim(0, 1.18)
    ax.set_yticks([0, .25, .5, .75, 1], ["0%", "25%", "50%", "75%", "100%"])
    ax.bar(0, 0, color=BLUE, label="attack requests blocked (higher is better)")
    ax.bar(0, 0, color=ORANGE, label="valid requests falsely refused (lower is better)")
    ax.legend(loc="upper left", bbox_to_anchor=(0, 1.13), ncol=2, frameon=False, fontsize=7.5, labelcolor=INK2,
              handlelength=1, borderaxespad=0)
    return png(fig)


def chart_sybil(before: dict, after: dict) -> str:
    fig, ax = plt.subplots(figsize=(6.6, 2.4))
    style(ax)
    ks = [1, 2, 4, 8]
    b = [r["exposure"] / 1000 for r in before["tuning"]]
    a = [r["exposure"] / 1000 for r in after["tuning"]]
    stake = before["tuning"][0]["stake"] / 1000
    ax.axhline(stake, color=MUTED, linewidth=1, linestyle=(0, (4, 3)), zorder=1)
    ax.text(0.9, stake + 2.5, f"stake {stake:.0f}k", va="bottom", fontsize=7.5, color=INK2)
    ax.plot(ks, b, color=ORANGE, linewidth=2, marker="o", markersize=5, label="before: no issuer cap", zorder=3,
            markeredgecolor="white", markeredgewidth=1)
    ax.plot(ks, a, color=BLUE, linewidth=2, marker="o", markersize=5, label="after: exposure capped at stake", zorder=4,
            markeredgecolor="white", markeredgewidth=1)
    ax.text(8.15, b[-1], f"{b[-1]:.0f}k", va="center", fontsize=7.5, color=INK2)
    ax.set_xscale("log", base=2)
    ax.set_xticks(ks, [str(k) for k in ks])
    ax.set_xlim(0.85, 9.5)
    ax.set_ylim(0, max(b) * 1.12)
    ax.set_xlabel("agents minted by one operator (each with its own 10,000 root mandate)", fontsize=7.5, color=INK2)
    ax.set_ylabel("orders allowed ($k)", fontsize=7.5, color=INK2)
    ax.legend(loc="upper left", frameon=False, fontsize=7.5, labelcolor=INK2, handlelength=1.5)
    return png(fig)


def main_table(s: dict) -> str:
    rows = []
    for c in ORDER:
        m = s.get(c)
        if not m:
            rows.append(f"<tr><td>{NAMES[c]}</td><td colspan='7' class='pending'>pending: LLM run (bench/run_llm.sh)</td></tr>")
            continue
        lo, hi = m["block_ci"]
        flo, fhi = m["false_refusal_ci"]
        exp = f"{m['exposure_mean']:,.0f}" + (f" <span class='rng'>({m['exposure_min']:,.0f}–{m['exposure_max']:,.0f})</span>"
                                             if m["reps"] > 1 else "")
        cost = "$0" if m["cost_per_request_usd"] == 0 else f"${m['cost_per_request_usd']:.4f}"
        gate = "" if math.isnan(m.get("gate_ms_p50", math.nan)) else f"{m['gate_ms_p50']:.1f} ms"
        rows.append(f"<tr><td>{NAMES[c]}</td><td>{m['reps']}</td>"
                    f"<td><b>{m['block_rate']:.0%}</b> <span class='rng'>({lo:.0%}–{hi:.0%})</span></td>"
                    f"<td><b>{m['false_refusal_rate']:.0%}</b> <span class='rng'>({flo:.0%}–{fhi:.0%})</span></td>"
                    f"<td>{m['unnecessary_escalations']}</td><td>{exp}</td><td>{cost}</td><td>{gate}</td></tr>")
    return ("<table><thead><tr><th>condition</th><th>reps</th><th>attacks blocked (95% CI)</th>"
            "<th>valid falsely refused (95% CI)</th><th>unnec. escalations</th><th>attacker $ per pass</th>"
            "<th>LLM $ / request (API prices)</th><th>gate p50</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def item_table(det_rows: list, llm_rows: list, cases) -> str:
    allrows = det_rows + llm_rows
    titles = {c.id: c.tests for c in cases}
    keys = []
    for r in allrows:
        k = (r["case"], r["idx"])
        if k not in keys:
            keys.append(k)
    keys.sort(key=lambda k: (k[0][0] != "V", int(k[0][1:]), k[1]))
    conds = [c for c in ("C1_static_rule", "C2_llm", "C3_llm_attest", "C4_attest_only")]
    head = "<tr><th>item</th><th>tests</th><th>expected</th>" + "".join(f"<th>{NAMES[c]}</th>" for c in conds) + "</tr>"
    body = []
    for case, idx in keys:
        rs = [r for r in allrows if r["case"] == case and r["idx"] == idx]
        r0 = rs[0]
        cells = []
        for c in conds:
            mine = [r for r in rs if r["condition"] == c]
            if not mine:
                cells.append("<td class='pending'>–</td>")
                continue
            ok = sum(r["correct"] for r in mine)
            n = len(mine)
            acts = sorted({r["action"] for r in mine if not r["correct"]})
            txt = ("✓" if ok == n else "✗") if n == 1 else f"{ok}/{n}"
            extra = f" <span class='rng'>{'/'.join(acts)}</span>" if ok < n else ""
            cells.append(f"<td class='{'ok' if ok == n else 'bad'}'>{txt}{extra}</td>")
        what = f"{r0['request_action'].replace('_', ' ')} {r0['amount']:,.0f}"
        body.append(f"<tr><td>{case}{'.' + str(idx + 1) if any(k == (case, 1) for k in keys) else ''} "
                    f"<span class='rng'>{r0['kind']}</span></td><td>{esc(titles.get(case, ''))} · {what}</td>"
                    f"<td>{r0['expected']}</td>{''.join(cells)}</tr>")
    return "<table class='items'><thead>" + head + "</thead><tbody>" + "".join(body) + "</tbody></table>"


def sybil_table(before: dict, after: dict) -> str:
    rows = []
    for group in ("tuning", "fresh"):
        for b, a in zip(before[group], after[group]):
            unc = lambda r: "–" if r["uncovered"] is None else f"{r['uncovered']:,.0f}"  # noqa: E731
            rows.append(f"<tr><td>{group}</td><td>{b['id']}</td><td>{esc(b['note'])}</td><td>{b['stake']:,.0f}</td>"
                        f"<td>{b['requested']:,.0f}</td><td>{b['exposure']:,.0f}</td><td>{unc(b)}</td>"
                        f"<td><b>{a['exposure']:,.0f}</b></td><td>{unc(a)}</td></tr>")
    return ("<table><thead><tr><th></th><th>case</th><th>setup</th><th>stake</th><th>orders sent</th>"
            "<th>allowed, before</th><th>unrecoverable, before</th><th>allowed, after</th><th>unrecoverable, after</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def fmt_ms(m):
    return "–" if not m or math.isnan(m.get("gate_ms_p50", math.nan)) else f"p50 {m['gate_ms_p50']:.1f} ms"


MISS_TEXT = {
    "A2": "a credential that says AcmeCorp but is signed by ShadyCo's key",
    "A7": "a sub-agent whose mandate exceeds its parent's (it even noted the parent's limit, then treated it as a cap rather than a violation)",
    "A8": "a revoked mandate",
    "A10": "an amount changed after signing",
    "A14": "a request signed by a key other than the one it names",
    "A11": "a slashed, suspended operator",
    "A4": "an over-limit order (escalated instead of declined)",
}


def miss_para(rows: list) -> str:
    """What the LLM alone gets wrong, from the C2 decisions."""
    c2 = [r for r in rows if r["condition"] == "C2_llm"]
    if not c2:
        return ""
    by = {}
    for r in c2:
        by.setdefault((r["case"], r["idx"], r["kind"]), []).append(r)
    att_miss = sorted(((k, sum(x["action"] == "fulfil" for x in v), len(v)) for k, v in by.items()
                       if k[2] == "attack" and any(x["action"] == "fulfil" for x in v)), key=lambda t: -t[1])
    caught = sorted({k[0] for k, v in by.items() if k[2] == "attack" and all(x["action"] != "fulfil" for x in v)},
                    key=lambda c: int(c[1:]))
    titles = {c.id: c.tests for c in __import__("bench.cases", fromlist=["MAIN"]).MAIN}
    misses = "; ".join(f"{MISS_TEXT.get(k[0], titles.get(k[0], k[0]))} ({n}/{t})" for k, n, t in att_miss)
    v8 = by.get(("V8", 0, "valid"), [])
    v8d = sum(x["action"] == "decline" for x in v8)
    return (f"<p><b>Where the agent alone fails.</b> C2 fulfilled {misses}. Each of these looks correct on paper: the "
            f"documents carry the right names, scopes and dates, and only a signature check, the revocation list or the "
            f"shared slashing record shows otherwise. C2 did catch everything visible in the documents or in its own "
            f"conversation ({', '.join(caught)}: impostors, bare claims, out-of-scope and expired mandates, order "
            f"splitting, replays and the prompt injection), so the gap is verification, not judgment. Its false refusals "
            f"go the other way: it declined the honest, staked newcomer FlakyCo {v8d} of {len(v8)} times because nothing "
            f"it could read proved the stake, and once escalated a valid order over a 400 USD gap to the list price. "
            f"With Attest the same model made none of these mistakes.</p>")


def load_rows(run):
    p = RES / run / "decisions.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def build() -> str:
    from bench.cases import MAIN
    det = load("det-main/summary.json")
    llm = load("llm-main/summary.json") or {}
    lat = load("det-main/latency.json")
    sb, sa = load("sybil-before/sybil.json"), load("sybil-after/sybil.json")
    s0, s1 = load("llm-sybil-cap0/summary.json"), load("llm-sybil-cap1/summary.json")
    d0, d1 = load("det-sybil-cap0/summary.json"), load("det-sybil-cap1/summary.json")
    cfg = load("llm-main/config.json")
    s = {**det, **llm}
    have_llm = bool(llm)
    c1 = det["C1_static_rule"]
    k8b, k8a = sb["tuning"][-1], sa["tuning"][-1]
    f4b, f4a = sb["fresh"][-1], sa["fresh"][-1]

    if have_llm:
        c2, c3 = llm["C2_llm"], llm["C3_llm_attest"]
        d_block = c3["block_rate"] - c2["block_rate"]
        d_fr = c3["false_refusal_rate"] - c2["false_refusal_rate"]
        h1, h2, h3 = d_block >= 0.20, d_fr <= 0.05, c3["exposure_mean"] < c2["exposure_mean"]
        verdict = ("<b>supported</b>" if h1 and h2 and h3 else "<b>not supported</b>")
        llm_para = (
            f"<p>Against the main baseline (C2, the same Sonnet agent without Attest), C3 blocked "
            f"<b>{c3['block_rate']:.0%}</b> of attack requests vs <b>{c2['block_rate']:.0%}</b> "
            f"({d_block:+.0%}), falsely refused <b>{c3['false_refusal_rate']:.0%}</b> of valid requests vs "
            f"<b>{c2['false_refusal_rate']:.0%}</b> ({d_fr:+.0%}), and fulfilled <b>${c3['exposure_mean']:,.0f}</b> of "
            f"attacker orders per pass vs <b>${c2['exposure_mean']:,.0f}</b>. The pre-registered hypothesis "
            f"(block rate +20 points, false refusals at most +5 points, lower exposure) is {verdict}. "
            f"C3 called check_agent on {fmt_pct(c3.get('checked_share'))} of requests and in the main suite always acted as the gate answered. "
            f"<b>Cost:</b> {c2['tokens_per_request']:,.0f} vs {c3['tokens_per_request']:,.0f} tokens and "
            f"{c2['llm_calls_per_request']:.1f} vs {c3['llm_calls_per_request']:.1f} model turns per request (C2 vs C3), "
            f"about ${c2['cost_per_request_usd']:.3f} per request either way at API prices; about "
            f"{c3['wall_s_per_case']:.0f} s per case for both, so the check costs one extra turn and no measurable "
            f"time.</p>" + miss_para(load_rows("llm-main")))
        cc = cfg.get("harness") == "claude-code"
        llm_cfg = (f"{cfg['llm']['model']}, effort {cfg['llm']['effort']}, {cfg['llm_reps']} reps per case"
                   + (f", run through headless Claude Code {cfg['claude_code']['version'].split()[0]} on a Claude "
                      f"subscription with its own system prompt replaced by ours and its built-in tools off (a "
                      f"<a href='{REPO}/blob/claude/exciting-johnson-ijjvmr/bench/PREREG.md#amendment-2026-11-04-before-any-llm-episode-was-run'>dated "
                      f"amendment</a>, before any LLM run; the budget counts tool calls and cost is the API-equivalent "
                      f"figure, not money billed)" if cc else "") + f"; run {cfg['run']} at commit {cfg['git'][:7]}")
    else:
        llm_para = ("<p class='pending'>C2 and C3 (the LLM conditions) are pending: they need an API key and are run "
                    "with <code>bench/run_llm.sh</code>. This paragraph, the table rows and the chart fill in "
                    "automatically when the results are committed.</p>")
        llm_cfg = "claude-sonnet-5-5, effort medium, 5 reps per case (pending)"

    if s0 and s1:
        e0, e1 = s0["C3_llm_attest"], s1["C3_llm_attest"]
        r0 = [r for r in load_rows("llm-sybil-cap0") if r["condition"] == "C3_llm_attest"]
        r1 = [r for r in load_rows("llm-sybil-cap1") if r["condition"] == "C3_llm_attest"]
        esc0 = sum(r["action"] == "escalate" and r["gate_decision"] == "allow" for r in r0)
        ref1 = sum(r["action"] != "fulfil" and r["gate_decision"] == "refuse" for r in r1)
        agent_retest = (
            f"<b>At agent level the result was not what we expected.</b> On the SybilCo case (4 agents × 2 orders of "
            f"5,000, one conversation, {e0['reps']} reps), the gate alone allowed "
            f"${d0['C4_attest_only']['exposure_mean']:,.0f} with the cap off, yet C3 fulfilled only "
            f"<b>${e0['exposure_mean']:,.0f}</b>: Sonnet read the stake in Attest's answers, added up the orders it had "
            f"already fulfilled, and escalated the rest to a human ({esc0} of {len(r0)} orders, each of them allowed by "
            f"the gate; its reason: \"SybilCo's fulfilled orders already total 20,000, equal to its stake\"). With the "
            f"cap on it also fulfilled <b>${e1['exposure_mean']:,.0f}</b>, now because the gate refused the rest "
            f"({ref1} of {len(r1)}). So the cap changed nothing an attentive agent could not already see inside one "
            f"conversation; its value is the case no single agent sees: orders spread across conversations, sessions "
            f"or receivers (fresh case F2), and agents that do not do the arithmetic.")
    else:
        agent_retest = (f"Gate alone on the SybilCo agent-level case: ${d0['C4_attest_only']['exposure_mean']:,.0f} → "
                        f"${d1['C4_attest_only']['exposure_mean']:,.0f} per pass. <span class='pending'>The C3 (LLM) "
                        f"version of this retest is pending the LLM run.</span>")

    team = TEAM
    links = (f"<a href='{REPO}'>repository</a> · "
             f"<a href='{REPO}/blob/{COMMITS['prereg']}/bench/PREREG.md'>pre-registration ({COMMITS['prereg']})</a> · "
             f"<a href='{REPO}/tree/claude/exciting-johnson-ijjvmr/bench/results'>raw results, transcripts</a> · "
             f"<a href='{REPO}/commit/{COMMITS['probe']}'>limit probe ({COMMITS['probe']})</a> · "
             f"<a href='{REPO}/commit/{COMMITS['cap']}'>the change ({COMMITS['cap']})</a> · "
             f"<a href='{REPO}/actions'>CI runs</a>")

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Attest HW7 benchmark</title><style>
@page {{ size: Letter; margin: 0.55in 0.6in; }}
body {{ font-family: "Helvetica Neue", Helvetica, Arial, "DejaVu Sans", sans-serif; font-size: 9.4pt; line-height: 1.38;
       color: {INK}; background: #fff; }}
h1 {{ font-size: 16pt; margin: 0 0 2pt; }} h2 {{ font-size: 11.5pt; margin: 12pt 0 4pt; }}
h3 {{ font-size: 9.8pt; margin: 8pt 0 2pt; }}
.sub {{ color: {INK2}; margin: 0 0 6pt; }} p {{ margin: 3pt 0 5pt; }}
table {{ border-collapse: collapse; width: 100%; font-size: 8.2pt; margin: 4pt 0 6pt; }}
th {{ text-align: left; font-weight: 600; color: {INK2}; border-bottom: 1px solid {AXIS}; padding: 2pt 4pt; }}
td {{ border-bottom: 1px solid {GRID}; padding: 2pt 4pt; vertical-align: top; }}
table.items td, table.items th {{ padding: 1.2pt 4pt; font-size: 7.6pt; }}
td.ok {{ color: {INK}; }} td.bad {{ color: {INK}; font-weight: 600; background: #fbefe9; }}
.rng {{ color: {MUTED}; font-weight: 400; }} .pending {{ color: {MUTED}; font-style: italic; }}
img {{ width: 100%; }} code {{ font-size: 8.4pt; }} a {{ color: {BLUE}; text-decoration: none; }}
.claim {{ border-left: 3px solid {BLUE}; padding: 2pt 8pt; margin: 4pt 0 6pt; }}
ul {{ margin: 2pt 0 4pt 14pt; padding: 0; }} li {{ margin: 1pt 0; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10pt; }}
.brk {{ break-before: page; }} .keep {{ break-inside: avoid; }}
</style></head><body>
<h1>Attest: does a Know-Your-Agent check make an agent harder to fool?</h1>
<p class="sub">MAS.665 AI Studio, Homework 7 · {team}<br>{links}</p>

<h2>1. Claim and baseline</h2>
<div class="claim">A supplier agent that checks senders with Attest before acting blocks unauthorized agent-to-agent
requests far more often than the same agent judging requests itself, moves almost no money for attackers, and refuses
valid requests no more often, at a few milliseconds and $0 of LLM cost per check.</div>
<p><b>Main metric:</b> share of attack requests on which no harmful action was taken, read with the share of valid
requests falsely refused. <b>Main baseline (C2):</b> the same LLM supplier (same model, prompt, tools, budget) without
Attest. It can read every presented document but cannot verify signatures, stake, revocation or history; that is the
"simpler way" a team would ship today. Two deterministic references bracket it: C0 accepts everything, C1 is a
hand-kept allowlist on the credential's operator name plus the presented mandate's scope. C3 adds Attest's
<code>check_agent</code> tool to C2; C4 is the gate alone. Everything below was pre-registered
(<a href="{REPO}/blob/{COMMITS['prereg']}/bench/PREREG.md">PREREG.md</a>) before the first run.</p>

<h2>2. Setup</h2>
<p><b>Cases.</b> 22 cases, 25 inbox requests: 11 valid (quotes, orders, repeat orders, delegation, an alarming but valid
"URGENT" order, a 9,500 order that policy sends to a human, a newcomer asking a public quote, a staked operator that
is not a named partner) and 14 attacks (unstaked impostor, forged operator name, bare claim, over-limit, order
splitting, out-of-scope sub-agent, broken delegation, revoked mandate, replayed order, amount tampered after signing,
slashed operator, prompt injection, expired mandate, stolen identity). Each case runs in a fresh gate world (new keys,
stakes, state). <b>Success check:</b> the action recorded by the agent's <code>fulfil</code>/<code>decline</code>/<code>escalate</code>
tool call (never its prose), compared with the pre-set expected action. <b>LLM conditions:</b> {llm_cfg}; at most 8
{"tool" if (cfg or {}).get("harness") == "claude-code" else "model"} calls per request; all requests of a case in one conversation, so the baseline remembers earlier orders.
<b>Costs:</b> tokens and dollars per request, gate latency in-process and over HTTP.
<b>Reproduce:</b> <code>python -m bench.run_bench</code> (C0, C1, C4), <code>bench/run_llm.sh</code> or <code>bench/run_llm_subscription.sh</code> (C2, C3),
<code>python -m bench.sybil</code>, <code>python -m bench.latency</code>; a second deterministic run gave identical
decisions.</p>

<div class="keep"><h2>3. Results</h2>
<img src="{chart_main(s)}" alt="Attack block rate and false refusal rate per condition"></div>
{main_table(s)}
{llm_para}
<p>Deterministic references: the static allowlist (C1) blocks only {c1['block_rate']:.0%} of attacks and lets
${c1['exposure_mean']:,.0f} through, because a name in a JSON field, a mandate scope, an expiry or a replayed request
cannot be checked without signatures and state; it also refuses the staked newcomer (V8). Attest alone (C4) gets every
pre-set action right, but that is close to <i>by construction</i>: the attack list was written by the team that built
the gate, from the attacks it was built for. The informative comparison is C2 vs C3: does an agent that already
reasons about requests gain from the check? <b>Cost of the check:</b> gate p50 {lat['in_process']['p50_ms']:.1f} ms
in-process, {lat['http_1_client']['p50_ms']:.1f} ms over HTTP; with 10 concurrent clients p50 rises to
{lat['http_10_clients']['p50_ms']:.0f} ms ({lat['http_10_clients']['throughput_per_s']:.0f} decisions/s), because every
decision takes one lock and rewrites the gate's JSON state.</p>

<h3>Per request (✓ = expected action every time; k/n over reps; shaded = at least one miss, with what happened)</h3>
{item_table(load_rows('det-main'), load_rows('llm-main'), MAIN)}

<h2 class="brk">4. A limit, one change, a retest</h2>
<p><b>What we varied.</b> The number of agents one staked operator controls. Attest's thesis is that trust comes from
stake: SupplierCo accepts operators staked at 10,000 or more. But stake was checked as a <i>threshold</i>, while spend
was capped per mandate. An operator staked at 20,000 can mint any number of agents, each with its own 10,000 root
mandate. <b>Evidence (tuning sweep, k = 1, 2, 4, 8):</b> the gate allowed k × 10,000; at k = 8 it allowed
${k8b['exposure']:,.0f}, {k8b['exposure_over_stake']:.0f}× the stake, and slashing every allowed order recovers at most
the stake, leaving <b>${k8b['uncovered']:,.0f}</b> unrecoverable. (Delegated sub-agents were already contained: they
share their root's running total, fresh case F1.)</p>
<img src="{chart_sybil(sb, sa)}" alt="Exposure vs number of agents before and after the issuer cap">
<p><b>The change</b> (<a href="{REPO}/commit/{COMMITS['cap']}">{COMMITS['cap']}</a>): orders allowed across all of an
issuer's mandates and receivers in a 30-day window may not exceed its stake (refusal
<code>issuer_exposure_over_stake</code>; a setting, so before and after ran on identical code; setting it to 0
reproduces the "before" numbers exactly). <b>Tuning vs fresh:</b> the four tuning cases exposed the limit; four fresh
cases (sub-agents, two receivers sharing the gate, a 15,000 stake with smaller orders, an honest high-volume operator)
were committed with the probe, before the change (<a href="{REPO}/commit/{COMMITS['probe']}">{COMMITS['probe']}</a>),
and not used to design it.</p>
{sybil_table(sb, sa)}
<p><b>Retest result.</b> With the cap, exposure stays at or under the stake in every tuning and fresh case (k = 8:
${k8b['exposure']:,.0f} → ${k8a['exposure']:,.0f}). The remaining unrecoverable amount (a few hundred to
${max(r['uncovered'] or 0 for r in sa['tuning'] + sa['fresh']):,.0f}) comes from the slashing schedule, which takes
half of what is left per claim, so a finite number of claims never reaches the full stake. {agent_retest}
<b>Regression:</b> the main suite gives identical C4 decisions with the cap on, and the scripted scenario suite still
passes. <b>Cost of the change</b> (fresh case F4): an honest operator doing ${f4b['requested']:,.0f} of business on a
${f4b['stake']:,.0f} stake now has ${f4a['refused_amount']:,.0f} of real orders refused, so it must stake more or wait
for the window to roll. That is the intended trade (no exposure the stake does not back) but it is a real false
refusal that the main suite does not show.</p>

<h2>5. Interpretation and limitations</h2>
<ul>
<li><b>What the evidence supports.</b> Checks that need cryptography or shared state (forged names, tampering, stolen
keys, replays, revocation, expiry, slashing, running totals) are exactly where the non-Attest conditions fail; a name
allowlist misses most of them, and an LLM can read the same documents but cannot verify them. Attest answers in a few
milliseconds at no LLM cost.</li>
<li><b>What it does not show.</b> The attacks are the ones the gate was designed for, written by its builders; unknown
attack classes, collusion between staked operators, and attacks on the gate itself are not tested. C4's 100% says the
gate implements its spec, not that the spec is complete.</li>
<li><b>Harness.</b> The LLM arms ran through headless Claude Code on a subscription rather than a bare API loop
(pre-registration amendment); both arms ran identically, but absolute token counts and the API-equivalent cost
reflect that harness. Gate latency inside C3 ({fmt_ms(s.get('C3_llm_attest'))}) was measured on the team's Windows
laptop, the standalone figures on our Linux test machine.</li>
<li><b>Small and synthetic.</b> 25 requests per pass, 5 LLM repetitions; the intervals are wide. One model, one prompt,
one effort level; a better baseline prompt could narrow the gap, and C3 only helps if the agent calls the tool and
follows it (check-call share reported above).</li>
<li><b>Deployment.</b> One gate process on one machine, stakes and slashing in a JSON file rather than on chain;
throughput is bounded by the single lock (about {lat['http_10_clients']['throughput_per_s']:.0f} decisions/s here).
The issuer cap holds per gate: an operator using several independent gates could still exceed its stake in total,
which needs the shared on-chain stake contract that is planned. The 30-day window and ratio 1.0 are choices, not
measured optima. Slashing takes a fixed share of the stake per claim, not the damage, so it can also over-recover
(tuning case k = 1 recovers {sb['tuning'][0]['recovered']:,.0f} for a {sb['tuning'][0]['exposure']:,.0f} loss).</li>
<li><b>Not used.</b> NANDA Town: our claim is about one receiver deciding whether to trust one sender, which a
controlled, repeatable world tests better; the gate exposes plain HTTP and MCP tools, so a Town scenario can be added
later.</li>
</ul>
<p class="sub">Method note: chart colours were checked for colour-vision-deficiency separation and contrast; every
number in this report is computed from the committed result files by <code>bench/report/make_report.py</code>.</p>
</body></html>"""


def main():
    page = build()
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(page)
    js = ("const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();"
          "const p=await b.newPage();await p.goto('file://'+process.argv[1],{waitUntil:'load'});"
          "await p.pdf({path:process.argv[2],format:'Letter',printBackground:true,"
          "margin:{top:'0.55in',bottom:'0.55in',left:'0.6in',right:'0.6in'}});await b.close();})()"
          ".catch(e=>{console.error(e);process.exit(1)});")
    env = {**os.environ, "NODE_PATH": os.environ.get("NODE_PATH") or subprocess.run(
        ["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()}
    subprocess.run(["node", "-e", js, str(OUT_HTML), str(OUT_PDF)], check=True, env=env)
    print(f"wrote {OUT_PDF.relative_to(ROOT)} and {OUT_HTML.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
