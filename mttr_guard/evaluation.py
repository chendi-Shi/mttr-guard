"""Registered-cohort evaluation; numeric evidence is never deployment certification.

The endpoint is E[min(TTR, predeclared horizon)], not unrestricted MTTR.
Incomplete follow-up produces bounds and blocks a point comparison.
"""
import math
import random
from datetime import datetime, timezone

from .core import digest


class InvalidStudy(ValueError):
    pass


def label(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise InvalidStudy(f"Missing or invalid {field}")
    return value


def number(value, field, low=0, high=1e12):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise InvalidStudy(f"Invalid {field}")
    return value


def timestamp(value):
    if not isinstance(value, str):
        raise InvalidStudy("Timestamps must be timezone-aware ISO 8601 strings")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise InvalidStudy("Timestamps must be timezone-aware ISO 8601 strings") from None


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    left, right = math.floor(position), math.ceil(position)
    return ordered[left] + (ordered[right] - ordered[left]) * (position - left)


def evaluate(plan, registry, outcomes):
    for value in (plan, registry, outcomes):
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise InvalidStudy("Expected study schema_version 1")
    label(plan.get("study_id"), "study_id")
    label(plan.get("registration_ref"), "independent registration reference")
    label(plan.get("inclusion_rule"), "inclusion rule")
    domain = plan.get("domain")
    if domain not in {"PRODUCTION", "STAGING", "SYNTHETIC"}:
        raise InvalidStudy("Declare a single evidence domain")
    if domain != "SYNTHETIC":
        if str(plan.get("registration_ref", "")).startswith("synthetic://") or str(registry.get("source_ref", "")).startswith("synthetic://"):
            raise InvalidStudy("Synthetic registration or registry cannot enter a real-data study")
        for container in (registry, outcomes):
            for row in container.get("incidents", []):
                if row.get("source_mode") == "DEMO" or any(str(row.get(k, "")).startswith("synthetic://")
                        for k in ("source_ref", "recovery_evidence_ref", "safety_review_ref")):
                    raise InvalidStudy("Explicitly synthetic evidence cannot enter a real-data study")
    if plan.get("exposure") not in {"INTERVENTION", "SHADOW"}:
        raise InvalidStudy("Declare actual intervention or shadow analysis")
    registered, start, end, cutoff = [timestamp(plan.get(k)) for k in
        ("registered_at", "window_start", "window_end", "analysis_cutoff")]
    if not registered <= start < end <= cutoff <= datetime.now(timezone.utc).timestamp():
        raise InvalidStudy("Invalid study timing or analysis cutoff in the future")
    horizon = number(plan.get("horizon_seconds"), "horizon_seconds", 1, 604800)
    target = number(plan.get("minimum_improvement_seconds"), "minimum improvement", 0, horizon)
    minimum = plan.get("minimum_per_arm_per_stratum")
    draws, seed = plan.get("bootstrap_samples", 1000), plan.get("seed", 0)
    if type(minimum) is not int or not 2 <= minimum <= 10000:
        raise InvalidStudy("Predeclare at least two observations per arm/stratum; this is not a power calculation")
    if type(draws) is not int or not 200 <= draws <= 2000 or type(seed) is not int:
        raise InvalidStudy("Use 200-2000 bootstrap samples and an integer seed")
    arms = (label(plan.get("baseline"), "baseline"), label(plan.get("candidate"), "candidate"))
    if arms[0] == arms[1]:
        raise InvalidStudy("Baseline and candidate must differ")
    weights = {}
    for row in plan.get("strata", []):
        name = label(row.get("name"), "stratum")
        if name in weights:
            raise InvalidStudy("Duplicate stratum")
        weights[name] = number(row.get("weight"), "stratum weight", .000001, 1)
    if not weights or len(weights) > 100 or not math.isclose(sum(weights.values()), 1, abs_tol=1e-9):
        raise InvalidStudy("Predeclared stratum weights must sum to one")
    if registry.get("plan_sha256") != digest(plan) or outcomes.get("registry_sha256") != digest(registry):
        raise InvalidStudy("Plan/registry/outcome hashes do not match")
    label(registry.get("source_ref"), "registry source reference")
    roster = registry.get("incidents")
    measurements = outcomes.get("incidents")
    if not isinstance(roster, list) or not 1 <= len(roster) <= 10000 or not isinstance(measurements, list):
        raise InvalidStudy("Provide 1-10000 registered incidents and an outcome list")
    assigned = {}
    for row in roster:
        iid = label(row.get("id"), "incident id")
        if iid in assigned:
            raise InvalidStudy("Duplicate incident; aggregate duplicate alerts before registration")
        if row.get("domain") != domain or row.get("arm") not in arms or row.get("stratum") not in weights:
            raise InvalidStudy("Domain, arm or stratum differs from the registered plan")
        detected = timestamp(row.get("detected_at"))
        if not start <= detected < end:
            raise InvalidStudy("Incident falls outside the enrollment window")
        label(row.get("source_ref"), "incident source reference")
        assigned[iid] = {**row, "detected": detected}
    seen, grouped, blockers = set(), {(a, s): [] for a in arms for s in weights}, []
    measured = {}
    for row in measurements:
        iid = row.get("id")
        if iid not in assigned or iid in seen:
            raise InvalidStudy("Unregistered or duplicate outcome")
        seen.add(iid)
        label(row.get("source_ref"), "outcome source reference")
        detected, observed = assigned[iid]["detected"], timestamp(row.get("last_observed_at"))
        if row.get("detected_at") is not None and timestamp(row["detected_at"]) != detected:
            raise InvalidStudy("Outcome source detection time differs from registered incident")
        if not detected <= observed <= cutoff:
            raise InvalidStudy("Invalid outcome follow-up time")
        recovery = timestamp(row["recovered_at"]) if row.get("recovered_at") is not None else None
        if recovery is not None:
            if not detected <= recovery <= observed:
                raise InvalidStudy("Recovery is outside the observed incident interval")
            label(row.get("recovery_evidence_ref"), "independent recovery evidence reference")
        safety = row.get("safety_events")
        if safety is not None:
            if type(safety) is not int or not 0 <= safety <= 100000:
                raise InvalidStudy("Invalid safety event count")
            label(row.get("safety_review_ref"), "safety review reference")
        duration = recovery - detected if recovery is not None else None
        followup = observed - detected
        exact = duration is not None or followup >= horizon
        capped = min(duration, horizon) if duration is not None else horizon
        measured[iid] = {"duration": duration, "lower": capped if exact else followup,
                         "upper": capped if exact else horizon, "exact": exact,
                         "missing": False, "safety_events": safety}
    for iid, incident in assigned.items():
        record = measured.get(iid, {"duration": None, "lower": 0, "upper": horizon, "exact": False,
                                     "missing": True, "safety_events": None})
        grouped[(incident["arm"], incident["stratum"])].append(record)
    summaries, points = {}, {}
    for (arm, stratum), rows in grouped.items():
        n = len(rows)
        recovered = [r["duration"] for r in rows if r["duration"] is not None]
        complete = bool(n) and all(r["exact"] for r in rows)
        if n < minimum:
            blockers.append(f"insufficient_sample:{arm}:{stratum}")
        if not complete:
            blockers.append(f"incomplete_followup:{arm}:{stratum}")
        if any(r["safety_events"] is None for r in rows):
            blockers.append(f"missing_safety_review:{arm}:{stratum}")
        summaries.setdefault(arm, {})[stratum] = {
            "registered": n, "recovered": len(recovered), "unresolved_or_missing": n - len(recovered),
            "missing_outcomes": sum(r["missing"] for r in rows),
            "early_censored": sum(not r["exact"] and not r["missing"] for r in rows),
            "restricted_mean_bounds_seconds": [sum(r[k] for r in rows) / n if n else None for k in ("lower", "upper")],
            "restricted_mean_seconds": sum(r["upper"] for r in rows) / n if complete else None,
            "completed_only": {"n": len(recovered), "mean_seconds": sum(recovered) / len(recovered) if recovered else None,
                               "p50_seconds": percentile(recovered, .5), "p90_seconds": percentile(recovered, .9)},
            "reviewed_safety_events": sum(r["safety_events"] or 0 for r in rows),
            "missing_safety_reviews": sum(r["safety_events"] is None for r in rows)}
        points[(arm, stratum)] = [r["upper"] for r in rows] if complete else None
    effect = None
    if all(points.values()) and not any(b.startswith("insufficient_sample") for b in blockers):
        means = {a: sum(weights[s] * sum(points[(a, s)]) / len(points[(a, s)]) for s in weights) for a in arms}
        rng, differences = random.Random(seed), []
        for _ in range(draws):
            sampled = {a: sum(weights[s] * sum(rng.choices(points[(a, s)], k=len(points[(a, s)]))) /
                              len(points[(a, s)]) for s in weights) for a in arms}
            differences.append(sampled[arms[0]] - sampled[arms[1]])
        effect = {"weighted_restricted_mean_seconds": means, "improvement_seconds": means[arms[0]] - means[arms[1]],
                  "exploratory_bootstrap_95_interval_seconds": [percentile(differences, .025), percentile(differences, .975)],
                  "bootstrap_samples": draws, "seed": seed,
                  "assumption": "Independent registered incidents within each arm/stratum; not a causal confidence statement"}
    candidate_safety = sum(r["safety_events"] or 0 for s in weights for r in grouped[(arms[1], s)])
    if candidate_safety:
        blockers.append("candidate_safety_events_present")
    if candidate_safety:
        status = "safety_gate_failed"
    elif blockers or effect is None:
        status = "insufficient_evidence"
    else:
        status = "numeric_gate_met" if effect["exploratory_bootstrap_95_interval_seconds"][0] > target else "numeric_gate_not_met"
    return {"schema_version": 1, "study_id": plan["study_id"], "domain": domain, "exposure": plan["exposure"],
        "numeric_status": status, "blockers": blockers, "registered": len(roster), "received_outcomes": len(seen),
        "endpoint": "Mean min(verified TTR, predeclared horizon); not unrestricted MTTR",
        "horizon_seconds": horizon, "minimum_improvement_seconds": target,
        "strata": summaries, "comparison": effect,
        "input_hashes": {"plan": digest(plan), "registry": digest(registry), "outcomes": digest(outcomes)},
        "claims": {"production_readiness": "not_assessed", "causal_mttr_reduction": "not_established",
                   "source_authenticity": "requires_independent_review", "registry_completeness": "requires_independent_review"},
        "limitations": ["Declared domain and references do not authenticate production data",
            "Hash binding detects changed inputs, not falsified sources or post-hoc registration",
            "Numeric gate is not a power calculation, randomized trial or production approval",
            "Shadow recommendations cannot demonstrate actual MTTR reduction",
            "Completed-only statistics must not exclude unresolved incidents from the primary endpoint"]}
