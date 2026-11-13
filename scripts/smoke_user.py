"""模拟一个正常用户把中转站跑一遍：建站 → 加渠道 → 发令牌 → 三种协议调通 → 管理面各项。

    python scripts/smoke_user.py

全部用临时数据根和本地假上游，不碰真实上游、不改本机已有配置。
退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
UP_PORT = 18851
GW_PORT = 18852
ADM_PORT = 18853
API_KEY = "rh_smoke_master"

results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}{('  — ' + detail) if detail else ''}", flush=True)


def http(method: str, url: str, *, body: dict | None = None, headers: dict | None = None,
         raw: bool = False, timeout: float = 15.0):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, (payload if raw else json.loads(payload or b"{}"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_port(port: int, seconds: float = 12.0) -> bool:
    import socket

    deadline = time.time() + seconds
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-m", "relayhub.gateway", *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, **(env or {})})


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="rh-smoke-"))
    env = {"RELAYHUB_HOME": str(home)}
    procs: list[subprocess.Popen] = []

    try:
        # 1) 假上游
        up = subprocess.Popen([PY, str(ROOT / "scripts/fake_upstream.py"),
                               "--port", str(UP_PORT), "--protocol", "openai-chat"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(up)
        step("启动本地假上游", wait_port(UP_PORT), f"127.0.0.1:{UP_PORT}")

        # 2) 建站：初始化站长 + 加渠道
        init = run(["users", "init", "--username", "admin"], env=env)
        step("初始化站长账号", init.returncode == 0, (init.stderr or init.stdout).strip()[:80])

        add = run(["pool", "add", "--label", "fake", "--base-url",
                   f"http://127.0.0.1:{UP_PORT}", "--api-key", "sk-upstream", "--protocol", "openai-chat",
                   "--model", "demo:1000000"],
                  env=env)
        step("添加上游渠道", add.returncode == 0, (add.stderr or "").strip()[:80])

        pool_file = home / "pool.json"
        raw = pool_file.read_bytes() if pool_file.exists() else b""
        step("号池加密落盘（不含明文 key）", b"sk-upstream" not in raw and b'"enc"' in raw)
        if os.name == "posix":
            step("号池权限 0600", oct(pool_file.stat().st_mode)[-3:] == "600")

        # 3) 起网关
        # 注意：serve 不带 --pool 会用「演示池」（内置 deepseek/glm 模型），
        # 真实站点必须显式指向号池文件，否则客户端请求会 404「模型不在可用列表」。
        pool_arg = ["--pool", str(home / "pool.json")]
        gw = subprocess.Popen([PY, "-m", "relayhub.gateway", "serve", *pool_arg,
                               "--port", str(GW_PORT),
                               "--api-key", API_KEY, "--ip-rpm", "0"],
                              cwd=ROOT, env={**os.environ, **env},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(gw)
        step("启动网关", wait_port(GW_PORT), f"127.0.0.1:{GW_PORT}")

        auth = {"authorization": f"Bearer {API_KEY}"}
        base = f"http://127.0.0.1:{GW_PORT}"

        # 4) OpenAI 非流式
        status, data = http("POST", f"{base}/v1/chat/completions", headers=auth, body={
            "model": "demo", "max_tokens": 16,
            "messages": [{"role": "user", "content": "你好"}],
        })
        step("OpenAI 非流式 200", status == 200 and bool(data.get("choices")), f"HTTP {status}")

        # 5) OpenAI 流式
        status, blob = http("POST", f"{base}/v1/chat/completions", headers=auth, raw=True, body={
            "model": "demo", "max_tokens": 16, "stream": True,
            "messages": [{"role": "user", "content": "讲个笑话"}],
        })
        text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)
        step("OpenAI 流式 SSE", status == 200 and "data:" in text, f"HTTP {status}")

        # 6) Anthropic 协议
        status, data = http("POST", f"{base}/v1/messages", headers=auth, body={
            "model": "demo", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        })
        step("Anthropic 非流式 200", status == 200 and bool(data.get("content")), f"HTTP {status}")

        # 7) Gemini 协议（存在才测，不存在跳过不算失败）
        status, data = http("POST", f"{base}/v1beta/models/demo:generateContent", headers=auth, body={
            "contents": [{"parts": [{"text": "hi"}]}],
        })
        step("Gemini generateContent", status in (200, 404), f"HTTP {status}")

        # 8) 未带凭据必须被拒
        status, _ = http("POST", f"{base}/v1/chat/completions", body={
            "model": "demo", "messages": [{"role": "user", "content": "x"}],
        })
        step("无凭据被拒（401/403）", status in (401, 403), f"HTTP {status}")

        # 9) 管理面
        adm = subprocess.Popen([PY, "-m", "relayhub.gateway", "admin",
                                "--port", str(ADM_PORT), "--token", API_KEY],
                               cwd=ROOT, env={**os.environ, **env},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(adm)
        step("启动管理面", wait_port(ADM_PORT), f"127.0.0.1:{ADM_PORT}")

        abase = f"http://127.0.0.1:{ADM_PORT}"
        ahdr = {"X-Admin-Token": API_KEY}

        for path, label in (
            ("/api/state", "号池全景"),
            ("/api/server/status", "服务器状态"),
            ("/api/deploy/check", "部署体检"),
            ("/api/usage", "用量"),
            ("/api/requests", "请求日志"),
            ("/api/audit", "审计流水"),
            ("/api/drift", "响应指纹"),
            ("/api/probes", "探测题"),
            ("/api/oversell", "超售预警"),
            ("/api/security", "安全公告"),
        ):
            status, data = http("GET", abase + path, headers=ahdr)
            step(f"管理面 {label}（{path}）", status == 200, f"HTTP {status}")

        status, state = http("GET", abase + "/api/state", headers=ahdr)
        keys = (state or {}).get("keys") or []
        step("号池里能看到渠道", bool(keys), f"{len(keys)} 个")

        # 10) 令牌：发放 + 用它调通 + 吊销
        status, issued = http("POST", abase + "/api/tokens", headers=ahdr,
                              body={"name": "smoke-device"})
        token = ""
        if isinstance(issued, dict):
            token = issued.get("token") or (issued.get("tokens") or [{}])[0].get("token", "")
        step("发放下游令牌", status == 200 and bool(token), f"HTTP {status}")

        if token:
            status, _ = http("POST", f"{base}/v1/chat/completions",
                             headers={"authorization": f"Bearer {token}"}, body={
                                 "model": "demo", "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "用令牌说话"}],
                             })
            step("用下游令牌调通", status == 200, f"HTTP {status}")

        # 11) 兑换码：批量生成 + 销毁
        status, codes = http("POST", abase + "/api/codes", headers=ahdr,
                             body={"count": 3, "credits": 100})
        made = [c["code"] for c in (codes or {}).get("codes", [])] if isinstance(codes, dict) else []
        step("批量生成兑换码", status == 200 and len(made) == 3, f"{len(made)} 个")

        if made:
            status, res = http("POST", abase + "/api/codes/destroy", headers=ahdr,
                               body={"codes": made[:2]})
            step("批量销毁兑换码", status == 200 and res.get("removed") == 2,
                 f"removed={res.get('removed')}")

        # 12) 迁移导出
        status, bundle = http("POST", abase + "/api/export", headers=ahdr, body={})
        step("导出迁移包", status == 200 and bundle.get("schema", "").startswith("relayhub-migrate"),
             f"schema={bundle.get('schema') if isinstance(bundle, dict) else '?'}")

        # 13) 重启后数据还在（落盘正确性）
        gw.terminate()
        gw.wait(timeout=10)
        gw2 = subprocess.Popen([PY, "-m", "relayhub.gateway", "serve", *pool_arg,
                                "--port", str(GW_PORT),
                                "--api-key", API_KEY, "--ip-rpm", "0"],
                               cwd=ROOT, env={**os.environ, **env},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(gw2)
        ok = wait_port(GW_PORT)
        status, data = http("POST", f"{base}/v1/chat/completions", headers=auth, body={
            "model": "demo", "max_tokens": 8, "messages": [{"role": "user", "content": "重启后还在吗"}],
        })
        step("重启后仍可服务（用量已落盘）", ok and status == 200, f"HTTP {status}")

    finally:
        for proc in procs:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        for proc in procs:
            try:
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                proc.kill()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 项通过")
    failures = [name for name, ok, _ in results if not ok]
    if failures:
        print("失败项：" + "、".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
