"""Explicit operator configuration; alerts and model outputs cannot supply URLs/queries."""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .core import Scope, digest


METRICS = ("error_rate", "control_error_rate", "p99_ms", "qps", "requests", "control_requests", "sample_age_seconds")
DEFAULT_POLICY = {"max_error_rate": .01, "max_p99_ms": 500, "min_qps_ratio": .8,
                  "min_requests": 100, "window_seconds": 60, "consecutive_windows": 3,
                  "max_sample_age_seconds": 30, "observation_timeout_seconds": 1800}


def number(value, name, low=0, high=float("inf")):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Invalid numeric field: {name}")
    return value


def endpoint(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Endpoints must be configured HTTP(S) URLs without inline credentials")
    return value.rstrip("/")


@dataclass(frozen=True)
class Identity:
    subject: str
    role: str
    service: str
    token_hash: str


@dataclass(frozen=True)
class Service:
    name: str
    scope: Scope
    owner: str
    prometheus_url: str
    evidence_url: str
    queries: dict
    baseline_qps: float
    policy: dict
    links: dict
    runbook: list[str]
    prometheus_token_env: str | None = None
    evidence_token_env: str | None = None
    mode: str = "READ_ONLY"

    @property
    def collection_hash(self):
        return digest({key: value for key, value in asdict(self).items() if key not in {"owner", "links", "runbook", "prometheus_token_env", "evidence_token_env"}})


class Config:
    def __init__(self, data):
        self.services = {}
        seen_scopes = set()
        for item in data["services"]:
            scope = Scope(**item["scope"])
            if not all(isinstance(v, str) and v.strip() for v in item["scope"].values()):
                raise ValueError("All five scope fields must be nonempty strings")
            name = item["id"]
            if not isinstance(name, str) or not name or name in self.services or scope.key in seen_scopes:
                raise ValueError("Service IDs and scopes must be unique")
            seen_scopes.add(scope.key)
            queries = item["queries"]
            if set(queries) != set(METRICS) or any(not isinstance(q, str) or not q.strip() or len(q) > 8192 for q in queries.values()):
                raise ValueError("Configure one bounded, scope-specific PromQL expression per required metric")
            policy = {**DEFAULT_POLICY, **item.get("policy", {})}
            if set(policy) != set(DEFAULT_POLICY):
                raise ValueError("Unknown recovery policy field")
            for key, value in policy.items():
                number(value, key, .000001)
            number(policy["max_error_rate"], "max_error_rate", 0, 1)
            number(policy["min_qps_ratio"], "min_qps_ratio", 0, 1)
            for key in ("window_seconds", "consecutive_windows"):
                if type(policy[key]) is not int:
                    raise ValueError(f"{key} must be an integer")
            if not 2 <= policy["consecutive_windows"] <= 10 or policy["window_seconds"] < 10:
                raise ValueError("Recovery requires 2-10 non-overlapping windows of at least 10 seconds")
            if policy["observation_timeout_seconds"] < policy["window_seconds"] * policy["consecutive_windows"]:
                raise ValueError("Observation timeout is shorter than recovery window")
            mode = item.get("mode", "READ_ONLY")
            if mode not in {"READ_ONLY", "DEMO"}:
                raise ValueError("Pilot supports only read-only or explicitly demo evidence")
            links = {name: endpoint(url) for name, url in item.get("links", {}).items()}
            owner, runbook = item["owner"], item["runbook"]
            if not isinstance(owner, str) or not owner.strip() or not isinstance(runbook, list) or not runbook or not all(isinstance(s, str) and s.strip() for s in runbook):
                raise ValueError("A service needs an owner and human-reviewed runbook steps")
            prometheus_token_env, evidence_token_env = item.get("prometheus_token_env"), item.get("evidence_token_env")
            for token_env in (prometheus_token_env, evidence_token_env):
                if token_env and not os.environ.get(token_env):
                    raise ValueError("Configured connector credential environment variable is missing")
            self.services[name] = Service(name, scope, owner, endpoint(item["prometheus_url"]),
                endpoint(item["evidence_url"]), queries, number(item["baseline_qps"], "baseline_qps", .000001),
                policy, links, runbook, prometheus_token_env, evidence_token_env, mode)
        if not self.services:
            raise ValueError("Configure at least one service")
        self.identities = []
        self.oidc = data.get("oidc")
        if self.oidc:
            if not isinstance(self.oidc.get("issuer"), str) or not self.oidc["issuer"].startswith("https://") or not self.oidc.get("audience"):
                raise ValueError("OIDC requires a fixed HTTPS issuer and API audience")
            if not endpoint(self.oidc["jwks_url"]).startswith("https://"):
                raise ValueError("OIDC signing keys must use HTTPS")
            number(self.oidc.get("max_token_seconds", 900), "max_token_seconds", 60, 3600)
            bindings = set()
            for binding in self.oidc["bindings"]:
                if not isinstance(binding["subject"], str) or not binding["subject"] or binding["service"] not in self.services or binding["role"] not in {"viewer", "operator"}:
                    raise ValueError("Invalid OIDC subject binding")
                key = binding["subject"], binding["service"]
                if key in bindings:
                    raise ValueError("Duplicate OIDC binding")
                bindings.add(key)
        token_hashes = set()
        for item in data.get("identities", []):
            if item["service"] not in self.services or item["role"] not in {"receiver", "viewer", "operator"} or not isinstance(item["subject"], str) or not 1 <= len(item["subject"]) <= 300:
                raise ValueError("Identity must have a configured service, subject and role")
            if bool(item.get("token_env")) == bool(item.get("token_file")):
                raise ValueError("Configure exactly one token_env or token_file per identity")
            token = Path(item["token_file"]).read_text(encoding="utf-8").strip() if item.get("token_file") else os.environ.get(item["token_env"], "")
            if len(token) < 32:
                raise ValueError("Identity tokens must be configured in environment variables (at least 32 characters)")
            hashed = hashlib.sha256(token.encode()).hexdigest()
            if hashed in token_hashes:
                raise ValueError("Identity tokens must be distinct")
            token_hashes.add(hashed)
            self.identities.append(Identity(item["subject"], item["role"], item["service"], hashed))
        if not self.identities and not self.oidc:
            raise ValueError("Configure at least one identity")

    @classmethod
    def read(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))
