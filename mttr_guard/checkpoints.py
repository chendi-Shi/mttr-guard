"""Bounded external audit anchors. Publication and protection are operator-owned."""
import json
import os
import re
import uuid
from pathlib import Path


def validate(checkpoint):
    if (not isinstance(checkpoint, dict) or set(checkpoint) != {"seq", "signature"}
            or type(checkpoint["seq"]) is not int or not 0 <= checkpoint["seq"] <= 9_223_372_036_854_775_807
            or not isinstance(checkpoint["signature"], str)
            or re.fullmatch(r"[0-9a-f]{64}", checkpoint["signature"]) is None
            or (checkpoint["seq"] == 0 and checkpoint["signature"] != "0" * 64)):
        raise ValueError("Invalid audit checkpoint")
    return checkpoint


def read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate checkpoint field")
            result[key] = value
        return result
    try:
        with Path(path).open("rb") as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError("Checkpoint exceeds size limit")
        checkpoint = validate(json.loads(raw, object_pairs_hook=unique))
        if checkpoint["seq"] == 0:
            raise ValueError("External checkpoint must anchor nonempty history")
        return checkpoint
    except (OSError, ValueError, RecursionError) as exc:
        # Paths and file contents may be sensitive. Never echo them in errors.
        raise ValueError("External audit checkpoint unavailable or invalid") from exc


def export(database, output, key, previous=None):
    from .operations import inspect_database
    if not key:
        raise ValueError("Checkpoint export requires keyed audit")
    checked = inspect_database(database, key, read(previous) if previous else None)
    head = checked["head"]
    if head["seq"] == 0:
        raise ValueError("Cannot export an empty audit history")
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(head, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic and never overwrites a previously published anchor.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return head
