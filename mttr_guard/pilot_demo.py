"""HTTP evidence fixtures for an explicitly synthetic, real-wall-clock local drill."""
import hmac
import json
import os
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .pilot_config import Config, METRICS
from .pilot_http import BoundedServer
from .pilot_store import PilotStore


SCOPE = {"tenant": "commerce", "environment": "staging", "cluster": "local", "namespace": "trade", "service": "checkout"}


def config_data(base_url):
    return {"services": [{"id": "checkout", "scope": SCOPE, "owner": "Checkout on-call (demo)",
        "mode": "DEMO", "prometheus_url": base_url, "evidence_url": base_url + "/evidence",
        "queries": {name: "demo_" + name for name in METRICS}, "baseline_qps": 1000,
        "policy": {"window_seconds": 10, "consecutive_windows": 3},
        "links": {"本地项目说明": "http://127.0.0.1:8787/"},
        "runbook": ["核对本次发布版本、灰度范围与数据库兼容性。", "接受或拒绝建议并记录理由；接受不会执行任何发布。",
            "演练时在另一终端运行 python -m mttr_guard.pilot demo-recover，模拟外部平台回退到 r41。",
            "在下方填写 r41 和操作说明，点击记录已人工处置，等待三个独立健康窗口。"]}],
        "identities": [{"subject": "demo:oncall", "role": "operator", "service": "checkout", "token_env": "MTTR_DEMO_OPERATOR_TOKEN"},
                       {"subject": "demo:alertmanager", "role": "receiver", "service": "checkout", "token_env": "MTTR_DEMO_RECEIVER_TOKEN"}]}


def evidence_server(port, control_token):
    from http.server import BaseHTTPRequestHandler
    state = {"healthy": False, "changed_at": time.time() - 180, "recovered_at": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            now = time.time()
            path = urlsplit(self.path)
            if path.path == "/evidence":
                return self.respond(200, {"scope": SCOPE, "mode": "DEMO", "observed_at": now, "window_seconds": 10,
                    "deployment": {"revision": "r41" if state["healthy"] else "r42", "previous_revision": "r41",
                        "changed_at": state["changed_at"], "traffic_percent": 5, "known_good": True,
                        "schema_migration": False, "rollout_complete": True},
                    "traces": {"downstream_healthy": True, "failing_span": "checkout.validate", "sample_count": 200}})
            if path.path == "/api/v1/query":
                query = parse_qs(path.query)
                name = query.get("query", [""])[0].removeprefix("demo_")
                stamp = float(query.get("time", [str(now)])[0])
                # A window overlapping the unhealthy period remains unhealthy.
                good = state["healthy"] and stamp - 10 >= state["recovered_at"]
                values = {"error_rate": .003 if good else .18, "control_error_rate": .002,
                    "p99_ms": 180 if good else 2400, "qps": 1000, "requests": 10000,
                    "control_requests": 190000, "sample_age_seconds": 1}
                if name not in values:
                    return self.respond(400, {"error": "Unknown fixture metric"})
                return self.respond(200, {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [stamp, str(values[name])]}]}})
            self.respond(404, {"error": "Not found"})

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 1024:
                return self.respond(413, {"error": "Demo request too large"})
            self.rfile.read(size)
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + control_token):
                return self.respond(401, {"error": "Demo control token required"})
            if self.path != "/recover":
                return self.respond(404, {"error": "Not found"})
            if not state["healthy"]:
                state["healthy"], state["recovered_at"] = True, time.time()
            self.respond(200, {"mode": "DEMO", "revision": "r41", "healthy": True})

    return BoundedServer(("127.0.0.1", port), Handler)


def launch(args):
    from .pilot import serve
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for role in ("operator", "receiver", "control"):
        path = root / (role + ".token")
        if not path.exists():
            path.write_text(secrets.token_urlsafe(36), encoding="utf-8")
        os.environ["MTTR_DEMO_" + role.upper() + "_TOKEN"] = path.read_text(encoding="utf-8").strip()
    evidence = evidence_server(args.evidence_port, os.environ["MTTR_DEMO_CONTROL_TOKEN"])
    thread = threading.Thread(target=evidence.serve_forever, daemon=True)
    thread.start()
    data = config_data(f"http://127.0.0.1:{evidence.server_port}")
    data["services"][0]["links"] = {"工作台": f"http://127.0.0.1:{args.port}/"}
    config = Config(data)
    (root / "config.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    store = PilotStore(root / "pilot.db")
    started = datetime.now(timezone.utc).isoformat()
    store.receive(config.services["checkout"], {"alerts": [{"status": "firing", "labels": {**SCOPE, "alertname": "CanaryErrorRateHigh"},
        "startsAt": started, "fingerprint": "demo-checkout", "annotations": {"summary": "结算服务灰度发布后错误率升高（本地演练）"}}]})
    print(f"Paste the token from this local file into the workbench: {root / 'operator.token'}", flush=True)
    print("Synthetic evidence only. Restart creates a NEW demo incident; real metrics exclude DEMO.", flush=True)
    try:
        serve(config, root / "pilot.db", args.port)
    finally:
        evidence.shutdown()
        evidence.server_close()


def recover(args):
    token = (Path(args.directory) / "control.token").read_text(encoding="utf-8").strip()
    req = urllib.request.Request(f"http://127.0.0.1:{args.evidence_port}/recover", data=b"{}",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        print(response.read().decode())


if __name__ == "__main__":
    raise SystemExit("Use python -m mttr_guard.pilot demo")
