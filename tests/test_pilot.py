import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mttr_guard.__main__ import DemoClock
from mttr_guard.collectors import Collector, EvidenceUnavailable
from mttr_guard.core import Principal, Rejected, Scope
from mttr_guard.pilot_config import Config
from mttr_guard.pilot_demo import config_data, evidence_server
from mttr_guard.pilot_http import make_server
from mttr_guard.pilot_store import PilotStore, QueueFull
from mttr_guard.pilot_worker import Worker


OPERATOR = "o" * 40
RECEIVER = "r" * 40
VIEWER = "v" * 40


class PilotFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"MTTR_DEMO_OPERATOR_TOKEN": OPERATOR, "MTTR_DEMO_RECEIVER_TOKEN": RECEIVER, "MTTR_VIEWER_TOKEN": VIEWER})
        self.env.start()
        self.addCleanup(self.env.stop)
        data = config_data("http://127.0.0.1:9191")
        data["services"][0]["mode"] = "READ_ONLY"
        data["identities"].append({"subject": "viewer", "role": "viewer", "service": "checkout", "token_env": "MTTR_VIEWER_TOKEN"})
        self.config = Config(data)
        self.service = self.config.services["checkout"]
        self.clock = DemoClock()
        self.changed_at = self.clock() - 180
        self.store = PilotStore(Path(self.temp.name) / "pilot.db", self.clock)
        self.operator = self.config.identities[0]
        self.healthy = False
        self.worker = Worker(self.store, self.config, collector_factory=self.factory)

    def payload(self, fingerprint="alert"):
        return {"alerts": [{"status": "firing", "labels": asdict(self.service.scope), "fingerprint": fingerprint,
            "startsAt": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), "annotations": {"summary": "Checkout regression"}}]}

    def context(self):
        return {"scope": asdict(self.service.scope), "mode": self.service.mode, "observed_at": self.clock(), "window_seconds": 10,
            "deployment": {"revision": "r41" if self.healthy else "r42", "previous_revision": "r41", "traffic_percent": 5,
                "known_good": True, "schema_migration": False, "rollout_complete": True, "changed_at": self.changed_at},
            "traces": {"downstream_healthy": True, "failing_span": "checkout.validate", "sample_count": 200}}

    def metric_values(self):
        return {"error_rate": .003 if self.healthy else .18, "control_error_rate": .002, "p99_ms": 180 if self.healthy else 2400,
            "qps": 1000, "requests": 10000, "control_requests": 190000, "sample_age_seconds": 1}

    def transport(self, url, token=None):
        if urlsplit(url).path == "/evidence":
            return self.context()
        query = parse_qs(urlsplit(url).query)
        key = query["query"][0].removeprefix("demo_")
        return {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [float(query["time"][0]), str(self.metric_values()[key])]}]}}

    def factory(self, service):
        return Collector(service, self.transport)

    def receive(self, fingerprint="alert"):
        return self.store.receive(self.service, self.payload(fingerprint))[0]

    def recommend(self):
        iid = self.receive()
        self.worker.run_once()
        inc = self.store.get(iid, self.service.scope)
        self.assertEqual(inc["state"], "RECOMMENDED")
        return iid, inc

    def action(self, iid, action, **body):
        return self.store.operate(iid, self.service, self.operator, action, {"request_id": action + str(self.clock()), **body})

    def observing(self):
        iid, inc = self.recommend()
        self.action(iid, "observe", reason="Rolled back in deployment platform, ticket 123", target_revision="r41")
        self.healthy = True
        return iid

class PilotTests(PilotFixture):
    def test_batch_receipt_and_job_commit_are_atomic(self):
        payload = self.payload()
        payload["alerts"].append({**payload["alerts"][0], "fingerprint": "second", "annotations": "invalid"})
        with self.assertRaises(Rejected):
            self.store.receive(self.service, payload)
        self.assertEqual(self.store.list_incidents(self.service.scope), [])
        self.assertIsNone(self.store.claim())

    def test_duplicate_alert_does_not_duplicate_job(self):
        iid = self.receive()
        self.assertEqual(self.receive(), iid)
        job, _ = self.store.claim()
        self.assertIsNone(self.store.claim())
        self.assertEqual(job["attempts"], 1)

    def test_concurrent_delivery_has_one_incident(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: self.receive(), range(12)))
        self.assertEqual(len(set(ids)), 1)

    def test_queue_backpressure_rolls_back_whole_batch(self):
        with self.assertRaises(QueueFull):
            self.store.receive(self.service, self.payload(), max_pending=0)
        self.assertEqual(self.store.list_incidents(self.service.scope), [])

    def test_resolved_alert_is_not_recovery(self):
        iid = self.receive()
        payload = self.payload()
        payload["alerts"][0]["status"] = "resolved"
        self.assertEqual(self.store.receive(self.service, payload), [])
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "QUEUED")

    def test_lease_restart_and_old_worker_fencing(self):
        iid = self.receive()
        old_job, _ = self.store.claim(10)
        snapshot = self.factory(self.service).collect(self.clock())
        self.clock.advance(11)
        restarted = PilotStore(self.store.path, self.clock)
        new_job, _ = restarted.claim()
        self.assertFalse(self.store.finish_diagnosis(old_job, self.service.scope, snapshot, [], "stale worker"))
        self.assertTrue(restarted.finish_diagnosis(new_job, self.service.scope, snapshot, [], "manual review"))
        self.assertEqual(restarted.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")

    def test_collection_failure_retries_then_handoffs_without_secret(self):
        iid = self.receive()
        with patch.object(Collector, "collect", side_effect=RuntimeError("secret-provider-token")):
            for delay in (0, 5, 10):
                self.clock.advance(delay)
                self.worker.run_once()
        inc = self.store.get(iid, self.service.scope)
        self.assertEqual(inc["state"], "NEEDS_ATTENTION")
        self.assertNotIn("secret-provider-token", json.dumps(self.store.events(iid, self.service.scope)))
        self.assertIsNone(self.store.claim())

    def test_unknown_evidence_and_dependency_failure_do_not_recommend(self):
        for name, change in (("downstream", {"traces": {"downstream_healthy": False, "failing_span": "db", "sample_count": 5}}),
                             ("migration", {"deployment": {**self.context()["deployment"], "schema_migration": True}})):
            with self.subTest(name=name):
                iid = self.receive(name)
                original = self.context()
                with patch.object(self, "context", return_value={**original, **change}):
                    self.worker.run_once()
                self.assertEqual(self.store.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")

    def test_non_integer_deployment_age_can_produce_candidate(self):
        base = self.context()
        base["deployment"]["changed_at"] -= .125
        with patch.object(self, "context", return_value=base):
            self.recommend()

    def test_deployment_after_alert_does_not_explain_original_failure(self):
        iid = self.receive()
        self.clock.advance(20)
        context = self.context()
        context["deployment"]["changed_at"] = self.clock() - 5
        with patch.object(self, "context", return_value=context):
            self.worker.run_once()
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")

    def test_prometheus_nan_empty_multiseries_stale_and_warnings_rejected(self):
        valid = self.transport("http://local/api/v1/query?query=demo_error_rate&time=" + str(self.clock()))
        malformed = []
        for raw in ("NaN", "Inf", "-1", "1.1"):
            body = copy.deepcopy(valid)
            body["data"]["result"][0]["value"][1] = raw
            malformed.append(body)
        for rows in ([], valid["data"]["result"] * 2):
            body = copy.deepcopy(valid)
            body["data"]["result"] = rows
            malformed.append(body)
        body = copy.deepcopy(valid)
        body["data"]["result"][0]["value"][0] -= 100
        malformed.extend([body, {**valid, "warnings": ["partial response"]}])
        for body in malformed:
            def transport(url, token=None):
                return body if "demo_error_rate" in url else self.transport(url, token)
            with self.subTest(body=body), self.assertRaises(ValueError):
                Collector(self.service, transport).collect(self.clock())

    def test_evidence_scope_provenance_and_freshness_checked(self):
        for update in ({"scope": {**asdict(self.service.scope), "tenant": "other"}}, {"mode": "DEMO"},
                       {"observed_at": self.clock() - 100}, {"observed_at": self.clock() + 100}, {"window_seconds": 60}):
            with self.subTest(update=update), self.assertRaises(EvidenceUnavailable):
                self.factory(self.service).validate(self.metric_values(), {**self.context(), **update}, self.clock())

    def test_stale_underlying_metric_samples_rejected(self):
        with self.assertRaises(EvidenceUnavailable):
            self.factory(self.service).validate({**self.metric_values(), "sample_age_seconds": 100}, self.context(), self.clock())

    def test_review_binds_hash_and_expiry(self):
        iid, inc = self.recommend()
        with self.assertRaises(Rejected):
            self.action(iid, "review", plan_hash="wrong", decision="accept", reason="checked")
        self.clock.advance(301)
        with self.assertRaises(Rejected):
            self.action(iid, "review", plan_hash=inc["recommendation"]["hash"], decision="accept", reason="checked")
        self.action(iid, "refresh", reason="expired")
        self.worker.run_once()
        new = self.store.get(iid, self.service.scope)
        self.assertNotEqual(new["recommendation"]["hash"], inc["recommendation"]["hash"])

    def test_review_acceptance_is_recorded_without_execution(self):
        iid, inc = self.recommend()
        reviewed = self.action(iid, "review", plan_hash=inc["recommendation"]["hash"], decision="accept", reason="checked")
        self.assertEqual(reviewed["state"], "RECOMMENDED")
        self.assertNotIn("execution", reviewed)
        with self.assertRaises(Rejected):
            self.store.execute(iid, self.service.scope, Principal("executor", "executor", self.service.scope), inc["recommendation"]["hash"])

    def test_operator_request_idempotency_rejects_changed_payload(self):
        iid = self.receive()
        first = self.action(iid, "ack")
        self.assertEqual(self.action(iid, "ack"), first)
        with self.assertRaises(Rejected):
            self.action(iid, "ack", extra="changed")

    def test_refresh_preserves_rejected_review_in_metrics(self):
        iid, inc = self.recommend()
        self.action(iid, "review", plan_hash=inc["recommendation"]["hash"], decision="reject", reason="Need more evidence")
        self.action(iid, "refresh", reason="Recheck evidence")
        self.assertEqual(self.store.metrics(self.service.scope)["reviews"]["rejected"], 1)

    def test_handoff_is_not_counted_as_actionable_recommendation(self):
        iid = self.receive()
        context = self.context()
        context["deployment"]["schema_migration"] = True
        with patch.object(self, "context", return_value=context):
            self.worker.run_once()
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")
        self.assertEqual(self.store.metrics(self.service.scope)["phases"]["time_to_recommendation"]["n"], 0)

    def test_expired_evidence_after_slow_reasoning_is_recollected(self):
        iid = self.receive()
        from mttr_guard.agents import RuleReasoner
        clock = self.clock
        class SlowReasoner(RuleReasoner):
            async def analyze(self, role, evidence, prior):
                result = await super().analyze(role, evidence, prior)
                if role == "critic":
                    clock.advance(31)
                return result
        Worker(self.store, self.config, self.factory, SlowReasoner()).run_once()
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "QUEUED")

    def test_another_scope_cannot_read_or_operate(self):
        iid = self.receive()
        with self.assertRaises(Rejected):
            self.store.get(iid, Scope("other", "staging", "local", "trade", "checkout"))

    def test_three_nonoverlapping_windows_verify_recovery_after_restart(self):
        iid = self.observing()
        restarted = PilotStore(self.store.path, self.clock)
        worker = Worker(restarted, self.config, collector_factory=self.factory)
        for n in range(3):
            self.clock.advance(10)
            worker.run_once()
            self.assertEqual(restarted.get(iid, self.service.scope)["state"], "RECOVERED" if n == 2 else "OBSERVING")
        metrics = restarted.metrics(self.service.scope)
        self.assertEqual(metrics["ttr_completed_only"]["mean_seconds"], 30)
        self.assertEqual(metrics["recovered"], 1)

    def test_zero_traffic_and_version_mismatch_cannot_recover(self):
        iid = self.observing()
        for _ in range(3):
            self.clock.advance(10)
            values = {**self.metric_values(), "qps": 0}
            with patch.object(self, "metric_values", return_value=values):
                self.worker.run_once()
        inc = self.store.get(iid, self.service.scope)
        self.assertEqual(inc["healthy_streak"], 0)
        self.assertIn("流量不足", inc["observations"][-1]["reasons"])
        self.clock.advance(10)
        context = self.context()
        context["deployment"]["revision"] = "r43"
        with patch.object(self, "context", return_value=context):
            self.worker.run_once()
        self.assertIn("目标版本未生效", self.store.get(iid, self.service.scope)["observations"][-1]["reasons"])

    def test_failed_observation_resets_streak_and_times_out(self):
        iid = self.observing()
        self.clock.advance(10)
        self.worker.run_once()
        self.clock.advance(10)
        with patch.object(Collector, "collect", side_effect=TimeoutError):
            self.worker.run_once()
        self.assertEqual(self.store.get(iid, self.service.scope)["healthy_streak"], 0)
        self.clock.advance(1800)
        self.worker.run_once()
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")

    def test_large_observation_gap_breaks_streak(self):
        iid = self.observing()
        for delay in (10, 30, 10):
            self.clock.advance(delay)
            self.worker.run_once()
        inc = self.store.get(iid, self.service.scope)
        self.assertEqual(inc["state"], "OBSERVING")
        self.assertEqual(inc["healthy_streak"], 1)

    def test_config_drift_does_not_silently_change_query_policy(self):
        iid = self.receive()
        self.service.queries["qps"] = "different_scope"
        for delay in (0, 5, 10):
            self.clock.advance(delay)
            self.worker.run_once()
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")

    def test_metrics_keep_unresolved_and_exclude_demo(self):
        self.receive()
        metrics = self.store.metrics(self.service.scope)
        self.assertEqual(metrics["unresolved"], 1)
        self.assertIsNone(metrics["ttr_completed_only"]["mean_seconds"])
        from dataclasses import replace
        demo = replace(self.service, mode="DEMO")
        self.store.receive(demo, self.payload("demo"))
        metrics = self.store.metrics(self.service.scope)
        self.assertEqual(metrics["excluded_demo_incidents"], 1)
        self.assertEqual(metrics["incidents"], 1)


class HTTPTests(PilotFixture):
    def start_http(self):
        server = make_server(self.store, self.config, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def request(self, base, path, token=None, body=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_http_authorization_receiver_and_viewer_separation(self):
        base = self.start_http()
        self.assertEqual(self.request(base, "/api/incidents")[0], 401)
        self.assertEqual(self.request(base, "/api/incidents", RECEIVER)[0], 403)
        self.assertEqual(self.request(base, "/v1/webhooks/alertmanager", OPERATOR, self.payload())[0], 403)
        status, receipt = self.request(base, "/v1/webhooks/alertmanager", RECEIVER, self.payload())
        self.assertEqual(status, 202)
        self.assertTrue(receipt["durable"])
        iid = receipt["incident_ids"][0]
        self.assertEqual(self.request(base, f"/api/incidents/{iid}", VIEWER)[0], 200)
        self.assertEqual(self.request(base, f"/api/incidents/{iid}/ack", VIEWER, {"request_id": "test"})[0], 403)
        self.assertEqual(self.request(base, f"/api/incidents/{iid}/ack", OPERATOR, {"request_id": "test"})[0], 200)

    def test_http_rejects_wrong_scope_and_nonobject_json(self):
        base = self.start_http()
        payload = self.payload()
        payload["alerts"][0]["labels"]["tenant"] = "other"
        self.assertEqual(self.request(base, "/v1/webhooks/alertmanager", RECEIVER, payload)[0], 409)
        self.assertEqual(self.request(base, "/v1/webhooks/alertmanager", RECEIVER, [1])[0], 400)

    def test_http_health_reports_worker_readiness(self):
        base = self.start_http()
        self.assertEqual(self.request(base, "/healthz")[0], 200)
        self.assertEqual(self.request(base, "/readyz")[0], 503)
        with self.store.tx() as db:
            db.execute("INSERT INTO config VALUES ('pilot_worker_heartbeat',?)", (str(self.clock()),))
        self.assertEqual(self.request(base, "/readyz")[0], 200)

    def test_real_http_collector_against_demo_evidence(self):
        server = evidence_server(0, "c" * 40)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = Config(config_data(f"http://127.0.0.1:{server.server_port}"))
        snapshot = Collector(config.services["checkout"]).collect(time.time())
        self.assertEqual(snapshot["mode"], "DEMO")
        self.assertEqual(snapshot["deployment"]["revision"], "r42")


if __name__ == "__main__":
    unittest.main()
