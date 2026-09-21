"""ASGI API for supervised deployments. Worker processes are launched separately."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .core import Rejected
from .audit import IntegrityError
from .pilot_store import QueueFull
from .security import AuthenticationFailed, Authenticator, ConfigProvider, PolicyUnavailable


class APIError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class BodyLimit:
    """Bound streaming bodies before framework parsing, including chunked requests."""
    def __init__(self, app, limit=1_048_576):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    data = message.get("body", b"")
                    size += len(data)
                    if size > self.limit:
                        return await JSONResponse({"error": "Request exceeds 1 MiB"}, status_code=413)(scope, receive, send)
                    chunks.append(data)
                    if not message.get("more_body"):
                        break
        except TimeoutError:
            return await JSONResponse({"error": "Request body deadline exceeded"}, status_code=408)(scope, receive, send)
        body = b"".join(chunks)
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


class PeerLimiter:
    def __init__(self, budget=240):
        self.budget, self.peers = budget, OrderedDict()

    def allow(self, peer):
        now = time.monotonic()
        start, count = self.peers.get(peer, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        self.peers[peer] = (start, count + 1)
        self.peers.move_to_end(peer)
        while len(self.peers) > 1024:
            self.peers.popitem(last=False)
        return count < self.budget


def create_app(store, config, authenticator=None, rate_limit=240):
    provider = config if isinstance(config, ConfigProvider) else ConfigProvider(config)
    auth = authenticator or Authenticator()
    limiter = PeerLimiter(rate_limit)
    app = FastAPI(title="MTTR Guard", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit)
    assets = Path(__file__).parent / "static"

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return JSONResponse({"error": exc.message}, status_code=exc.code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": "Invalid request fields"}, status_code=400)

    @app.middleware("http")
    async def audit_request(request, call_next):
        rid = uuid.uuid4().hex
        request.state.identity = None
        request.state.service = None
        path = request.url.path
        public = path in {"/", "/app.js", "/style.css", "/healthz", "/readyz"}
        try:
            if not public and not limiter.allow(request.client.host if request.client else "unknown"):
                response = JSONResponse({"error": "Request rate exceeded"}, status_code=429, headers={"Retry-After": "60"})
            else:
                response = await call_next(request)
        except QueueFull:
            response = JSONResponse({"error": "Queue full; retry later"}, status_code=503)
        except Rejected as exc:
            response = JSONResponse({"error": str(exc)}, status_code=409)
        except IntegrityError:
            response = JSONResponse({"error": "Audit integrity failed; operator recovery required", "code": "audit_integrity_failed"}, status_code=503)
        except sqlite3.Error:
            response = JSONResponse({"error": "Storage unavailable"}, status_code=503)
        except (PolicyUnavailable, AuthenticationFailed) as exc:
            response = JSONResponse({"error": "Authorization unavailable" if isinstance(exc, PolicyUnavailable) else "Invalid credentials"}, status_code=503 if isinstance(exc, PolicyUnavailable) else 401)
        except Exception:
            response = JSONResponse({"error": "Internal error"}, status_code=500)
        if not public:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            identity, service = request.state.identity, request.state.service
            try:
                await run_in_threadpool(store.access_audit, identity.subject if identity else None,
                    asdict(service.scope) if service else None, request.method + " " + route, response.status_code, rid)
            except IntegrityError:
                response = JSONResponse({"error": "Audit integrity failed; operator recovery required", "code": "audit_integrity_failed"}, status_code=503)
            except Exception:
                response = JSONResponse({"error": "Audit storage unavailable"}, status_code=503)
        response.headers.update({"X-Request-ID": rid, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if response.status_code == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        return response

    async def authorize(request, roles):
        if len(request.headers.getlist("authorization")) != 1:
            raise APIError(401, "One Bearer token required")
        current = await run_in_threadpool(provider.get)
        identity = await run_in_threadpool(auth.authenticate, request.headers.get("authorization"), current, request.headers.get("x-mttr-service"))
        service = current.services[identity.service]
        request.state.identity, request.state.service, request.state.config = identity, service, current
        if identity.role not in roles:
            raise APIError(403, "Role denied")
        return identity, service

    async def body(request):
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise APIError(415, "JSON required")
        try:
            result = json.loads(await request.body(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, "Invalid JSON")
        if not isinstance(result, dict):
            raise APIError(400, "JSON object required")
        return result

    @app.get("/")
    async def index():
        return FileResponse(assets / "index.html")

    @app.get("/app.js")
    async def javascript():
        return FileResponse(assets / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    async def stylesheet():
        return FileResponse(assets / "style.css")

    @app.get("/healthz")
    async def health():
        await run_in_threadpool(store.operational)
        return {"storage": "ok"}

    @app.get("/readyz")
    async def readiness():
        await run_in_threadpool(provider.get)
        result = await run_in_threadpool(store.operational)
        return JSONResponse({"storage": "ok", "worker_ready": result["ready"]}, status_code=200 if result["ready"] else 503)

    @app.post("/v1/webhooks/alertmanager", status_code=202)
    async def receive(request: Request):
        _, service = await authorize(request, {"receiver"})
        payload = await body(request)
        try:
            ids = await run_in_threadpool(store.receive, service, payload)
        except Rejected:
            raise
        except (ValueError, TypeError, KeyError):
            raise APIError(400, "Invalid Alertmanager payload")
        return {"incident_ids": ids, "durable": True}

    @app.get("/api/session")
    async def session(request: Request):
        identity, service = await authorize(request, {"viewer", "operator"})
        cfg = request.state.config
        operators = {i.subject for i in cfg.identities if i.service == service.name and i.role == "operator"}
        operators.update("oidc:" + b["subject"] for b in (cfg.oidc or {}).get("bindings", []) if b["service"] == service.name and b["role"] == "operator")
        return {"subject": identity.subject, "role": identity.role, "service": service.name, "mode": service.mode, "owner": service.owner,
                "features": ["pause", "handoff"], "operators": sorted(operators) if identity.role == "operator" else []}

    @app.get("/api/incidents")
    async def incidents(request: Request, before: int | None = None):
        _, service = await authorize(request, {"viewer", "operator"})
        if before is not None and not 0 < before <= 9_223_372_036_854_775_807:
            raise APIError(400, "Invalid cursor")
        items = await run_in_threadpool(store.list_incidents, service.scope, before=before)
        return {"items": items, "next_cursor": items[-1]["cursor"] if len(items) == 100 else None}

    @app.get("/api/incidents/{iid}")
    async def detail(iid: str, request: Request):
        _, service = await authorize(request, {"viewer", "operator"})
        def read():
            incident = store.get(iid, service.scope)
            with store.tx() as db:
                events = [{**dict(row), "body": json.loads(row["body"])} for row in db.execute(
                    "SELECT * FROM events WHERE incident=? ORDER BY seq DESC LIMIT 200", (iid,))]
            return {"incident": incident, "events": list(reversed(events)), "events_limit": 200}
        return await run_in_threadpool(read)

    @app.post("/api/incidents/{iid}/{action}")
    async def operate(iid: str, action: str, request: Request):
        identity, service = await authorize(request, {"operator"})
        if action not in {"ack", "review", "refresh", "observe", "pause", "handoff"}:
            raise APIError(404, "Unknown action")
        payload = await body(request)
        if action == "handoff":
            cfg = request.state.config
            allowed = {i.subject for i in cfg.identities if i.service == service.name and i.role == "operator"}
            allowed.update("oidc:" + b["subject"] for b in (cfg.oidc or {}).get("bindings", []) if b["service"] == service.name and b["role"] == "operator")
            if payload.get("assignee") not in allowed:
                raise APIError(403, "Handoff target must be a configured operator for this service")
        return await run_in_threadpool(store.operate, iid, service, identity, action, payload)

    @app.get("/api/metrics")
    async def metrics(request: Request):
        _, service = await authorize(request, {"viewer", "operator"})
        return await run_in_threadpool(store.metrics, service.scope)

    @app.get("/api/operations")
    async def operations(request: Request):
        _, service = await authorize(request, {"viewer", "operator"})
        return await run_in_threadpool(store.operational, service.scope)

    @app.get("/metrics")
    async def prometheus(request: Request):
        _, service = await authorize(request, {"viewer", "operator"})
        result = await run_in_threadpool(store.operational, service.scope)
        lines = ["# TYPE mttr_guard_workers gauge", f"mttr_guard_workers {result['workers']}",
                 "# TYPE mttr_guard_ready gauge", f"mttr_guard_ready {int(result['ready'])}",
                 "# TYPE mttr_guard_needs_attention gauge", f"mttr_guard_needs_attention {result['needs_attention']}",
                 "# TYPE mttr_guard_jobs gauge", "# TYPE mttr_guard_oldest_due_seconds gauge"]
        for row in result["jobs"]:
            labels = f'kind="{row["kind"]}",state="{row["state"]}"'
            lines.extend([f'mttr_guard_jobs{{{labels}}} {row["count"]}', f'mttr_guard_oldest_due_seconds{{{labels}}} {row["overdue_seconds"]:.3f}'])
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    return app
