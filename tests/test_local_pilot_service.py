"""Check that lab evidence comes from actual handled requests, not canned fixtures."""
import importlib.util
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path


ENABLED = importlib.util.find_spec("prometheus_client") is not None and importlib.util.find_spec("opentelemetry") is not None
if ENABLED:
    from scripts.local_pilot_service import Lab, make_server
    from scripts.run_local_pilot import request


@unittest.skipUnless(ENABLED, "Install requirements-local-pilot.txt")
class LocalPilotServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lab = Lab(self.temp.name, "test-control", "test-evidence")
        self.addCleanup(self.lab.provider.shutdown)
        self.server = make_server(self.lab, 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def release(self, target, expected):
        return request(self.base + "/release", "test-control", {"expected_revision": expected, "target_revision": target,
            "actor": "test-harness", "reason": "Synthetic local test"})

    def test_no_traffic_means_no_evidence(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request(self.base + "/evidence", "test-evidence")
        self.assertEqual(caught.exception.code, 503)
        caught.exception.close()

    def test_fault_creates_http_errors_and_linked_spans_then_rollback_recovers(self):
        for _ in range(22):
            self.assertEqual(request(self.base + "/checkout?track=canary")["total"], 95)
        time.sleep(.05)  # Response delivery precedes recording the completed request.
        self.release("r42", "r41")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request(self.base + "/checkout?track=canary")
        self.assertEqual(caught.exception.code, 500)
        caught.exception.close()
        request(self.base + "/checkout?track=stable")
        time.sleep(.05)
        evidence = request(self.base + "/evidence", "test-evidence")
        self.assertTrue(evidence["deployment"]["known_good"])
        self.assertTrue(evidence["traces"]["downstream_healthy"])
        self.assertEqual(evidence["traces"]["failing_span"], "checkout.validate")
        rows = self.lab.exporter.snapshot(time.time())
        failed = next(r for r in rows if r["name"] == "checkout.validate" and r["status"] == "ERROR")
        related = [r for r in rows if r["trace_id"] == failed["trace_id"]]
        root = next(r for r in related if r["name"] == "checkout.request")
        inventory = next(r for r in related if r["name"] == "inventory.lookup")
        self.assertEqual(failed["parent_span_id"], root["span_id"])
        self.assertEqual(inventory["parent_span_id"], root["span_id"])
        self.release("r41", "r42")
        self.assertEqual(request(self.base + "/checkout?track=canary")["revision"], "r41")
        releases = [json.loads(line) for line in (Path(self.temp.name) / "releases.jsonl").read_text().splitlines()]
        self.assertEqual([r["after"] for r in releases], ["r41", "r42", "r41"])

    def test_read_only_credential_cannot_release_and_cas_is_enforced(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request(self.base + "/release", "test-evidence", {})
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request(self.base + "/stop", "test-evidence", {})
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.release("r42", "wrong-revision")
        self.assertEqual(caught.exception.code, 409)
        caught.exception.close()
        self.assertEqual(request(self.base + "/status")["revision"], "r41")
