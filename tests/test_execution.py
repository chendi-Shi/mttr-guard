import asyncio
import copy
import subprocess
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mttr_guard.__main__ import DemoClock
from mttr_guard.agents import RuleReasoner
from mttr_guard.core import Engine, Principal, Rejected, RUNBOOK, Scope, canonical
from mttr_guard.deployment_simulator import FaultInjectingAdapter, SQLiteDeploymentAdapter


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.root = (Path.cwd() / "artifacts" / "tests").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.local = self.root / (uuid.uuid4().hex + ".db")
        self.remote = self.root / (uuid.uuid4().hex + ".db")
        self.clock = DemoClock()
        self.engine = Engine(self.local, self.clock)
        self.adapter = SQLiteDeploymentAdapter(self.remote, self.clock)
        self.scope = Scope("commerce", "staging", "cn-east-1", "trade", "checkout")
        self.approver = Principal("oncall", "approver", self.scope)
        self.executor = Principal("runner", "executor", self.scope)
        self.engine.seed_simulator(self.scope)
        self.adapter.seed(self.scope)
        self.iid, self.plan_hash = self.approved("first")

    def tearDown(self):
        for path in (self.local, self.remote):
            assert path.resolve().is_relative_to(self.root)
            path.unlink(missing_ok=True)

    def approved(self, fingerprint):
        iid = self.engine.ingest(self.scope, fingerprint, self.clock())
        inc = asyncio.run(self.engine.diagnose(iid, self.scope))
        self.engine.approve(iid, self.scope, self.approver, inc["plan"]["hash"])
        return iid, inc["plan"]["hash"]

    def dispatch(self, adapter=None, iid=None, plan_hash=None):
        return self.engine.dispatch(iid or self.iid, self.scope, self.executor, plan_hash or self.plan_hash, adapter or self.adapter)

    def reconcile(self, adapter=None):
        return self.engine.reconcile(self.iid, self.scope, self.executor, adapter or self.adapter)

    def locked(self):
        with self.engine.tx() as db:
            return db.execute("SELECT COUNT(*) FROM service_locks WHERE scope=?", (self.scope.key,)).fetchone()[0] == 1

    def crash_worker(self, phase):
        # Real process death: no Python finally/rollback after the committed intent.
        program = """
import json, os, sys
from mttr_guard.__main__ import DemoClock
from mttr_guard.core import Engine, Principal, Scope
from mttr_guard.deployment_simulator import SQLiteDeploymentAdapter
local, remote, scope_json, iid, plan_hash, phase = sys.argv[1:]
clock = DemoClock()
scope = Scope(**json.loads(scope_json))
class CrashAdapter(SQLiteDeploymentAdapter):
    def submit(self, operation_id, request):
        if phase == 'after':
            super().submit(operation_id, request)
        os._exit(73)
engine = Engine(local, clock)
engine.dispatch(iid, scope, Principal('runner', 'executor', scope), plan_hash, CrashAdapter(remote, clock))
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.local), str(self.remote),
                                 self.scope.key, self.iid, self.plan_hash, phase], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))

    def test_remote_success_does_not_fake_telemetry(self):
        inc = self.dispatch()
        self.assertEqual(inc["state"], "VERIFYING")
        self.assertEqual(self.adapter.inspect(self.scope)["revision"], "r41")
        with self.engine.tx() as db:
            self.assertEqual(self.engine._service(db, self.scope)["revision"], "r42")
        for _ in range(3):
            self.clock.advance(30)
            self.engine.simulate_observation(self.scope, True)
            inc = self.engine.verify(self.iid, self.scope)
        self.assertEqual(inc["state"], "VERIFYING")
        self.assertTrue(self.locked())
        for _ in range(3):
            self.clock.advance(30)
            self.engine.simulate_observation(self.scope, True, revision="r41")
            inc = self.engine.verify(self.iid, self.scope)
        self.assertEqual(inc["state"], "RECOVERED")
        self.assertFalse(self.locked())

    def test_lost_reply_is_reconciled_once_after_restart(self):
        inc = self.dispatch(FaultInjectingAdapter(self.adapter, "after_submit"))
        self.assertEqual(inc["state"], "EXECUTION_UNKNOWN")
        self.assertTrue(self.locked())
        self.assertEqual(self.adapter.inspect(self.scope)["executions"], 1)
        self.engine = Engine(self.local, self.clock)
        self.adapter = SQLiteDeploymentAdapter(self.remote, self.clock)
        inc = self.reconcile()
        self.assertEqual(inc["state"], "VERIFYING")
        for _ in range(3):
            self.dispatch()
            self.reconcile()
        self.assertEqual(self.adapter.inspect(self.scope)["executions"], 1)

    def test_not_found_is_not_permission_to_resubmit(self):
        self.dispatch(FaultInjectingAdapter(self.adapter, "before_submit"))
        self.assertEqual(self.reconcile()["state"], "EXECUTION_UNKNOWN")
        self.dispatch()
        self.assertEqual(self.adapter.inspect(self.scope)["executions"], 0)
        self.assertTrue(self.locked())

    def test_process_crash_after_remote_commit_recovers_via_lookup(self):
        self.crash_worker("after")
        self.assertEqual(self.engine.get(self.iid, self.scope)["state"], "EXECUTING")
        self.engine = Engine(self.local, self.clock)
        self.assertEqual(self.reconcile()["state"], "VERIFYING")
        self.assertEqual(self.adapter.inspect(self.scope)["executions"], 1)

    def test_process_crash_before_send_keeps_lock(self):
        self.crash_worker("before")
        self.assertEqual(self.reconcile()["state"], "EXECUTION_UNKNOWN")
        self.assertTrue(self.locked())
        self.assertEqual(self.adapter.inspect(self.scope)["executions"], 0)

    def test_pending_response_cannot_be_marked_success(self):
        def pending(operation, request):
            from mttr_guard.core import digest
            return {"operation_id": operation, "request_digest": digest(request), "mode": "SIMULATED", "status": "PENDING"}
        with patch.object(self.adapter, "submit", side_effect=pending):
            inc = self.dispatch()
        self.assertEqual(inc["state"], "EXECUTING")
        self.assertIsNone(inc["receipt"])
        self.assertTrue(self.locked())

    def test_other_incident_cannot_act_during_unknown_outcome(self):
        other, plan_hash = self.approved("second")
        self.dispatch(FaultInjectingAdapter(self.adapter, "after_submit"))
        with self.assertRaises(Rejected):
            self.dispatch(iid=other, plan_hash=plan_hash)
        with self.assertRaises(Rejected):
            self.engine.execute(other, self.scope, self.executor, plan_hash)

    def test_same_service_concurrent_admission_allows_one_incident(self):
        other, plan_hash = self.approved("second")
        def attempt(pair):
            try:
                return self.dispatch(iid=pair[0], plan_hash=pair[1])["state"]
            except Rejected:
                return "BLOCKED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(attempt, [(self.iid, self.plan_hash), (other, plan_hash)]))
        self.assertEqual(sorted(states), ["BLOCKED", "VERIFYING"])
        self.assertEqual(self.adapter.inspect(self.scope)["executions"], 1)

    def test_concurrent_retries_do_not_resubmit(self):
        with patch.object(self.adapter, "submit", wraps=self.adapter.submit) as submit:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda _: self.dispatch(), range(12)))
        self.assertEqual(submit.call_count, 1)

    def test_adapter_checks_revision_atomically(self):
        with self.adapter.tx() as db:
            db.execute("UPDATE deployments SET revision='r43' WHERE scope=?", (self.scope.key,))
        inc = self.dispatch()
        self.assertEqual(inc["state"], "ESCALATED")
        self.assertEqual(inc["execution"]["status"], "REJECTED")
        self.assertEqual(self.adapter.inspect(self.scope)["executions"], 0)
        self.assertFalse(self.locked())

    def test_remote_idempotency_and_payload_conflict(self):
        execution = self.dispatch()["execution"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.adapter.submit(execution["operation_id"], execution["request"]), range(12)))
        changed = copy.deepcopy(execution["request"])
        changed["target_revision"] = "r40"
        with self.assertRaises(ValueError):
            self.adapter.submit(execution["operation_id"], changed)
        self.assertEqual(self.adapter.inspect(self.scope)["executions"], 1)

    def test_request_deadline_enforced_by_adapter(self):
        original = self.adapter.submit
        def delayed(operation, request):
            self.clock.advance(301)
            return original(operation, request)
        with patch.object(self.adapter, "submit", side_effect=delayed):
            inc = self.dispatch()
        self.assertEqual(inc["execution"]["status"], "REJECTED")
        self.assertEqual(self.adapter.inspect(self.scope)["executions"], 0)

    def test_lookup_still_works_after_expiry_and_kill_switch(self):
        self.dispatch(FaultInjectingAdapter(self.adapter, "after_submit"))
        self.clock.advance(301)
        self.engine.set_kill_switch(True)
        self.assertEqual(self.reconcile()["state"], "VERIFYING")

    def test_malformed_receipt_retains_unknown_and_lock(self):
        original = self.adapter.submit
        def malformed(operation, request):
            result = original(operation, request)
            result["request_digest"] = "wrong-request"
            return result
        with patch.object(self.adapter, "submit", side_effect=malformed):
            inc = self.dispatch()
        self.assertEqual(inc["state"], "EXECUTION_UNKNOWN")
        self.assertTrue(self.locked())
        self.assertEqual(self.reconcile()["state"], "VERIFYING")

    def test_lookup_error_does_not_leak_secrets(self):
        self.dispatch(FaultInjectingAdapter(self.adapter, "after_submit"))
        with patch.object(self.adapter, "lookup", side_effect=TimeoutError("secret credential")):
            inc = self.reconcile()
        self.assertEqual(inc["state"], "EXECUTION_UNKNOWN")
        self.assertNotIn("secret", canonical(inc))
        self.assertTrue(self.locked())

    def test_late_unknown_cannot_regress_success(self):
        from mttr_guard.execution import _record
        inc = self.dispatch()
        result = _record(self.engine, self.iid, self.scope, inc["execution"]["operation_id"], None, "TimeoutError")
        self.assertEqual(result["state"], "VERIFYING")

    def test_different_adapter_and_live_mode_are_rejected(self):
        self.dispatch(FaultInjectingAdapter(self.adapter, "after_submit"))
        with patch.object(self.adapter, "adapter_id", "different-backend"):
            with self.assertRaises(Rejected):
                self.reconcile()
        with patch.object(self.adapter, "mode", "LIVE"):
            with self.assertRaises(Rejected):
                self.reconcile()

    def test_revocation_blocks_both_execution_paths(self):
        inc = self.engine.revoke_approval(self.iid, self.scope, self.approver, self.plan_hash, "Owner withdrew approval")
        self.assertEqual(inc["state"], "CANCELLED")
        with self.assertRaises(Rejected):
            self.dispatch()
        with self.assertRaises(Rejected):
            self.engine.execute(self.iid, self.scope, self.executor, self.plan_hash)

    def test_revocation_after_admission_does_not_pretend_to_cancel(self):
        self.dispatch(FaultInjectingAdapter(self.adapter, "after_submit"))
        with self.assertRaises(Rejected):
            self.engine.revoke_approval(self.iid, self.scope, self.approver, self.plan_hash, "Too late")
        self.assertEqual(self.reconcile()["state"], "VERIFYING")

    def test_recovery_keeps_the_approved_policy_after_upgrade(self):
        self.dispatch()
        with patch.dict(RUNBOOK, {"verification": {"max_error_rate": 0}}):
            for _ in range(3):
                self.clock.advance(30)
                self.engine.simulate_observation(self.scope, True, revision="r41")
                inc = self.engine.verify(self.iid, self.scope)
        self.assertEqual(inc["state"], "RECOVERED")

    def test_reasoner_cannot_mutate_shared_evidence(self):
        class Mutating(RuleReasoner):
            async def analyze(self, role, evidence, prior):
                for e in evidence:
                    e["observed_at"] = 0
                return await super().analyze(role, evidence, prior)
        iid = self.engine.ingest(self.scope, "mutation", self.clock())
        inc = asyncio.run(self.engine.diagnose(iid, self.scope, Mutating()))
        self.assertEqual(inc["state"], "AWAITING_APPROVAL")
        self.assertTrue(all(e["observed_at"] == self.clock() for e in inc["evidence"]))


if __name__ == "__main__":
    unittest.main()
