"""An independent, durable simulated deployment platform (separate SQLite DB).

Models server-side idempotency and compare-and-swap. This is NOT a live deployment
connector. Fault injection wrappers can lose replies without undoing remote writes.
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .core import canonical, digest


class SQLiteDeploymentAdapter:
    mode = "SIMULATED"

    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(Path(path).resolve()), clock
        self.adapter_id = "sqlite-simulator:" + digest(self.path)
        with self.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deployments (
                    scope TEXT PRIMARY KEY, revision TEXT NOT NULL, previous_revision TEXT NOT NULL,
                    traffic_percent REAL NOT NULL, executions INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, result TEXT NOT NULL);
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

    def seed(self, scope, revision="r42", previous_revision="r41", traffic_percent=5):
        with self.tx() as db:
            db.execute("INSERT INTO deployments(scope,revision,previous_revision,traffic_percent) VALUES (?,?,?,?)", (scope.key, revision, previous_revision, traffic_percent))

    def inspect(self, scope):
        with self.tx() as db:
            row = db.execute("SELECT * FROM deployments WHERE scope=?", (scope.key,)).fetchone()
            if not row:
                raise ValueError("Unknown deployment")
            return dict(row)

    def submit(self, operation_id, request):
        request_hash = digest(request)
        with self.tx() as db:
            existing = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if existing:
                if existing["request_digest"] != request_hash:
                    raise ValueError("Idempotency key reused for a different request")
                return json.loads(existing["result"])
            scope = canonical(request["scope"])
            deployment = db.execute("SELECT * FROM deployments WHERE scope=?", (scope,)).fetchone()
            allowed = (
                deployment is not None and request["action"] == "rollback_canary"
                and self.clock() < request["deadline"]
                and deployment["revision"] == request["expected_revision"]
                and deployment["previous_revision"] == request["target_revision"]
                and deployment["traffic_percent"] == request["traffic_percent"]
                and 0 < request["traffic_percent"] <= 5
            )
            result = {"operation_id": operation_id, "request_digest": request_hash, "mode": self.mode,
                      "status": "SUCCEEDED" if allowed else "REJECTED"}
            if allowed:
                db.execute("UPDATE deployments SET revision=?,executions=executions+1 WHERE scope=?", (request["target_revision"], scope))
                result.update(revision=request["target_revision"], completed_at=self.clock())
            db.execute("INSERT INTO operations VALUES (?,?,?)", (operation_id, request_hash, canonical(result)))
            return result

    def lookup(self, operation_id):
        with self.tx() as db:
            row = db.execute("SELECT result FROM operations WHERE id=?", (operation_id,)).fetchone()
            return json.loads(row[0]) if row else {"operation_id": operation_id, "mode": self.mode, "status": "NOT_FOUND"}


class FaultInjectingAdapter:
    """One submit per wrapper; faults describe precisely where a reply is lost."""
    mode = "SIMULATED"

    def __init__(self, delegate, fault):
        if fault not in {"before_submit", "after_submit", "none"}:
            raise ValueError("Unknown fault injection")
        self.delegate, self.fault = delegate, fault
        self.adapter_id = delegate.adapter_id

    def submit(self, operation_id, request):
        if self.fault == "before_submit":
            raise TimeoutError("Simulated transport failed before delivery")
        result = self.delegate.submit(operation_id, request)
        if self.fault == "after_submit":
            raise TimeoutError("Simulated response lost after durable remote completion")
        return result

    def lookup(self, operation_id):
        return self.delegate.lookup(operation_id)
