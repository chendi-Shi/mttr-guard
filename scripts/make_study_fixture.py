"""Generate explicitly SYNTHETIC inputs for exercising the evaluation CLI."""
import argparse
from pathlib import Path

from mttr_guard.core import digest
from mttr_guard.validation import iso, write_report


def fixture():
    plan = {"schema_version": 1, "study_id": "SYNTHETIC-cli-contract", "domain": "SYNTHETIC",
        "exposure": "INTERVENTION", "registration_ref": "synthetic://not-a-real-registration",
        "inclusion_rule": "All generated fixture incidents; no real systems or production measurements",
        "registered_at": "2020-01-01T00:00:00Z", "window_start": "2020-01-02T00:00:00Z",
        "window_end": "2020-01-03T00:00:00Z", "analysis_cutoff": "2020-01-04T00:00:00Z",
        "horizon_seconds": 600, "baseline": "manual", "candidate": "multi_agent",
        "minimum_per_arm_per_stratum": 5, "minimum_improvement_seconds": 10,
        "bootstrap_samples": 500, "seed": 7, "strata": [{"name": "checkout/sev2/release", "weight": 1}]}
    rows, measured = [], []
    for arm in ("manual", "multi_agent"):
        for n in range(8):
            iid, detected = f"SYNTHETIC-{arm}-{n}", 1577923200 + n * 1000
            recovery = detected + (140 if arm == "manual" else 50) + n * 5
            rows.append({"id": iid, "domain": "SYNTHETIC", "arm": arm, "stratum": "checkout/sev2/release",
                         "detected_at": iso(detected), "source_ref": "synthetic://" + iid})
            measured.append({"id": iid, "last_observed_at": iso(recovery), "recovered_at": iso(recovery),
                "recovery_evidence_ref": "synthetic://recovery/" + iid, "source_ref": "synthetic://outcome/" + iid,
                "safety_events": 0, "safety_review_ref": "synthetic://review/" + iid})
    registry = {"schema_version": 1, "plan_sha256": digest(plan), "source_ref": "synthetic://complete-roster", "incidents": rows}
    outcomes = {"schema_version": 1, "registry_sha256": digest(registry), "incidents": measured}
    return plan, registry, outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()
    root = Path(args.output_directory)
    root.mkdir(parents=True, exist_ok=False)
    for name, data in zip(("plan", "registry", "outcomes"), fixture()):
        write_report(root / (name + ".json"), data)
    print("Created SYNTHETIC inputs only:", root)


if __name__ == "__main__":
    main()
