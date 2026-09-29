"""Loopback checkout lab: real HTTP work, Prometheus metrics and OTel spans.

Traffic and release faults are controlled experiments, never production evidence.
The evidence endpoint reads completed spans and actual release records.
"""
import argparse
import hmac
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST


SCOPE = {"tenant": "local-lab", "environment": "local-pilot", "cluster": "loopback",
         "namespace": "experiment", "service": "checkout"}
WINDOW = 30


class SpanLog(SpanExporter):
    def __init__(self, root):
        self.lock = threading.Lock()
        self.recent = deque(maxlen=20000)
        self.path = Path(os.environ.get("MTTR_LAB_SPAN_FILE", str(root / "spans.jsonl")))
        self.pending = []
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.output = self.path.open("a", encoding="utf-8", buffering=65536)
        self.writer = threading.Thread(target=self._write_loop, daemon=True)
        self.writer.start()

    def export(self, spans):
        with self.lock:
            for span in spans:
                row = {"trace_id": format(span.context.trace_id, "032x"), "span_id": format(span.context.span_id, "016x"),
                    "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
                    "name": span.name, "started_at": span.start_time / 1e9, "ended_at": span.end_time / 1e9,
                    "status": span.status.status_code.name, "attributes": dict(span.attributes or {})}
                self.recent.append(row)
                self.pending.append(json.dumps(row) + "\n")
            if len(self.pending) >= 200:
                self.wake.set()
        return SpanExportResult.SUCCESS

    def _write_loop(self):
        while True:
            self.wake.wait(.5)
            self.wake.clear()
            with self.lock:
                batch, self.pending = self.pending, []
            if batch:
                self.output.writelines(batch)
                self.output.flush()
            if self.stopping.is_set():
                break
        self.output.close()

    def shutdown(self):
        self.stopping.set()
        self.wake.set()
        self.writer.join(timeout=20)
        if self.writer.is_alive():
            raise TimeoutError("Local span writer did not stop")

    def snapshot(self, now):
        with self.lock:
            return [r for r in self.recent if now - WINDOW <= r["ended_at"] <= now]


class Lab:
    def __init__(self, root, control_token, evidence_token):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.control_token, self.evidence_token = control_token, evidence_token
        self.lock = threading.Lock()
        self.revision, self.previous, self.changed = "r41", "r40", time.time()
        self.counts = {revision: {"ok": 0, "error": 0} for revision in ("r41", "r42", "r43")}
        self.exporter = SpanLog(self.root)
        self.provider = TracerProvider(resource=Resource.create({"service.name": "checkout-local-pilot"}))
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("mttr-guard-local-pilot")
        self.registry = CollectorRegistry()
        self.requests = Counter("lab_http_requests_total", "Completed checkout HTTP requests", ["track", "status"], registry=self.registry)
        self.latency = Histogram("lab_http_request_duration_seconds", "Checkout request duration", ["track"],
            buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2, 5), registry=self.registry)
        self.last = Gauge("lab_last_request_timestamp_seconds", "Last actual completed checkout request", ["track"], registry=self.registry)
        for track in ("canary", "stable"):
            for status in ("200", "500"):
                self.requests.labels(track, status).inc(0)
            self.latency.labels(track)
            self.last.labels(track).set(self.changed)
        self._release_record("bootstrap", "r40", "r41", "Initial local test service")

    def _release_record(self, actor, before, after, reason):
        row = {"at": self.changed, "actor": actor, "before": before, "after": after,
               "reason": reason, "scope": SCOPE, "environment_type": "LOCAL_LAB"}
        with (self.root / "releases.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(row) + "\n")

    def release(self, body):
        if (set(body) != {"expected_revision", "target_revision", "actor", "reason"}
                or body["target_revision"] not in {"r41", "r42", "r43"}
                or any(not isinstance(body[k], str) or not 1 <= len(body[k]) <= 300 for k in body)):
            raise ValueError("Invalid release request")
        with self.lock:
            if body["expected_revision"] != self.revision or body["target_revision"] == self.revision:
                raise ValueError("Release precondition mismatch")
            self.previous, self.revision, self.changed = self.revision, body["target_revision"], time.time()
            self._release_record(body["actor"], self.previous, self.revision, body["reason"])
            return {"revision": self.revision, "changed_at": self.changed}

    def evidence(self):
        now = time.time()
        rows = self.exporter.snapshot(now)
        roots = [r for r in rows if r["name"] == "checkout.request" and r["attributes"].get("track") == "canary"]
        dependencies = [r for r in rows if r["name"] == "inventory.lookup"]
        if not roots or not dependencies:
            raise ValueError("No complete recent request/dependency samples")
        failed = [r for r in rows if r["status"] == "ERROR" and r["name"] == "checkout.validate"]
        with self.lock:
            known = self.counts[self.previous]["ok"] >= 20 and self.counts[self.previous]["error"] == 0 if self.previous in self.counts else False
            deployment = {"revision": self.revision, "previous_revision": self.previous, "changed_at": self.changed,
                "known_good": known, "schema_migration": False, "rollout_complete": True, "traffic_percent": 5}
        return {"scope": SCOPE, "mode": "READ_ONLY", "window_seconds": WINDOW,
            "observed_at": min(max(r["ended_at"] for r in roots), max(r["ended_at"] for r in dependencies)),
            "deployment": deployment, "traces": {"sample_count": len(roots),
                "downstream_healthy": all(r["status"] != "ERROR" for r in dependencies),
                "failing_span": "checkout.validate" if failed else "none"},
            "provenance": "Local experiment: actual Prometheus samples and completed OpenTelemetry spans; synthetic traffic"}


def make_server(lab, port):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def finish(self):
            try:
                super().finish()
            finally:
                connection = getattr(self, "inventory_connection", None)
                if connection is not None:
                    connection.close()

        def respond(self, status, body, content_type="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self, token):
            return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)

        def do_POST(self):
            if not self.authorized(lab.control_token):
                return self.respond(403, {"error": "Denied"})
            if self.path == "/stop":
                self.respond(200, {"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path != "/release":
                return self.respond(404, {"error": "Not found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError("Body too large")
                result = lab.release(json.loads(self.rfile.read(size)))
            except (ValueError, KeyError, TypeError):
                return self.respond(409, {"error": "Invalid release or version changed"})
            self.respond(200, result)

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/metrics":
                return self.respond(200, generate_latest(lab.registry), CONTENT_TYPE_LATEST)
            if parsed.path == "/status":
                return self.respond(200, {"revision": lab.revision, "scope": SCOPE, "environment_type": "LOCAL_LAB"})
            if parsed.path == "/evidence":
                if not self.authorized(lab.evidence_token):
                    return self.respond(403, {"error": "Denied"})
                try:
                    result = lab.evidence()
                except ValueError:
                    return self.respond(503, {"error": "Insufficient recent trace evidence"})
                return self.respond(200, result)
            track = urllib.parse.parse_qs(parsed.query).get("track", [""])[0]
            if parsed.path not in {"/checkout", "/inventory"} or track not in {"canary", "stable"}:
                return self.respond(404, {"error": "Not found"})
            context = propagate.extract({key.lower(): value for key, value in self.headers.items()})
            if parsed.path == "/inventory":
                with lab.tracer.start_as_current_span("inventory.lookup", context=context, kind=trace.SpanKind.SERVER) as span:
                    span.set_attribute("track", track)
                    with lab.lock:
                        unavailable = track == "canary" and lab.revision == "r43"
                    status = 503 if unavailable else 200
                    span.set_attribute("http.status_code", status)
                    span.set_status(Status(StatusCode.ERROR if unavailable else StatusCode.OK))
                    self.respond(status, {"available": not unavailable})
                return
            started = time.perf_counter()
            with lab.lock:
                revision = lab.revision if track == "canary" else "r41"
            status = 200
            with lab.tracer.start_as_current_span("checkout.request", context=context, kind=trace.SpanKind.SERVER) as root:
                root.set_attribute("track", track)
                root.set_attribute("revision", revision)
                try:
                    headers = {}
                    propagate.inject(headers)
                    connection = getattr(self, "inventory_connection", None)
                    if connection is None:
                        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
                        self.inventory_connection = connection
                    try:
                        connection.request("GET", "/inventory?track=" + track, headers=headers)
                        response = connection.getresponse()
                        payload = response.read()
                        available = json.loads(payload)["available"] if response.status == 200 else False
                        if not available:
                            raise ValueError("Inventory unavailable")
                    except (OSError, http.client.HTTPException):
                        connection.close()
                        self.inventory_connection = None
                        raise
                    with lab.tracer.start_as_current_span("checkout.validate") as child:
                        child.set_attribute("track", track)
                        child.set_attribute("revision", revision)
                        # r42 has an actual parsing regression for otherwise valid requests.
                        discount = int("five" if revision == "r42" else "5")
                        total = 100 - discount
                    body = {"total": total, "revision": revision}
                except (ValueError, OSError, http.client.HTTPException) as exc:
                    status, body = 500, {"error": type(exc).__name__, "revision": revision}
                    root.record_exception(exc)
                    root.set_status(Status(StatusCode.ERROR))
                root.set_attribute("http.status_code", status)
                self.respond(status, body)
            lab.requests.labels(track, str(status)).inc()
            lab.latency.labels(track).observe(time.perf_counter() - started)
            lab.last.labels(track).set(time.time())
            with lab.lock:
                lab.counts[revision]["ok" if status == 200 else "error"] += 1

    class LabServer(ThreadingHTTPServer):
        request_queue_size = 128

    server = LabServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    lab = Lab(args.output, os.environ["MTTR_LAB_CONTROL_TOKEN"], os.environ["MTTR_LAB_EVIDENCE_TOKEN"])
    server = make_server(lab, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        lab.provider.shutdown()


if __name__ == "__main__":
    main()
