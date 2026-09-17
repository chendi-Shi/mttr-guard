"""Exercise authenticated HTTP intake, collectors, human handoff and recovery with real time.

Run from the repository root: python -m scripts.run_pilot_drill
All evidence is DEMO. Elapsed time is NOT a production MTTR benchmark.
"""
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mttr_guard.pilot_config import Config
from mttr_guard.pilot_demo import SCOPE, config_data, evidence_server
from mttr_guard.pilot_http import make_server
from mttr_guard.pilot_store import PilotStore
from mttr_guard.pilot_worker import Worker


def request(base, path, token, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        return json.loads(response.read())


def main():
    tokens = {name: secrets.token_urlsafe(36) for name in ("operator", "receiver", "control")}
    for name, value in tokens.items():
        os.environ["MTTR_DEMO_" + name.upper() + "_TOKEN"] = value
    stop = threading.Event()
    evidence = evidence_server(0, tokens["control"])
    evidence_thread = threading.Thread(target=evidence.serve_forever, daemon=True)
    evidence_thread.start()
    evidence_url = f"http://127.0.0.1:{evidence.server_port}"
    config = Config(config_data(evidence_url))
    try:
        with tempfile.TemporaryDirectory() as temp:
            store = PilotStore(Path(temp) / "pilot.db")
            server = make_server(store, config, 0)
            http_thread = threading.Thread(target=server.serve_forever, daemon=True)
            http_thread.start()
            worker = threading.Thread(target=Worker(store, config).run, args=(stop,), daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                started = time.time()
                payload = {"alerts": [{"status": "firing", "labels": SCOPE, "fingerprint": "http-drill",
                    "startsAt": datetime.fromtimestamp(started, timezone.utc).isoformat()}]}
                receipt = request(base, "/v1/webhooks/alertmanager", tokens["receiver"], payload)
                iid = receipt["incident_ids"][0]
                assert request(base, "/v1/webhooks/alertmanager", tokens["receiver"], payload) == receipt

                def detail():
                    return request(base, f"/api/incidents/{iid}", tokens["operator"])

                deadline = time.monotonic() + 25
                while detail()["incident"]["state"] == "QUEUED" and time.monotonic() < deadline:
                    stop.wait(.1)
                incident = detail()["incident"]
                assert incident["state"] == "RECOMMENDED", detail()
                for action, body in (("ack", {}), ("review", {"plan_hash": incident["recommendation"]["hash"], "decision": "accept", "reason": "Synthetic drill approval"})):
                    request(base, f"/api/incidents/{iid}/{action}", tokens["operator"], {"request_id": action, **body})
                request(evidence_url, "/recover", tokens["control"], {})
                request(base, f"/api/incidents/{iid}/observe", tokens["operator"],
                    {"request_id": "observe", "target_revision": "r41", "reason": "Synthetic external deployment completed"})
                # Replace the worker after the durable observation job exists.
                stop.set()
                worker.join(10)
                assert not worker.is_alive()
                stop.clear()
                worker = threading.Thread(target=Worker(PilotStore(store.path), config).run, args=(stop,), daemon=True)
                worker.start()
                deadline = time.monotonic() + 50
                while detail()["incident"]["state"] == "OBSERVING" and time.monotonic() < deadline:
                    stop.wait(.5)
                report = detail()
                assert report["incident"]["state"] == "RECOVERED", report["incident"]
                metrics = request(base, "/api/metrics", tokens["operator"])
                assert metrics["incidents"] == 0 and metrics["excluded_demo_incidents"] == 1
                report.update(mode="DEMO", wall_clock_seconds=time.time() - started, metrics=metrics,
                    disclaimer="Synthetic HTTP integration drill, not production MTTR or an effectiveness comparison")
                output = Path("artifacts/pilot-http-drill.json")
                output.parent.mkdir(exist_ok=True)
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"state": report["incident"]["state"], "healthy_windows": report["incident"]["healthy_streak"],
                    "wall_clock_seconds": round(report["wall_clock_seconds"], 2), "mode": "DEMO", "report": str(output)}))
            finally:
                stop.set()
                worker.join(10)
                server.shutdown()
                server.server_close()
    finally:
        evidence.shutdown()
        evidence.server_close()


if __name__ == "__main__":
    main()
