#!/bin/bash
# HW6 demo: Attest, two real Hermes agents, one gate. Resets state, then:
#  1 buyer agent sends a signed quote request with its credential and mandate
#  2 supplier agent checks it through the gate (allow), prices it, replies
#  3 buyer agent reads the reply
#  4 a sub-agent whose mandate exceeds its parent tries to order: gate refuses
#  5 the scripted scenarios (stakes, proof of work, slashing claims and disputes, order splitting, concurrent orders, revocation), decisions compared with the design, receipts checked
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")"
B=$'\e[1m'; C=$'\e[1;33m'; G=$'\e[1;32m'; R=$'\e[0m'
cap() { echo; echo "${C}▶ $1${R}"; [ -n "$2" ] && echo "${B}  $2${R}"; echo; sleep ${3:-2}; }
H="hermes chat -m sonnet --ignore-rules --oneshot --max-turns 8"
LOG=${DEMO_LOG:-runs/demo_$(date +%Y%m%d_%H%M).log}; mkdir -p runs; : > "$LOG"; exec > >(tee -a "$LOG") 2>&1
date +%s > runs/demo_timing

pkill -f "uvicorn gate.server" 2>/dev/null; sleep 1
rm -f state/gate_state.json state/inbox.json logs/receipts.jsonl logs/tools_audit.log
.venv/bin/python setup_demo.py > /dev/null
(.venv/bin/uvicorn gate.server:app --host 127.0.0.1 --port 8402 > logs/server.log 2>&1 &)
sleep 2
clear
echo "${G}Samuel Haas and Eduard Alsina · MAS.665 Homework 6 · Attest: a Know-Your-Agent check for agent-to-agent requests${R}"
echo "${B}Gate running on :8402, key $(curl -s localhost:8402/pubkey | cut -c10-25)...  Two Hermes agents, every result below is a real tool call.${R}"
sleep 3

cap "1 of 5  BUYER AGENT (AcmeCorp procurement) asks for a quote" "It signs the request and attaches its credential (issued by AcmeCorp, which has staked 25,000) and its mandate: request_quote and place_order up to 10,000 USD."
$H -t kya_buyer -q "You are AcmeCorp's procurement agent. Call whoami to see what you may do. Then ask SupplierCo for a quote for 200 units of part X-200 with send_request, action request_quote, amount 4000, message 'Quote please: 200 units of X-200'. Finish by stating the request id and what was attached to the request."

cap "2 of 5  SUPPLIER AGENT checks the sender through Attest before answering" "inbox_next → check_agent (POST /verify): staked issuer, mandate chain, signature → allow → quote_price → reply. Receipt signed by the gate, verified by the agent."
$H -t kya_supplier -q "You are SupplierCo's quoting agent. Take the next request with inbox_next. Before acting, call check_agent with its request_id. If the decision is allow, price it with quote_price (part and units are in the message) and send the total with reply. If refuse, escalate or unknown, reply in one line that you cannot proceed and why. End with: decision, reason, receipt id, receipt verified yes or no, and what you replied."

cap "3 of 5  BUYER AGENT reads the reply" ""
$H -t kya_buyer -q "Call read_replies and report, in one line, what SupplierCo answered to your quote request."

cap "4 of 5  A SUB-AGENT with a mandate bigger than its parent's tries to place an order" "acme-procurement delegated 'place_order up to 15,000' to a sub-agent, but its own limit is 10,000. The gate must refuse: a child mandate can only narrow its parent."
$H -t kya_greedy -q "You are a sub-agent of AcmeCorp's procurement agent. Call whoami, then place an order with send_request, action place_order, amount 500, message 'PO-3001: 25 units of Z-FLANGE'. State the request id."
$H -t kya_supplier -q "Take the next request with inbox_next, call check_agent on it, then reply accordingly (quote with quote_price only if allowed, otherwise one line saying you cannot proceed and why). End with: decision, reason, receipt verified yes or no."

cap "5 of 5  RESULT CHECK: the scripted scenarios, no LLM, every decision compared with the design, every receipt verified" "Includes: unstaked agent must solve a proof-of-work puzzle seeded by the live Base Sepolia block hash, order splitting, slashing with a dispute window, revocation cascade." ""
.venv/bin/python run_scenarios.py

cap "DONE" "Limitations today: stakes, slashing and revocations live in the gate's own table, not yet in a contract on Base Sepolia (only the block hash is read from chain); escalation only logs." 4
date +%s >> runs/demo_timing
