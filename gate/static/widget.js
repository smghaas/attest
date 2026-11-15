// Attest check box: a reverse CAPTCHA. A CAPTCHA asks you to prove you are human; this box proves an
// agent is who it says, for the task you care about, with the checks you chose.
//
// Embed on any page:
//   <script src="https://<gate>/static/widget.js"></script>
//   <div id="attest"></div>
//   <script>
//     AttestWidget.mount(document.getElementById("attest"), {
//       gate: "https://<gate>", checker: "your name",            // the agent's request must be addressed to you
//       getEnvelope: async () => envelopeTheAgentSentYou,        // {credential, chain, request, work?}
//       require: { backer: true, allowed: true, min_stake: 5000, work_bits: 16, either: true }
//     });
//   </script>
"use strict";

const AttestWidget = (() => {
  const CSS = `
.atw{--ok:#12a150;--bad:#c62828;--wait:#b7791f;--line:#d6dae0;--ink:#1d2330;--mute:#6b7380;--bg:#fafbfc;
  font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);max-width:420px}
@media (prefers-color-scheme:dark){.atw{--line:#3a4250;--ink:#e8ebf0;--mute:#9aa4b2;--bg:#1b2028}}
.atw-box{display:flex;align-items:center;gap:14px;border:1px solid var(--line);border-radius:6px;background:var(--bg);
  padding:14px 14px;box-shadow:0 1px 3px rgba(0,0,0,.08);min-height:78px}
.atw-mark{flex:0 0 auto;width:34px;height:34px;border:2px solid #c1c7d0;border-radius:4px;background:#fff;cursor:pointer;
  position:relative;display:grid;place-items:center;padding:0}
.atw-mark:focus-visible{outline:2px solid #2f4bd6;outline-offset:2px}
.atw-mark.busy,.atw-mark.done{border-color:transparent;background:transparent;cursor:default}
.atw-spin{width:30px;height:30px;border-radius:50%;border:3px solid #cfe9db;border-top-color:var(--ok);animation:atw-r .8s linear infinite}
@keyframes atw-r{to{transform:rotate(360deg)}}
.atw-mark svg{width:34px;height:34px;overflow:visible}
.atw-mark path{fill:none;stroke-width:4;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:60;stroke-dashoffset:60;animation:atw-d .55s ease-out forwards}
@keyframes atw-d{to{stroke-dashoffset:0}}
@media (prefers-reduced-motion:reduce){.atw-spin{animation-duration:3s}.atw-mark path{animation:none;stroke-dashoffset:0}}
.atw-text{flex:1;min-width:0}
.atw-title{font-weight:600}
.atw-sub{font-size:13px;color:var(--mute)}
.atw-brand{flex:0 0 auto;text-align:center;font-size:10px;color:var(--mute);line-height:1.2}
.atw-brand svg{display:block;margin:0 auto 2px;width:28px;height:28px}
.atw-list{list-style:none;margin:8px 0 0;padding:0;border:1px solid var(--line);border-radius:6px;background:var(--bg)}
.atw-list li{display:flex;gap:10px;padding:8px 12px;border-top:1px solid var(--line);opacity:0;transform:translateY(4px);transition:opacity .3s,transform .3s}
.atw-list li:first-child{border-top:0}.atw-list li.on{opacity:1;transform:none}
.atw-ic{flex:0 0 18px;font-weight:800;text-align:center}
.atw-ic.pass{color:var(--ok)}.atw-ic.fail{color:var(--bad)}.atw-ic.wait{color:var(--wait)}
.atw-li b{display:block;font-weight:600}.atw-li span{font-size:13px;color:var(--mute)}
.atw-foot{font-size:12px;color:var(--mute);margin-top:6px}`;

  const LOGO = '<svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="#12a150"/><path d="M8.5 24.5 L16.5 7.5 L23 18.5" stroke="#fff" stroke-width="3.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  // the drawn marks: the reversed check (Attest's A) for yes, a cross for no, a pause for "a person decides"
  const MARK = {
    pass: '<svg viewBox="0 0 32 32"><path d="M6 27 L16 5 L24 20" stroke="#12a150"/></svg>',
    fail: '<svg viewBox="0 0 32 32"><path d="M8 8 L24 24 M24 8 L8 24" stroke="#c62828"/></svg>',
    wait: '<svg viewBox="0 0 32 32"><path d="M11 7 V25 M21 7 V25" stroke="#b7791f"/></svg>'
  };
  const TITLE = { pass: "Verified agent", fail: "Not verified", wait: "A person must approve" };
  const ICON = { pass: "✓", fail: "✕", wait: "…" };
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function style() {
    if (document.getElementById("atw-css")) return;
    const s = document.createElement("style"); s.id = "atw-css"; s.textContent = CSS; document.head.appendChild(s);
  }

  function mount(el, opts) {
    style();
    el.classList.add("atw");
    el.innerHTML = `<div class="atw-box"><button class="atw-mark" type="button" aria-label="Verify this agent"></button>
      <div class="atw-text"><div class="atw-title">${esc(opts.label || "Verify this agent")}</div>
      <div class="atw-sub">${esc(opts.hint || "Click the box. Attest checks it for you.")}</div></div>
      <div class="atw-brand">${LOGO}Attest</div></div><div class="atw-more"></div>`;
    const mark = el.querySelector(".atw-mark"), title = el.querySelector(".atw-title"), sub = el.querySelector(".atw-sub");
    const more = el.querySelector(".atw-more");
    let running = false;
    const clean = opts.clean || (s => s);
    async function go() {
      if (running) return; running = true;
      mark.className = "atw-mark busy"; mark.innerHTML = '<div class="atw-spin"></div>';
      mark.setAttribute("aria-label", "Checking");
      title.textContent = "Checking…"; sub.textContent = ""; more.innerHTML = "";
      try {
        const env = await opts.getEnvelope(m => { sub.textContent = m; });
        sub.textContent = "Asking Attest…";
        const r = await fetch((opts.gate || "") + "/check", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ envelope: env, checker: opts.checker, require: opts.require || {} }) });
        const out = await r.json();
        if (!r.ok) throw new Error(out.detail || ("Attest said " + r.status));
        show(out);
        if (opts.onResult) opts.onResult(out, env);
      } catch (e) {
        mark.className = "atw-mark done"; mark.innerHTML = MARK.fail;
        title.textContent = "Could not check"; sub.textContent = e.message;
      } finally { running = false; }
    }
    function show(out) {
      mark.className = "atw-mark done"; mark.innerHTML = MARK[out.verdict];
      mark.setAttribute("aria-label", TITLE[out.verdict]);
      title.textContent = (opts.titles || TITLE)[out.verdict];
      const passed = out.items.filter(i => i.status === "pass").length;
      sub.textContent = `${passed} of ${out.items.length} checks passed for ${out.action} ($${Number(out.amount || 0).toLocaleString()})`;
      more.innerHTML = `<ul class="atw-list">${out.items.map(i => `<li><span class="atw-ic ${i.status}">${ICON[i.status]}</span>
        <span class="atw-li"><b>${esc(i.label)}</b><span>${esc(clean(i.why))}</span></span></li>`).join("")}</ul>
        <div class="atw-foot">Signed by Attest (${esc(out.id.slice(0, 10))}) · anyone can check this answer was not changed.</div>`;
      [...more.querySelectorAll("li")].forEach((li, k) => setTimeout(() => li.classList.add("on"), 250 + 180 * k));
    }
    mark.onclick = go;
    return { run: go, reset: () => mount(el, opts) };
  }
  return { mount };
})();
