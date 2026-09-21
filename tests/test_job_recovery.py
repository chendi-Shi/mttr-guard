"""Bounded recovery from abandoned leases, using durable state and real exits."""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from test_pilot import PilotFixture
from mttr_guard.core import Rejected
from mttr_guard.operations import backup, inspect_database, restore
from mttr_guard.pilot_store import PilotStore


KEY = b"job-recovery-test-key-not-a-secret-0001"


class JobRecoveryTests(PilotFixture):
    def setUp(self):
        super().setUp()
        self.store = PilotStore(Path(self.temp.name) / "keyed.db", self.clock, audit_key=KEY)
        self.worker.store = self.store

    def abandon(self, attempts=3):
        for attempt in range(1, attempts + 1):
            job, _ = self.store.claim()
            self.assertEqual(job["attempts"], attempt)
            self.clock.advance(120)
        return job

    def job(self, iid):
        with self.store.tx() as db:
            return dict(db.execute("SELECT * FROM pilot_jobs WHERE incident=?", (iid,)).fetchone())

    def stopped_events(self, iid):
        return [e for e in self.store.events(iid, self.service.scope) if e["kind"] == "JOB_STOPPED"]

    def test_three_abandoned_leases_stop_without_fourth_collection(self):
        iid = self.receive()
        old = self.abandon()
        self.worker.runner = Mock(side_effect=AssertionError("Must not collect"))
        self.assertFalse(self.worker.run_once())
        self.worker.runner.assert_not_called()
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")
        job = self.job(iid)
        self.assertEqual((job["state"], job["attempts"], job["last_error"]), ("DONE", 3, "DiagnosisAttemptsExhausted"))
        self.assertIsNone(job["lease"])
        self.assertFalse(self.store.finish_diagnosis(old, self.service.scope, {}, [], None))
        self.store.fail_job(old, self.service.scope, "LateFailure")
        self.assertIsNone(self.store.claim())
        self.assertEqual(len(self.stopped_events(iid)), 1)
        self.assertTrue(inspect_database(self.store.path, KEY)["keyed"])

    def test_live_third_lease_is_not_prematurely_stopped(self):
        iid = self.receive()
        self.abandon(2)
        third, _ = self.store.claim()
        self.clock.advance(119)
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "QUEUED")
        snapshot = self.factory(self.service).collect(self.clock())
        self.assertTrue(self.store.finish_diagnosis(third, self.service.scope, snapshot, [], "Human review"))
        self.assertEqual(self.stopped_events(iid), [])

    def test_reported_failure_and_crashes_share_budget(self):
        iid = self.receive()
        job, _ = self.store.claim()
        self.store.fail_job(job, self.service.scope, "TimeoutError")
        self.clock.advance(5)
        second, _ = self.store.claim()
        self.assertEqual(second["attempts"], 2)
        self.clock.advance(120)
        third, _ = self.store.claim()
        self.store.fail_job(third, self.service.scope, "EvidenceUnavailable")
        self.assertEqual(self.job(iid)["state"], "DONE")
        self.assertEqual(len(self.stopped_events(iid)), 1)
        self.assertIsNone(self.store.claim())

    def test_terminal_handoff_does_not_block_following_incident(self):
        first = self.receive()
        self.abandon()
        second = self.receive("another-incident")
        self.assertIsNone(self.store.claim())
        job, inc = self.store.claim()
        self.assertEqual(inc["id"], second)
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(self.job(first)["state"], "DONE")

    def test_concurrent_restart_records_one_terminal_handoff(self):
        iid = self.receive()
        self.abandon()
        other = PilotStore(self.store.path, self.clock, audit_key=KEY)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(lambda store: store.claim(), [self.store, other])), [None, None])
        self.assertEqual(len(self.stopped_events(iid)), 1)

    def test_operator_refresh_resets_budget_once_and_fences_old_lease(self):
        iid = self.receive()
        old = self.abandon()
        self.store.claim()
        body = {"request_id": "retry-after-fix", "reason": "Evidence gateway fixed, ticket 42"}
        with self.assertRaises(Rejected):
            self.store.operate(iid, self.service, replace(self.operator, role="viewer"), "refresh", body)
        with self.assertRaises(Rejected):
            self.store.operate(iid, self.service, self.operator, "refresh", {"request_id": "no-reason"})
        self.store.operate(iid, self.service, self.operator, "refresh", body)
        new, _ = self.store.claim()
        self.store.operate(iid, self.service, self.operator, "refresh", body)
        self.assertEqual(self.job(iid)["attempts"], 1)
        self.assertEqual(self.job(iid)["lease"], new["lease"])
        self.assertFalse(self.store.finish_diagnosis(old, self.service.scope, {}, [], None))
        snapshot = self.factory(self.service).collect(self.clock())
        self.assertTrue(self.store.finish_diagnosis(new, self.service.scope, snapshot, [], "Review evidence"))

    def test_expired_observation_never_calls_collector(self):
        iid = self.observing()
        self.clock.advance(self.service.policy["observation_timeout_seconds"])
        with patch.object(self.worker, "collector_factory") as factory:
            self.worker.run_once()
            factory.assert_not_called()
        inc = self.store.get(iid, self.service.scope)
        self.assertEqual(inc["state"], "NEEDS_ATTENTION")
        self.assertNotIn("recovered_at", inc)
        self.assertEqual(self.job(iid)["last_error"], "ObservationDeadlineExceeded")
        self.assertEqual(self.store.metrics(self.service.scope)["unresolved"], 1)

    def test_crashed_observer_is_fenced_when_deadline_expires(self):
        iid = self.observing()
        self.clock.advance(10)
        old, _ = self.store.claim(queue="observe")
        self.clock.advance(self.service.policy["observation_timeout_seconds"])
        self.assertIsNone(self.store.claim(queue="observe"))
        self.assertFalse(self.store.observe(old, self.service.scope, {}))
        self.assertEqual(self.job(iid)["state"], "DONE")
        self.assertEqual(len(self.stopped_events(iid)), 1)

    def test_observation_finishing_at_deadline_cannot_report_recovery(self):
        iid = self.observing()
        self.clock.advance(self.service.policy["observation_timeout_seconds"] - 1)
        job, _ = self.store.claim(queue="observe")
        self.clock.advance(1)
        # Deadline is checked even before parsing an otherwise invalid result.
        self.assertTrue(self.store.observe(job, self.service.scope, {}))
        inc = self.store.get(iid, self.service.scope)
        self.assertEqual((inc["state"], inc["healthy_streak"], inc["observations"]), ("NEEDS_ATTENTION", 0, []))
        self.assertEqual(len(self.stopped_events(iid)), 1)

    def test_failed_audit_write_rolls_back_entire_handoff(self):
        iid = self.receive()
        self.abandon()
        with patch.object(self.store, "event", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.claim()
        self.assertEqual(self.job(iid)["state"], "RUNNING")
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "QUEUED")
        self.store.claim()
        self.assertEqual(len(self.stopped_events(iid)), 1)
        inspect_database(self.store.path, KEY)

    def test_backup_restore_does_not_reset_consumed_attempt_budget(self):
        iid = self.receive()
        self.abandon()
        root = Path(self.temp.name)
        backup(self.store.path, root / "backup.db", KEY)
        with patch("mttr_guard.operations.time.time", return_value=self.clock()):
            restore(root / "backup.db", root / "restored.db", KEY)
        restored = PilotStore(root / "restored.db", self.clock, audit_key=KEY)
        self.assertIsNone(restored.claim())
        self.assertEqual(restored.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")
        inspect_database(restored.path, KEY)

    def test_abrupt_subprocess_exits_preserve_retry_budget(self):
        iid = self.receive()
        code = (
            "import os,sys; from mttr_guard.pilot_store import PilotStore; "
            "s=PilotStore(sys.argv[1],lambda:float(sys.argv[2]),audit_key=" + repr(KEY) + "); "
            "claimed=s.claim(); os._exit(23 if claimed else 24)"
        )
        for _ in range(3):
            result = subprocess.run([sys.executable, "-c", code, self.store.path, str(self.clock())],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 23, result.stderr.decode(errors="replace"))
            self.clock.advance(120)
        restarted = PilotStore(self.store.path, self.clock, audit_key=KEY)
        self.assertIsNone(restarted.claim())
        self.assertEqual(restarted.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")
        self.assertEqual(len(self.stopped_events(iid)), 1)
        inspect_database(self.store.path, KEY)

    def test_attention_metrics_are_scoped_and_clear_after_manual_refresh(self):
        iid = self.receive()
        self.abandon()
        self.store.claim()
        other_scope = replace(self.service.scope, tenant="other-tenant")
        self.assertEqual(self.store.operational(other_scope)["needs_attention"], 0)
        self.assertEqual(self.store.operational(self.service.scope)["needs_attention"], 1)
        self.assertEqual(self.store.operational()["needs_attention"], 1)
        self.action(iid, "refresh", reason="Gateway fixed, collect new evidence")
        self.assertEqual(self.store.operational(self.service.scope)["needs_attention"], 0)
