import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from test_pilot import PilotFixture
from mttr_guard.core import canonical
from mttr_guard.pilot_store import PilotStore
from mttr_guard.validation import export_outcomes, iso, preflight, read_json, write_report


KEY = b"outcome-export-regression-key-1234567890"


class ValidationTests(PilotFixture):
    def setUp(self):
        super().setUp()
        self.store = PilotStore(Path(self.temp.name) / "export.db", self.clock, audit_key=KEY)

    def test_export_is_scoped_audited_and_never_invents_followup_or_safety(self):
        iid = self.receive()
        self.clock.advance(300)
        registry = {"incidents": [{"id": iid, "detected_at": iso(self.clock() - 300)}, {"id": "missing"}]}
        report = export_outcomes(self.store.path, KEY, self.service.scope, registry)
        self.assertEqual(len(report["incidents"]), 1)
        row = report["incidents"][0]
        self.assertIsNone(row["recovered_at"])
        self.assertIsNone(row["safety_events"])
        self.assertNotIn("summary", row)
        self.assertIn("mttr-guard:audit:", row["source_ref"])
        self.assertNotEqual(row["last_observed_at"], report["exported_at"])
        other = replace(self.service.scope, tenant="other")
        self.assertEqual(export_outcomes(self.store.path, KEY, other, registry)["incidents"], [])

    def test_export_detects_tampering_and_wrong_key(self):
        iid = self.receive()
        registry = {"incidents": [{"id": iid, "detected_at": iso(self.clock())}]}
        with self.assertRaises(ValueError):
            export_outcomes(self.store.path, b"wrong", self.service.scope, registry)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            body = json.loads(db.execute("SELECT body FROM incidents").fetchone()[0])
            body["summary"] = "forged"
            db.execute("UPDATE incidents SET body=?", (canonical(body),))
        with self.assertRaises(ValueError):
            export_outcomes(self.store.path, KEY, self.service.scope, registry)

    def test_export_cannot_shift_registered_detection_time(self):
        iid = self.receive()
        with self.assertRaises(ValueError):
            export_outcomes(self.store.path, KEY, self.service.scope,
                            {"incidents": [{"id": iid, "detected_at": iso(self.clock() + 1)}]})

    def test_export_excludes_demo_even_when_registry_lists_it(self):
        service = replace(self.service, mode="DEMO")
        iid = self.store.receive(service, self.payload())[0]
        report = export_outcomes(self.store.path, KEY, service.scope, {"incidents": [{"id": iid}]})
        self.assertEqual(report["incidents"], [])
        self.assertEqual(report["excluded_demo_incidents"], 1)

    def test_preflight_rejects_demo_without_contacting_sources(self):
        factory = Mock()
        with self.assertRaises(ValueError):
            preflight(replace(self.service, mode="DEMO"), factory)
        factory.assert_not_called()

    def test_preflight_reports_contract_and_sample_failure_without_certification(self):
        snapshot = self.factory(self.service).collect(self.clock())
        collector = Mock()
        collector.collect.return_value = snapshot
        report = preflight(self.service, lambda _: collector)
        self.assertEqual(report["status"], "source_contract_passed")
        self.assertEqual(report["claims"]["production_capacity"], "not_assessed")
        snapshot["metrics"]["requests"] = 0
        self.assertEqual(preflight(self.service, lambda _: collector)["status"], "insufficient_samples")

    def test_inputs_reject_duplicate_keys_nonfinite_and_oversized_data(self):
        path = Path(self.temp.name) / "input.json"
        for text in ('{"id":1,"id":2}', '{"value":NaN}', ' ' * 10_485_761):
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                read_json(path)

    def test_report_cannot_overwrite_prior_evidence(self):
        path = Path(self.temp.name) / "report.json"
        write_report(path, {"run": 1})
        with self.assertRaises(FileExistsError):
            write_report(path, {"run": 2})
        self.assertEqual(read_json(path), {"run": 1})
