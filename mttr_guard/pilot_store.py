"""Single-host durable inbox, leased read-only jobs and measured human handoffs."""
from __future__ import annotations

import json
import logging
import math
import uuid
from contextlib import contextmanager
from dataclasses import asdict

from .alertmanager import normalize_webhook
from .core import Engine, Rejected, canonical, digest
from . import audit


class QueueFull(Rejected):
    pass


class PilotStore(Engine):
    def __init__(self, path, clock=None, audit_key=None, audit_cache_bytes=16 * 1024 * 1024):
        self.audit_key = audit_key
        self._audit_cache = audit.VerificationCache(audit_key, audit_cache_bytes)
        self._audit_ready = False
        self._integrity_failed = False
        super().__init__(path, **({"clock": clock} if clock else {}))
        with self.tx() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS pilot_jobs (
                    incident TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                    due REAL NOT NULL, lease TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT);
                CREATE INDEX IF NOT EXISTS pilot_jobs_due ON pilot_jobs(state, due);
                CREATE TABLE IF NOT EXISTS pilot_requests (
                    incident TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(incident, request_id));
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, queue TEXT NOT NULL, seen REAL NOT NULL, state TEXT NOT NULL)")
            audit.initialize(db, self.audit_key)
            audit.verify(db, self.audit_key, cache=self._audit_cache)
        self._audit_ready = True

    @contextmanager
    def tx(self):
        # BEGIN IMMEDIATE precedes verification: another connection cannot
        # change verified input before this transaction reads or signs it.
        # Scan the entire history, including on reads and worker heartbeats.
        # Only byte-for-byte identical, previously verified rows reuse crypto
        # results; the mutable audit_head alone can never authorize a cache hit.
        if self._integrity_failed:
            raise audit.IntegrityError("Audit integrity failed; operator recovery required")
        with super().tx() as db:
            if self._integrity_failed:
                raise audit.IntegrityError("Audit integrity failed; operator recovery required")
            if self._audit_ready:
                try:
                    audit.verify(db, self.audit_key, cache=self._audit_cache)
                except ValueError as exc:
                    self._integrity_failed = True
                    logging.getLogger(__name__).critical(
                        "audit_integrity_failed: storage quarantined; operator recovery required")
                    raise audit.IntegrityError("Audit integrity failed; operator recovery required") from exc
            yield db

    def _save(self, db, incident):
        super()._save(db, incident)
        audit.append(db, self.audit_key, self.clock(), "STATE", incident["scope"], None,
                     {"incident": incident["id"], "body_hash": digest(incident)})

    def event(self, db, iid, kind, body):
        super().event(db, iid, kind, body)
        row = db.execute("SELECT seq,incident,at,kind,body FROM events WHERE seq=last_insert_rowid()").fetchone()
        inc = db.execute("SELECT scope FROM incidents WHERE id=?", (iid,)).fetchone()
        audit.append(db, self.audit_key, row["at"], "WORKFLOW", json.loads(inc[0]) if inc else None, body.get("subject"),
                     {"event_seq": row["seq"], "event_hash": digest([row["incident"], row["at"], row["kind"], json.loads(row["body"])])})

    def access_audit(self, subject, scope, route, status, request_id):
        with self.tx() as db:
            audit.append(db, self.audit_key, self.clock(), "ACCESS", scope, subject,
                         {"route": route, "status": status, "request_id": request_id})

    def heartbeat(self, worker_id, queue, state):
        with self.tx() as db:
            db.execute("INSERT INTO workers VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET queue=excluded.queue,seen=excluded.seen,state=excluded.state",
                       (worker_id, queue, self.clock(), state))

    def operational(self, scope=None):
        now = self.clock()
        with self.tx() as db:
            workers = [dict(row) for row in db.execute("SELECT * FROM workers WHERE seen>=? AND state<>'STOPPED'", (now - 150,))]
            clause, args = (" AND i.scope=?", (scope.key,)) if scope else ("", ())
            jobs = [dict(row) for row in db.execute("SELECT j.kind,j.state,COUNT(*) AS count,MIN(j.due) AS oldest FROM pilot_jobs j JOIN incidents i ON i.id=j.incident WHERE j.state<>'DONE'" + clause + " GROUP BY j.kind,j.state", args)]
            for row in jobs:
                row["overdue_seconds"] = max(0, now - row.pop("oldest"))
            queues = {q for w in workers for q in (["diagnose", "observe"] if w["queue"] == "all" else [w["queue"]])}
            return {"ready": queues == {"diagnose", "observe"}, "workers": len(workers), "queues": sorted(queues), "jobs": jobs}

    def receive(self, service, payload, max_pending=1000):
        normalized = normalize_webhook(service.scope, payload, self.clock())
        firing = [a for a in payload["alerts"] if a["status"] == "firing"]
        result = []
        with self.tx() as db:
            pending = db.execute("SELECT COUNT(*) FROM pilot_jobs WHERE state<>'DONE'").fetchone()[0]
            for (fingerprint, started), alert in zip(normalized, firing):
                key = digest([service.scope.key, fingerprint, started])
                row = db.execute("SELECT id FROM incidents WHERE dedup=?", (key,)).fetchone()
                if row:
                    result.append(row[0])
                    continue
                if pending >= max_pending:
                    raise QueueFull("Incident queue is full; sender should retry")
                pending += 1
                iid, now = uuid.uuid4().hex, self.clock()
                annotations = alert.get("annotations", {})
                if not isinstance(annotations, dict):
                    raise Rejected("Invalid alert annotations")
                summary = annotations.get("summary", alert["labels"].get("alertname", fingerprint))
                if not isinstance(summary, str):
                    raise Rejected("Invalid alert summary")
                incident = {"id": iid, "scope": asdict(service.scope), "service_id": service.name,
                    "mode": service.mode, "state": "QUEUED", "summary": summary[:1000],
                    "severity": str(alert["labels"].get("severity", "unclassified"))[:64],
                    "detected_at": started, "ingested_at": now, "owner": service.owner,
                    "links": service.links, "runbook": service.runbook, "findings": [], "evidence": [],
                    "recommendation": None, "plan": None, "approval": None, "receipt": None,
                    "observations": [], "timeline": {"received": now},
                    "assignment": None, "review": None, "policy": service.policy,
                    "baseline_qps": service.baseline_qps, "collection_hash": service.collection_hash}
                db.execute("INSERT INTO incidents VALUES (?,?,?,?,?)", (iid, service.scope.key, key, incident["state"], canonical(incident)))
                self._save(db, incident)
                db.execute("INSERT INTO pilot_jobs(incident,kind,state,due) VALUES (?,?,?,?)", (iid, "diagnose", "READY", now))
                self.event(db, iid, "ALERT_RECEIVED", {"summary": incident["summary"], "mode": service.mode})
                result.append(iid)
        return result

    def list_incidents(self, scope, limit=100, before=None):
        with self.tx() as db:
            rows = db.execute("SELECT rowid,body FROM incidents WHERE scope=? AND rowid<? ORDER BY rowid DESC LIMIT ?",
                (scope.key, before or 9_223_372_036_854_775_807, limit)).fetchall()
            return [{**json.loads(row["body"]), "cursor": row["rowid"]} for row in rows]

    def claim(self, lease_seconds=120, queue="all"):
        if queue not in {"all", "diagnose", "observe"}:
            raise ValueError("Invalid worker queue")
        now = self.clock()
        with self.tx() as db:
            row = db.execute("SELECT * FROM pilot_jobs WHERE ((state='READY' AND due<=?) OR (state='RUNNING' AND lease_until<=?)) AND (?='all' OR kind=?) ORDER BY CASE kind WHEN 'observe' THEN 0 ELSE 1 END,due LIMIT 1", (now, now, queue, queue)).fetchone()
            if not row:
                return None
            lease = uuid.uuid4().hex
            db.execute("UPDATE pilot_jobs SET state='RUNNING',lease=?,lease_until=?,attempts=attempts+1 WHERE incident=?", (lease, now + lease_seconds, row["incident"]))
            job = {**dict(row), "lease": lease, "attempts": row["attempts"] + 1}
            incident = json.loads(db.execute("SELECT body FROM incidents WHERE id=?", (row["incident"],)).fetchone()[0])
            self.event(db, incident["id"], "JOB_STARTED", {"kind": job["kind"], "attempt": job["attempts"]})
            return job, incident

    def _owns(self, db, job):
        return db.execute("SELECT 1 FROM pilot_jobs WHERE incident=? AND state='RUNNING' AND lease=? AND lease_until>?",
                          (job["incident"], job["lease"], self.clock())).fetchone() is not None

    def finish_diagnosis(self, job, scope, snapshot, findings, blocker):
        with self.tx() as db:
            if not self._owns(db, job):
                return False
            inc = self._load(db, job["incident"], scope)
            if inc["state"] != "QUEUED":
                return False
            now = self.clock()
            inc["evidence"], inc["findings"] = snapshot["evidence"], findings
            inc["snapshot"] = snapshot
            inc["timeline"].setdefault("evidence_ready", snapshot["collected_at"])
            inc["timeline"].setdefault("analysis_ready", now)
            if blocker:
                inc["state"], inc["attention_reason"] = "NEEDS_ATTENTION", blocker
            else:
                inc["timeline"].setdefault("recommendation_ready", now)
                d = snapshot["deployment"]
                recommendation = {"action": "rollback_canary", "expected_revision": d["revision"],
                    "target_revision": d["previous_revision"], "traffic_percent": d["traffic_percent"],
                    "scope": inc["scope"], "evidence_digest": digest(snapshot["evidence"]),
                    "policy": inc["policy"], "created_at": now, "expires_at": now + 300,
                    "instructions": "请在现有发布平台核对当前版本、迁移兼容性和影响范围，再按已审核 Runbook 操作。此服务不会执行发布。"}
                inc["recommendation"] = {**recommendation, "hash": digest(recommendation)}
                inc["state"] = "RECOMMENDED"
            self._save(db, inc)
            self.event(db, inc["id"], "EVIDENCE_READY", {"evidence_ids": [e["id"] for e in snapshot["evidence"]]})
            self.event(db, inc["id"], inc["state"], {"reason": blocker, "recommendation": inc["recommendation"]})
            db.execute("UPDATE pilot_jobs SET state='DONE',lease=NULL,lease_until=NULL WHERE incident=?", (inc["id"],))
            return True

    def fail_job(self, job, scope, error_type):
        with self.tx() as db:
            if not self._owns(db, job):
                return
            inc = self._load(db, job["incident"], scope)
            now = self.clock()
            self.event(db, inc["id"], "COLLECTION_FAILED", {"error_type": error_type, "attempt": job["attempts"]})
            if job["kind"] == "observe":
                # Missing evidence breaks the recovery streak, but observation continues to its deadline.
                inc["healthy_streak"] = 0
                expired = now >= inc["observation_deadline"]
                delay = inc["policy"]["window_seconds"]
            else:
                expired = job["attempts"] >= 3
                delay = min(60, 5 * 2 ** (job["attempts"] - 1))
            if expired:
                inc["state"] = "NEEDS_ATTENTION"
                inc["attention_reason"] = "证据采集失败或恢复观察超时；请通过服务链接与 Runbook 继续人工处置。"
            self._save(db, inc)
            db.execute("UPDATE pilot_jobs SET state=?,due=?,lease=NULL,lease_until=NULL,last_error=? WHERE incident=?",
                       ("DONE" if expired else "READY", now + delay, error_type, inc["id"]))

    def operate(self, iid, service, identity, action, body):
        if identity.role != "operator" or identity.service != service.name:
            raise Rejected("Operator role required")
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise Rejected("A bounded request_id is required")
        request_hash = digest([identity.subject, action, body])
        with self.tx() as db:
            inc = self._load(db, iid, service.scope)
            old = db.execute("SELECT digest FROM pilot_requests WHERE incident=? AND request_id=?", (iid, request_id)).fetchone()
            if old:
                if old[0] != request_hash:
                    raise Rejected("Request ID reused for different input")
                return inc
            now = self.clock()
            if action == "ack":
                if inc["assignment"] and inc["assignment"]["subject"] != identity.subject:
                    raise Rejected("Another operator has acknowledged this incident")
                inc["assignment"] = {"subject": identity.subject, "at": now}
                inc["timeline"].setdefault("acknowledged", now)
            elif action == "review":
                rec = inc["recommendation"]
                if inc["state"] != "RECOMMENDED" or not rec or body.get("plan_hash") != rec["hash"]:
                    raise Rejected("Review must bind the current recommendation")
                if body.get("decision") not in {"accept", "reject"}:
                    raise Rejected("Review decision must be accept or reject")
                if body["decision"] == "accept" and now >= rec["expires_at"]:
                    raise Rejected("Recommendation expired; refresh evidence first")
                reason = self._reason(body)
                if inc["review"]:
                    raise Rejected("Recommendation already reviewed; refresh for a new review")
                inc["review"] = {"subject": identity.subject, "at": now, "plan_hash": rec["hash"], "decision": body["decision"], "reason": reason}
                inc["timeline"].setdefault("reviewed", now)
                if body["decision"] == "reject":
                    inc["state"] = "NEEDS_ATTENTION"
                    inc["attention_reason"] = "值班人员拒绝建议：" + reason
            elif action == "refresh":
                if inc["state"] not in {"RECOMMENDED", "NEEDS_ATTENTION"}:
                    raise Rejected("Only inactive recommendations can be refreshed")
                self._reason(body)
                inc["state"], inc["recommendation"], inc["review"] = "QUEUED", None, None
                inc.pop("attention_reason", None)
                db.execute("INSERT INTO pilot_jobs(incident,kind,state,due) VALUES (?,'diagnose','READY',?) ON CONFLICT(incident) DO UPDATE SET kind='diagnose',state='READY',due=excluded.due,lease=NULL,lease_until=NULL,attempts=0,last_error=NULL", (iid, now))
            elif action == "observe":
                if inc["state"] not in {"RECOMMENDED", "NEEDS_ATTENTION"}:
                    raise Rejected("Incident cannot begin recovery observation in its current state")
                reason = self._reason(body)
                target = body.get("target_revision")
                if not isinstance(target, str) or not 1 <= len(target) <= 200:
                    raise Rejected("Report the version actually expected after the manual action")
                inc["manual_action"] = {"subject": identity.subject, "reported_at": now, "reason": reason, "target_revision": target}
                inc["timeline"].setdefault("mitigation_reported", now)
                inc["observation_started_at"] = now
                inc["observation_deadline"] = now + inc["policy"]["observation_timeout_seconds"]
                inc["healthy_streak"], inc["observations"], inc["state"] = 0, [], "OBSERVING"
                inc.pop("attention_reason", None)
                db.execute("INSERT INTO pilot_jobs(incident,kind,state,due) VALUES (?,'observe','READY',?) ON CONFLICT(incident) DO UPDATE SET kind='observe',state='READY',due=excluded.due,lease=NULL,lease_until=NULL,attempts=0,last_error=NULL", (iid, now + inc["policy"]["window_seconds"]))
            elif action == "pause":
                if inc["state"] != "OBSERVING":
                    raise Rejected("Only active observation can be paused")
                inc["state"], inc["attention_reason"] = "NEEDS_ATTENTION", "人工接管：" + self._reason(body)
                db.execute("UPDATE pilot_jobs SET state='DONE',lease=NULL,lease_until=NULL WHERE incident=?", (iid,))
            elif action == "handoff":
                reason = self._reason(body)
                previous = inc["assignment"]["subject"] if inc["assignment"] else None
                target = body.get("assignee")
                if body.get("expected_assignee") != previous or not isinstance(target, str) or not 1 <= len(target) <= 300:
                    raise Rejected("Assignment changed or invalid handoff target")
                inc["assignment"] = {"subject": target, "at": now, "from": previous, "reason": reason}
            else:
                raise Rejected("Unknown operator action")
            self._save(db, inc)
            db.execute("INSERT INTO pilot_requests VALUES (?,?,?)", (iid, request_id, request_hash))
            self.event(db, iid, "OPERATOR_" + action.upper(), {"subject": identity.subject, "request_id": request_id,
                "reason": body.get("reason"), "decision": body.get("decision"), "plan_hash": body.get("plan_hash"), "target_revision": body.get("target_revision"), "assignee": body.get("assignee")})
            return inc

    @staticmethod
    def _reason(body):
        reason = body.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise Rejected("A bounded human explanation is required")
        return reason.strip()

    def observe(self, job, scope, snapshot):
        with self.tx() as db:
            if not self._owns(db, job):
                return False
            inc = self._load(db, job["incident"], scope)
            if inc["state"] != "OBSERVING":
                return False
            now, p = self.clock(), inc["policy"]
            stamp, m, d = snapshot["collected_at"], snapshot["metrics"], snapshot["deployment"]
            previous = inc["observations"][-1]["at"] if inc["observations"] else inc["observation_started_at"]
            if stamp - previous < p["window_seconds"] or stamp > now or now - stamp > p["max_sample_age_seconds"]:
                raise Rejected("Recovery requires fresh, non-overlapping windows after the manual action")
            reasons = []
            checks = {"错误率未恢复": m["error_rate"] < p["max_error_rate"],
                "对照组不健康": m["control_error_rate"] < p["max_error_rate"],
                "P99 未恢复": m["p99_ms"] < p["max_p99_ms"],
                "流量不足": m["qps"] >= inc["baseline_qps"] * p["min_qps_ratio"],
                "样本不足": min(m["requests"], m["control_requests"]) >= p["min_requests"],
                "目标版本未生效": d["revision"] == inc["manual_action"]["target_revision"] and d["rollout_complete"],
                "下游不健康": snapshot["traces"]["downstream_healthy"],
                "观测窗口中断": stamp - previous <= p["window_seconds"] * 1.5}
            reasons = [key for key, ok in checks.items() if not ok]
            healthy = not reasons
            inc["healthy_streak"] = inc.get("healthy_streak", 0) + 1 if healthy else 0
            observation = {"at": stamp, "healthy": healthy, "reasons": reasons, "metrics": m,
                           "revision": d["revision"], "evidence_digest": digest(snapshot["evidence"])}
            inc["observations"] = [*inc["observations"], observation][-20:]
            self.event(db, inc["id"], "RECOVERY_WINDOW", observation)
            if now >= inc["observation_deadline"]:
                inc["state"], inc["attention_reason"] = "NEEDS_ATTENTION", "恢复验证超时，请继续人工处置。"
            elif inc["healthy_streak"] >= p["consecutive_windows"]:
                inc["state"], inc["recovered_at"] = "RECOVERED", stamp
                inc["recovery_seconds"] = stamp - inc["detected_at"]
                inc["timeline"]["verified_recovery"] = stamp
                self.event(db, inc["id"], "RECOVERY_VERIFIED", {"ttr_seconds": inc["recovery_seconds"], "mode": inc["mode"]})
            self._save(db, inc)
            db.execute("UPDATE pilot_jobs SET state=?,due=?,lease=NULL,lease_until=NULL,attempts=0,last_error=NULL WHERE incident=?",
                ("READY" if inc["state"] == "OBSERVING" else "DONE", stamp + p["window_seconds"], inc["id"]))
            return True

    def metrics(self, scope):
        with self.tx() as db:
            items = [json.loads(row[0]) for row in db.execute("SELECT body FROM incidents WHERE scope=?", (scope.key,))]
            live_ids = {i["id"] for i in items if i.get("mode") == "READ_ONLY"}
            reviews = [json.loads(row["body"]) for row in db.execute(
                "SELECT e.incident,e.body FROM events e JOIN incidents i ON e.incident=i.id WHERE i.scope=? AND e.kind='OPERATOR_REVIEW'", (scope.key,))
                if row["incident"] in live_ids]
        # Never mix synthetic runs into real incident outcomes.
        live = [i for i in items if i.get("mode") == "READ_ONLY"]
        recovered = [i["recovery_seconds"] for i in live if i["state"] == "RECOVERED"]

        def summary(values):
            values = sorted(values)
            return {"n": len(values), "mean_seconds": sum(values) / len(values) if values else None,
                    "p50_seconds": values[math.ceil(.5 * len(values)) - 1] if values else None,
                    "p90_seconds": values[math.ceil(.9 * len(values)) - 1] if values else None}

        phases = {}
        for name, start, end in (("time_to_evidence", "received", "evidence_ready"),
                ("time_to_recommendation", "received", "recommendation_ready"),
                ("time_to_acknowledge", "received", "acknowledged"),
                ("review_wait", "recommendation_ready", "reviewed"),
                ("post_action_verification", "mitigation_reported", "verified_recovery")):
            values = [i["timeline"][end] - i["timeline"][start] for i in live
                      if start in i["timeline"] and end in i["timeline"]]
            phases[name] = summary(values)
        return {"scope": asdict(scope), "generated_at": self.clock(), "incidents": len(live),
            "recovered": len(recovered), "unresolved": len(live) - len(recovered),
            "excluded_demo_incidents": len(items) - len(live), "ttr_completed_only": summary(recovered),
            "phases": phases, "reviews": {"accepted": sum(r["decision"] == "accept" for r in reviews),
                "rejected": sum(r["decision"] == "reject" for r in reviews)},
            "caveat": "Only verified recoveries enter completed TTR; unresolved incidents are reported separately. No baseline or causal MTTR reduction is inferred."}
