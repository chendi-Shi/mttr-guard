"""Normalize Alertmanager webhooks behind an authenticated, scope-bound receiver.

This module does not implement HTTP authentication. Scope must come from the
receiver's trusted credentials/configuration, never from an untrusted tenant label.
"""
from datetime import datetime

from .core import Rejected, Scope, digest


def ingest_webhook(engine, scope: Scope, payload: dict) -> list[str]:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or len(alerts) > 100:
        raise Rejected("Expected at most 100 alerts")
    normalized = []
    # Validate the whole batch before storing any item.
    for alert in alerts:
        if not isinstance(alert, dict) or alert.get("status") not in {"firing", "resolved"}:
            raise Rejected("Invalid alert status")
        labels = alert.get("labels", {})
        if not isinstance(labels, dict):
            raise Rejected("Invalid labels")
        for name in ("tenant", "environment", "cluster", "namespace", "service"):
            if labels.get(name) != getattr(scope, name):
                raise Rejected("Alert scope does not match the authenticated receiver")
        if alert["status"] == "resolved":
            # A resolved notification does not prove end-user recovery.
            continue
        try:
            started = datetime.fromisoformat(alert["startsAt"].replace("Z", "+00:00"))
            if started.tzinfo is None:
                raise ValueError("Timezone required")
            stamp = started.timestamp()
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise Rejected("Invalid startsAt") from exc
        fingerprint = alert.get("fingerprint") or digest(labels)
        if not isinstance(fingerprint, str) or not 0 < len(fingerprint) <= 256 or not 0 <= stamp <= engine.clock():
            raise Rejected("Invalid alert identity or time")
        normalized.append((fingerprint, stamp))
    return [engine.ingest(scope, fingerprint, stamp) for fingerprint, stamp in normalized]
