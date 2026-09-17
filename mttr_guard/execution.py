"""Dispatch and reconciliation across a non-transactional adapter boundary.

Intent is durable before I/O. Ambiguous results never authorize another write or
release the service lock. Current Engine evidence is simulated, so only explicitly
simulated adapters are admitted. A live write adapter is deliberately not wired.
"""
from __future__ import annotations

import copy
import math
from typing import Protocol

from .core import Rejected, digest


class DeploymentAdapter(Protocol):
    adapter_id: str
    mode: str

    def submit(self, operation_id: str, request: dict) -> dict: ...
    def lookup(self, operation_id: str) -> dict: ...


def _check_adapter(adapter, execution=None):
    if not isinstance(adapter.adapter_id, str) or not adapter.adapter_id or adapter.mode != "SIMULATED":
        raise Rejected("Only configured simulator adapters may use simulated evidence")
    if execution and execution["adapter_id"] != adapter.adapter_id:
        raise Rejected("Execution is bound to a different adapter")


def dispatch(engine, iid, scope, actor, plan_hash, adapter):
    engine._authorize(actor, scope, "executor")
    _check_adapter(adapter)
    with engine.tx() as db:
        inc = engine._load(db, iid, scope)
        if inc.get("execution"):
            _check_adapter(adapter, inc["execution"])
            if inc["execution"]["plan_hash"] != plan_hash:
                raise Rejected("Idempotency key mismatch")
            return inc  # Retry is a read of existing state, not a second submit.
        if inc["receipt"]:
            raise Rejected("Incident was executed through a different execution path")
        plan, _ = engine._execution_preflight(db, inc, scope, actor, plan_hash)
        operation = digest([iid, plan_hash])
        engine._acquire_action_lock(db, scope, iid, operation)
        request = {
            "scope": plan["scope"], "action": plan["action"], "plan_hash": plan_hash,
            "expected_revision": plan["expected_revision"], "target_revision": plan["target_revision"],
            "traffic_percent": plan["traffic_percent"], "deadline": plan["expires_at"],
        }
        inc["execution"] = {
            "operation_id": operation, "adapter_id": adapter.adapter_id, "plan_hash": plan_hash,
            "request": request, "request_digest": digest(request), "status": "PREPARED",
            "prepared_at": engine.clock(), "executor": actor.subject,
        }
        inc["state"] = "EXECUTING"
        engine._save(db, inc)
        engine.event(db, iid, "EXECUTION_PREPARED", inc["execution"])
    # There is intentionally no database transaction around external I/O.
    try:
        result = adapter.submit(operation, copy.deepcopy(request))
        _validate_result(result, inc["execution"], engine.clock())
    except Exception as exc:
        return _record(engine, iid, scope, operation, None, type(exc).__name__)
    # Process death (BaseException in tests) leaves EXECUTING durable for lookup.
    return _record(engine, iid, scope, operation, result)


def reconcile(engine, iid, scope, actor, adapter):
    engine._authorize(actor, scope, "executor")
    with engine.tx() as db:
        inc = engine._load(db, iid, scope)
        execution = inc.get("execution")
        if not execution:
            raise Rejected("No admitted remote operation")
        _check_adapter(adapter, execution)
        if inc["state"] not in {"EXECUTING", "EXECUTION_UNKNOWN"}:
            return inc
    # Approval expiry and kill switch block NEW writes, not read-only reconciliation.
    try:
        result = adapter.lookup(execution["operation_id"])
        _validate_result(result, execution, engine.clock())
    except Exception as exc:
        return _record(engine, iid, scope, execution["operation_id"], None, type(exc).__name__)
    return _record(engine, iid, scope, execution["operation_id"], result)


def _validate_result(result, execution, now):
    if not isinstance(result, dict) or result.get("operation_id") != execution["operation_id"] or result.get("mode") != "SIMULATED":
        raise ValueError("Invalid operation identity/mode")
    status = result.get("status")
    if status not in {"SUCCEEDED", "REJECTED", "PENDING", "NOT_FOUND"}:
        raise ValueError("Invalid remote status")
    if status != "NOT_FOUND" and result.get("request_digest") != execution["request_digest"]:
        raise ValueError("Receipt belongs to another request")
    if status == "SUCCEEDED":
        stamp = result.get("completed_at")
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or not execution["prepared_at"] <= stamp <= now:
            raise ValueError("Invalid remote completion time")
        if result.get("revision") != execution["request"]["target_revision"]:
            raise ValueError("Remote revision does not match plan")


def _record(engine, iid, scope, operation, result, error=None):
    with engine.tx() as db:
        inc = engine._load(db, iid, scope)
        execution = inc["execution"]
        if execution["operation_id"] != operation:
            raise Rejected("Operation identity changed")
        if inc["state"] not in {"EXECUTING", "EXECUTION_UNKNOWN"}:
            return inc  # Late/duplicate responses cannot regress a terminal result.
        status = result["status"] if result else "UNKNOWN"
        execution["last_checked_at"] = engine.clock()
        execution["status"] = status
        if error:
            execution["last_error_type"] = error
        else:
            execution.pop("last_error_type", None)
        if status == "SUCCEEDED":
            inc["receipt"] = {
                "mode": "SIMULATED", "plan_hash": execution["plan_hash"], "idempotency_key": operation,
                "revision": result["revision"], "remote_completed_at": result["completed_at"],
                # Conservative: recovery observations must follow confirmed completion.
                "executed_at": engine.clock(),
            }
            inc["state"] = "VERIFYING"
        elif status == "REJECTED":
            # Contract: terminal rejection certifies that this operation had NO effect.
            inc["state"] = "ESCALATED"
            inc["escalation_reason"] = "Deployment adapter rejected the action without side effects"
            engine._release_action_lock(db, scope, iid)
        elif status == "PENDING":
            inc["state"] = "EXECUTING"
        else:
            inc["state"] = "EXECUTION_UNKNOWN"
            inc["escalation_reason"] = "Remote outcome uncertain; keep service lock and reconcile; never blindly resubmit"
        engine._save(db, inc)
        engine.event(db, iid, "EXECUTION_OBSERVED", {"operation_id": operation, "status": status, "state": inc["state"], "error_type": error})
        return inc
