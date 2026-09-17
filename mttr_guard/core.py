"""Persistent incident workflow with a transactionally isolated SIMULATED executor.

The executor only changes the local SQLite simulator; no production write adapter
is installed. SQLite transactions do not provide atomicity for external systems.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from .agents import RuleReasoner, investigate


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Rejected(ValueError):
    pass


@dataclass(frozen=True)
class Scope:
    tenant: str
    environment: str
    cluster: str
    namespace: str
    service: str

    @property
    def key(self):
        return canonical(asdict(self))


@dataclass(frozen=True)
class Principal:
    """Trusted in-process identity for demo/tests. Production must use verified IAM."""
    subject: str
    role: str
    scope: Scope


RUNBOOK = {
    "id": "rollback-canary-v1", "action": "rollback_canary",
    "max_traffic_percent": 5, "policy_version": "2026-09-17.1",
    "verification": {"max_error_rate": .01, "max_p99_ms": 500, "min_qps_ratio": .8,
                     "consecutive_samples": 3, "min_interval_seconds": 30, "min_window_seconds": 60},
}


class Engine:
    def __init__(self, path: str, clock=time.time):
        self.path, self.clock = str(path), clock
        with self.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, dedup TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS services (scope TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, incident TEXT NOT NULL,
                    at REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO config VALUES ('kill_switch', 'off');
            """)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, iid, kind, body):
        db.execute("INSERT INTO events(incident,at,kind,body) VALUES (?,?,?,?)", (iid, self.clock(), kind, canonical(body)))

    def _load(self, db, iid, scope):
        row = db.execute("SELECT body FROM incidents WHERE id=? AND scope=?", (iid, scope.key)).fetchone()
        if not row:
            raise Rejected("Incident not found in authorized scope")
        return json.loads(row[0])

    def _save(self, db, incident):
        db.execute("UPDATE incidents SET state=?,body=? WHERE id=?", (incident["state"], canonical(incident), incident["id"]))

    def _service(self, db, scope):
        row = db.execute("SELECT body FROM services WHERE scope=?", (scope.key,)).fetchone()
        if not row:
            raise Rejected("No telemetry adapter for this scope")
        return json.loads(row[0])

    def _write_service(self, db, scope, service):
        db.execute("INSERT INTO services(scope,body) VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET body=excluded.body", (scope.key, canonical(service)))

    def seed_simulator(self, scope: Scope, scenario="release"):
        """Explicit fixture control; not an alert ingestion or production API."""
        if scenario not in {"release", "downstream", "migration", "missing"}:
            raise ValueError("Unknown scenario")
        with self.tx() as db:
            if db.execute("SELECT 1 FROM services WHERE scope=?", (scope.key,)).fetchone():
                raise Rejected("Simulator already seeded; use a fresh DB or a new scope")
            self._write_service(db, scope, {
                "revision": "r42", "previous_revision": "r41", "known_good": True,
                "traffic_percent": 5, "baseline_qps": 1000, "observed_at": self.clock(),
                "metrics": {"error_rate": .18, "control_error_rate": .002, "p99_ms": 2400, "qps": 1000},
                "traces": {"downstream_healthy": scenario != "downstream", "failing_span": "checkout.validate"},
                "changes": {"seconds_before_alert": 180, "schema_migration": scenario == "migration"},
                "missing": scenario == "missing", "executions": 0,
            })

    def ingest(self, scope: Scope, fingerprint: str, starts_at: float) -> str:
        """One incident per scoped alert episode. Aggregation is a production extension."""
        now = self.clock()
        if not fingerprint or len(fingerprint) > 256 or not isinstance(starts_at, (int, float)) or not 0 <= starts_at <= now:
            raise Rejected("Invalid alert identity or time")
        key = digest([scope.key, fingerprint, starts_at])
        with self.tx() as db:
            row = db.execute("SELECT id FROM incidents WHERE dedup=?", (key,)).fetchone()
            if row:
                return row[0]
            iid = uuid.uuid4().hex
            incident = {"id": iid, "scope": asdict(scope), "state": "NEW", "detected_at": starts_at,
                        "ingested_at": now, "findings": [], "evidence": [], "plan": None,
                        "approval": None, "receipt": None, "observations": [], "revision": 0}
            db.execute("INSERT INTO incidents VALUES (?,?,?,?,?)", (iid, scope.key, key, "NEW", canonical(incident)))
            self.event(db, iid, "ALERT_INGESTED", {"scope": asdict(scope), "fingerprint": fingerprint})
            return iid

    def get(self, iid: str, scope: Scope):
        with self.tx() as db:
            return self._load(db, iid, scope)

    def events(self, iid: str, scope: Scope):
        with self.tx() as db:
            self._load(db, iid, scope)
            return [{**dict(r), "body": json.loads(r["body"])} for r in db.execute("SELECT * FROM events WHERE incident=? ORDER BY seq", (iid,))]

    async def diagnose(self, iid: str, scope: Scope, reasoner=None):
        reasoner = reasoner or RuleReasoner()
        with self.tx() as db:
            inc = self._load(db, iid, scope)
            if inc["state"] not in {"NEW", "DIAGNOSING"}:
                return inc
            service = self._service(db, scope)
            inc["state"] = "DIAGNOSING"
            inc["revision"] += 1
            generation = inc["revision"]
            evidence = [{"id": digest([iid, generation, kind])[:20], "kind": kind,
                         "scope": asdict(scope), "observed_at": service["observed_at"],
                         "source": "SIMULATED/" + kind, "data": service[kind]}
                        for kind in ("metrics", "traces", "changes") if not (service["missing"] and kind == "traces")]
            inc["evidence"] = evidence
            self._save(db, inc)
            self.event(db, iid, "DIAGNOSIS_STARTED", {"generation": generation, "reasoner": reasoner.name})
        findings = await investigate(reasoner, evidence)
        with self.tx() as db:
            inc = self._load(db, iid, scope)
            if inc["revision"] != generation or inc["state"] != "DIAGNOSING":
                return inc  # A newer diagnostic generation owns the decision.
            inc["findings"] = [asdict(f) for f in findings]
            current = self._service(db, scope)
            expected = {"metrics": "regression", "traces": "local_failure", "changes": "recent_change", "critic": "candidate"}
            reason = self._candidate_blocker(current, evidence)
            if current["revision"] != service["revision"]:
                reason = "Deployment changed during diagnosis"
            if any(f.verdict != expected[f.agent] for f in findings):
                reason = reason or "Diagnostic disagreement, missing evidence or agent failure"
            if reason:
                inc["state"], inc["escalation_reason"] = "ESCALATED", reason
            else:
                plan = {"scope": asdict(scope), "action": "rollback_canary", "expected_revision": current["revision"],
                        "target_revision": current["previous_revision"], "traffic_percent": current["traffic_percent"],
                        "runbook": json.loads(canonical(RUNBOOK)), "evidence_digest": digest(evidence), "created_at": self.clock(),
                        "expires_at": self.clock() + 300, "proposer": "agent:planner", "risk": "medium"}
                inc["plan"] = {**plan, "hash": digest(plan)}
                inc["state"] = "AWAITING_APPROVAL"
            inc["diagnosed_at"] = self.clock()
            self._save(db, inc)
            self.event(db, iid, inc["state"], {"findings": inc["findings"], "plan": inc["plan"], "reason": reason})
            return inc

    def _candidate_blocker(self, service, evidence):
        if {e["kind"] for e in evidence} != {"metrics", "traces", "changes"}:
            return "Missing evidence source"
        if any(not 0 <= self.clock() - e["observed_at"] <= 300 for e in evidence):
            return "Stale or future evidence"
        m, c = service["metrics"], service["changes"]
        if m["error_rate"] <= .05 or m["control_error_rate"] >= .01 or m["qps"] < service["baseline_qps"] * .8:
            return "No isolated, sufficiently sampled canary regression"
        if not service["traces"]["downstream_healthy"]:
            return "Downstream regression; do not rollback an unrelated service"
        if c["schema_migration"] or not service["known_good"] or not 0 <= c["seconds_before_alert"] <= 900:
            return "Rollback compatibility or known-good revision unverified"
        if not 0 < service["traffic_percent"] <= RUNBOOK["max_traffic_percent"]:
            return "Blast radius exceeds policy"
        return None

    def _authorize(self, actor, scope, role):
        if actor.scope != scope or actor.role != role or not actor.subject:
            raise Rejected("Identity role or scope denied")

    def _validate_plan(self, inc):
        plan = inc["plan"]
        if not plan or plan["hash"] != digest({k: v for k, v in plan.items() if k != "hash"}):
            raise Rejected("Plan integrity check failed")
        if plan["scope"] != inc["scope"] or plan["evidence_digest"] != digest(inc["evidence"]):
            raise Rejected("Plan scope or evidence binding changed")
        if plan["runbook"] != RUNBOOK or plan["action"] != "rollback_canary":
            raise Rejected("Unknown runbook or policy revision")
        if self.clock() >= plan["expires_at"]:
            raise Rejected("Plan expired; a fresh review is required")
        return plan

    def approve(self, iid: str, scope: Scope, actor: Principal, plan_hash: str):
        self._authorize(actor, scope, "approver")
        with self.tx() as db:
            inc = self._load(db, iid, scope)
            plan = self._validate_plan(inc)
            if inc["state"] != "AWAITING_APPROVAL" or plan_hash != plan["hash"] or actor.subject == plan["proposer"]:
                raise Rejected("Approval does not match a pending, independently proposed plan")
            inc["approval"] = {"subject": actor.subject, "plan_hash": plan_hash, "at": self.clock()}
            inc["state"] = "APPROVED"
            self._save(db, inc)
            self.event(db, iid, "APPROVED", inc["approval"])
            return inc

    def set_kill_switch(self, enabled: bool):
        """Operator-only in-process demo control. Do not expose without IAM."""
        with self.tx() as db:
            db.execute("UPDATE config SET value=? WHERE key='kill_switch'", ("on" if enabled else "off",))
            self.event(db, "SYSTEM", "KILL_SWITCH", {"enabled": enabled})

    def execute(self, iid: str, scope: Scope, actor: Principal, plan_hash: str):
        self._authorize(actor, scope, "executor")
        with self.tx() as db:
            inc = self._load(db, iid, scope)
            if inc["receipt"]:
                if inc["receipt"]["plan_hash"] != plan_hash:
                    raise Rejected("Idempotency key mismatch")
                return inc["receipt"]
            if db.execute("SELECT value FROM config WHERE key='kill_switch'").fetchone()[0] == "on":
                raise Rejected("Global execution kill switch is on")
            plan = self._validate_plan(inc)
            if inc["state"] != "APPROVED" or plan_hash != plan["hash"] or inc["approval"]["plan_hash"] != plan_hash:
                raise Rejected("Execution requires approval bound to this plan")
            if actor.subject == inc["approval"]["subject"]:
                raise Rejected("Approver cannot be the executor identity")
            service = self._service(db, scope)
            reason = self._candidate_blocker(service, inc["evidence"])
            if reason or service["revision"] != plan["expected_revision"] or service["previous_revision"] != plan["target_revision"] or service["traffic_percent"] != plan["traffic_percent"]:
                raise Rejected(reason or "Deployment drift invalidated approval")
            if not 0 <= self.clock() - service["observed_at"] <= 60:
                raise Rejected("Preflight telemetry is stale")
            # Simulator ONLY: update, execution receipt and workflow transition are atomic.
            # A real adapter needs remote idempotency + precondition CAS + reconciliation.
            service["revision"] = plan["target_revision"]
            service["executions"] += 1
            self._write_service(db, scope, service)
            inc["receipt"] = {"mode": "SIMULATED", "plan_hash": plan_hash,
                              "idempotency_key": digest([iid, plan_hash]), "executed_at": self.clock(),
                              "revision": service["revision"]}
            inc["state"] = "VERIFYING"
            self._save(db, inc)
            self.event(db, iid, "SIMULATED_ACTION_EXECUTED", inc["receipt"])
            return inc["receipt"]

    def simulate_observation(self, scope: Scope, healthy: bool, qps: float = 1000):
        """Advance independent simulated telemetry; execution does not auto-heal metrics."""
        with self.tx() as db:
            service = self._service(db, scope)
            service["observed_at"] = self.clock()
            service["metrics"].update(error_rate=.003 if healthy else .18, p99_ms=180 if healthy else 2400, qps=qps)
            self._write_service(db, scope, service)

    def verify(self, iid: str, scope: Scope):
        with self.tx() as db:
            inc = self._load(db, iid, scope)
            if inc["state"] != "VERIFYING":
                raise Rejected("Incident is not awaiting recovery verification")
            service = self._service(db, scope)
            now, stamp = self.clock(), service["observed_at"]
            execution_at = inc["receipt"]["executed_at"]
            if now - execution_at > 600:
                inc["state"], inc["escalation_reason"] = "ESCALATED", "Verification timed out; no automatic second action"
            elif stamp <= execution_at or not 0 <= now - stamp <= 60:
                raise Rejected("Recovery needs fresh, post-execution telemetry")
            elif inc["observations"] and stamp - inc["observations"][-1]["at"] < 30:
                raise Rejected("Duplicate or too closely spaced telemetry window")
            else:
                m, v = service["metrics"], RUNBOOK["verification"]
                healthy = (m["error_rate"] < v["max_error_rate"] and m["p99_ms"] < v["max_p99_ms"]
                           and m["control_error_rate"] < v["max_error_rate"]
                           and m["qps"] >= service["baseline_qps"] * v["min_qps_ratio"]
                           and service["revision"] == inc["receipt"]["revision"])
                inc["observations"].append({"at": stamp, "healthy": healthy, "metrics": m})
                tail = inc["observations"][-v["consecutive_samples"]:]
                contiguous = all(b["at"] - a["at"] <= 60 for a, b in zip(tail, tail[1:]))
                if len(tail) == v["consecutive_samples"] and all(o["healthy"] for o in tail) and contiguous and tail[-1]["at"] - tail[0]["at"] >= v["min_window_seconds"]:
                    inc["state"] = "RECOVERED"
                    inc["recovered_at"] = stamp
                    inc["recovery_seconds"] = stamp - inc["detected_at"]
            self._save(db, inc)
            self.event(db, iid, "VERIFICATION", {"state": inc["state"], "observations": inc["observations"][-3:]})
            return inc
