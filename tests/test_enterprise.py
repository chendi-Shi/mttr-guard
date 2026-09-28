import importlib.util
import json
import os
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_pilot import PilotFixture, OPERATOR, RECEIVER, VIEWER
from mttr_guard.pilot_config import Config
from mttr_guard.pilot_demo import config_data
from mttr_guard.pilot_store import PilotStore
from mttr_guard.security import Authenticator, AuthenticationFailed, ConfigProvider, PolicyUnavailable


ENABLED = all(importlib.util.find_spec(p) for p in ("fastapi", "httpx", "jwt", "cryptography"))
if ENABLED:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient
    from mttr_guard.asgi import create_app


@unittest.skipUnless(ENABLED, "Install requirements-enterprise.txt")
class AuthenticationTests(PilotFixture):
    def setUp(self):
        super().setUp()
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        self.jwk.update(kid="key1", alg="RS256", use="sig")
        data = config_data("http://local")
        data["oidc"] = {"issuer": "https://identity.example", "audience": "mttr-guard", "jwks_url": "https://identity.example/keys",
            "bindings": [{"subject": "alice", "service": "checkout", "role": "operator"}]}
        self.config = Config(data)
        self.calls = []
        def fetch(url):
            self.calls.append(url)
            return {"keys": [self.jwk]}
        self.auth = Authenticator(fetch=fetch)

    def token(self, claims=None, headers=None):
        now = int(time.time())
        return jwt.encode({"iss": "https://identity.example", "aud": "mttr-guard", "sub": "alice", "iat": now,
            "nbf": now, "exp": now + 300, **(claims or {})}, self.private, algorithm="RS256", headers={"typ": "at+jwt", "kid": "key1", **(headers or {})})

    def test_oidc_signature_and_local_binding(self):
        identity = self.auth.authenticate("Bearer " + self.token({"role": "admin"}), self.config)
        self.assertEqual(identity.role, "operator")
        self.assertEqual(identity.subject, "oidc:alice")
        self.assertEqual(self.calls, ["https://identity.example/keys"])

    def test_wrong_issuer_audience_expiry_subject_lifetime_rejected(self):
        for changed in ({"iss": "https://other"}, {"aud": "different-api"}, {"exp": int(time.time()) - 1},
                        {"sub": "mallory"}, {"exp": int(time.time()) + 3600}, {"nbf": int(time.time()) + 300}):
            with self.subTest(changed=changed), self.assertRaises(AuthenticationFailed):
                self.auth.authenticate("Bearer " + self.token(changed), self.config)

    def test_id_token_and_unknown_key_rejected_without_fetch_flood(self):
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate("Bearer " + self.token(headers={"typ": "JWT"}), self.config)
        for _ in range(5):
            with self.assertRaises(AuthenticationFailed):
                self.auth.authenticate("Bearer " + self.token(headers={"kid": "unknown"}), self.config)
        self.assertEqual(len(self.calls), 1)

    def test_cached_keys_expire_and_failure_is_closed(self):
        self.auth.authenticate("Bearer " + self.token(), self.config)
        with patch.object(self.auth, "clock", return_value=time.time() + 61), patch.object(self.auth, "fetch", side_effect=OSError):
            with self.assertRaises(PolicyUnavailable):
                self.auth.authenticate("Bearer " + self.token(), self.config)

    def test_binding_removal_revokes_existing_token(self):
        token = self.token()
        self.auth.authenticate("Bearer " + token, self.config)
        self.config.oidc["bindings"] = []
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate("Bearer " + token, self.config)

    def test_signature_and_algorithm_confusion_rejected(self):
        different = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        good_claims = jwt.decode(self.token(), options={"verify_signature": False})
        for token in (jwt.encode(good_claims, different, algorithm="RS256", headers={"typ": "at+jwt", "kid": "key1"}),
                      jwt.encode(good_claims, "wrong-secret", algorithm="HS256", headers={"typ": "at+jwt", "kid": "key1"})):
            with self.assertRaises(AuthenticationFailed):
                self.auth.authenticate("Bearer " + token, self.config)

    def test_service_header_cannot_expand_authorization(self):
        with self.assertRaises(AuthenticationFailed):
            self.auth.authenticate("Bearer " + self.token(), self.config, "another-service")


@unittest.skipUnless(ENABLED, "Install requirements-enterprise.txt")
class ASGITests(PilotFixture):
    def client(self, **kwargs):
        return TestClient(create_app(self.store, self.config, **kwargs), raise_server_exceptions=False)

    def headers(self, token=OPERATOR):
        return {"Authorization": "Bearer " + token}

    def test_live_corruption_returns_503_without_serving_or_signing_data(self):
        self.store = PilotStore(Path(self.temp.name) / "sealed-http.db", self.clock,
                                audit_key=b"http-integrity-regression-key-1234567890")
        iid = self.receive()
        self.store.heartbeat("worker", "all", "RUNNING")
        with self.client() as client:
            self.assertEqual(client.get("/readyz").status_code, 200)
            with closing(sqlite3.connect(self.store.path)) as db, db:
                db.execute("UPDATE incidents SET body='forged-secret-body'")
                damaged = list(db.iterdump())
            for path in ("/healthz", "/readyz", "/api/session", "/api/incidents",
                         f"/api/incidents/{iid}", "/api/metrics", "/api/operations", "/metrics"):
                with self.subTest(path=path):
                    result = client.get(path, headers=self.headers())
                    self.assertEqual(result.status_code, 503, result.text)
                    self.assertEqual(result.json()["code"], "audit_integrity_failed")
                    self.assertNotIn("forged-secret-body", result.text)
                    self.assertIn("X-Request-ID", result.headers)
            result = client.post(f"/api/incidents/{iid}/ack", headers=self.headers(), json={"request_id": "ack"})
            self.assertEqual(result.status_code, 503)
            result = client.post("/v1/webhooks/alertmanager", headers=self.headers(RECEIVER), json=self.payload())
            self.assertEqual(result.status_code, 503)
            with closing(sqlite3.connect(self.store.path)) as db:
                self.assertEqual(list(db.iterdump()), damaged)

    def test_auth_rbac_and_durable_intake(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/incidents").status_code, 401)
            self.assertEqual(client.get("/api/incidents", headers=self.headers(RECEIVER)).status_code, 403)
            response = client.post("/v1/webhooks/alertmanager", headers=self.headers(RECEIVER), json=self.payload())
            self.assertEqual(response.status_code, 202, response.text)
            iid = response.json()["incident_ids"][0]
            self.assertEqual(client.get(f"/api/incidents/{iid}", headers=self.headers(VIEWER)).status_code, 200)
            self.assertEqual(client.post(f"/api/incidents/{iid}/ack", headers=self.headers(VIEWER), json={"request_id": "x"}).status_code, 403)

    def test_request_audit_does_not_store_secrets_or_arbitrary_paths(self):
        with self.client() as client:
            client.get("/not-found/secret-query?token=secret-body", headers=self.headers())
            client.get("/api/incidents", headers={"Authorization": "Bearer secret-auth"})
        with self.store.tx() as db:
            raw = " ".join(row[0] for row in db.execute("SELECT payload FROM audit_records"))
        self.assertNotIn("secret-", raw)
        self.assertIn('"status":401', raw)

    def test_streaming_body_limit_and_rate_limit(self):
        with self.client(rate_limit=2) as client:
            oversized = client.post("/v1/webhooks/alertmanager", headers={**self.headers(RECEIVER), "Content-Type": "application/json"}, content=(b"x" * 1_048_577))
            self.assertEqual(oversized.status_code, 413)
            self.assertEqual(client.get("/api/session", headers=self.headers()).status_code, 200)
            self.assertEqual(client.get("/api/session", headers=self.headers()).status_code, 429)

    def test_readiness_and_scoped_prometheus_metrics(self):
        with self.client() as client:
            self.assertEqual(client.get("/readyz").status_code, 503)
            self.store.heartbeat("w", "all", "RUNNING")
            self.assertEqual(client.get("/readyz").status_code, 200)
            self.receive()
            response = client.get("/metrics", headers=self.headers())
            self.assertEqual(response.status_code, 200)
            self.assertIn('mttr_guard_jobs{kind="diagnose",state="READY"} 1', response.text)

    def test_exhausted_job_is_visible_and_operator_can_retry_through_api(self):
        iid = self.receive()
        for _ in range(3):
            self.store.claim()
            self.clock.advance(120)
        self.store.claim()
        with self.client() as client:
            metrics = client.get("/metrics", headers=self.headers(VIEWER))
            self.assertEqual(metrics.status_code, 200)
            self.assertIn("mttr_guard_needs_attention 1", metrics.text)
            operations = client.get("/api/operations", headers=self.headers(VIEWER))
            self.assertEqual(operations.json()["needs_attention"], 1)
            payload = {"request_id": "fix-and-retry", "reason": "Collector repaired, ticket 42"}
            route = f"/api/incidents/{iid}/refresh"
            self.assertEqual(client.post(route, headers=self.headers(VIEWER), json=payload).status_code, 403)
            self.assertEqual(client.post(route, headers=self.headers(), json=payload).json()["state"], "QUEUED")
            job, _ = self.store.claim()
            self.assertEqual(job["attempts"], 1)
            self.assertEqual(client.post(route, headers=self.headers(), json=payload).status_code, 200)
            self.assertIsNone(self.store.claim())
            self.assertIn("mttr_guard_needs_attention 0", client.get("/metrics", headers=self.headers()).text)

    def test_live_api_refuses_signed_database_rollback(self):
        from test_checkpoints import KEY, copy_database
        from mttr_guard import checkpoints
        from mttr_guard.operations import inspect_database
        self.store = PilotStore(Path(self.temp.name) / "anchored.db", self.clock, audit_key=KEY)
        iid = self.receive()
        old = Path(self.temp.name) / "old.db"
        copy_database(self.store.path, old)
        self.action(iid, "ack")
        anchor = Path(self.temp.name) / "trusted.json"
        checkpoints.export(self.store.path, anchor, KEY)
        self.store = PilotStore(self.store.path, self.clock, audit_key=KEY, audit_checkpoint=anchor)
        with self.client() as client:
            self.assertEqual(client.get(f"/api/incidents/{iid}", headers=self.headers()).status_code, 200)
            copy_database(old, self.store.path)
            before = inspect_database(self.store.path, KEY)
            for path in ("/healthz", "/readyz", f"/api/incidents/{iid}", "/api/operations"):
                response = client.get(path, headers=self.headers())
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["code"], "audit_integrity_failed")
            response = client.post(f"/api/incidents/{iid}/ack", headers=self.headers(), json={"request_id": "after-rollback"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(inspect_database(self.store.path, KEY), before)

    def test_rotated_token_file_takes_effect_without_restart(self):
        root = Path(self.temp.name)
        credential = root / "receiver.token"
        credential.write_text(RECEIVER)
        data = config_data("http://local")
        data["identities"][1].pop("token_env")
        data["identities"][1]["token_file"] = str(credential)
        configuration = root / "config.json"
        configuration.write_text(json.dumps(data), encoding="utf-8")
        with TestClient(create_app(self.store, ConfigProvider(configuration)), raise_server_exceptions=False) as client:
            self.assertEqual(client.post("/v1/webhooks/alertmanager", headers=self.headers(RECEIVER), json=self.payload()).status_code, 202)
            credential.write_text("new-token-" * 8)
            self.assertEqual(client.post("/v1/webhooks/alertmanager", headers=self.headers(RECEIVER), json=self.payload()).status_code, 401)
            self.assertEqual(client.post("/v1/webhooks/alertmanager", headers=self.headers("new-token-" * 8), json=self.payload()).status_code, 202)
            configuration.write_text("invalid")
            self.assertEqual(client.get("/api/session", headers=self.headers()).status_code, 503)

    def test_handoff_to_unbound_identity_is_denied(self):
        iid = self.receive()
        with self.client() as client:
            response = client.post(f"/api/incidents/{iid}/handoff", headers=self.headers(), json={"request_id": "handoff", "assignee": "mallory", "expected_assignee": None, "reason": "Shift"})
            self.assertEqual(response.status_code, 403)
