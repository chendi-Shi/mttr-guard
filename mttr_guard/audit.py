"""Transactional audit chaining. External checkpoints are required to detect rollback."""
import hashlib
import hmac
import json
import sqlite3

from .core import canonical, digest


ZERO = "0" * 64


class IntegrityError(RuntimeError):
    """Stored history is untrusted; callers must not serve or sign its state."""


def key_id(key):
    return hashlib.sha256(key).hexdigest() if key else "UNKEYED-DEVELOPMENT"


def initialize(db, key):
    db.execute("CREATE TABLE IF NOT EXISTS audit_records (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous TEXT NOT NULL, signature TEXT NOT NULL)")
    existing = db.execute("SELECT value FROM config WHERE key='audit_key_id'").fetchone()
    if key and not existing and (db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] or db.execute("SELECT COUNT(*) FROM events").fetchone()[0]):
        raise ValueError("Keyed audit requires a new database; legacy history needs an explicit migration")
    if existing and existing[0] != key_id(key):
        raise ValueError("Audit key does not match this database; refusing a silent key change")
    db.execute("INSERT OR IGNORE INTO config VALUES ('audit_key_id',?)", (key_id(key),))
    db.execute("INSERT OR IGNORE INTO config VALUES ('audit_head',?)", (canonical({"seq": 0, "signature": ZERO}),))


def signature(key, record):
    value = canonical(record).encode()
    return hmac.new(key, value, hashlib.sha256).hexdigest() if key else hashlib.sha256(value).hexdigest()


def append(db, key, at, kind, scope, subject, data):
    head = json.loads(db.execute("SELECT value FROM config WHERE key='audit_head'").fetchone()[0])
    payload = {"at": at, "kind": kind, "scope": scope, "subject": subject, "data": data}
    seq = head["seq"] + 1
    signed = signature(key, {"seq": seq, "previous": head["signature"], "payload": payload})
    db.execute("INSERT INTO audit_records VALUES (?,?,?,?)", (seq, canonical(payload), head["signature"], signed))
    db.execute("UPDATE config SET value=? WHERE key='audit_head'", (canonical({"seq": seq, "signature": signed}),))


def verify(db, key, checkpoint=None):
    # Keep offline callers' ValueError contract; runtime guards translate this
    # into IntegrityError, distinct from invalid user input (HTTP 400/409).
    try:
        return _verify(db, key, checkpoint)
    except (TypeError, KeyError, IndexError, AttributeError, RecursionError, sqlite3.DatabaseError) as exc:
        raise ValueError("Audit data is malformed or unavailable") from exc


def _verify(db, key, checkpoint=None):
    stored_key = db.execute("SELECT value FROM config WHERE key='audit_key_id'").fetchone()
    if not stored_key or stored_key[0] != key_id(key):
        raise ValueError("Audit key mismatch or audit not initialized")
    previous, seq, states, event_ids = ZERO, 0, {}, set()
    checkpoint_seen = checkpoint is None or checkpoint == {"seq": 0, "signature": ZERO}
    for row in db.execute("SELECT seq,payload,previous,signature FROM audit_records ORDER BY seq"):
        current, raw, prev, signed = tuple(row)
        payload = json.loads(raw)
        expected = signature(key, {"seq": current, "previous": prev, "payload": payload})
        if current != seq + 1 or prev != previous or not hmac.compare_digest(expected, signed):
            raise ValueError("Audit chain integrity check failed")
        if checkpoint and current == checkpoint["seq"]:
            checkpoint_seen = hmac.compare_digest(signed, checkpoint["signature"])
        if payload["kind"] == "WORKFLOW":
            event_ids.add(payload["data"]["event_seq"])
            original = db.execute("SELECT incident,at,kind,body FROM events WHERE seq=?", (payload["data"]["event_seq"],)).fetchone()
            if not original or digest([original[0], original[1], original[2], json.loads(original[3])]) != payload["data"]["event_hash"]:
                raise ValueError("Workflow event differs from its audit record")
        if payload["kind"] == "STATE":
            states[payload["data"]["incident"]] = payload["data"]["body_hash"]
        seq, previous = current, signed
    head = {"seq": seq, "signature": previous}
    stored = json.loads(db.execute("SELECT value FROM config WHERE key='audit_head'").fetchone()[0])
    if stored != head or not checkpoint_seen:
        raise ValueError("Audit head mismatch or external checkpoint missing")
    if key and (db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] != len(states) or db.execute("SELECT COUNT(*) FROM events").fetchone()[0] != len(event_ids)):
        raise ValueError("Unaudited incident or workflow event found")
    for iid, expected in states.items():
        row = db.execute("SELECT body,scope,state FROM incidents WHERE id=?", (iid,)).fetchone()
        if not row or digest(json.loads(row[0])) != expected:
            raise ValueError("Incident state differs from its latest audit record")
        body = json.loads(row[0])
        if body["id"] != iid or canonical(body["scope"]) != row[1] or body["state"] != row[2]:
            raise ValueError("Incident lookup fields differ from audited state")
    return {"head": head, "keyed": bool(key), "records": seq, "verified_states": len(states)}
