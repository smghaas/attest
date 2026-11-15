import json, sys, urllib.request, pathlib
BASE = "http://127.0.0.1:37883"
INBOX = pathlib.Path("/tmp/attest-coldstart/crash-before-haiku-1/exchange/to-widget-shop")
RECEIPTS = pathlib.Path("/tmp/attest-coldstart/crash-before-haiku-1/seller/receipts")
for f in sorted(INBOX.glob("*.json")):
    doc = json.loads(f.read_text())
    env = doc["envelope"] if "envelope" in doc else {k: doc[k] for k in ("credential", "chain", "request")}
    if "pow" in doc: env["pow"] = doc["pow"]
    req = urllib.request.Request(f"{BASE}/verify/widget-shop", data=json.dumps(env).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        res = json.loads(r.read())
    print("==", f.name, "request id", env["request"]["id"])
    print(json.dumps({k: res[k] for k in ("decision", "reason", "detail")}, indent=1))
    (RECEIPTS / f"{env['request']['id']}.json").write_text(json.dumps(res["receipt"], indent=1))
    (RECEIPTS / f"{env['request']['id']}.result.json").write_text(json.dumps(res, indent=1))
