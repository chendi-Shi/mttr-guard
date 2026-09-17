"""Loopback HTTP application for the pilot. Put TLS/authenticated ingress in front of it."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import Rejected
from .pilot_store import QueueFull


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(store, config, port=8787):
    assets = Path(__file__).parent / "static"

    class Handler(BaseHTTPRequestHandler):
        server_version = "MTTRGuardPilot"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # Do not put incident IDs, URLs or tokens in an unauthenticated access log.

        def respond(self, code, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def identity(self):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or len(auth) > 4096:
                return None
            hashed = hashlib.sha256(auth[7:].encode()).hexdigest()
            return next((i for i in config.identities if hmac.compare_digest(i.token_hash, hashed)), None)

        def do_GET(self):
            self.handle_request(False)

        def do_POST(self):
            self.handle_request(True)

        def handle_request(self, write):
            try:
                self.route(write)
            except QueueFull as exc:
                self.respond(503, {"error": str(exc)})
            except Rejected as exc:
                self.respond(409, {"error": str(exc)})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {"error": "Invalid request"})
            except sqlite3.Error:
                self.respond(503, {"error": "Storage unavailable; retry later"})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True
            except Exception:
                self.respond(500, {"error": "Internal error"})

        def route(self, write):
            parsed = urlsplit(self.path)
            path = parsed.path
            if not write and path in {"/", "/app.js", "/style.css"}:
                name, mime = {"/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8")}[path]
                return self.respond(200, (assets / name).read_bytes(), mime)
            if not write and path in {"/healthz", "/readyz"}:
                with store.tx() as db:
                    db.execute("SELECT 1")
                    row = db.execute("SELECT value FROM config WHERE key='pilot_worker_heartbeat'").fetchone()
                ready = bool(row and store.clock() - float(row[0]) < 150)
                code = 200 if path == "/healthz" or ready else 503
                return self.respond(code, {"storage": "ok", "worker_ready": ready})
            identity = self.identity()
            if not identity:
                return self.respond(401, {"error": "Bearer token required"})
            service = config.services[identity.service]
            if identity.role == "receiver" and path != "/v1/webhooks/alertmanager":
                return self.respond(403, {"error": "Receiver cannot read incident data"})
            if write:
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    return self.respond(400, {"error": "One Content-Length is required"})
                size = int(self.headers["Content-Length"])
                if not 0 < size <= 1_048_576:
                    return self.respond(413, {"error": "Request must be between 1 byte and 1 MiB"})
                if self.headers.get_content_type() != "application/json":
                    return self.respond(415, {"error": "JSON required"})
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("Truncated request")
                body = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
                if not isinstance(body, dict):
                    raise ValueError("Expected an object")
                if path == "/v1/webhooks/alertmanager":
                    if identity.role != "receiver":
                        return self.respond(403, {"error": "Receiver role required"})
                    ids = store.receive(service, body)
                    return self.respond(202, {"incident_ids": ids, "durable": True})
                match = re.fullmatch(r"/api/incidents/([a-f0-9]{32})/(ack|review|refresh|observe)", path)
                if match:
                    if identity.role != "operator":
                        return self.respond(403, {"error": "Operator role required"})
                    return self.respond(200, store.operate(match[1], service, identity, match[2], body))
            else:
                if path == "/api/session":
                    return self.respond(200, {"subject": identity.subject, "role": identity.role,
                        "service": service.name, "mode": service.mode, "owner": service.owner})
                if path == "/api/incidents":
                    query = parse_qs(parsed.query)
                    before = int(query["before"][0]) if "before" in query else None
                    if before is not None and before <= 0:
                        raise ValueError("Invalid cursor")
                    items = store.list_incidents(service.scope, before=before)
                    return self.respond(200, {"items": items, "next_cursor": items[-1]["cursor"] if len(items) == 100 else None})
                if path == "/api/metrics":
                    return self.respond(200, store.metrics(service.scope))
                match = re.fullmatch(r"/api/incidents/([a-f0-9]{32})", path)
                if match:
                    inc = store.get(match[1], service.scope)
                    with store.tx() as db:
                        events = [{**dict(row), "body": json.loads(row["body"])} for row in db.execute(
                            "SELECT * FROM events WHERE incident=? ORDER BY seq DESC LIMIT 200", (inc["id"],))]
                    return self.respond(200, {"incident": inc, "events": list(reversed(events)), "events_limit": 200})
            return self.respond(404, {"error": "Not found"})

    return BoundedServer(("127.0.0.1", port), Handler)
