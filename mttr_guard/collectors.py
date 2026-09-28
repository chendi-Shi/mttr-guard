"""Read-only Prometheus and evidence-gateway collectors with explicit freshness checks."""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from decimal import Decimal, ROUND_FLOOR

from .core import digest
from .pilot_config import METRICS, number


class EvidenceUnavailable(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EvidenceUnavailable("Evidence endpoint redirected")


def get_json(url, token=None, timeout=5):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    # Internal collectors must not inherit arbitrary desktop proxy configuration.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(1_048_577)
    if len(raw) > 1_048_576:
        raise EvidenceUnavailable("Evidence response exceeds 1 MiB")
    return json.loads(raw)


class Collector:
    def __init__(self, service, transport=get_json):
        self.service, self.transport = service, transport

    def collect(self, now, detected_at=None):
        started = time.perf_counter()
        service = self.service
        prometheus_token = os.environ.get(service.prometheus_token_env) if service.prometheus_token_env else None
        evidence_token = os.environ.get(service.evidence_token_env) if service.evidence_token_env else None
        # Prometheus rounds numeric API times to milliseconds. Round down here
        # so legitimate responses never appear newer than the collection time.
        query_time = str(Decimal(str(now)).quantize(Decimal("0.001"), rounding=ROUND_FLOOR))

        def metric(name):
            query = urllib.parse.urlencode({"query": service.queries[name], "time": query_time, "timeout": "4s"})
            body = self.transport(service.prometheus_url + "/api/v1/query?" + query, prometheus_token)
            if body.get("status") != "success" or body.get("warnings") or body.get("data", {}).get("resultType") != "vector":
                raise EvidenceUnavailable("Prometheus returned an unsuccessful or partial query")
            rows = body["data"]["result"]
            if len(rows) != 1:
                raise EvidenceUnavailable("Prometheus must return exactly one aggregate series")
            stamp, raw = rows[0]["value"]
            number(stamp, "query timestamp")
            if not 0 <= now - stamp <= 15:
                raise EvidenceUnavailable("Prometheus query result is stale or in the future")
            value = number(float(raw), name)
            if name.endswith("error_rate"):
                number(value, name, 0, 1)
            return value

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {name: pool.submit(metric, name) for name in METRICS}
            context_future = pool.submit(self.transport, service.evidence_url, evidence_token)
            metrics = {name: future.result() for name, future in futures.items()}
            context = context_future.result()
        return self.validate(metrics, context, now, detected_at, checked_at=now + time.perf_counter() - started)

    def validate(self, metrics, context, now, detected_at=None, checked_at=None):
        service, policy = self.service, self.service.policy
        if context.get("scope") != asdict(service.scope) or context.get("mode") != service.mode:
            raise EvidenceUnavailable("Evidence scope or provenance does not match configuration")
        observed = number(context.get("observed_at"), "context observed_at")
        if not -1 <= (checked_at if checked_at is not None else now) - observed <= policy["max_sample_age_seconds"]:
            raise EvidenceUnavailable("Deployment/trace evidence is stale or future-dated")
        if metrics["sample_age_seconds"] > policy["max_sample_age_seconds"]:
            raise EvidenceUnavailable("Underlying metric samples are stale")
        if context.get("window_seconds") != policy["window_seconds"]:
            raise EvidenceUnavailable("Evidence and configured metric windows differ")
        deployment = {key: context["deployment"].get(key) for key in
            ("revision", "previous_revision", "known_good", "schema_migration", "rollout_complete", "changed_at", "traffic_percent")}
        traces = {key: context["traces"].get(key) for key in ("downstream_healthy", "failing_span", "sample_count")}
        for name in ("revision", "previous_revision"):
            if not isinstance(deployment.get(name), str) or not 0 < len(deployment[name]) <= 200:
                raise EvidenceUnavailable("Missing deployment revision")
        for name in ("known_good", "schema_migration", "rollout_complete"):
            if type(deployment.get(name)) is not bool:
                raise EvidenceUnavailable("Deployment compatibility must be explicitly reported")
        changed = number(deployment.get("changed_at"), "changed_at", 0, now)
        traffic = number(deployment.get("traffic_percent"), "traffic_percent", .000001, 100)
        if type(traces.get("downstream_healthy")) is not bool or not isinstance(traces.get("failing_span"), str) or len(traces["failing_span"]) > 500:
            raise EvidenceUnavailable("Missing trace dependency result")
        number(traces.get("sample_count"), "trace sample count", 1)
        data = {"metrics": metrics, "traces": traces,
                "changes": {"seconds_before_alert": (detected_at if detected_at is not None else now) - changed, "schema_migration": deployment["schema_migration"]}}
        evidence = [{"id": digest([asdict(service.scope), now, kind, value]), "kind": kind,
                     "scope": asdict(service.scope), "source": service.mode + "/" + ("prometheus" if kind == "metrics" else "evidence-gateway"),
                     "observed_at": min(observed, now - metrics["sample_age_seconds"]),
                     "window_start": now - policy["window_seconds"], "window_end": now,
                     "data": value} for kind, value in data.items()]
        return {"collected_at": now, "evidence": evidence, "metrics": metrics, "traces": traces,
                "deployment": {**deployment, "traffic_percent": traffic}, "mode": service.mode,
                "scope": asdict(service.scope)}


def candidate_blocker(snapshot, service):
    m, d, t = snapshot["metrics"], snapshot["deployment"], snapshot["traces"]
    if min(m["requests"], m["control_requests"]) < service.policy["min_requests"]:
        return "灰度或对照组样本不足，先核实请求量和采集范围。"
    if m["qps"] < service.baseline_qps * service.policy["min_qps_ratio"]:
        return "流量不足，不能据此确认版本回归或恢复。"
    if not t["downstream_healthy"]:
        return "下游存在异常，请先联系下游 Owner 并检查依赖。"
    if d["schema_migration"] or not d["known_good"] or d["revision"] == d["previous_revision"]:
        return "回退兼容性或已知稳定版本不满足条件，请按人工 Runbook 处置。"
    if not d["rollout_complete"] or d["traffic_percent"] > 5:
        return "发布尚未完成或灰度范围超过 5%，需要人工评估。"
    changes = next(e["data"] for e in snapshot["evidence"] if e["kind"] == "changes")
    if not 0 <= changes["seconds_before_alert"] <= 900:
        return "发布与异常的时间关系不足以支持当前回滚建议。"
    if m["error_rate"] <= .05 or m["control_error_rate"] >= .01:
        return "尚未观察到稳定组健康且灰度组显著退化的证据。"
    return None
