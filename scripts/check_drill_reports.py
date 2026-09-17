"""Fail CI if an end-to-end fault drill reports the wrong outcome."""
import json
import sys
from pathlib import Path


def check(lost_reply, not_delivered):
    recovered = json.loads(Path(lost_reply).read_text(encoding="utf-8"))
    uncertain = json.loads(Path(not_delivered).read_text(encoding="utf-8"))
    for report in (recovered, uncertain):
        if report["mode"] != "SIMULATED":
            raise ValueError("Drills must use simulated execution")
        prepared = [e for e in report["events"] if e["kind"] == "EXECUTION_PREPARED"]
        if len(prepared) != 1:
            raise ValueError("A drill must admit exactly one operation")
        unknown = [e for e in report["events"] if e["kind"] == "EXECUTION_OBSERVED" and e["body"]["state"] == "EXECUTION_UNKNOWN"]
        if not unknown:
            raise ValueError("Fault drill did not exercise uncertainty")
    if recovered["incident"]["state"] != "RECOVERED" or recovered["incident"]["receipt"] is None:
        raise ValueError("A lost reply must be recovered by lookup and telemetry verification")
    if uncertain["incident"]["state"] != "EXECUTION_UNKNOWN" or uncertain["incident"]["receipt"] is not None:
        raise ValueError("An unobserved request must not be called successful")
    print("Fault drill outcomes verified (simulated data; not an MTTR benchmark)")


if __name__ == "__main__":
    check(*sys.argv[1:])
