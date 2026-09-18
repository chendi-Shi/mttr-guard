"""Real API/worker subprocess drill with restart, HMAC audit, backup and restore.

Run with enterprise dependencies: python -m scripts.run_enterprise_drill
No production systems are contacted; the evidence server explicitly uses DEMO.
"""
import json
import argparse
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from mttr_guard.operations import backup, inspect_database, restore
from mttr_guard.pilot_demo import SCOPE, config_data, evidence_server
from scripts.run_pilot_drill import request


def check_live_quarantine(database, base, tokens, iid, key):
    """Corrupt the disposable DEMO database while API/workers are still live."""
    with closing(sqlite3.connect(database, timeout=10)) as db, db:
        db.execute("UPDATE incidents SET body='forged-drill-state' WHERE id=?", (iid,))
        damaged = list(db.iterdump())
    for path, body in (("/readyz", None), (f"/api/incidents/{iid}", None),
                       (f"/api/incidents/{iid}/ack", {"request_id": "after-tamper"})):
        try:
            request(base, path, tokens["operator"], body)
        except urllib.error.HTTPError as exc:
            try:
                assert exc.code == 503
                assert json.load(exc)["code"] == "audit_integrity_failed"
            finally:
                exc.close()
        else:
            raise AssertionError("Live API accepted corrupt storage")
    # Both idle workers poll once per second. No heartbeat, lease or audit
    # record may advance even while their processes remain alive.
    time.sleep(1.2)
    with closing(sqlite3.connect(database)) as db:
        assert list(db.iterdump()) == damaged
    try:
        inspect_database(database, key)
    except ValueError:
        return {"api_refused": True, "database_unchanged": True, "tampering_still_detected": True}
    raise AssertionError("Normal traffic legitimized corrupted state")


def wait_until(check, processes, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if any(p.poll() is not None for p in processes):
            raise AssertionError("API or worker exited unexpectedly; inspect local drill logs")
        try:
            result = check()
            if result:
                return result
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(.25)
    raise AssertionError("Drill phase deadline exceeded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-check", action="store_true", help="Also exercise the workbench using local Node, Playwright and Edge")
    args = parser.parse_args()
    started = time.time()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        audit_key = secrets.token_urlsafe(40)
        tokens = {name: secrets.token_urlsafe(36) for name in ("operator", "receiver", "control")}
        environment = {**os.environ, "MTTR_AUDIT_KEY": audit_key,
            "MTTR_DEMO_OPERATOR_TOKEN": tokens["operator"], "MTTR_DEMO_RECEIVER_TOKEN": tokens["receiver"]}
        environment.pop("MTTR_AUDIT_KEY_FILE", None)
        fixture = evidence_server(0, tokens["control"])
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        fixture_url = f"http://127.0.0.1:{fixture.server_port}"
        config = root / "config.json"
        config.write_text(json.dumps(config_data(fixture_url)), encoding="utf-8")
        database = root / "live.db"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        processes, logs = [], []
        def launch(role, *args):
            log = (root / (role + str(len(processes)) + ".log")).open("wb")
            logs.append(log)
            proc = subprocess.Popen([sys.executable, "-m", "mttr_guard.enterprise", role,
                "--config", str(config), "--db", str(database), *args], env=environment,
                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            processes.append(proc)
            return proc
        try:
            launch("api", "--port", str(port))
            launch("worker", "--queue", "diagnose")
            observation = launch("worker", "--queue", "observe")
            wait_until(lambda: request(base, "/readyz", tokens["operator"])["worker_ready"], processes)
            now = time.time()
            payload = {"alerts": [{"status": "firing", "labels": SCOPE, "fingerprint": "enterprise-drill",
                "startsAt": datetime.fromtimestamp(now, timezone.utc).isoformat()}]}
            receipt = request(base, "/v1/webhooks/alertmanager", tokens["receiver"], payload)
            iid = receipt["incident_ids"][0]
            assert request(base, "/v1/webhooks/alertmanager", tokens["receiver"], payload) == receipt
            def incident():
                return request(base, f"/api/incidents/{iid}", tokens["operator"])["incident"]
            wait_until(lambda: incident()["state"] == "RECOMMENDED", processes)
            inc = incident()
            request(base, f"/api/incidents/{iid}/review", tokens["operator"], {
                "request_id": "review", "plan_hash": inc["recommendation"]["hash"], "decision": "accept", "reason": "DEMO checks passed"})
            request(fixture_url, "/recover", tokens["control"], {})
            request(base, f"/api/incidents/{iid}/observe", tokens["operator"], {
                "request_id": "observe", "reason": "DEMO external rollback", "target_revision": "r41"})
            if args.browser_check:
                subprocess.run([os.environ.get("NODE_BINARY", "node"), "scripts/check_enterprise_ui.cjs"],
                    env={**os.environ, "MTTR_UI_BASE": base, "MTTR_UI_TOKEN": tokens["operator"], "MTTR_UI_INCIDENT": iid},
                    check=True, timeout=45)
            wait_until(lambda: len(incident()["observations"]) >= 1, processes)
            observation.kill()
            observation.wait(timeout=5)
            processes.remove(observation)
            launch("worker", "--queue", "observe")
            wait_until(lambda: incident()["state"] == "RECOVERED", processes, seconds=45)
            saved, restored = root / "backup.db", root / "restored.db"
            backup_result = backup(database, saved, audit_key.encode())
            restored_result = restore(saved, restored, audit_key.encode())
            assert backup_result["head"] == restored_result["head"]
            metrics = request(base, "/api/metrics", tokens["operator"])
            assert metrics["incidents"] == 0 and metrics["excluded_demo_incidents"] == 1
            report = {"mode": "DEMO", "state": incident()["state"], "worker_process_restarted": True,
                "healthy_windows": incident()["healthy_streak"], "audit_verified": restored_result["keyed"],
                "audit_records_in_backup": restored_result["records"], "restored_incidents": restored_result["incidents"],
                "wall_clock_seconds": round(time.time() - started, 2),
                "browser_check": "passed" if args.browser_check else "not_run",
                "disclaimer": "Synthetic deployment drill; not a real MTTR result or high-availability validation"}
            report["live_integrity_quarantine"] = check_live_quarantine(database, base, tokens, iid, audit_key.encode())
            report["wall_clock_seconds"] = round(time.time() - started, 2)
            output = Path("artifacts/enterprise-drill.json")
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report))
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.terminate()
            for proc in processes:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            for log in logs:
                log.close()
            fixture.shutdown()
            fixture.server_close()


if __name__ == "__main__":
    main()
