"""本机吞吐基线：只量网关自身开销（演示池，不走网络上游）。

用法：
    python scripts/bench.py --threads 16 --requests 50

对比 new-api 时请用同样的并发与请求体，否则数字没有可比性。
"""

from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import threading
import time
from pathlib import Path


def worker(port: int, n: int, body: bytes, out: list[list[float]]) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    lat: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        conn.request(
            "POST", "/v1/messages", body=body,
            headers={"content-type": "application/json", "x-api-key": "k"},
        )
        resp = conn.getresponse()
        resp.read()
        lat.append((time.perf_counter() - started) * 1000)
    conn.close()
    out.append(lat)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bench")
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--requests", type=int, default=50)
    args = parser.parse_args(argv)

    # 从仓库根运行 `python scripts/bench.py` 时，sys.path[0] 是 scripts/ 而不是仓库根
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from relayhub.gateway.service import DemoRouter, RelayServer, demo_pool

    server = RelayServer(("127.0.0.1", 0), DemoRouter(demo_pool()), api_key="k")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    body = json.dumps(
        {"model": "demo", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
    ).encode()

    buckets: list[list[float]] = []
    started = time.perf_counter()
    workers = [
        threading.Thread(target=worker, args=(port, args.requests, body, buckets))
        for _ in range(args.threads)
    ]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    elapsed = time.perf_counter() - started

    lat = sorted(x for bucket in buckets for x in bucket)
    if not lat:
        print("没有成功的请求")
        return 1
    print(
        f"{args.threads}x{args.requests}: {len(lat)/elapsed:.0f} req/s  "
        f"p50={statistics.median(lat):.1f}ms  p95={lat[int(len(lat)*0.95)]:.1f}ms  "
        f"max={lat[-1]:.1f}ms"
    )
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
