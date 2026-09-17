"""Restartable read-only processing. Leases fence all worker writes after network I/O."""
import asyncio
import logging
import uuid
from dataclasses import asdict, replace

from .agents import RuleReasoner, investigate
from .collectors import Collector, EvidenceUnavailable, candidate_blocker


class Worker:
    def __init__(self, store, config, collector_factory=Collector, reasoner=None, queue="all", runner=None):
        self.store, self.config = store, config
        self.collector_factory, self.reasoner = collector_factory, reasoner or RuleReasoner()
        self.queue, self.runner, self.worker_id = queue, runner, uuid.uuid4().hex

    def run_once(self):
        config = self.config.get() if hasattr(self.config, "get") else self.config
        claimed = self.store.claim(queue=self.queue)
        if not claimed:
            return False
        job, inc = claimed
        service = config.services.get(inc["service_id"])
        if not service or asdict(service.scope) != inc["scope"] or service.mode != inc["mode"] or service.collection_hash != inc["collection_hash"]:
            # Configuration drift must not authorize querying a different service.
            from .core import Scope
            self.store.fail_job(job, Scope(**inc["scope"]), "ServiceConfigurationChanged")
            return True
        # The incident's verification policy is frozen; connector queries must retain that window.
        service = replace(service, policy=inc["policy"], baseline_qps=inc["baseline_qps"])
        try:
            if self.runner:
                output = self.runner(service, inc, self.store.clock(), job["kind"])
                snapshot = output["snapshot"]
                if snapshot["scope"] != inc["scope"] or snapshot["mode"] != inc["mode"]:
                    raise EvidenceUnavailable("Isolated result scope mismatch")
                if self.store.clock() - min(e["observed_at"] for e in snapshot["evidence"]) > service.policy["max_sample_age_seconds"]:
                    raise EvidenceUnavailable("Evidence expired during isolated work")
                if job["kind"] == "observe":
                    self.store.observe(job, service.scope, snapshot)
                else:
                    self.store.finish_diagnosis(job, service.scope, snapshot, output["findings"], output["blocker"])
                return True
            snapshot = self.collector_factory(service).collect(self.store.clock(), detected_at=inc["detected_at"] if job["kind"] == "diagnose" else None)
            if job["kind"] == "observe":
                self.store.observe(job, service.scope, snapshot)
            else:
                findings = asyncio.run(investigate(self.reasoner, snapshot["evidence"]))
                if self.store.clock() - min(e["observed_at"] for e in snapshot["evidence"]) > service.policy["max_sample_age_seconds"]:
                    raise EvidenceUnavailable("Evidence expired while reasoning")
                expected = {"metrics": "regression", "traces": "local_failure", "changes": "recent_change", "critic": "candidate"}
                blocker = candidate_blocker(snapshot, service)
                if any(f.verdict != expected[f.agent] for f in findings):
                    blocker = blocker or "分析结论不一致或模型不可用，请根据证据和 Runbook 人工判断。"
                self.store.finish_diagnosis(job, service.scope, snapshot, [asdict(f) for f in findings], blocker)
        except Exception as exc:
            # Persist type only: provider bodies may contain credentials or customer data.
            self.store.fail_job(job, service.scope, type(exc).__name__)
        return True

    def run(self, stop):
        while not stop.is_set():
            try:
                self.store.heartbeat(self.worker_id, self.queue, "RUNNING")
                with self.store.tx() as db:
                    db.execute("INSERT INTO config(key,value) VALUES ('pilot_worker_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(self.store.clock()),))
                worked = self.run_once()
            except Exception as exc:
                # SQLite contention/unavailability must not silently kill the worker.
                logging.getLogger("mttr_guard.worker").error("worker_loop_failed type=%s", type(exc).__name__)
                worked = False
            if not worked:
                stop.wait(1)
        self.store.heartbeat(self.worker_id, self.queue, "STOPPED")
