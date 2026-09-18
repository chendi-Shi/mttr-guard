"""Synthetic single-process audit cost, not a capacity or production MTTR test.

python -m scripts.benchmark_audit_guard --output artifacts/audit-cost.json
"""
import argparse
import json
import math
import platform
import secrets
import statistics
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from mttr_guard import audit
from mttr_guard.pilot_config import Config
from mttr_guard.pilot_demo import config_data
from mttr_guard.pilot_store import PilotStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--cache-mib", type=int, default=16, help="Verified-row cache budget; 0 disables reuse for comparison")
    parser.add_argument("--output", type=Path, default=Path("artifacts/audit-cost.json"))
    args = parser.parse_args()
    if args.samples < 2 or any(n < 200 for n in args.records) or args.cache_mib < 0:
        parser.error("Use at least 2 samples and 200 records (100 incidents)")
    results = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        token = root / "synthetic.token"
        token.write_text(secrets.token_urlsafe(40), encoding="utf-8")
        data = config_data("http://127.0.0.1:9191")
        for identity in data["identities"]:
            identity.pop("token_env", None)
            identity["token_file"] = str(token)
        # Distinct identities must use distinct tokens; only config parsing is
        # needed here, so retain one synthetic operator and never start a server.
        data["identities"] = data["identities"][:1]
        service = Config(data).services["checkout"]
        for target in args.records:
            store = PilotStore(root / f"history-{target}.db", audit_key=secrets.token_bytes(32),
                               audit_cache_bytes=args.cache_mib * 1024 * 1024)
            starts = datetime.now(timezone.utc).isoformat()
            store.receive(service, {"alerts": [{"status": "firing", "labels": asdict(service.scope),
                "fingerprint": f"synthetic-{n}", "startsAt": starts,
                "annotations": {"summary": "Synthetic benchmark incident"}} for n in range(100)]})
            # Batch valid access records only for fixture setup. Measured calls
            # below use the public store methods and the full integrity guard.
            with store.tx() as db:
                initial = db.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
                for n in range(initial, target):
                    audit.append(db, store.audit_key, store.clock(), "ACCESS", None, "synthetic",
                                 {"route": "GET /api/operations", "status": 200, "request_id": str(n)})
            # Measure a cold full scan separately; all timed samples then start
            # with a populated (possibly budget-limited) verified-row cache.
            store._audit_cache = audit.VerificationCache(store.audit_key, args.cache_mib * 1024 * 1024)
            started = time.perf_counter()
            store.operational(service.scope)
            cold_read_ms = (time.perf_counter() - started) * 1000
            elapsed = []
            for n in range(args.samples):
                started = time.perf_counter()
                store.operational(service.scope)
                store.access_audit("synthetic", asdict(service.scope), "GET /api/operations", 200, str(n))
                elapsed.append((time.perf_counter() - started) * 1000)
            results.append({"starting_records": target, "incidents": 100, "samples": args.samples,
                "operation": "operational + access_audit (two verified transactions)",
                "cold_verified_read_ms": round(cold_read_ms, 3),
                "cache_retained_bytes": store._audit_cache.retained_bytes,
                "last_verification": store._audit_cache.last_stats,
                "p50_ms": round(statistics.median(elapsed), 3),
                "p95_ms": round(sorted(elapsed)[math.ceil(.95 * len(elapsed)) - 1], 3),
                "max_ms": round(max(elapsed), 3)})
    report = {"synthetic": True, "python": platform.python_version(), "platform": platform.platform(),
              "concurrent_clients": 1, "cache_budget_mib": args.cache_mib, "results": results,
              "limitation": "Local audit cost only; excludes HTTP, collectors, models and concurrent lock contention"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
