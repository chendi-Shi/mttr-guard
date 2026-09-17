"""Verified online SQLite backups, non-overwriting restore, and audit checkpoints."""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from .audit import verify


def read_key(env_name="MTTR_AUDIT_KEY"):
    file = os.environ.get(env_name + "_FILE")
    if file and os.environ.get(env_name):
        raise ValueError("Set either the audit key or audit key file, not both")
    value = Path(file).read_text(encoding="utf-8").strip() if file else os.environ.get(env_name, "")
    if len(value) < 32:
        raise ValueError("Configure a stable audit key of at least 32 characters in " + env_name)
    return value.encode()


def readonly(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def inspect_database(path, key, checkpoint=None):
    with closing(readonly(path)) as db:
        db.execute("BEGIN")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        result = verify(db, key, checkpoint)
        result["incidents"] = db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        result["pending_jobs"] = db.execute("SELECT COUNT(*) FROM pilot_jobs WHERE state<>'DONE'").fetchone()[0]
        return result


def file_hash(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def backup(source, destination, key):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    manifest = destination.with_suffix(destination.suffix + ".json")
    if destination.exists() or manifest.exists() or source == destination:
        raise ValueError("Backup must use a new destination and manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.parent / (destination.name + ".partial-" + uuid.uuid4().hex)
    started = time.monotonic()
    def progress(status, remaining, total):
        if time.monotonic() - started > 30:
            raise TimeoutError("Backup exceeded its deadline")
    try:
        with closing(readonly(source)) as src, closing(sqlite3.connect(partial)) as target:
            src.backup(target, pages=256, progress=progress, sleep=.05)
        checked = inspect_database(partial, key)
        content_hash = file_hash(partial)
        # Hard link creation is atomic and fails if the destination already exists.
        os.link(partial, destination)
        with manifest.open("x", encoding="utf-8") as output:
            json.dump({"format": 1, "sha256": content_hash, "checkpoint": checked["head"],
                       "created_at": time.time(), "incidents": checked["incidents"], "pending_jobs": checked["pending_jobs"]}, output, indent=2)
        return {"database": str(destination), "manifest": str(manifest), **checked}
    finally:
        partial.unlink(missing_ok=True)


def restore(source, destination, key):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or source == destination:
        raise ValueError("Restore refuses to overwrite any existing database")
    manifest = json.loads(source.with_suffix(source.suffix + ".json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest["sha256"] != file_hash(source):
        raise ValueError("Backup file hash mismatch")
    inspect_database(source, key, manifest["checkpoint"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.parent / (destination.name + ".partial-" + uuid.uuid4().hex)
    try:
        with source.open("rb") as src, partial.open("xb") as dest:
            import shutil
            shutil.copyfileobj(src, dest)
            dest.flush()
            os.fsync(dest.fileno())
        with closing(sqlite3.connect(partial)) as db:
            # Only read-only evidence jobs are reset. Execution state is never retried here.
            db.execute("UPDATE pilot_jobs SET state='READY',lease=NULL,lease_until=NULL,due=? WHERE state='RUNNING'", (time.time(),))
            db.execute("UPDATE workers SET state='STOPPED'")
            db.commit()
        checked = inspect_database(partial, key, manifest["checkpoint"])
        os.link(partial, destination)
        return {"database": str(destination), **checked}
    finally:
        partial.unlink(missing_ok=True)
