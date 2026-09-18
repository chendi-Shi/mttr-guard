"""Bounded open-loop GET /api/operations probe; not a production capacity certificate."""
import argparse
import asyncio
import json
import math
import os
import time
from urllib.parse import urlsplit

from .core import digest
from .evaluation import percentile
from .validation import iso, read_json, write_report


def validate(plan):
    parsed = urlsplit(plan["base_url"])
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Use an origin without credentials, query or path")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Remote targets require HTTPS")
    if not parsed.hostname or plan.get("domain") not in {"SYNTHETIC", "STAGING", "PRODUCTION"}:
        raise ValueError("Declare target and evidence domain")
    limits = {"rate_per_second": (1, 100), "duration_seconds": (1, 120), "concurrency": (1, 32),
              "request_timeout_seconds": (.1, 10), "max_error_ratio": (0, 1), "max_p95_seconds": (.001, 30)}
    for name, (low, high) in limits.items():
        value = plan.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid probe limit")
    if type(plan["concurrency"]) is not int or not isinstance(plan.get("service"), str) or not plan["service"]:
        raise ValueError("Declare integer concurrency and service")


async def run_probe(plan, token):
    validate(plan)
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("Configure an operator/viewer token outside command arguments")
    import httpx
    rate, duration, concurrency = plan["rate_per_second"], plan["duration_seconds"], plan["concurrency"]
    planned = math.ceil(rate * duration)
    active, observations = set(), []
    drops, late = 0, 0
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                limits=httpx.Limits(max_connections=concurrency)) as client:
        started, stamp = time.monotonic(), time.time()
        async def request(scheduled):
            status, error = None, None
            try:
                async with asyncio.timeout(plan["request_timeout_seconds"]):
                    async with client.stream("GET", plan["base_url"].rstrip("/") + "/api/operations",
                            headers={"Authorization": "Bearer " + token, "X-MTTR-Service": plan["service"]}) as response:
                        status, chunks, size = response.status_code, [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 65536:
                                raise ValueError("Oversized response")
                            chunks.append(chunk)
                        if status == 200:
                            payload = json.loads(b"".join(chunks))
                            if not isinstance(payload, dict) or type(payload.get("ready")) is not bool or not payload["ready"]:
                                error = "not_ready_or_invalid_body"
                        else:
                            error = "http_error"
            except Exception as exc:
                error = type(exc).__name__
            observations.append({"latency_seconds": time.monotonic() - scheduled, "status": status, "error": error})
        for n in range(planned):
            scheduled = started + n / rate
            await asyncio.sleep(max(0, scheduled - time.monotonic()))
            if time.monotonic() - scheduled >= 1 / rate:
                late += 1
                continue
            if len(active) >= concurrency:
                drops += 1
                continue
            task = asyncio.create_task(request(scheduled))
            active.add(task)
            task.add_done_callback(active.discard)
        if active:
            await asyncio.gather(*active)
        await asyncio.sleep(max(0, started + duration - time.monotonic()))
    elapsed = time.monotonic() - started
    successful = sum(o["error"] is None for o in observations)
    failed = planned - successful  # Includes skipped starts and every failed response.
    latencies = [o["latency_seconds"] for o in observations]
    p95 = percentile(latencies, .95)
    status_counts, error_counts = {}, {}
    for row in observations:
        key = str(row["status"]) if row["status"] is not None else "no_response"
        status_counts[key] = status_counts.get(key, 0) + 1
        if row["error"]:
            error_counts[row["error"]] = error_counts.get(row["error"], 0) + 1
    return {"schema_version": 1, "domain": plan["domain"], "plan_sha256": digest(plan), "started_at": iso(stamp),
        "run_configuration": {k: plan[k] for k in ("base_url", "service", "rate_per_second", "duration_seconds",
            "concurrency", "request_timeout_seconds", "max_error_ratio", "max_p95_seconds")},
        "workload": "GET /api/operations including its server-side access audit; not alert intake or diagnosis",
        "planned": planned, "started": len(observations), "successful": successful,
        "failed_or_skipped": failed, "skipped_concurrency": drops, "skipped_scheduler": late,
        "response_statuses": status_counts, "errors": error_counts, "error_ratio": failed / planned,
        "latency_seconds": {"p50": percentile(latencies, .5), "p95": p95, "p99": percentile(latencies, .99)},
        "elapsed_seconds": elapsed, "successful_per_second": successful / max(elapsed, duration),
        "numeric_gate_met": successful > 0 and p95 is not None and failed / planned <= plan["max_error_ratio"] and p95 <= plan["max_p95_seconds"],
        "claims": {"production_capacity": "Not established; applies only to this workload/run",
                   "mttr_reduction": "not_assessed", "domain_authenticity": "operator_declared"},
        "limitations": ["Open-loop scheduling; latency includes scheduler delay; skipped starts count as failures",
                         "Percentiles cover started requests including failures; skipped starts have no latency sample",
                         "Does not establish maximum sustainable capacity, long-run behavior or failover"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--token-env", default="MTTR_LOAD_TOKEN")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run_probe(read_json(args.plan), os.environ.get(args.token_env)))
        write_report(args.output, report)
        print(json.dumps({"numeric_gate_met": report["numeric_gate_met"], "output": args.output}))
        return 0 if report["numeric_gate_met"] else 2
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
