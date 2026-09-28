import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_pilot import PilotFixture
from mttr_guard import audit
from mttr_guard.core import canonical, Rejected
from mttr_guard.job_runner import bounded_process, JobDeadlineExceeded
from mttr_guard.operations import backup, inspect_database, restore
from mttr_guard.pilot_store import PilotStore


KEY = b"a-dedicated-high-entropy-test-key-1234567890"


class OperationalTests(PilotFixture):
    def sealed(self):
        self.store = PilotStore(Path(self.temp.name) / "sealed.db", self.clock, audit_key=KEY)
        from mttr_guard.pilot_worker import Worker
        self.worker = Worker(self.store, self.config, collector_factory=self.factory)

    def test_audit_covers_workflow_and_final_state(self):
        self.sealed()
        iid, _ = self.recommend()
        self.action(iid, "ack")
        result = inspect_database(self.store.path, KEY)
        self.assertTrue(result["keyed"])
        self.assertEqual(result["verified_states"], 1)
        self.assertGreater(result["records"], 5)

    def test_audit_detects_incident_or_event_tampering(self):
        for target in ("incident", "event", "signature", "scope", "extra_event", "extra_incident"):
            with self.subTest(target=target):
                path = Path(self.temp.name) / (target + ".db")
                store = PilotStore(path, self.clock, audit_key=KEY)
                iid = store.receive(self.service, self.payload(target))[0]
                with store.tx() as db:
                    if target == "incident":
                        inc = store._load(db, iid, self.service.scope)
                        inc["summary"] = "forged"
                        db.execute("UPDATE incidents SET body=? WHERE id=?", (canonical(inc), iid))
                    elif target == "event":
                        db.execute("UPDATE events SET kind='FORGED'")
                    elif target == "scope":
                        db.execute("UPDATE incidents SET scope='forged'")
                    elif target == "extra_event":
                        db.execute("INSERT INTO events(incident,at,kind,body) VALUES (?,0,'FORGED','{}')", (iid,))
                    elif target == "extra_incident":
                        db.execute("INSERT INTO incidents SELECT 'extra',scope,'extra',state,body FROM incidents WHERE id=?", (iid,))
                    else:
                        db.execute("UPDATE audit_records SET signature=? WHERE seq=1", ("0" * 64,))
                with self.assertRaises(ValueError):
                    inspect_database(path, KEY)

    def test_external_checkpoint_detects_whole_database_rollback(self):
        self.sealed()
        self.receive()
        saved = Path(self.temp.name) / "backup.db"
        backup(self.store.path, saved, KEY)
        self.store.access_audit("alice", None, "GET /api/metrics", 200, "r1")
        anchor = inspect_database(self.store.path, KEY)["head"]
        with self.assertRaises(ValueError):
            inspect_database(saved, KEY, anchor)

    def test_wrong_audit_key_is_rejected_on_startup(self):
        self.sealed()
        with self.assertRaises(ValueError):
            PilotStore(self.store.path, self.clock, audit_key=b"different-key")

    def test_keyed_audit_refuses_unverified_legacy_history(self):
        self.receive()
        # Construct a legacy fixture outside the guarded application transaction;
        # runtime writes must retain their audit head through COMMIT.
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("DELETE FROM config WHERE key IN ('audit_key_id','audit_head')")
            db.execute("DROP TABLE audit_records")
        with self.assertRaisesRegex(ValueError, "legacy history"):
            PilotStore(self.store.path, self.clock, audit_key=KEY)

    def test_backup_restore_preserves_state_and_releases_read_job_lease(self):
        self.sealed()
        iid = self.receive()
        self.store.claim()
        self.store.heartbeat("old-worker", "all", "RUNNING")
        saved, restored = Path(self.temp.name) / "saved.db", Path(self.temp.name) / "restored.db"
        result = backup(self.store.path, saved, KEY)
        self.assertEqual(result["pending_jobs"], 1)
        restore(saved, restored, KEY)
        replacement = PilotStore(restored, self.clock, audit_key=KEY)
        self.assertEqual(replacement.get(iid, self.service.scope)["state"], "QUEUED")
        self.assertIsNotNone(replacement.claim())
        self.assertFalse(replacement.operational()["ready"])

    def test_backup_and_restore_never_overwrite_existing_files(self):
        self.sealed()
        saved = Path(self.temp.name) / "saved.db"
        backup(self.store.path, saved, KEY)
        before = saved.read_bytes()
        with self.assertRaises(ValueError):
            backup(self.store.path, saved, KEY)
        with self.assertRaises(ValueError):
            restore(saved, self.store.path, KEY)
        self.assertEqual(saved.read_bytes(), before)

    def test_backup_corruption_fails_before_restore_target_creation(self):
        self.sealed()
        saved, target = Path(self.temp.name) / "saved.db", Path(self.temp.name) / "new.db"
        backup(self.store.path, saved, KEY)
        with saved.open("ab") as output:
            output.write(b"tampered")
        with self.assertRaises(ValueError):
            restore(saved, target, KEY)
        self.assertFalse(target.exists())

    def test_worker_readiness_requires_both_queues_and_expires(self):
        self.store.heartbeat("one", "diagnose", "RUNNING")
        self.assertFalse(self.store.operational()["ready"])
        self.store.heartbeat("two", "observe", "RUNNING")
        self.assertTrue(self.store.operational()["ready"])
        self.clock.advance(151)
        self.assertFalse(self.store.operational()["ready"])

    def test_observation_priority_and_dedicated_queue(self):
        iid = self.observing()
        diagnostic = self.receive("new-diagnostic")
        self.clock.advance(10)
        claimed, _ = self.store.claim()
        self.assertEqual(claimed["incident"], iid)
        diag, _ = self.store.claim(queue="diagnose")
        self.assertEqual(diag["incident"], diagnostic)
        self.assertIsNone(self.store.claim(queue="observe"))

    def test_pause_fences_inflight_observation(self):
        iid = self.observing()
        self.clock.advance(10)
        job, _ = self.store.claim()
        snapshot = self.factory(self.service).collect(self.clock())
        self.action(iid, "pause", reason="Owner takes over incident")
        self.assertFalse(self.store.observe(job, self.service.scope, snapshot))
        self.assertEqual(self.store.get(iid, self.service.scope)["state"], "NEEDS_ATTENTION")

    def test_handoff_uses_compare_and_swap_assignment(self):
        iid = self.receive()
        self.action(iid, "ack")
        self.action(iid, "handoff", assignee="bob", expected_assignee=self.operator.subject, reason="Shift change")
        with self.assertRaises(Rejected):
            self.store.operate(iid, self.service, self.operator, "handoff", {"request_id": "another", "assignee": "carol",
                "expected_assignee": self.operator.subject, "reason": "Stale screen"})

    def test_total_deadline_terminates_actual_child_process(self):
        marker = Path(self.temp.name) / "finished.txt"
        command = [sys.executable, "-c", "import time,sys; from pathlib import Path; time.sleep(1); Path(sys.argv[1]).write_text('late')", str(marker)]
        with self.assertRaises(JobDeadlineExceeded):
            bounded_process(command, b"", .15)
        time.sleep(1.1)
        self.assertFalse(marker.exists(), "Timed-out work must not keep running after the parent returns")
