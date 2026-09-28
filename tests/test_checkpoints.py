"""Validly signed snapshots must not erase already observed audit history."""
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from test_pilot import PilotFixture
from mttr_guard import audit, checkpoints
from mttr_guard.operations import backup, inspect_database, restore
from mttr_guard.pilot_store import PilotStore
from mttr_guard.pilot_worker import Worker


KEY = b"checkpoint-test-key-not-a-secret-0001"


def copy_database(source, target):
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


class CheckpointTests(PilotFixture):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.store = PilotStore(self.root / "sealed.db", self.clock, audit_key=KEY)
        self.worker.store = self.store
        self.iid = self.receive()
        self.anchor = self.root / "trusted.json"
        self.head = checkpoints.export(self.store.path, self.anchor, KEY)
        self.old = self.root / "old.db"
        copy_database(self.store.path, self.old)

    def guarded(self, cache=16 * 1024 * 1024):
        return PilotStore(self.store.path, self.clock, audit_key=KEY, audit_checkpoint=self.anchor, audit_cache_bytes=cache)

    def publish(self, head):
        temporary = self.root / "publish.json"
        temporary.write_text(json.dumps(head), encoding="utf-8")
        os.replace(temporary, self.anchor)

    def test_committed_write_pins_history_without_waiting_for_next_read(self):
        self.action(self.iid, "ack")
        copy_database(self.old, self.store.path)
        # Old snapshot is cryptographically valid when no external history is supplied.
        self.assertEqual(inspect_database(self.store.path, KEY)["head"], self.head)
        with self.assertRaises(audit.IntegrityError):
            self.store.get(self.iid, self.service.scope)
        self.assertEqual(inspect_database(self.store.path, KEY)["head"], self.head)

    def test_observed_external_commit_pins_history(self):
        reader = self.guarded()
        self.action(self.iid, "ack")
        reader.get(self.iid, self.service.scope)
        copy_database(self.old, self.store.path)
        with self.assertRaises(audit.IntegrityError):
            reader.claim()

    def test_signed_fork_at_same_sequence_is_not_accepted(self):
        self.action(self.iid, "ack")
        head_a = inspect_database(self.store.path, KEY)["head"]
        copy_database(self.old, self.store.path)
        fork = PilotStore(self.store.path, self.clock, audit_key=KEY)
        fork.operate(self.iid, self.service, self.operator, "ack", {"request_id": "different-valid-branch"})
        head_b = inspect_database(self.store.path, KEY)["head"]
        self.assertEqual(head_a["seq"], head_b["seq"])
        self.assertNotEqual(head_a["signature"], head_b["signature"])
        with self.assertRaises(audit.IntegrityError):
            self.store.operational()

    def test_restarted_process_rejects_old_snapshot_with_external_anchor(self):
        self.action(self.iid, "ack")
        self.publish(inspect_database(self.store.path, KEY)["head"])
        copy_database(self.old, self.store.path)
        with self.assertRaises(ValueError):
            self.guarded()

    def test_new_process_without_anchor_cannot_detect_prestartup_rollback(self):
        self.action(self.iid, "ack")
        copy_database(self.old, self.store.path)
        restarted = PilotStore(self.store.path, self.clock, audit_key=KEY)
        self.assertIsNone(restarted.get(self.iid, self.service.scope)["assignment"])

    def test_checkpoint_reload_can_advance_and_then_reject_downgrade(self):
        store = self.guarded()
        self.action(self.iid, "ack")
        newer = inspect_database(self.store.path, KEY)["head"]
        self.publish(newer)
        store.operational()
        self.publish(self.head)
        with self.assertRaises(audit.IntegrityError):
            store.operational()
        self.publish(newer)
        with self.assertRaises(audit.IntegrityError):
            store.operational()
        self.guarded().operational()

    def test_missing_corrupt_or_wrong_checkpoint_quarantines_without_writes(self):
        for value in (None, "not-json", json.dumps({**self.head, "signature": "f" * 64})):
            with self.subTest(value=value):
                self.publish(self.head)
                store = self.guarded()
                before = inspect_database(self.store.path, KEY)
                if value is None:
                    self.anchor.unlink()
                else:
                    self.anchor.write_text(value, encoding="utf-8")
                with self.assertRaises(audit.IntegrityError):
                    store.access_audit("operator", None, "GET /metrics", 200, "cannot-sign")
                self.assertEqual(inspect_database(self.store.path, KEY), before)

    def test_failed_business_transaction_does_not_pin_uncommitted_head(self):
        store = self.guarded()
        with self.assertRaises(RuntimeError):
            with store.tx() as db:
                audit.append(db, KEY, self.clock(), "ACCESS", None, None, {"test": "aborted"})
                raise RuntimeError("rollback")
        self.assertEqual(store.get(self.iid, self.service.scope)["state"], "QUEUED")
        self.assertEqual(inspect_database(store.path, KEY)["head"], self.head)

    def test_failed_commit_does_not_pin_uncommitted_head(self):
        store = self.guarded()
        # A deferred foreign key violation fails COMMIT after yield has returned.
        from contextlib import contextmanager
        from mttr_guard.core import Engine
        @contextmanager
        def failing_transaction(engine):
            with closing(sqlite3.connect(engine.path)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE parent_for_test(id INTEGER PRIMARY KEY)")
                db.execute("CREATE TABLE child_for_test(id INTEGER REFERENCES parent_for_test(id) DEFERRABLE INITIALLY DEFERRED)")
                try:
                    yield db
                    db.execute("INSERT INTO child_for_test VALUES (7)")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        with patch.object(Engine, "tx", failing_transaction), self.assertRaises(sqlite3.IntegrityError):
            store.access_audit(None, None, "GET /metrics", 200, "failed-commit")
        store.operational()
        self.assertEqual(inspect_database(store.path, KEY)["head"], self.head)

    def test_cache_enabled_and_disabled_both_enforce_anchor(self):
        stores = [self.guarded(0), self.guarded()]
        self.action(self.iid, "ack")
        self.publish(inspect_database(self.store.path, KEY)["head"])
        copy_database(self.old, self.store.path)
        for store in stores:
            with self.assertRaises(audit.IntegrityError):
                store.operational()

    def test_concurrent_commits_keep_latest_floor(self):
        store = self.guarded()
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda n: store.access_audit(None, None, "GET /metrics", 200, str(n)), range(18)))
        self.assertEqual(store._audit_floor, inspect_database(store.path, KEY)["head"])
        store.operational()
        copy_database(self.old, self.store.path)
        with self.assertRaises(audit.IntegrityError):
            store.operational()

    def test_worker_stops_when_external_anchor_is_unavailable(self):
        store = self.guarded()
        worker = Worker(store, self.config, collector_factory=Mock())
        self.anchor.unlink()
        stop = Mock()
        stop.is_set.return_value = False
        worker.run(stop)
        worker.collector_factory.assert_not_called()
        stop.wait.assert_called_once_with()

    def test_export_is_non_overwriting_and_checks_prior_anchor(self):
        with self.assertRaises(FileExistsError):
            checkpoints.export(self.store.path, self.anchor, KEY)
        self.assertEqual(checkpoints.read(self.anchor), self.head)
        self.action(self.iid, "ack")
        new_file = self.root / "new.json"
        newer = checkpoints.export(self.store.path, new_file, KEY, self.anchor)
        self.assertGreater(newer["seq"], self.head["seq"])
        copy_database(self.old, self.store.path)
        with self.assertRaises(ValueError):
            checkpoints.export(self.store.path, self.root / "invalid.json", KEY, new_file)
        self.assertFalse((self.root / "invalid.json").exists())
        self.assertEqual(list(self.root.glob("*.partial-*")), [])

    def test_export_rejects_empty_unkeyed_and_wrong_key(self):
        empty = self.root / "empty.db"
        PilotStore(empty, audit_key=KEY)
        for database, key in ((empty, KEY), (self.store.path, None), (self.store.path, b"wrong-key")):
            with self.assertRaises(ValueError):
                checkpoints.export(database, self.root / "invalid.json", key)
        with self.assertRaises(ValueError):
            PilotStore(self.root / "unkeyed.db", audit_checkpoint=self.anchor)
        self.assertFalse((self.root / "invalid.json").exists())

    def test_malformed_checkpoint_inputs_are_rejected(self):
        invalid = ["null", "[]", "x" * 4097, '{"seq":1,"seq":2,"signature":"' + "a" * 64 + '"}',
                   json.dumps({"seq": True, "signature": "a" * 64}), json.dumps({"seq": 1.0, "signature": "a" * 64}),
                   json.dumps({"seq": -1, "signature": "a" * 64}), json.dumps({"seq": 2**63, "signature": "a" * 64}),
                   json.dumps({"seq": 0, "signature": audit.ZERO}), json.dumps({**self.head, "extra": "bad"}),
                   json.dumps({"seq": 1, "signature": "A" * 64}), '[' * 2000 + ']' * 2000]
        for content in invalid:
            self.anchor.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                checkpoints.read(self.anchor)

    def test_cli_exports_and_verifies_with_key_in_environment(self):
        output = self.root / "cli.json"
        env = {**os.environ, "MTTR_AUDIT_KEY": KEY.decode()}
        env.pop("MTTR_AUDIT_KEY_FILE", None)
        command = [sys.executable, "-m", "mttr_guard.enterprise"]
        for arguments in (["audit-checkpoint", "--db", self.store.path, "--output", str(output), "--checkpoint", str(self.anchor)],
                          ["audit-verify", "--db", self.store.path, "--checkpoint", str(output)]):
            result = subprocess.run(command + arguments, env=env, capture_output=True, timeout=20,
                                    cwd=Path(__file__).resolve().parents[1])
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertNotIn(KEY, result.stdout + result.stderr)
        self.assertEqual(checkpoints.read(output), self.head)

    def test_backup_and_restore_reject_history_older_than_external_anchor(self):
        saved = self.root / "backup.db"
        backup(self.store.path, saved, KEY, self.head)
        self.action(self.iid, "ack")
        newer = inspect_database(self.store.path, KEY)["head"]
        destination = self.root / "restored.db"
        with self.assertRaises(ValueError):
            restore(saved, destination, KEY, newer)
        self.assertFalse(destination.exists())
        copy_database(self.old, self.store.path)
        with self.assertRaises(ValueError):
            backup(self.store.path, self.root / "bad-backup.db", KEY, newer)
        self.assertFalse((self.root / "bad-backup.db").exists())
        self.assertFalse((self.root / "bad-backup.db.json").exists())
        restore(saved, destination, KEY, self.head)
        self.assertEqual(inspect_database(destination, KEY, self.head)["head"], self.head)
