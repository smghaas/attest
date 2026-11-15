// Attest in the browser: keys, signing and the same documents the Python SDK makes.
// Private keys are created and kept in this browser (localStorage). The gate only ever sees signatures.
// Documents match gate/crypto.py: canonical JSON (sorted keys, no spaces), id = sha256 of the body,
// Ed25519 signature over the body including its id.
"use strict";

const AttestKeys = (() => {
  const enc = new TextEncoder();
  const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
  const unhex = h => new Uint8Array(h.match(/../g).map(x => parseInt(x, 16)));
  const b64u = bytes => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const unb64u = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));

  function canon(v) {
    if (v === null || v === undefined) return "null";
    if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
    if (typeof v === "object")
      return "{" + Object.keys(v).sort().map(k => JSON.stringify(k) + ":" + canon(v[k])).join(",") + "}";
    if (typeof v === "number" && (!Number.isFinite(v) || Math.abs(v) >= 1e15)) throw new Error("number out of range");
    return JSON.stringify(v);
  }

  async function supported() {
    try { await crypto.subtle.generateKey({ name: "Ed25519" }, false, ["sign"]); return true; }
    catch (e) { return false; }
  }

  async function keygen() {
    const k = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
    const jwk = await crypto.subtle.exportKey("jwk", k.privateKey);
    return { priv: hex(unb64u(jwk.d)), pub: hex(unb64u(jwk.x)) };
  }

  async function sign(who, body) {
    const key = await crypto.subtle.importKey("jwk",
      { kty: "OKP", crv: "Ed25519", d: b64u(unhex(who.priv)), x: b64u(unhex(who.pub)), ext: true },
      { name: "Ed25519" }, false, ["sign"]);
    return hex(await crypto.subtle.sign({ name: "Ed25519" }, key, enc.encode(canon(body))));
  }

  const sha = async s => hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
  const now = () => Date.now() / 1000;
  const nonce = () => hex(crypto.getRandomValues(new Uint8Array(16)));

  async function signed(who, body) {
    const b = { ...body };
    b.id = await sha(canon(b));
    return { ...b, sig: await sign(who, b) };
  }

  // ---- identities kept in this browser, in the same shape as ~/.attest/<name>.json --------------
  const STORE = "attest.identities";
  function all() { try { return JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { return {}; } }
  function save(doc) {
    const ids = all(); ids[doc.name] = doc;
    try { localStorage.setItem(STORE, JSON.stringify(ids)); } catch (e) { /* private window: kept for this page only */ }
    memory[doc.name] = doc;
    return doc;
  }
  const memory = {};
  const get = name => all()[name] || memory[name];
  const list = () => Object.values({ ...memory, ...all() });
  function forget() { try { localStorage.removeItem(STORE); } catch (e) {} for (const k in memory) delete memory[k]; }
  function exists(name) { return !!get(name); }

  // ---- talking to the gate ----------------------------------------------------------------------
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.detail || ("the gate said " + r.status));
    return out;
  }

  // Whoever stands behind agents: a person or a company. Money down (test stake) is optional.
  async function createOperator(name, backerKind = "company", putMoney = true) {
    if (exists(name)) throw new Error(name + " already exists in this browser");
    const k = await keygen();
    const op = { kind: "operator", name, backer_kind: backerKind, ...k };
    let out = { stake: 0 };
    if (putMoney)
      out = await post("/sandbox/faucet", { issuer: op.pub, name, sig: await sign(op, { faucet: true, issuer: op.pub, name }) });
    save(op);                                  // keep the key only once the gate accepted it
    return out;
  }

  // Proof of work on this request: a nonce so sha256(request id + ":" + nonce) starts with `bits` zero bits.
  async function work(env, bits, progress) {
    const rid = env.request.id;
    let n = 0;
    while (zeroBits(await sha(rid + ":" + n)) < bits) {
      n++;
      if (progress && n % 2000 === 0) progress(n);
    }
    return { ...env, work: { nonce: n, bits } };
  }

  // The person (or agent) doing the checking gets a key too, so it can sign the outcome with the agent.
  async function ensureChecker(name) {
    const have = get(name);
    if (have && (have.kind === "checker" || have.kind === "receiver")) return have;
    if (have) throw new Error(name + " is already used for something else in this browser; pick another name");
    return save({ kind: "checker", name, ...(await keygen()) });
  }

  async function createReceiver(name, minStake, escalateAbove, escalateFor, readOnly) {
    if (exists(name)) throw new Error(name + " already exists in this browser");
    const k = await keygen();
    const rc = { kind: "receiver", name, ...k };
    const policy = escalateAbove > 0 ? { escalate_above: { [escalateFor || "*"]: escalateAbove } } : {};
    if (readOnly && readOnly.length) policy.read_only = readOnly;
    const body = { register_receiver: name, pubkey: rc.pub, min_stake: minStake, policy };
    const out = await post("/register/receiver", { name, pubkey: rc.pub, min_stake: minStake, policy, sig: await sign(rc, body) });
    save(rc);
    return out;
  }

  async function createAgent(name, issuerName, purpose, actions, maxAmount, days) {
    if (exists(name)) throw new Error(name + " already exists in this browser");
    const issuer = get(issuerName);
    if (!issuer) throw new Error("no identity called " + issuerName);
    const k = await keygen();
    const t = now();
    if (issuer.kind === "operator") {
      const cred = await signed(issuer, { type: "kya.credential/1", agent: k.pub, operator: issuer.name, issuer: issuer.pub,
                                          backer_kind: issuer.backer_kind || "company",
                                          purpose, issued_at: t, expires_at: t + days * 86400 });
      const root = await signed(issuer, { type: "kya.mandate/1", subject: k.pub, issuer: issuer.pub, parent: null,
                                          scope: { actions, max_amount: maxAmount, currency: "USD" }, expires_at: t + days * 86400 });
      return save({ kind: "agent", name, ...k, operator: issuer.name, credential: cred, chain: [root] });
    }
    // a sub-agent: the parent agent signs a narrower mandate (the gate refuses a wider one)
    const child = await signed(issuer, { type: "kya.mandate/1", subject: k.pub, issuer: issuer.pub,
                                         parent: issuer.chain[issuer.chain.length - 1].id,
                                         scope: { actions, max_amount: maxAmount, currency: "USD" }, expires_at: t + days * 86400 });
    return save({ kind: "agent", name, ...k, operator: issuer.operator, delegated_by: issuer.name,
                  credential: issuer.credential, chain: [...issuer.chain, child] });
  }

  async function envelope(agentName, to, action, amount, message) {
    const a = get(agentName);
    const req = await signed(a, { type: "kya.request/1", from: a.pub, to, action, amount, message: message || "",
                                  ts: now(), nonce: nonce() });
    return { credential: a.credential, chain: a.chain, request: req };
  }

  // Only the receiver may ask about requests sent to it: it signs {verify: request id, receiver}.
  async function check(agentName, receiver, action, amount) {
    const rc = get(receiver);
    if (!rc || (rc.kind !== "receiver" && rc.kind !== "checker"))
      throw new Error("Only " + receiver + " can check requests sent to it, and its key is not in this browser. " +
                      "Pick a receiver you created here (step 3).");
    const env = await envelope(agentName, receiver, action, amount, "sent from the browser");
    const sig = await sign(rc, { verify: env.request.id, receiver });
    const r = await fetch("/verify/" + encodeURIComponent(receiver), { method: "POST",
      headers: { "content-type": "application/json", "X-Attest-Receiver-Sig": sig }, body: JSON.stringify(env) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.detail || ("the gate said " + r.status));
    return { envelope: env, result: out };
  }

  // ---- the ledger -------------------------------------------------------------------------------
  const zeroBits = h => { let n = 0; for (const c of h) { const v = parseInt(c, 16); if (v === 0) { n += 4; continue; } return n + Math.clz32(v) - 28; } return n; };

  async function hasPassport(who) {
    const r = await fetch("/ledger/agents/" + who.pub + "/history");
    return r.ok && (await r.json()).passport;
  }

  async function passport(name) {
    const a = get(name);
    // an agent's operator vouches for it; a receiver vouches for itself
    const self = a.kind === "receiver" || a.kind === "checker";
    const op = self ? a : get(a.operator);
    if (!op) throw new Error("the passport needs the operator's key, and " + a.operator + " is not in this browser");
    const bits = (await (await fetch("/ledger/info")).json()).registration_puzzle_bits;
    let n = 0;
    while (zeroBits(await sha(a.pub + ":" + n)) < bits) n++;     // the registration puzzle (proof of work)
    const scope = self ? { actions: ["receive"], max_amount: 0 } : a.chain[0].scope;
    const doc = await signed(op, { type: "passport", agent: a.pub, operator: op.pub, name: a.name,
                                   actions: [...scope.actions].sort(), max_amount: scope.max_amount, nonce: n });
    doc.agent_sig = await sign(a, { id: doc.id });
    return post("/ledger/records", doc);
  }

  async function ensurePassport(name) {
    if (!(await hasPassport(get(name)))) await passport(name);
  }

  // After the gate allowed a request: the receiver (provider) and the agent (buyer) both sign what happened.
  // The record's request field is the gate request's id, so the history points back to the gate's receipt.
  async function settle(agentName, receiverName, requestId, amount, status) {
    const rc = get(receiverName);
    if (!rc || (rc.kind !== "receiver" && rc.kind !== "checker"))
      throw new Error(receiverName + " must sign too, and its key is not in this browser. " +
                      "Only someone whose key is here can confirm a deal from this page.");
    await ensurePassport(agentName);
    await ensurePassport(receiverName);
    return trade(receiverName, agentName, requestId, amount, status);
  }

  async function trade(providerName, buyerName, request, amount, status) {
    const p = get(providerName), b = get(buyerName);
    const body = { type: "outcome", request, provider: p.pub, buyer: b.pub, amount, status, ts: now() };
    body.id = await sha(canon(body));
    return post("/ledger/records", { ...body, sig: await sign(p, body), buyer_sig: await sign(b, body) });
  }

  return { supported, canon, list, get, save, forget, createOperator, createReceiver, createAgent, envelope, check,
           passport, trade, settle, hasPassport, nonce, work, ensureChecker };
})();
