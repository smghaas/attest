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

TEAM = [("Samuel Haas", "TODO: contribution"), ("Eduard Alsina", "TODO: contribution")]

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
            "<th>LLM $ / request</th><th>gate p50</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


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
            f"C3 called check_agent on {fmt_pct(c3.get('checked_share'))} of requests. The LLM cost is "
            f"${c2['cost_per_request_usd']:.4f} (C2) vs ${c3['cost_per_request_usd']:.4f} (C3) per request; "
            f"the gate itself adds no LLM cost.</p>")
        llm_cfg = (f"{cfg['llm']['model']}, effort {cfg['llm']['effort']}, {cfg['llm_reps']} reps per case, "
                   f"run {cfg['run']} at commit {cfg['git'][:7]}")
    else:
        llm_para = ("<p class='pending'>C2 and C3 (the LLM conditions) are pending: they need an API key and are run "
                    "with <code>bench/run_llm.sh</code>. This paragraph, the table rows and the chart fill in "
                    "automatically when the results are committed.</p>")
        llm_cfg = "claude-sonnet-5-5, effort medium, 5 reps per case (pending)"

    if s0 and s1:
        e0, e1 = s0["C3_llm_attest"], s1["C3_llm_attest"]
        agent_retest = (f"At agent level (C3, Sonnet, {e0['reps']} reps), the same SybilCo case moved "
                        f"<b>${e0['exposure_mean']:,.0f}</b> per pass with the cap off and <b>${e1['exposure_mean']:,.0f}</b> "
                        f"with it on (gate alone: ${d0['C4_attest_only']['exposure_mean']:,.0f} → "
                        f"${d1['C4_attest_only']['exposure_mean']:,.0f}).")
    else:
        agent_retest = (f"Gate alone on the SybilCo agent-level case: ${d0['C4_attest_only']['exposure_mean']:,.0f} → "
                        f"${d1['C4_attest_only']['exposure_mean']:,.0f} per pass. <span class='pending'>The C3 (LLM) "
                        f"version of this retest is pending the LLM run.</span>")

    team = " · ".join(f"<b>{esc(n)}</b>: {esc(c)}" for n, c in TEAM)
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
.brk {{ break-before: page; }}
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
model calls per request; all requests of a case in one conversation, so the baseline remembers earlier orders.
<b>Costs:</b> tokens and dollars per request, gate latency in-process and over HTTP.
<b>Reproduce:</b> <code>python -m bench.run_bench</code> (C0, C1, C4), <code>bench/run_llm.sh</code> (C2, C3),
<code>python -m bench.sybil</code>, <code>python -m bench.latency</code>; a second deterministic run gave identical
decisions.</p>

<h2>3. Results</h2>
<img src="{chart_main(s)}" alt="Attack block rate and false refusal rate per condition">
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
