"""Multi-service admission and dispatch invariants under overload and restart."""
import copy
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from test_pilot import PilotFixture
from mttr_guard.core import Scope, digest
from mttr_guard.operations import backup, restore, inspect_database
from mttr_guard.pilot_config import Config
from mttr_guard.pilot_demo import config_data
from mttr_guard.pilot_store import PilotStore, QueueFull


KEY = b"queue-isolation-test-key-not-secret-0001"


class QueueIsolationTests(PilotFixture):
    def setUp(self):
        super().setUp()
        self.store = PilotStore(Path(self.temp.name) / "queues.db", self.clock, audit_key=KEY)
        self.worker.store = self.store
        self.service = replace(self.service, queue_limits={"max_pending_per_queue": 8, "max_running_per_queue": 2})
        self.other = replace(self.service, name="payments", scope=replace(self.service.scope, service="payments"))
        self.config.services.update(checkout=self.service, payments=self.other)

    def send(self, service, fingerprint):
        payload = self.payload(fingerprint)
        payload["alerts"][0]["labels"] = asdict(service.scope)
        return self.store.receive(service, payload)[0]

    def finish(self, claimed, store=None):
        job, inc = claimed
        return (store or self.store).finish_diagnosis(job, Scope(**inc["scope"]),
            {"evidence": [], "collected_at": self.clock()}, [], "Synthetic queue test, inspect manually")

    def test_service_flood_does_not_consume_other_service_admission(self):
        limited = replace(self.service, queue_limits={"max_pending_per_queue": 2, "max_running_per_queue": 1})
        first = self.send(limited, "one")
        self.send(limited, "two")
        with self.assertRaises(QueueFull):
            self.send(limited, "three")
        self.assertEqual(self.send(limited, "one"), first, "Duplicate delivery must succeed even at capacity")
        self.send(self.other, "quiet-service")
        self.assertEqual(len(self.store.list_incidents(limited.scope)), 2)
        self.assertEqual(len(self.store.list_incidents(self.other.scope)), 1)

    def test_batch_exceeding_service_capacity_is_atomic(self):
        limited = replace(self.service, queue_limits={"max_pending_per_queue": 2, "max_running_per_queue": 1})
        self.send(limited, "existing")
        before = inspect_database(self.store.path, KEY)
        payload = self.payload("new-a")
        payload["alerts"].append({**payload["alerts"][0], "fingerprint": "new-b"})
        with self.assertRaises(QueueFull):
            self.store.receive(limited, payload)
        self.assertEqual(inspect_database(self.store.path, KEY), before)

    def test_global_capacity_still_applies_across_services(self):
        self.send(self.other, "other")
        with self.assertRaises(QueueFull):
            self.store.receive(self.service, self.payload(), max_pending=1)
        self.assertEqual(self.store.list_incidents(self.service.scope), [])

    def test_dispatch_rotates_services_and_preserves_fifo_within_each(self):
        expected_a = [self.send(self.service, "a" + str(n)) for n in range(3)]
        expected_b = [self.send(self.other, "b" + str(n)) for n in range(3)]
        dispatched = []
        for _ in range(6):
            claimed = self.store.claim(queue="diagnose")
            dispatched.append(claimed[0]["incident"])
            self.finish(claimed)
        self.assertEqual(dispatched, [iid for pair in zip(expected_a, expected_b) for iid in pair])

    def test_turn_survives_restart_and_new_quiet_service_gets_a_turn(self):
        self.send(self.service, "a1")
        self.send(self.service, "a2")
        self.finish(self.store.claim())
        quiet = self.send(self.other, "b1")
        restarted = PilotStore(self.store.path, self.clock, audit_key=KEY)
        self.assertEqual(restarted.claim()[0]["incident"], quiet)

    def test_concurrent_store_instances_cannot_exceed_active_lease_limit(self):
        for n in range(6):
            self.send(self.service, "a" + str(n))
            self.send(self.other, "b" + str(n))
        stores = [PilotStore(self.store.path, self.clock, audit_key=KEY) for _ in range(12)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            claimed = list(pool.map(lambda store: store.claim(), stores))
        accepted = [result for result in claimed if result is not None]
        self.assertEqual(len(accepted), 4)
        self.assertEqual(len({j["incident"] for j, _ in accepted}), 4)
        for service in (self.service, self.other):
            self.assertEqual(sum(i["scope"] == asdict(service.scope) for _, i in accepted), 2)

    def test_worker_uses_hot_service_running_limit(self):
        self.send(self.service, "a1")
        self.send(self.service, "a2")
        self.store.claim()
        self.config.services["checkout"] = replace(self.service,
            queue_limits={"max_pending_per_queue": 8, "max_running_per_queue": 1})
        with patch.object(self.worker, "collector_factory") as collector:
            self.assertFalse(self.worker.run_once())
            collector.assert_not_called()
        self.config.services["checkout"] = self.service
        with patch.object(self.worker, "collector_factory", side_effect=RuntimeError("test gateway offline")) as collector:
            self.assertTrue(self.worker.run_once())
            collector.assert_called_once()

    def test_expired_leases_free_capacity_and_old_results_are_fenced(self):
        ids = [self.send(self.service, "a" + str(n)) for n in range(3)]
        old, inc = self.store.claim()
        self.store.claim()
        self.assertIsNone(self.store.claim())
        self.clock.advance(120)
        replacement = self.store.claim()
        self.assertEqual(replacement[0]["incident"], ids[0])
        self.assertNotEqual(old["lease"], replacement[0]["lease"])
        self.assertFalse(self.finish((old, inc)))
        self.assertTrue(self.finish(replacement))

    def test_diagnosis_cap_does_not_consume_observation_capacity(self):
        iid = self.observing()
        for n in range(2):
            self.send(self.service, "diagnose" + str(n))
            self.assertIsNotNone(self.store.claim(queue="diagnose"))
        self.clock.advance(10)
        observed = self.store.claim(queue="observe")
        self.assertEqual(observed[0]["incident"], iid)

    def test_human_refresh_cannot_bypass_pending_cap_or_consume_request_id(self):
        limited = replace(self.service, queue_limits={"max_pending_per_queue": 1, "max_running_per_queue": 1})
        iid = self.send(limited, "first")
        self.finish(self.store.claim())
        self.send(limited, "second")
        body = {"request_id": "retry-after-capacity", "reason": "Gateway repaired"}
        before = self.store.get(iid, limited.scope)
        with self.assertRaises(QueueFull):
            self.store.operate(iid, limited, self.operator, "refresh", body)
        self.assertEqual(self.store.get(iid, limited.scope), before)
        self.finish(self.store.claim())
        self.assertEqual(self.store.operate(iid, limited, self.operator, "refresh", body)["state"], "QUEUED")
        self.store.claim()
        self.store.operate(iid, limited, self.operator, "refresh", body)
        self.assertIsNone(self.store.claim())

    def test_manual_observation_has_separate_admission_cap(self):
        limited = replace(self.service, queue_limits={"max_pending_per_queue": 1, "max_running_per_queue": 1})
        ids = []
        for n in range(2):
            ids.append(self.send(limited, str(n)))
            self.finish(self.store.claim())
        for iid in ids:
            body = {"request_id": iid, "reason": "Manual action done", "target_revision": "r41"}
            if iid == ids[0]:
                self.store.operate(iid, limited, self.operator, "observe", body)
            else:
                with self.assertRaises(QueueFull):
                    self.store.operate(iid, limited, self.operator, "observe", body)
        self.send(limited, "diagnosis-still-admitted")

    def test_scheduler_turn_rolls_back_if_claim_audit_fails(self):
        self.send(self.service, "a")
        with patch.object(self.store, "event", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.store.claim()
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pilot_dispatch").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM pilot_jobs").fetchone()[0], "READY")

    def test_old_database_gets_dispatch_table_without_changing_signed_history(self):
        self.send(self.service, "a")
        before = inspect_database(self.store.path, KEY)["head"]
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("DROP TABLE pilot_dispatch")
        restarted = PilotStore(self.store.path, self.clock, audit_key=KEY)
        self.assertEqual(inspect_database(self.store.path, KEY)["head"], before)
        self.assertIsNotNone(restarted.claim())

    def test_backup_restore_preserves_dispatch_order(self):
        self.send(self.service, "a1")
        self.send(self.service, "a2")
        self.send(self.other, "b1")
        self.finish(self.store.claim())
        root = Path(self.temp.name)
        backup(self.store.path, root / "backup.db", KEY)
        restore(root / "backup.db", root / "restored.db", KEY)
        restarted = PilotStore(root / "restored.db", self.clock, audit_key=KEY)
        self.assertEqual(restarted.claim()[1]["service_id"], "payments")

    def test_scope_is_full_five_part_identity_not_just_service_name(self):
        other_tenant = replace(self.service, scope=replace(self.service.scope, tenant="second-tenant"),
            queue_limits={"max_pending_per_queue": 1, "max_running_per_queue": 1})
        self.send(other_tenant, "a")
        self.send(self.service, "a")
        with self.assertRaises(QueueFull):
            self.send(other_tenant, "b")
        self.assertEqual(len(self.store.list_incidents(self.service.scope)), 1)

    def test_queue_config_validation_and_collection_hash_compatibility(self):
        original_fields = {k: v for k, v in asdict(self.service).items()
                           if k not in {"owner", "links", "runbook", "prometheus_token_env", "evidence_token_env", "queue_limits"}}
        self.assertEqual(self.service.collection_hash, digest(original_fields))
        data = config_data("http://127.0.0.1:9191")
        baseline = Config(data).services["checkout"].collection_hash
        for invalid in (None, [], {"unknown": 1}, {"max_pending_per_queue": True}, {"max_pending_per_queue": 0},
                        {"max_pending_per_queue": 1001}, {"max_running_per_queue": 33}, {"max_running_per_queue": 1.5},
                        {"max_pending_per_queue": 1, "max_running_per_queue": 2}):
            variant = copy.deepcopy(data)
            variant["services"][0]["queue_limits"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Config(variant)
        data["services"][0]["queue_limits"] = {"max_pending_per_queue": 5, "max_running_per_queue": 1}
        self.assertEqual(Config(data).services["checkout"].collection_hash, baseline)

    def test_separate_processes_share_running_cap(self):
        for n in range(6):
            self.send(self.service, str(n))
        code = ("import sys; from mttr_guard.pilot_store import PilotStore; "
                "s=PilotStore(sys.argv[1],lambda:float(sys.argv[2]),audit_key=" + repr(KEY) + "); "
                "print('claimed' if s.claim() else 'idle')")
        def run(_):
            return subprocess.run([sys.executable, "-c", code, self.store.path, str(self.clock())],
                capture_output=True, timeout=30, cwd=Path(__file__).resolve().parents[1],
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(4)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(sum(r.stdout.strip() == b"claimed" for r in results), 2)
        self.assertIsNone(self.store.claim())

    def test_observation_dispatch_rotates_services(self):
        expected = []
        for service in (self.service, self.other):
            iid = self.send(service, "observe")
            self.finish(self.store.claim())
            self.store.operate(iid, service, replace(self.operator, service=service.name), "observe",
                {"request_id": iid, "reason": "Manual mitigation", "target_revision": "r41"})
            expected.append(iid)
        self.clock.advance(10)
        self.assertEqual([self.store.claim(queue="observe")[0]["incident"] for _ in range(2)], expected)

    def test_limits_lowered_below_backlog_preserve_existing_work(self):
        ids = [self.send(self.service, str(n)) for n in range(3)]
        limited = replace(self.service, queue_limits={"max_pending_per_queue": 1, "max_running_per_queue": 1})
        self.assertEqual(self.send(limited, "0"), ids[0])
        with self.assertRaises(QueueFull):
            self.send(limited, "new")
        for _ in range(3):
            claimed = self.store.claim(limits_by_scope={limited.scope.key: 1})
            self.assertIsNotNone(claimed)
            self.assertIsNone(self.store.claim(limits_by_scope={limited.scope.key: 1}))
            self.finish(claimed)
        self.assertIsNotNone(self.send(limited, "new"))
