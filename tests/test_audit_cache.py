"""Verified-row reuse must have the same integrity verdict as offline replay."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_pilot import PilotFixture
from mttr_guard import audit
from mttr_guard.core import canonical
from mttr_guard.pilot_store import PilotStore


KEY = b"cache-specific-regression-key-1234567890"


class AuditCacheTests(PilotFixture):
    def setUp(self):
        super().setUp()
        self.store = PilotStore(Path(self.temp.name) / "cache.db", self.clock, audit_key=KEY)
        self.iid = self.receive()

    def verify(self, cache=None, key=KEY, checkpoint=None):
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("BEGIN")
            return audit.verify(db, key, checkpoint, cache=cache)

    def test_unchanged_rows_reuse_crypto_but_still_read_all_tables(self):
        cache = audit.VerificationCache(KEY)
        expected = self.verify(cache)
        statements = []
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("BEGIN")
            db.set_trace_callback(statements.append)
            with patch.object(audit, "signature", side_effect=AssertionError("Unexpected repeated HMAC")):
                self.assertEqual(audit.verify(db, KEY, cache=cache), expected)
        self.assertEqual(cache.last_stats["misses"], 0)
        self.assertGreater(cache.last_stats["hits"], 0)
        for table in ("audit_records", "incidents", "events", "config"):
            self.assertTrue(any("FROM " + table in sql for sql in statements), table)

    def test_warm_cache_rejects_every_protected_row_change(self):
        changes = {
            "body": "UPDATE incidents SET body='{}'",
            "scope": "UPDATE incidents SET scope='different'",
            "state": "UPDATE incidents SET state='RECOVERED'",
            "event": "UPDATE events SET kind='FORGED'",
            "signature": "UPDATE audit_records SET signature='forged' WHERE seq=1",
            "payload": "UPDATE audit_records SET payload='{}' WHERE seq=1",
            "previous": "UPDATE audit_records SET previous='forged' WHERE seq=2",
            "deleted_record": "DELETE FROM audit_records WHERE seq=1",
            "deleted_event": "DELETE FROM events",
            "deleted_incident": "DELETE FROM incidents",
            "extra_event": "INSERT INTO events(incident,at,kind,body) VALUES ('extra',0,'FORGED','{}')",
            "head": "UPDATE config SET value='{}' WHERE key='audit_head'",
            "key": "UPDATE config SET value='different' WHERE key='audit_key_id'",
        }
        for name, sql in changes.items():
            with self.subTest(target=name):
                cache = audit.VerificationCache(KEY)
                self.verify(cache)
                entries = cache.entries
                with closing(sqlite3.connect(self.store.path)) as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(sql)
                    for mode in (cache, None):
                        with self.assertRaises(ValueError):
                            audit.verify(db, KEY, cache=mode)
                    self.assertIs(cache.entries, entries, "A failed pass cannot publish any candidate entries")
                    db.rollback()
                self.assertEqual(self.verify(cache), self.verify())

    def test_new_records_from_another_instance_are_verified(self):
        cache = audit.VerificationCache(KEY)
        self.verify(cache)
        other = PilotStore(self.store.path, self.clock, audit_key=KEY)
        other.operate(self.iid, self.service, self.operator, "ack", {"request_id": "other-instance"})
        with patch.object(audit, "signature", wraps=audit.signature) as signatures:
            result = self.verify(cache)
            self.assertGreater(signatures.call_count, 0)
        self.assertEqual(result, self.verify())
        self.assertGreater(cache.last_stats["hits"], 0)
        self.assertGreater(cache.last_stats["misses"], 0)

    def test_sql_numeric_type_change_is_not_a_cache_hit(self):
        cache = audit.VerificationCache(KEY)
        self.verify(cache)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            before = db.execute("SELECT at FROM events").fetchone()[0]
            db.execute("CREATE TABLE changed_events (seq INTEGER PRIMARY KEY, incident TEXT, at INTEGER, kind TEXT, body TEXT)")
            db.execute("INSERT INTO changed_events SELECT * FROM events")
            db.execute("DROP TABLE events")
            db.execute("ALTER TABLE changed_events RENAME TO events")
            after = db.execute("SELECT at FROM events").fetchone()[0]
            self.assertEqual(before, after)
            self.assertIs(type(before), float)
            self.assertIs(type(after), int)
        for mode in (cache, None):
            with self.assertRaises(ValueError):
                self.verify(mode)

    def test_replayed_state_is_rejected_after_cache_warmup(self):
        original = self.store.get(self.iid, self.service.scope)
        self.action(self.iid, "ack")
        cache = audit.VerificationCache(KEY)
        self.verify(cache)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE incidents SET body=?", (canonical(original),))
        for mode in (cache, None):
            with self.assertRaises(ValueError):
                self.verify(mode)

    def test_cache_budget_and_disabled_cache_do_not_skip_checks(self):
        with self.store.tx() as db:
            for n in range(30):
                audit.append(db, KEY, self.clock(), "ACCESS", None, "test",
                             {"route": "GET /metrics", "request_id": str(n), "padding": "x" * 4000})
        expected = self.verify()
        for budget in (0, 2048, 8192):
            with self.subTest(budget=budget):
                cache = audit.VerificationCache(KEY, budget)
                self.verify(cache)
                with patch.object(audit, "signature", wraps=audit.signature) as signatures:
                    self.assertEqual(self.verify(cache), expected)
                    self.assertGreater(signatures.call_count, 0)
                self.assertLessEqual(cache.retained_bytes, budget)
                if budget == 0:
                    self.assertEqual(cache.entries, {})

    def test_unretained_record_tamper_is_detected(self):
        with self.store.tx() as db:
            for n in range(10):
                audit.append(db, KEY, self.clock(), "ACCESS", None, "test", {"request_id": str(n)})
        cache = audit.VerificationCache(KEY, 2048)
        self.verify(cache)
        self.assertGreater(len(cache.entries), 0)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            last = db.execute("SELECT MAX(seq) FROM audit_records").fetchone()[0]
            self.assertNotIn(("record", last), cache.entries)
            db.execute("UPDATE audit_records SET signature='forged' WHERE seq=?", (last,))
        with self.assertRaises(ValueError):
            self.verify(cache)

    def test_failed_pass_does_not_cache_new_valid_prefix(self):
        cache = audit.VerificationCache(KEY)
        self.verify(cache)
        entries, size, stats = cache.entries, cache.retained_bytes, cache.last_stats
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            audit.append(db, KEY, self.clock(), "ACCESS", None, "test", {"request_id": "new-valid-record"})
            db.execute("UPDATE incidents SET body='{}'")
            with self.assertRaises(ValueError):
                audit.verify(db, KEY, cache=cache)
            self.assertIs(cache.entries, entries)
            self.assertEqual((cache.retained_bytes, cache.last_stats), (size, stats))
            db.rollback()
        self.assertEqual(self.verify(cache), self.verify())

    def test_cache_cannot_cross_key_or_bypass_external_checkpoint(self):
        with self.assertRaises(ValueError):
            self.verify(audit.VerificationCache(b"different-key"))
        cache = audit.VerificationCache(KEY)
        self.verify(cache)
        with self.assertRaises(ValueError):
            self.verify(cache, checkpoint={"seq": 99999, "signature": "0" * 64})

    def test_rollback_never_admits_uncommitted_state_as_trusted(self):
        original = self.store.get(self.iid, self.service.scope)
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.tx() as db:
                modified = {**original, "summary": "rolled-back update"}
                self.store._save(db, modified)
                raise RuntimeError("abort")
        self.assertEqual(self.store.get(self.iid, self.service.scope), original)
        self.assertEqual(self.verify(self.store._audit_cache), self.verify())

    def test_same_head_does_not_mask_live_state_tampering(self):
        self.store.get(self.iid, self.service.scope)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            head = db.execute("SELECT value FROM config WHERE key='audit_head'").fetchone()[0]
            body = json.loads(db.execute("SELECT body FROM incidents").fetchone()[0])
            body["summary"] = "forged with unchanged head"
            db.execute("UPDATE incidents SET body=?", (canonical(body),))
            self.assertEqual(db.execute("SELECT value FROM config WHERE key='audit_head'").fetchone()[0], head)
        with self.assertRaises(audit.IntegrityError):
            self.action(self.iid, "ack")
