"""One interactive localhost pilot using real monitoring binaries and request telemetry.

Waits for a human decision file after recommendation; never auto-approves rollback.
"""
import argparse
import hashlib
import http.client
import json
import os
import platform
import shutil
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.local_pilot_service import SCOPE, WINDOW
from mttr_guard.operations import backup


def request(url, token=None, body=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        return json.load(response)


def write(path, data):
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def queries():
    scope = ",".join(k + "=" + json.dumps(v) for k, v in SCOPE.items())
    canary = scope + ',track="canary"'
    stable = scope + ',track="stable"'
    def error(labels):
        return f'sum(rate(lab_http_requests_total{{{labels},status="500"}}[30s])) / sum(rate(lab_http_requests_total{{{labels}}}[30s]))'
    return {"error_rate": error(canary), "control_error_rate": error(stable),
        "p99_ms": f'1000 * histogram_quantile(0.99, sum by (le) (rate(lab_http_request_duration_seconds_bucket{{{canary}}}[30s])))',
        "qps": f'sum(rate(lab_http_requests_total{{{canary}}}[30s]))',
        "requests": f'sum(increase(lab_http_requests_total{{{canary}}}[30s]))',
        "control_requests": f'sum(increase(lab_http_requests_total{{{stable}}}[30s]))',
        "sample_age_seconds": f'max(time() - lab_last_request_timestamp_seconds{{{scope}}})'}


class Traffic:
    WORKERS = 12
    INTERVAL = .3

    def __init__(self, base):
        self.base, self.stop = base, threading.Event()
        self.lock, self.sequence, self.results = threading.Lock(), 0, {}
        self.pool = ThreadPoolExecutor(max_workers=self.WORKERS)

    def run(self, offset):
        self.stop.wait(offset)
        parsed = urllib.parse.urlsplit(self.base)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        while not self.stop.is_set():
            started = time.monotonic()
            with self.lock:
                self.sequence += 1
                track = "canary" if self.sequence % 20 == 0 else "stable"
            try:
                connection.request("GET", "/checkout?track=" + track)
                response = connection.getresponse()
                code = str(response.status)
                response.read()
            except Exception as exc:
                code = type(exc).__name__
                connection.close()
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            with self.lock:
                key = track + "/" + code
                self.results[key] = self.results.get(key, 0) + 1
            self.stop.wait(max(0, self.INTERVAL - (time.monotonic() - started)))
        connection.close()

    def start(self):
        for index in range(self.WORKERS):
            self.pool.submit(self.run, index * self.INTERVAL / self.WORKERS)

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="A new ignored artifacts directory")
    parser.add_argument("--tools", default="artifacts/local-pilot-tools")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    artifact_root = Path("artifacts").resolve()
    if root == artifact_root or not root.is_relative_to(artifact_root):
        parser.error("Output must be a new subdirectory of ignored artifacts/")
    root.mkdir(parents=True, exist_ok=False)
    scratch = tempfile.TemporaryDirectory(prefix="mttr-local-pilot-")
    scratch_root = Path(scratch.name)
    binaries = Path(args.tools).resolve()
    started = time.time()
    phases, processes, logs = [], [], []
    traffic = None
    iid = None
    report = {"schema_version": 1, "domain": "LOCAL_LAB", "traffic": "synthetic HTTP workload",
        "telemetry": "Actual HTTP counters, histograms, release records and OpenTelemetry spans",
        "reasoner": "deterministic-baseline, no LLM", "status": "RUNNING", "scope": SCOPE,
        "runtime": {"python": platform.python_version(), "os": platform.system(), "machine": platform.machine()},
        "claims": {"production_readiness": "not_assessed", "causal_mttr_reduction": "not_established",
                   "human_effort": "Not measured separately from chat/tool waiting time"}}
    used_ports = set()
    def port():
        while True:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                candidate = sock.getsockname()[1]
            if candidate not in used_ports:
                used_ports.add(candidate)
                return candidate
    service_port, prometheus_port, alertmanager_port, api_port = [port() for _ in range(4)]
    service_url, prom_url, am_url, api_url = [f"http://127.0.0.1:{p}" for p in (service_port, prometheus_port, alertmanager_port, api_port)]
    tokens = {name: secrets.token_urlsafe(36) for name in ("control", "evidence", "operator", "receiver", "audit")}
    # Lab-only key retained locally for independent backup verification; never publish this directory.
    (root / "audit.key").write_text(tokens["audit"], encoding="utf-8")
    runtime_env = {**os.environ, "MTTR_LAB_EVIDENCE_TOKEN": tokens["evidence"], "MTTR_LAB_OPERATOR_TOKEN": tokens["operator"],
        "MTTR_LAB_RECEIVER_TOKEN": tokens["receiver"], "MTTR_AUDIT_KEY": tokens["audit"]}
    runtime_env.pop("MTTR_AUDIT_KEY_FILE", None)
    runtime_env.pop("MTTR_LAB_CONTROL_TOKEN", None)
    lab_env = {**os.environ, "MTTR_LAB_CONTROL_TOKEN": tokens["control"], "MTTR_LAB_EVIDENCE_TOKEN": tokens["evidence"],
               "MTTR_LAB_SPAN_FILE": str(scratch_root / "spans.jsonl")}

    def phase(name, **details):
        row = {"phase": name, "at": time.time(), **details}
        phases.append(row)
        write(root / "status.json", row)
        write(root / "phases.json", phases)
        print(json.dumps(row), flush=True)

    def launch(name, command, env=None):
        log = (root / (name + ".log")).open("wb")
        logs.append(log)
        process = subprocess.Popen(command, stdout=log, stderr=log, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append((name, process))

    def wait(check, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for name, process in processes:
                if process.poll() is not None:
                    raise RuntimeError("A pilot subprocess exited: " + name)
            try:
                value = check()
                if value:
                    return value
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(2)
        raise TimeoutError("Pilot phase deadline exceeded")

    def capture_prometheus(label):
        now = time.time()
        values = {name: request(prom_url + "/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": now}))
                  for name, query in queries().items()}
        write(root / (label + "-prometheus.json"), {"at": now, "queries": queries(), "responses": values})

    try:
        phase("CONFIGURING", service_url=service_url, api_url=api_url, prometheus_url=prom_url)
        query = queries()
        policy = {"window_seconds": WINDOW, "consecutive_windows": 3, "min_requests": 20,
                  "max_sample_age_seconds": 5, "max_error_rate": .01, "max_p99_ms": 500,
                  "min_qps_ratio": .5, "observation_timeout_seconds": 240}
        plan = {"scenario": "r42 checkout discount parser regression", "warmup_seconds": 40,
            "target_total_rps": 40, "canary_fraction": .05, "baseline_canary_qps": 2,
            "traffic_driver": {"workers": Traffic.WORKERS, "interval_seconds": Traffic.INTERVAL},
            "alert": {"expression": f'({query["error_rate"]}) > 0.05 and ({query["control_error_rate"]}) < 0.01', "for": "4s"},
            "recovery_policy": policy, "allowed_data": ["aggregate request metrics", "synthetic request spans", "local release records"],
            "fault_target": "Local lab only", "human_decision_required": True, "control_group_for_mttr": False}
        write(root / "plan.json", plan)
        config = {"services": [{"id": "checkout", "scope": SCOPE, "owner": "local-pilot operator", "mode": "READ_ONLY",
            "prometheus_url": prom_url, "evidence_url": service_url + "/evidence", "evidence_token_env": "MTTR_LAB_EVIDENCE_TOKEN",
            "baseline_qps": 2, "policy": policy, "queries": query,
            "links": {"local monitoring": prom_url}, "runbook": ["Check canary/control evidence and release record before local rollback."]}],
            "identities": [{"subject": "pilot:operator", "role": "operator", "service": "checkout", "token_env": "MTTR_LAB_OPERATOR_TOKEN"},
                           {"subject": "pilot:alertmanager", "role": "receiver", "service": "checkout", "token_env": "MTTR_LAB_RECEIVER_TOKEN"}]}
        write(root / "config.local.json", config)
        receiver_file = root / "receiver.token"
        receiver_file.write_text(tokens["receiver"], encoding="utf-8")
        rules = {"groups": [{"name": "checkout-local-pilot", "rules": [{"alert": "LocalCanaryRegression",
            "expr": plan["alert"]["expression"], "for": "4s", "labels": {**SCOPE, "severity": "warning"},
            "annotations": {"summary": "Local checkout canary error rate above 5% with healthy control"}}]}]}
        write(root / "rules.json", rules)
        prometheus = {"global": {"scrape_interval": "2s", "evaluation_interval": "2s"},
            "rule_files": [(root / "rules.json").as_posix()],
            "scrape_configs": [{"job_name": "local-checkout", "static_configs": [{"targets": [f"127.0.0.1:{service_port}"], "labels": SCOPE}]}],
            "alerting": {"alertmanagers": [{"static_configs": [{"targets": [f"127.0.0.1:{alertmanager_port}"]}]}]}}
        write(root / "prometheus.json", prometheus)
        alertmanager = {"route": {"receiver": "mttr", "group_by": ["alertname", "service"], "group_wait": "1s", "group_interval": "5s", "repeat_interval": "10m"},
            "receivers": [{"name": "mttr", "webhook_configs": [{"url": api_url + "/v1/webhooks/alertmanager", "send_resolved": True,
                "http_config": {"authorization": {"type": "Bearer", "credentials_file": receiver_file.as_posix()}}}]}]}
        write(root / "alertmanager.json", alertmanager)
        for command in ([str(binaries / "promtool.exe"), "check", "config", str(root / "prometheus.json")],
                        [str(binaries / "amtool.exe"), "check-config", str(root / "alertmanager.json")]):
            result = subprocess.run(command, capture_output=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                (root / "configuration-error.log").write_bytes(result.stdout + result.stderr)
                raise ValueError("Monitoring configuration invalid")
        launch("service", [sys.executable, "-m", "scripts.local_pilot_service", "--port", str(service_port), "--output", str(root)], lab_env)
        launch("prometheus", [str(binaries / "prometheus.exe"), "--config.file=" + str(root / "prometheus.json"),
            "--web.listen-address=127.0.0.1:" + str(prometheus_port), "--storage.tsdb.path=" + str(scratch_root / "prometheus-data"), "--storage.tsdb.retention.time=2h"])
        launch("alertmanager", [str(binaries / "alertmanager.exe"), "--config.file=" + str(root / "alertmanager.json"),
            "--web.listen-address=127.0.0.1:" + str(alertmanager_port), "--cluster.listen-address=", "--storage.path=" + str(scratch_root / "alertmanager-data")])
        for role, extra in (("api", ["--port", str(api_port)]), ("diagnose", ["--queue", "diagnose"]), ("observe", ["--queue", "observe"])):
            launch(role, [sys.executable, "-m", "mttr_guard.enterprise", "api" if role == "api" else "worker",
                "--config", str(root / "config.local.json"), "--db", str(root / "pilot.db"), *extra], runtime_env)
        wait(lambda: request(service_url + "/status"), 30)
        wait(lambda: request(api_url + "/readyz")["worker_ready"], 30)
        traffic = Traffic(service_url)
        traffic.start()
        phase("WARMING_UP")
        until = time.monotonic() + 40
        wait(lambda: time.monotonic() >= until, 50)
        capture_prometheus("baseline")
        write(root / "baseline-evidence.json", request(service_url + "/evidence", tokens["evidence"]))
        result = subprocess.run([sys.executable, "-m", "mttr_guard.validation", "preflight", "--config", str(root / "config.local.json"),
            "--service", "checkout", "--output", str(root / "preflight.json")], env=runtime_env, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        (root / "preflight.log").write_bytes(result.stdout + result.stderr)
        if result.returncode:
            # Local-only diagnostic, with all generated credentials redacted.
            from mttr_guard.collectors import Collector
            from mttr_guard.pilot_config import Config
            os.environ.update({key: value for key, value in runtime_env.items() if key.startswith("MTTR_LAB_")})
            try:
                collector = Collector(Config.read(root / "config.local.json").services["checkout"])
                for _ in range(5):
                    collector.collect(time.time())
            except Exception as diagnostic:
                message = str(diagnostic)
                for secret in tokens.values():
                    message = message.replace(secret, "[REDACTED]")
                write(root / "preflight-diagnostic.json", {"error_type": type(diagnostic).__name__, "message": message})
            raise ValueError("Read-only source preflight did not pass")
        baseline = json.loads((root / "preflight.json").read_text(encoding="utf-8"))
        m = baseline["metrics"]
        baseline_checks = {
            "source_contract": baseline["status"] == "source_contract_passed",
            "canary_error_rate": m["error_rate"] < policy["max_error_rate"],
            "control_error_rate": m["control_error_rate"] < policy["max_error_rate"],
            "p99_ms": m["p99_ms"] < policy["max_p99_ms"],
            "canary_qps": m["qps"] >= config["services"][0]["baseline_qps"] * policy["min_qps_ratio"],
            "sample_count": min(m["requests"], m["control_requests"]) >= policy["min_requests"],
            "sample_age": m["sample_age_seconds"] <= policy["max_sample_age_seconds"],
        }
        write(root / "baseline-gate.json", {"checks": baseline_checks, "passed": all(baseline_checks.values()),
                                            "thresholds": policy, "actual_metrics": m})
        if not all(baseline_checks.values()):
            raise ValueError("Baseline is not healthy under the registered recovery policy")
        phase("PREFLIGHT_PASSED")
        released = request(service_url + "/release", tokens["control"], {"expected_revision": "r41", "target_revision": "r42",
            "actor": "fault-injection-harness", "reason": "Registered local discount parser fault"})
        phase("FAULT_INJECTED", **released)
        def find_incident():
            items = request(api_url + "/api/incidents", tokens["operator"])["items"]
            return items[0] if items else None
        inc = wait(find_incident, 90)
        iid = inc["id"]
        def current():
            return request(api_url + "/api/incidents/" + iid, tokens["operator"])["incident"]
        def diagnosed():
            candidate = current()
            return candidate if candidate["state"] in {"RECOMMENDED", "NEEDS_ATTENTION"} else None
        inc = wait(diagnosed, 90)
        write(root / "diagnosis.json", inc)
        capture_prometheus("fault")
        write(root / "fault-evidence.json", request(service_url + "/evidence", tokens["evidence"]))
        if inc["state"] != "RECOMMENDED":
            raise ValueError("Workflow did not produce an actionable recommendation")
        rec = inc["recommendation"]
        phase("AWAITING_OPERATOR", incident_id=iid, plan_hash=rec["hash"], target_revision=rec["target_revision"],
              metrics=inc["snapshot"]["metrics"], decision_file=str(root / "decision.json"), expires_at=rec["expires_at"])
        decision = wait(lambda: json.loads((root / "decision.json").read_text(encoding="utf-8")) if (root / "decision.json").exists() else None,
                        max(1, min(240, rec["expires_at"] - time.time() - 5)))
        if decision.get("action") != "rollback" or decision.get("incident_id") != iid or decision.get("plan_hash") != rec["hash"]:
            report["human_decision"] = decision.get("action")
            raise ValueError("Human declined or decision does not bind this recommendation")
        report["human_decision"] = decision
        def operate(action, body):
            return request(api_url + f"/api/incidents/{iid}/{action}", tokens["operator"], {"request_id": "local-pilot-" + action, **body})
        operate("ack", {})
        operate("review", {"plan_hash": rec["hash"], "decision": "accept", "reason": decision["reason"]})
        phase("OPERATOR_DECISION_RECORDED")
        rolled_back = request(service_url + "/release", tokens["control"], {"expected_revision": "r42", "target_revision": "r41",
            "actor": "operator-via-chat", "reason": decision["reason"]})
        phase("MANUAL_ROLLBACK_APPLIED", **rolled_back)
        operate("observe", {"target_revision": "r41", "reason": "Human-approved local rollback completed; verify actual request telemetry"})
        def finished():
            candidate = current()
            return candidate if candidate["state"] in {"RECOVERED", "NEEDS_ATTENTION"} else None
        final = wait(finished, 270)
        write(root / "final-incident.json", final)
        write(root / "timeline.json", request(api_url + f"/api/incidents/{iid}", tokens["operator"])["events"])
        capture_prometheus("recovery")
        write(root / "recovery-evidence.json", request(service_url + "/evidence", tokens["evidence"]))
        audit = backup(root / "pilot.db", root / "verified-backup.db", tokens["audit"].encode())
        report.update(status=final["state"], incident_id=iid, timeline=final["timeline"],
            detected_at=final["detected_at"], recovered_at=final.get("recovered_at"),
            recovery_seconds=final.get("recovery_seconds"), healthy_windows=final.get("healthy_streak"),
            observations=final.get("observations", []), audit_head=audit["head"], audit_verified=audit["keyed"],
            case_count=1, source_preflight="passed")
        phase("COMPLETED", state=final["state"], recovery_seconds=final.get("recovery_seconds"))
    except Exception as exc:
        report.update(status="FAILED", failure_phase=phases[-1]["phase"] if phases else "startup", error_type=type(exc).__name__)
        if iid is not None:
            report["incident_id"] = iid
            try:
                detail = request(api_url + "/api/incidents/" + iid, tokens["operator"])
                write(root / "final-incident.json", detail["incident"])
                write(root / "timeline.json", detail["events"])
                report["timeline"] = detail["incident"]["timeline"]
                report["incident_state_at_stop"] = detail["incident"]["state"]
            except Exception as capture_error:
                report["final_capture_error_type"] = type(capture_error).__name__
        phase("FAILED", error_type=type(exc).__name__)
    finally:
        if traffic:
            traffic.close()
            report["http_outcomes"] = traffic.results
        service_process = next((process for name, process in processes if name == "service"), None)
        if service_process is not None and service_process.poll() is None:
            try:
                request(service_url + "/stop", tokens["control"], {})
                service_process.wait(timeout=20)
                report["span_writer_flushed"] = True
            except (OSError, urllib.error.URLError, subprocess.TimeoutExpired):
                report["span_writer_flushed"] = False
        for _, process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()
        span_file = scratch_root / "spans.jsonl"
        if span_file.exists():
            shutil.copy2(span_file, root / "spans.jsonl")
        report["monitoring_data_storage"] = "OS temporary directory during live trial; captured query responses and span log retained in ignored artifacts"
        scratch.cleanup()
        report["owned_processes_stopped"] = all(process.poll() is not None for _, process in processes)
        report["elapsed_seconds"] = time.time() - started
        report["phases"] = phases
        report["artifacts_sha256"] = {}
        for name in ("plan.json", "spans.jsonl", "releases.jsonl", "preflight.json", "baseline-gate.json", "baseline-prometheus.json", "fault-prometheus.json",
                     "recovery-prometheus.json", "diagnosis.json", "final-incident.json", "timeline.json", "decision.json"):
            path = root / name
            if path.exists():
                report["artifacts_sha256"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        write(root / "report.json", report)
        print(json.dumps({"status": report["status"], "report": str(root / "report.json")}), flush=True)
    return 0 if report["status"] == "RECOVERED" else 1


if __name__ == "__main__":
    sys.exit(main())
