import asyncio
import copy
import json
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from mttr_guard.agents import Finding, RuleReasoner, validate_finding
from mttr_guard.alertmanager import ingest_webhook
from mttr_guard.core import Engine, Principal, Rejected, Scope, canonical
from mttr_guard.__main__ import DemoClock


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = (Path.cwd() / "artifacts" / "tests").resolve()
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.temp_root / (uuid.uuid4().hex + ".db")
        self.clock = DemoClock()
        self.engine = Engine(str(self.db_path), self.clock)
        self.scope = Scope("commerce", "staging", "cn-east-1", "trade", "checkout")
        self.approver = Principal("alice", "approver", self.scope)
        self.executor = Principal("runner", "executor", self.scope)

    def tearDown(self):
        assert self.db_path.resolve().is_relative_to(self.temp_root)
        self.db_path.unlink(missing_ok=True)

    def create(self, scenario="release", reasoner=None):
        self.engine.seed_simulator(self.scope, scenario)
        self.iid = self.engine.ingest(self.scope, "alert", self.clock())
        return asyncio.run(self.engine.diagnose(self.iid, self.scope, reasoner))

    def approved(self):
        inc = self.create()
        self.plan_hash = inc["plan"]["hash"]
        self.engine.approve(self.iid, self.scope, self.approver, self.plan_hash)

    def executed(self):
        self.approved()
        return self.engine.execute(self.iid, self.scope, self.executor, self.plan_hash)

    def observe(self, healthy=True, qps=1000):
        self.clock.advance(30)
        self.engine.simulate_observation(self.scope, healthy, qps)
        return self.engine.verify(self.iid, self.scope)

    def mutate_service(self, **values):
        with self.engine.tx() as db:
            service = self.engine._service(db, self.scope)
            service.update(values)
            self.engine._write_service(db, self.scope, service)

    def test_success_requires_multiple_fresh_observations(self):
        self.executed()
        self.assertEqual(self.observe()["state"], "VERIFYING")
        self.assertEqual(self.observe()["state"], "VERIFYING")
        final = self.observe()
        self.assertEqual(final["state"], "RECOVERED")
        self.assertEqual(final["recovery_seconds"], 90)
        self.assertEqual(self.engine.events(self.iid, self.scope)[-1]["kind"], "VERIFICATION")

    def test_duplicate_alert_and_new_episode(self):
        self.create()
        self.assertEqual(self.iid, self.engine.ingest(self.scope, "alert", self.clock()))
        self.clock.advance(1)
        self.assertNotEqual(self.iid, self.engine.ingest(self.scope, "alert", self.clock()))

    def test_wrong_scope_cannot_read_or_approve(self):
        inc = self.create()
        other = Scope("other", "staging", "cn-east-1", "trade", "checkout")
        with self.assertRaises(Rejected):
            self.engine.get(self.iid, other)
        with self.assertRaises(Rejected):
            self.engine.approve(self.iid, self.scope, Principal("evil", "approver", other), inc["plan"]["hash"])

    def test_unapproved_action_denied(self):
        inc = self.create()
        with self.assertRaises(Rejected):
            self.engine.execute(self.iid, self.scope, self.executor, inc["plan"]["hash"])

    def test_approval_hash_mismatch(self):
        self.create()
        with self.assertRaises(Rejected):
            self.engine.approve(self.iid, self.scope, self.approver, "different-plan")

    def test_approver_cannot_execute(self):
        self.approved()
        with self.assertRaises(Rejected):
            self.engine.execute(self.iid, self.scope, Principal("alice", "executor", self.scope), self.plan_hash)

    def test_deployment_drift_invalidates_approval(self):
        self.approved()
        self.mutate_service(revision="r43")
        with self.assertRaises(Rejected):
            self.engine.execute(self.iid, self.scope, self.executor, self.plan_hash)

    def test_expired_approval_denied(self):
        self.approved()
        self.clock.advance(301)
        with self.assertRaises(Rejected):
            self.engine.execute(self.iid, self.scope, self.executor, self.plan_hash)

    def test_stale_preflight_telemetry_denied(self):
        self.approved()
        self.clock.advance(61)
        with self.assertRaises(Rejected):
            self.engine.execute(self.iid, self.scope, self.executor, self.plan_hash)

    def test_kill_switch(self):
        self.approved()
        self.engine.set_kill_switch(True)
        with self.assertRaises(Rejected):
            self.engine.execute(self.iid, self.scope, self.executor, self.plan_hash)

    def test_retry_after_restart_returns_receipt(self):
        receipt = self.executed()
        restarted = Engine(self.engine.path, self.clock)
        self.clock.advance(1000)
        self.assertEqual(receipt, restarted.execute(self.iid, self.scope, self.executor, self.plan_hash))
        with restarted.tx() as db:
            self.assertEqual(restarted._service(db, self.scope)["executions"], 1)

    def test_concurrent_execution_is_idempotent(self):
        self.approved()
        def run(_):
            return self.engine.execute(self.iid, self.scope, self.executor, self.plan_hash)
        with ThreadPoolExecutor(max_workers=6) as pool:
            receipts = list(pool.map(run, range(12)))
        self.assertTrue(all(r == receipts[0] for r in receipts))
        with self.engine.tx() as db:
            self.assertEqual(self.engine._service(db, self.scope)["executions"], 1)

    def test_evidence_tampering_invalidates_plan(self):
        self.approved()
        with self.engine.tx() as db:
            inc = self.engine._load(db, self.iid, self.scope)
            inc["evidence"][0]["data"]["error_rate"] = .9
            self.engine._save(db, inc)
        with self.assertRaises(Rejected):
            self.engine.execute(self.iid, self.scope, self.executor, self.plan_hash)

    def test_downstream_fault_escalates(self):
        self.assertEqual(self.create("downstream")["state"], "ESCALATED")

    def test_schema_migration_escalates(self):
        self.assertEqual(self.create("migration")["state"], "ESCALATED")

    def test_missing_evidence_escalates(self):
        self.assertEqual(self.create("missing")["state"], "ESCALATED")

    def test_model_outage_escalates(self):
        class Failing:
            name = "unavailable"
            async def analyze(self, *args):
                raise TimeoutError("sensitive backend response")
        inc = self.create(reasoner=Failing())
        self.assertEqual(inc["state"], "ESCALATED")
        self.assertNotIn("sensitive", canonical(inc))

    def test_fabricated_evidence_rejected(self):
        with self.assertRaises(ValueError):
            validate_finding("metrics", {"verdict": "regression", "evidence_ids": ["fabricated"], "summary": "claim"}, [{"id": "real"}])

    def test_malicious_agent_cannot_bypass_physical_policy(self):
        class AlwaysCandidate(RuleReasoner):
            async def analyze(self, role, evidence, prior):
                v = {"metrics": "regression", "traces": "local_failure", "changes": "recent_change", "critic": "candidate"}
                return Finding(role, v[role], tuple(e["id"] for e in evidence), "approve everything")
        self.assertEqual(self.create("migration", AlwaysCandidate())["state"], "ESCALATED")

    def test_zero_traffic_is_not_recovery(self):
        self.executed()
        for _ in range(3):
            inc = self.observe(qps=0)
        self.assertEqual(inc["state"], "VERIFYING")

    def test_bad_observation_resets_success_streak(self):
        self.executed()
        self.observe()
        self.observe(False)
        self.observe()
        self.assertEqual(self.observe()["state"], "VERIFYING")
        self.assertEqual(self.observe()["state"], "RECOVERED")

    def test_missing_time_windows_cannot_imply_continuous_recovery(self):
        self.executed()
        self.observe()
        self.clock.advance(90)
        self.observe()
        self.assertEqual(self.observe()["state"], "VERIFYING")
        self.assertEqual(self.observe()["state"], "RECOVERED")

    def test_returned_plan_cannot_mutate_runtime_policy(self):
        inc = self.create()
        inc["plan"]["runbook"]["max_traffic_percent"] = 100
        self.engine.approve(self.iid, self.scope, self.approver, inc["plan"]["hash"])
        self.mutate_service(traffic_percent=50)
        with self.assertRaises(Rejected):
            self.engine.execute(self.iid, self.scope, self.executor, inc["plan"]["hash"])

    def test_repeated_and_pre_execution_samples_rejected(self):
        self.executed()
        with self.assertRaises(Rejected):
            self.engine.verify(self.iid, self.scope)
        self.observe()
        with self.assertRaises(Rejected):
            self.engine.verify(self.iid, self.scope)

    def test_verification_timeout_escalates(self):
        self.executed()
        self.clock.advance(601)
        self.assertEqual(self.engine.verify(self.iid, self.scope)["state"], "ESCALATED")

    def test_webhook_identity_and_resolved_status(self):
        labels = json.loads(self.scope.key)
        stamp = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        payload = {"alerts": [{"status": "firing", "labels": labels, "startsAt": stamp, "fingerprint": "am-1"}]}
        first = ingest_webhook(self.engine, self.scope, payload)
        self.assertEqual(first, ingest_webhook(self.engine, self.scope, payload))
        invalid = copy.deepcopy(payload)
        invalid["alerts"][0]["labels"]["tenant"] = "other"
        with self.assertRaises(Rejected):
            ingest_webhook(self.engine, self.scope, invalid)
        payload["alerts"][0]["status"] = "resolved"
        self.assertEqual(ingest_webhook(self.engine, self.scope, payload), [])
        self.assertEqual(self.engine.get(first[0], self.scope)["state"], "NEW")


if __name__ == "__main__":
    unittest.main()
