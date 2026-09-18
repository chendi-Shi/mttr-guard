"""Corruption is injected through an independent connection, as in the incident."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from test_pilot import PilotFixture
from mttr_guard import audit
from mttr_guard.core import canonical
from mttr_guard.operations import inspect_database
from mttr_guard.pilot_store import PilotStore
from mttr_guard.pilot_worker import Worker


KEY = b"isolated-runtime-integrity-test-key-1234567890"


class RuntimeIntegrityTests(PilotFixture):
    def setUp(self):
        super().setUp()
        self.store = PilotStore(Path(self.temp.name) / "sealed.db", self.clock, audit_key=KEY)
        self.worker = Worker(self.store, self.config, collector_factory=self.factory)

    def dump(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            return list(db.iterdump())

    def forge(self, iid, value="forged incident"):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            body = json.loads(db.execute("SELECT body FROM incidents WHERE id=?", (iid,)).fetchone()[0])
            body["summary"] = value
            db.execute("UPDATE incidents SET body=? WHERE id=?", (canonical(body), iid))

    def test_legitimate_ack_cannot_resign_tampered_state(self):
        iid = self.receive()
        self.forge(iid)
        damaged = self.dump()
        with self.assertRaises(ValueError):
            inspect_database(self.store.path, KEY)
        with self.assertRaises(audit.IntegrityError):
            self.action(iid, "ack")
        self.assertEqual(self.dump(), damaged, "No request, event, state or audit head may advance")
        with self.assertRaises(ValueError):
            inspect_database(self.store.path, KEY)

    def test_each_runtime_entry_refuses_tainted_data(self):
        iid = self.receive()
        actions = {
            "get": lambda s: s.get(iid, self.service.scope),
            "events": lambda s: s.events(iid, self.service.scope),
            "list": lambda s: s.list_incidents(self.service.scope),
            "metrics": lambda s: s.metrics(self.service.scope),
            "operations": lambda s: s.operational(),
            "heartbeat": lambda s: s.heartbeat("worker", "all", "RUNNING"),
            "duplicate_alert": lambda s: s.receive(self.service, self.payload()),
            "new_alert": lambda s: s.receive(self.service, self.payload("new")),
            "claim": lambda s: s.claim(),
            "access_audit": lambda s: s.access_audit("alice", None, "GET /api/metrics", 200, "request"),
        }
        # Independent live instances model API and workers already running when
        # another SQLite connection changes the database. None is pre-quarantined.
        stores = {name: PilotStore(self.store.path, self.clock, audit_key=KEY) for name in actions}
        self.forge(iid)
        damaged = self.dump()
        for name, action in actions.items():
            with self.subTest(entry=name), self.assertRaises(audit.IntegrityError):
                action(stores[name])
        self.assertEqual(self.dump(), damaged)

    def test_corruption_shapes_block_access_audit_and_startup(self):
        corruptions = {
            "event": "UPDATE events SET kind='FORGED'",
            "scope": "UPDATE incidents SET scope='forged'",
            "signature": "UPDATE audit_records SET signature='forged' WHERE seq=1",
            "deleted_record": "DELETE FROM audit_records WHERE seq=1",
            "head": "UPDATE config SET value='{}' WHERE key='audit_head'",
            "null_head": "UPDATE config SET value='null' WHERE key='audit_head'",
            "missing_head": "DELETE FROM config WHERE key='audit_head'",
            "malformed_payload": "UPDATE audit_records SET payload='null' WHERE seq=1",
            "invalid_json": "UPDATE incidents SET body='{'",
        }
        for name, statement in corruptions.items():
            with self.subTest(corruption=name):
                store = PilotStore(Path(self.temp.name) / (name + ".db"), self.clock, audit_key=KEY)
                store.receive(self.service, self.payload())
                with closing(sqlite3.connect(store.path)) as db, db:
                    db.execute(statement)
                with self.assertRaises(audit.IntegrityError):
                    store.access_audit("alice", None, "GET /api/metrics", 200, "request")
                with self.assertRaises(ValueError):
                    PilotStore(store.path, self.clock, audit_key=KEY)

    def test_replayed_older_state_cannot_be_legitimized(self):
        iid = self.receive()
        original = self.store.get(iid, self.service.scope)
        self.action(iid, "ack")
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE incidents SET body=? WHERE id=?", (canonical(original), iid))
        with self.assertRaises(audit.IntegrityError):
            self.action(iid, "ack", request_id="second-ack")

    def test_inflight_diagnosis_and_failure_cannot_overwrite_corruption(self):
        iid = self.receive()
        job, _ = self.store.claim()
        other = PilotStore(self.store.path, self.clock, audit_key=KEY)
        snapshot = self.factory(self.service).collect(self.clock())
        self.forge(iid)
        damaged = self.dump()
        with self.assertRaises(audit.IntegrityError):
            self.store.finish_diagnosis(job, self.service.scope, snapshot, [], "missing evidence")
        with self.assertRaises(audit.IntegrityError):
            other.fail_job(job, self.service.scope, "collection failed")
        self.assertEqual(self.dump(), damaged)

    def test_inflight_observation_cannot_overwrite_corruption(self):
        iid = self.observing()
        self.clock.advance(10)
        job, _ = self.store.claim()
        snapshot = self.factory(self.service).collect(self.clock())
        self.forge(iid)
        damaged = self.dump()
        with self.assertRaises(audit.IntegrityError):
            self.store.observe(job, self.service.scope, snapshot)
        self.assertEqual(self.dump(), damaged)

    def test_quarantine_requires_verified_restart(self):
        iid = self.receive()
        original = self.store.get(iid, self.service.scope)
        self.forge(iid)
        with self.assertRaises(audit.IntegrityError):
            self.store.get(iid, self.service.scope)
        # Restoring bytes alone must not silently re-enable a live instance.
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE incidents SET body=? WHERE id=?", (canonical(original), iid))
        with self.assertRaises(audit.IntegrityError):
            self.store.get(iid, self.service.scope)
        restarted = PilotStore(self.store.path, self.clock, audit_key=KEY)
        self.assertEqual(restarted.get(iid, self.service.scope), original)

    def test_quarantined_worker_waits_for_shutdown_without_retrying(self):
        iid = self.receive()
        self.forge(iid)
        damaged = self.dump()
        stop = Mock()
        stop.is_set.return_value = False
        with patch.object(self.worker, "run_once") as run_once:
            self.worker.run(stop)
            run_once.assert_not_called()
        stop.wait.assert_called_once_with()
        self.assertEqual(self.dump(), damaged)

    def test_concurrent_writer_cannot_change_verified_input(self):
        iid = self.receive()
        verify = audit.verify
        def try_competing_write(db, key, **kwargs):
            result = verify(db, key, **kwargs)
            with closing(sqlite3.connect(self.store.path, timeout=0)) as competing, competing:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    competing.execute("UPDATE incidents SET body='{}' WHERE id=?", (iid,))
            return result
        with patch.object(audit, "verify", side_effect=try_competing_write):
            self.action(iid, "ack")
        self.assertEqual(self.store.get(iid, self.service.scope)["assignment"]["subject"], self.operator.subject)
        inspect_database(self.store.path, KEY)
