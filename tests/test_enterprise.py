import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_pilot import PilotFixture, OPERATOR, RECEIVER, VIEWER
from mttr_guard.pilot_config import Config
from mttr_guard.pilot_demo import config_data
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
