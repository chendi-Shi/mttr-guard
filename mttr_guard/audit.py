"""Transactional audit chaining. External checkpoints are required to detect rollback."""
import hashlib
import hmac
import json
import sqlite3
import sys

from .core import canonical, digest
from .checkpoints import validate as validate_checkpoint


ZERO = "0" * 64


class IntegrityError(RuntimeError):
    """Stored history is untrusted; callers must not serve or sign its state."""


class VerificationCache:
    """Process-local verified row memo; never a trusted database head shortcut.

    max_bytes bounds estimated retained entries, not total process RSS. A verify
    pass builds a replacement index and publishes it only after all checks pass.
    Callers must serialize access, as PilotStore does with BEGIN IMMEDIATE.
    """
    def __init__(self, key, max_bytes=16 * 1024 * 1024):
        if max_bytes < 0:
            raise ValueError("Cache budget must be nonnegative")
        self.key_id = key_id(key)
        self.max_bytes = max_bytes
        self.entries = {}
        self.retained_bytes = 0
        self.last_stats = {"hits": 0, "misses": 0}


def _size(value):
    # Memo values contain only immutable tuples/scalars, never full JSON trees.
    return sys.getsizeof(value) + (sum(_size(v) for v in value) if isinstance(value, tuple) else 0)


def _same_fields(left, right):
    # Python considers 1 == 1.0 and 0.0 == -0.0, but their canonical JSON
    # hashes differ. Schema/storage-type changes must also invalidate reuse.
    return left == right and all(type(a) is type(b) and (type(a) is not float or a.hex() == b.hex())
                                 for a, b in zip(left, right))


class _Memo:
    def __init__(self, cache, key):
        if cache is not None and cache.key_id != key_id(key):
            raise ValueError("Verification cache key mismatch")
        self.cache, self.entries = cache, {}
        self.size = self.hits = self.misses = 0

    def get(self, identity, raw, compute):
        if self.cache is None or self.cache.max_bytes == 0:
            self.misses += 1
            return compute()
        previous = self.cache.entries.get(identity)
        if previous is not None and _same_fields(previous[0], raw):
            entry = previous
            self.hits += 1
        else:
            value = compute()
            # Account for tuple/scalar objects and conservative index overhead.
            entry = (raw, value, _size(identity) + _size(raw) + _size(value) + 256)
            self.misses += 1
        if identity not in self.entries and self.size + entry[2] <= self.cache.max_bytes:
            self.entries[identity] = entry
            self.size += entry[2]
        return entry[1]

    def publish(self):
        if self.cache is not None:
            self.cache.entries = self.entries
            self.cache.retained_bytes = self.size
            self.cache.last_stats = {"hits": self.hits, "misses": self.misses}


def _record(key, row):
    current, raw, prev, signed = row
    payload = json.loads(raw)
    expected = signature(key, {"seq": current, "previous": prev, "payload": payload})
    if not hmac.compare_digest(expected, signed):
        raise ValueError("Audit chain integrity check failed")
    kind = payload["kind"]
    if kind == "STATE":
        return kind, payload["data"]["incident"], payload["data"]["body_hash"]
    if kind == "WORKFLOW":
        return kind, payload["data"]["event_seq"], payload["data"]["event_hash"]
    return None, None, None


def _state(row):
    body = json.loads(row[0])
    return digest(body), body["id"], canonical(body["scope"]), body["state"]


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


def verify(db, key, checkpoint=None, *, cache=None):
    # Keep offline callers' ValueError contract; runtime guards translate this
    # into IntegrityError, distinct from invalid user input (HTTP 400/409).
    try:
        memo = _Memo(cache, key)
        result = _verify(db, key, checkpoint, memo)
        memo.publish()
        return result
    except (TypeError, KeyError, IndexError, AttributeError, RecursionError, sqlite3.DatabaseError) as exc:
        raise ValueError("Audit data is malformed or unavailable") from exc


def _verify(db, key, checkpoint, memo):
    if checkpoint is not None:
        validate_checkpoint(checkpoint)
    stored_key = db.execute("SELECT value FROM config WHERE key='audit_key_id'").fetchone()
    if not stored_key or stored_key[0] != key_id(key):
        raise ValueError("Audit key mismatch or audit not initialized")
    previous, seq, states, event_ids = ZERO, 0, {}, set()
    checkpoint_seen = checkpoint is None or checkpoint == {"seq": 0, "signature": ZERO}
    for row in db.execute("SELECT seq,payload,previous,signature FROM audit_records ORDER BY seq"):
        row = tuple(row)
        current, raw, prev, signed = row
        if current != seq + 1 or prev != previous:
            raise ValueError("Audit chain integrity check failed")
        kind, subject, expected_hash = memo.get(("record", current), row, lambda: _record(key, row))
        if checkpoint and current == checkpoint["seq"]:
            checkpoint_seen = hmac.compare_digest(signed, checkpoint["signature"])
        if kind == "WORKFLOW":
            event_ids.add(subject)
            original = db.execute("SELECT incident,at,kind,body FROM events WHERE seq=?", (subject,)).fetchone()
            if not original:
                raise ValueError("Workflow event differs from its audit record")
            original = tuple(original)
            actual = memo.get(("event", subject), original,
                lambda: digest([original[0], original[1], original[2], json.loads(original[3])]))
            if actual != expected_hash:
                raise ValueError("Workflow event differs from its audit record")
        if kind == "STATE":
            states[subject] = expected_hash
        seq, previous = current, signed
    head = {"seq": seq, "signature": previous}
    stored = json.loads(db.execute("SELECT value FROM config WHERE key='audit_head'").fetchone()[0])
    if stored != head or not checkpoint_seen:
        raise ValueError("Audit head mismatch or external checkpoint missing")
    if key and (db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] != len(states) or db.execute("SELECT COUNT(*) FROM events").fetchone()[0] != len(event_ids)):
        raise ValueError("Unaudited incident or workflow event found")
    for iid, expected in states.items():
        row = db.execute("SELECT body,scope,state FROM incidents WHERE id=?", (iid,)).fetchone()
        if not row:
            raise ValueError("Incident state differs from its latest audit record")
        row = tuple(row)
        actual, body_id, scope, state = memo.get(("state", iid), row, lambda: _state(row))
        if actual != expected:
            raise ValueError("Incident state differs from its latest audit record")
        if body_id != iid or scope != row[1] or state != row[2]:
            raise ValueError("Incident lookup fields differ from audited state")
    return {"head": head, "keyed": bool(key), "records": seq, "verified_states": len(states)}


def require_checkpoint(db, verified_head, checkpoint):
    """Check ancestry only AFTER full verification in the same transaction."""
    validate_checkpoint(checkpoint)
    if checkpoint["seq"] == 0:
        return
    if verified_head["seq"] < checkpoint["seq"]:
        raise ValueError("Audit history moved behind a trusted checkpoint")
    row = db.execute("SELECT signature FROM audit_records WHERE seq=?", (checkpoint["seq"],)).fetchone()
    if not row or not hmac.compare_digest(row[0], checkpoint["signature"]):
        raise ValueError("Audit history diverged from a trusted checkpoint")
