"""Short-lived OIDC access tokens and local, reloadable authorization bindings."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .collectors import get_json
from .pilot_config import Config, Identity


class AuthenticationFailed(ValueError):
    pass


class PolicyUnavailable(RuntimeError):
    pass


class ConfigProvider:
    """Reload configuration and token files on each request; invalid changes fail closed."""
    def __init__(self, config):
        self.config = config

    def get(self):
        try:
            return Config.read(self.config) if isinstance(self.config, (str, Path)) else self.config
        except Exception as exc:
            raise PolicyUnavailable("Authorization configuration unavailable") from exc


class Authenticator:
    def __init__(self, clock=time.time, fetch=get_json):
        self.clock, self.fetch = clock, fetch
        self.lock = threading.Lock()
        self.cached = {}

    def _keys(self, options, kid):
        url = options["jwks_url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise PolicyUnavailable("JWKS requires a configured HTTPS URL")
        cache_id = (options["issuer"], url)
        with self.lock:
            cached = self.cached.get(cache_id)
            now = self.clock()
            if not cached or now - cached["at"] >= 60 or (kid not in cached["keys"] and now - cached["at"] >= 10):
                try:
                    document = self.fetch(url)
                    raw = document["keys"]
                    if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
                        raise ValueError("Invalid JWKS size")
                    keys = {}
                    for key in raw:
                        if key.get("kty") == "RSA" and key.get("use", "sig") == "sig" and key.get("alg", "RS256") == "RS256":
                            key_name = key.get("kid")
                            if not isinstance(key_name, str) or key_name in keys:
                                raise ValueError("Invalid or duplicate key ID")
                            keys[key_name] = key
                    cached = {"at": now, "keys": keys}
                    self.cached[cache_id] = cached
                except Exception as exc:
                    # Never extend the life of an expired cache when the provider is unavailable.
                    raise PolicyUnavailable("Signing keys unavailable") from exc
            if kid not in cached["keys"]:
                raise AuthenticationFailed("Unknown signing key")
            return cached["keys"][kid]

    def authenticate(self, authorization, config, requested_service=None):
        if not isinstance(authorization, str) or not authorization.startswith("Bearer ") or len(authorization) > 16384:
            raise AuthenticationFailed("Bearer access token required")
        token = authorization[7:]
        hashed = hashlib.sha256(token.encode()).hexdigest()
        for identity in config.identities:
            if hmac.compare_digest(hashed, identity.token_hash):
                if requested_service and requested_service != identity.service:
                    raise AuthenticationFailed("Token is not authorized for this service")
                return identity
        options = config.oidc
        if not options:
            raise AuthenticationFailed("Invalid token")
        import jwt
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or header.get("typ") != "at+jwt" or not isinstance(header.get("kid"), str):
                raise AuthenticationFailed("Expected RS256 access token")
            key = jwt.PyJWK.from_dict(self._keys(options, header["kid"])).key
            if key.key_size < 2048:
                raise AuthenticationFailed("Signing key is too small")
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=options["audience"], issuer=options["issuer"],
                options={"require": ["exp", "iat", "nbf", "sub", "iss", "aud"]}, leeway=0)
            for name in ("iat", "nbf", "exp"):
                if type(claims[name]) not in (int, float) or not math.isfinite(claims[name]):
                    raise AuthenticationFailed("Invalid token time")
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 200:
                raise AuthenticationFailed("Invalid token subject")
            if not 0 < claims["exp"] - claims["iat"] <= options.get("max_token_seconds", 900):
                raise AuthenticationFailed("Access token lifetime exceeds policy")
            # The issuer's role/group claims do not grant permissions in this application.
            bindings = [b for b in options["bindings"] if b["subject"] == claims["sub"]
                        and (requested_service is None or b["service"] == requested_service)]
            if len(bindings) != 1:
                raise AuthenticationFailed("No unique service binding; select X-MTTR-Service")
            binding = bindings[0]
            return Identity("oidc:" + claims["sub"], binding["role"], binding["service"], "")
        except (AuthenticationFailed, PolicyUnavailable):
            raise
        except Exception as exc:
            raise AuthenticationFailed("Invalid or expired access token") from exc
