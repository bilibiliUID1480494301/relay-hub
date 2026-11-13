"""真链压测：起假上游 + 真号池 + 网关，打 OpenAI 非流式，看 RPS 与分位。

    python scripts/bench_e2e.py --label baseline
    RELAYHUB_DRIFT=off python scripts/bench_e2e.py --label drift-off

和 `bench.py` 的区别：那个量的是演示池（跳过号池/记账/观测），这个量的是真实用户路径。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def wait_port(port: int, seconds: float = 15.0) -> bool:
    import socket

    deadline = time.time() + seconds
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def worker(port: int, n: int, body: bytes, out: list[list[float]]) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    lat: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"content-type": "application/json", "authorization": "Bearer k"})
        resp = conn.getresponse()
        resp.read()
        lat.append((time.perf_counter() - started) * 1000)
    conn.close()
    out.append(lat)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--threads", default="8,32,64")
    parser.add_argument("--requests", type=int, default=40)
    args = parser.parse_args()

    home = Path(tempfile.mkdtemp(prefix="rh-bench-"))
    env = {**os.environ, "RELAYHUB_HOME": str(home)}
    up_port, gw_port = 18881, 18882

    up = subprocess.Popen([PY, str(ROOT / "scripts/fake_upstream.py"),
                           "--port", str(up_port), "--protocol", "openai-chat"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gw = None
    try:
        wait_port(up_port)
        subprocess.run([PY, "-m", "relayhub.gateway", "pool", "add", "--label", "fake",
                        "--base-url", f"http://127.0.0.1:{up_port}", "--api-key", "sk-up",
                        "--protocol", "openai-chat", "--model", "demo:1000000"],
                       cwd=ROOT, env=env, capture_output=True, timeout=60)

        gw = subprocess.Popen([PY, "-m", "relayhub.gateway", "serve",
                               "--pool", str(home / "pool.json"),
                               "--port", str(gw_port), "--api-key", "k",
                               "--ip-rpm", "0", "--max-concurrency", "128", "--queue-size", "512"],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(gw_port):
            print("网关没起来")
            return 1

        body = json.dumps({"model": "demo", "max_tokens": 8,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        # 预热
        for _ in range(5):
            worker(gw_port, 1, body, [])

        print(f"== {args.label}  drift={os.environ.get('RELAYHUB_DRIFT', 'on')}")
        for threads in [int(x) for x in args.threads.split(",")]:
            best = None
            for _ in range(3):
                out: list[list[float]] = []
                started = time.perf_counter()
                ws = [threading.Thread(target=worker, args=(gw_port, args.requests, body, out))
                      for _ in range(threads)]
                for w in ws:
                    w.start()
                for w in ws:
                    w.join()
                elapsed = time.perf_counter() - started
                lat = sorted(x for chunk in out for x in chunk)
                if not lat:
                    continue
                rps = len(lat) / elapsed
                if best is None or rps > best[0]:
                    best = (rps, statistics.median(lat), lat[int(len(lat) * 0.95)], lat[-1])
            if best:
                print(f"  c={threads:<3} {best[0]:7.0f} req/s   p50={best[1]:6.1f}ms  "
                      f"p95={best[2]:6.1f}ms  max={best[3]:7.1f}ms")
        return 0
    finally:
        for proc in (gw, up):
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=8)
                except Exception:  # noqa: BLE001
                    proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
