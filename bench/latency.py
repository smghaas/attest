"""Gate decision latency: in-process, and over HTTP with 1 and 10 concurrent clients.

  python -m bench.latency [--n 1000] [--out bench/results/<run>/latency.json]

Each request is a fresh, valid, signed quote request from AcmeCorp's procurement agent
(decision: allow), so every call runs the full check: stake, credential, mandate chain,
request signature, nonce, scope. Requests are signed before timing starts.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .analyze import pct
from .world import RECEIVER, demo_world, request

ROOT = Path(__file__).resolve().parent.parent


def stats(ms: list[float], wall: float) -> dict:
    return {"n": len(ms), "p50_ms": pct(ms, 50), "p95_ms": pct(ms, 95), "max_ms": max(ms),
            "throughput_per_s": len(ms) / wall}


def in_process(n: int) -> dict:
    w = demo_world()
    try:
        envs = [request(w.agents["proc"], "request_quote", 100, f"latency {i}") for i in range(n)]
        ms, t0 = [], time.perf_counter()
        for e in envs:
            t = time.perf_counter()
            assert w.gate.verify(RECEIVER, e)["decision"] == "allow"
            ms.append((time.perf_counter() - t) * 1000)
        return stats(ms, time.perf_counter() - t0)
    finally:
        w.close()


def over_http(n: int, clients: int) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="attest-latency-"))
    env = {**os.environ, "ATTEST_STATE_DIR": str(tmp / "state"), "ATTEST_LOG_DIR": str(tmp / "logs")}
    subprocess.run([sys.executable, "setup_demo.py"], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "gate.server:app", "--host", "127.0.0.1", "--port",
                            str(port), "--log-level", "warning"], cwd=ROOT, env=env)
    url = f"http://127.0.0.1:{port}/verify/{RECEIVER}"
    try:
        for _ in range(100):
            try:
                httpx.get(f"http://127.0.0.1:{port}/pubkey", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        ag = json.loads((tmp / "state" / "agents" / "acme-procurement.json").read_text())
        envs = [request(ag, "request_quote", 100, f"latency {i}") for i in range(n)]
        local = threading.local()

        def one(e):
            if not hasattr(local, "client"):
                local.client = httpx.Client(timeout=30)
            t = time.perf_counter()
            r = local.client.post(url, json=e).json()
            assert r["decision"] == "allow", r
            return (time.perf_counter() - t) * 1000

        t0 = time.perf_counter()
        with ThreadPoolExecutor(clients) as ex:
            ms = list(ex.map(one, envs))
        return {**stats(ms, time.perf_counter() - t0), "clients": clients}
    finally:
        srv.terminate()
        srv.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    res = {"in_process": in_process(a.n), "http_1_client": over_http(a.n, 1), "http_10_clients": over_http(a.n, 10)}
    for k, v in res.items():
        print(f"{k:16} p50 {v['p50_ms']:.2f} ms  p95 {v['p95_ms']:.2f} ms  max {v['max_ms']:.1f} ms  "
              f"{v['throughput_per_s']:.0f} decisions/s")
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    main()
