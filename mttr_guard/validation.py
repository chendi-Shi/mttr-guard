"""Read-only source preflight, audited export and registered-cohort evaluation CLI."""
import argparse
import json
import os
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import audit
from .core import Scope, digest
from .evaluation import InvalidStudy, evaluate, timestamp
from .job_runner import IsolatedRunner
from .operations import readonly, read_key
from .pilot_config import Config


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    with Path(path).open("rb") as source:
        raw = source.read(10_485_761)
    if len(raw) > 10_485_760:
        raise ValueError("Input exceeds 10 MiB")
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))


def write_report(path, report):
    path = Path(path)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (path.name + ".partial-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        # Atomic, no-overwrite publication, as used by the backup utility.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def export_outcomes(database, key, scope, registry):
    requested = {row["id"]: row for row in registry["incidents"]}
    if len(requested) != len(registry["incidents"]):
        raise ValueError("Duplicate registry incident")
    with closing(readonly(database)) as db:
        db.execute("BEGIN")
        checked = audit.verify(db, key)
        if not checked["keyed"]:
            raise ValueError("Outcome export requires keyed audit")
        items = [json.loads(row[0]) for row in db.execute("SELECT body FROM incidents WHERE scope=?", (scope.key,))]
    head = checked["head"]
    rows, excluded_demo, unregistered = [], 0, 0
    for item in items:
        if item.get("mode") != "READ_ONLY":
            excluded_demo += 1
            continue
        if item["id"] not in requested:
            unregistered += 1
            continue
        if timestamp(requested[item["id"]].get("detected_at")) != item["detected_at"]:
            raise ValueError("Registered detection time differs from audited incident")
        recovery = item.get("recovered_at") if item["state"] == "RECOVERED" else None
        observed = max([item["ingested_at"], *[o["at"] for o in item.get("observations", [])],
                        *([recovery] if recovery is not None else [])])
        rows.append({"id": item["id"], "detected_at": iso(item["detected_at"]), "source_mode": "READ_ONLY",
            "last_observed_at": iso(observed),
            "recovered_at": iso(recovery) if recovery is not None else None,
            "recovery_evidence_ref": "sha256:" + digest(item.get("observations", [])) if recovery is not None else None,
            "source_ref": f"mttr-guard:audit:{head['seq']}:{head['signature']}:incident:{item['id']}",
            "safety_events": None, "safety_review_ref": None})
    return {"schema_version": 1, "registry_sha256": digest(registry), "incidents": rows,
        "audit_head": head, "scope": json.loads(scope.key), "exported_at": iso(time.time()),
        "excluded_demo_incidents": excluded_demo, "unregistered_in_scope": unregistered,
        "provenance": "Keyed local audit verified; external source authenticity and registry completeness require review",
        "followup": "Last recorded ingestion/observation/recovery, never assumed continuous until export time",
        "review_required": "Safety counts remain unknown until independent review; do not auto-fill zero"}


def preflight(service, collector_factory=None):
    if service.mode != "READ_ONLY":
        raise ValueError("Preflight rejects DEMO services")
    started = time.perf_counter()
    now = time.time()
    snapshot = collector_factory(service).collect(now) if collector_factory else (
        IsolatedRunner(timeout=20)(service, {"detected_at": now}, now, "observe")["snapshot"])
    metrics = snapshot["metrics"]
    enough = min(metrics["requests"], metrics["control_requests"]) >= service.policy["min_requests"] and metrics["qps"] > 0
    return {"schema_version": 1, "status": "source_contract_passed" if enough else "insufficient_samples",
        "scope": snapshot["scope"], "mode": snapshot["mode"], "checked_at": iso(snapshot["collected_at"]),
        "collection_hash": service.collection_hash, "snapshot_sha256": digest(snapshot),
        "sample_age_seconds": metrics["sample_age_seconds"], "sufficient_samples": enough,
        "metrics": metrics, "recovery_policy": service.policy,
        "trace_sample_count": snapshot["traces"]["sample_count"],
        "elapsed_seconds": time.perf_counter() - started,
        "claims": {"production_capacity": "not_assessed", "mttr_reduction": "not_assessed",
                   "source_authenticity": "Configured source declaration; requires operator verification"},
        "limitation": "One point-in-time read of configured evidence; no fault injection, IAM integration or deployment writes"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    evaluation = commands.add_parser("evaluate")
    for name in ("plan", "registry", "outcomes", "output"):
        evaluation.add_argument("--" + name, required=True)
    export = commands.add_parser("export")
    for name in ("db", "scope", "registry", "output"):
        export.add_argument("--" + name, required=True)
    probe = commands.add_parser("preflight")
    for name in ("config", "service", "output"):
        probe.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        if args.command == "evaluate":
            report = evaluate(read_json(args.plan), read_json(args.registry), read_json(args.outcomes))
        elif args.command == "export":
            report = export_outcomes(args.db, read_key(), Scope(**read_json(args.scope)), read_json(args.registry))
        else:
            report = preflight(Config.read(args.config).services[args.service])
        write_report(args.output, report)
        print(json.dumps({"output": str(args.output), "status": report.get("numeric_status", report.get("status", "exported"))}))
        if report.get("numeric_status", "numeric_gate_met") != "numeric_gate_met" or report.get("status") == "insufficient_samples":
            return 2
        return 0
    except InvalidStudy as exc:
        print(json.dumps({"status": "invalid_study", "reason": str(exc)}), file=sys.stderr)
        return 1
    except Exception as exc:
        # Provider errors may contain credentials; emit a type, not its message.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
