import copy
import unittest

from mttr_guard.core import digest
from mttr_guard.evaluation import InvalidStudy, evaluate, timestamp
from mttr_guard.validation import iso
from scripts.make_study_fixture import fixture


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.registry, self.outcomes = fixture()

    def bind(self):
        self.registry["plan_sha256"] = digest(self.plan)
        self.outcomes["registry_sha256"] = digest(self.registry)

    def report(self):
        return evaluate(self.plan, self.registry, self.outcomes)

    def test_known_difference_and_reproducibility_without_production_claim(self):
        result = self.report()
        self.assertEqual(result["numeric_status"], "numeric_gate_met")
        self.assertEqual(result["comparison"]["improvement_seconds"], 90)
        self.assertEqual(result, self.report())
        self.assertEqual(result["domain"], "SYNTHETIC")
        self.assertEqual(result["claims"]["causal_mttr_reduction"], "not_established")
        self.assertEqual(result["claims"]["production_readiness"], "not_assessed")

    def test_unresolved_incidents_reverse_completed_only_success(self):
        candidate = self.outcomes["incidents"][8:]
        for row, original in zip(candidate[4:], self.registry["incidents"][12:]):
            row.update(recovered_at=None, recovery_evidence_ref=None,
                       last_observed_at=iso(timestamp(original["detected_at"]) + 600))
        result = self.report()
        self.assertLess(result["comparison"]["improvement_seconds"], 0)
        self.assertEqual(result["numeric_status"], "numeric_gate_not_met")
        stats = result["strata"]["multi_agent"]["checkout/sev2/release"]
        self.assertEqual(stats["unresolved_or_missing"], 4)
        self.assertLess(stats["completed_only"]["mean_seconds"], stats["restricted_mean_seconds"])

    def test_missing_outcome_is_retained_and_blocks_comparison(self):
        self.outcomes["incidents"].pop()
        result = self.report()
        self.assertEqual(result["registered"], 16)
        self.assertEqual(result["received_outcomes"], 15)
        self.assertEqual(result["numeric_status"], "insufficient_evidence")
        self.assertIsNone(result["comparison"])
        self.assertEqual(result["strata"]["multi_agent"]["checkout/sev2/release"]["missing_outcomes"], 1)

    def test_early_censoring_reports_bounds_without_inventing_recovery(self):
        row = self.outcomes["incidents"][-1]
        detected = timestamp(self.registry["incidents"][-1]["detected_at"])
        row.update(recovered_at=None, last_observed_at=iso(detected + 20))
        result = self.report()
        stats = result["strata"]["multi_agent"]["checkout/sev2/release"]
        self.assertEqual(stats["early_censored"], 1)
        self.assertIsNone(stats["restricted_mean_seconds"])
        self.assertLess(*stats["restricted_mean_bounds_seconds"])
        self.assertIsNone(result["comparison"])

    def test_safety_unknown_and_observed_harm_block_gate(self):
        for count in (None, 1):
            with self.subTest(count=count):
                self.outcomes["incidents"][-1]["safety_events"] = count
                self.assertEqual(self.report()["numeric_status"], "insufficient_evidence" if count is None else "safety_gate_failed")

    def test_small_sample_cannot_pass_despite_large_effect(self):
        self.plan["minimum_per_arm_per_stratum"] = 20
        self.bind()
        self.assertIsNone(self.report()["comparison"])

    def test_input_changes_require_rebinding(self):
        self.plan["horizon_seconds"] = 300
        with self.assertRaises(InvalidStudy):
            self.report()

    def test_domain_mixing_and_duplicate_registry_are_rejected(self):
        original = copy.deepcopy(self.registry)
        self.registry["incidents"][0]["domain"] = "PRODUCTION"
        self.bind()
        with self.assertRaises(InvalidStudy):
            self.report()
        self.registry = original
        self.registry["incidents"].append(self.registry["incidents"][0])
        self.bind()
        with self.assertRaises(InvalidStudy):
            self.report()

    def test_duplicate_unknown_outcome_and_unverified_recovery_rejected(self):
        original = copy.deepcopy(self.outcomes)
        for change in ("duplicate", "unknown", "no_evidence"):
            with self.subTest(change=change):
                self.outcomes = copy.deepcopy(original)
                if change == "duplicate":
                    self.outcomes["incidents"].append(self.outcomes["incidents"][0])
                elif change == "unknown":
                    self.outcomes["incidents"][0]["id"] = "unregistered"
                else:
                    self.outcomes["incidents"][0]["recovery_evidence_ref"] = None
                with self.assertRaises(InvalidStudy):
                    self.report()

    def test_naive_future_and_post_hoc_timestamps_rejected(self):
        for key, value in (("analysis_cutoff", "2020-01-04T00:00:00"),
                           ("analysis_cutoff", "2999-01-01T00:00:00Z"),
                           ("registered_at", "2020-01-02T12:00:00Z")):
            with self.subTest(key=key):
                self.plan, self.registry, self.outcomes = fixture()
                self.plan[key] = value
                self.bind()
                with self.assertRaises(InvalidStudy):
                    self.report()

    def test_recovery_after_observation_is_rejected(self):
        self.outcomes["incidents"][0]["recovered_at"] = "2020-01-03T00:00:00Z"
        with self.assertRaises(InvalidStudy):
            self.report()

    def test_shadow_and_declared_production_never_certify_causality(self):
        self.plan.update(domain="PRODUCTION", exposure="SHADOW")
        self.plan["registration_ref"] = "https://example.invalid/operator-declared-registration"
        self.registry["source_ref"] = "https://example.invalid/operator-declared-registry"
        for row in self.registry["incidents"]:
            row["domain"] = "PRODUCTION"
        for container in (self.registry, self.outcomes):
            for row in container["incidents"]:
                for key in ("source_ref", "recovery_evidence_ref", "safety_review_ref"):
                    if key in row:
                        row[key] = "https://example.invalid/operator-declared-reference"
        self.bind()
        result = self.report()
        self.assertEqual(result["claims"]["source_authenticity"], "requires_independent_review")
        self.assertEqual(result["claims"]["causal_mttr_reduction"], "not_established")

    def test_explicit_synthetic_sources_cannot_be_reclassified_as_production(self):
        self.plan["domain"] = "PRODUCTION"
        for row in self.registry["incidents"]:
            row["domain"] = "PRODUCTION"
        self.bind()
        with self.assertRaises(InvalidStudy):
            self.report()

    def test_predeclared_weights_prevent_incident_mix_from_changing_estimand(self):
        self.plan.update(minimum_per_arm_per_stratum=2, minimum_improvement_seconds=0,
                         strata=[{"name": "easy", "weight": .5}, {"name": "hard", "weight": .5}])
        rows, measured = [], []
        for arm, stratum, n, duration in (("manual", "easy", 8, 10), ("manual", "hard", 2, 100),
                                          ("multi_agent", "easy", 2, 5), ("multi_agent", "hard", 8, 90)):
            for i in range(n):
                iid = f"{arm}-{stratum}-{i}"
                rows.append({**self.registry["incidents"][0], "id": iid, "arm": arm, "stratum": stratum})
                observed = iso(timestamp(rows[-1]["detected_at"]) + duration)
                measured.append({**self.outcomes["incidents"][0], "id": iid,
                                 "last_observed_at": observed, "recovered_at": observed})
        self.registry["incidents"], self.outcomes["incidents"] = rows, measured
        self.bind()
        self.assertEqual(self.report()["comparison"]["improvement_seconds"], 7.5)

    def test_late_recovery_is_capped_not_mislabeled_as_unrestricted_mttr(self):
        for row, enrolled in zip(self.outcomes["incidents"], self.registry["incidents"]):
            stamp = iso(timestamp(enrolled["detected_at"]) + 900)
            row.update(last_observed_at=stamp, recovered_at=stamp)
        result = self.report()
        stats = result["strata"]["manual"]["checkout/sev2/release"]
        self.assertEqual(stats["restricted_mean_seconds"], 600)
        self.assertEqual(stats["completed_only"]["mean_seconds"], 900)
