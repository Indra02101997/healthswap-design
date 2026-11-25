"""Auth.cs ported: scopes, the ES256 signing key, the development OAuth 2.0 client-credentials server and bearer validation.

* `TokenKeys`: the ES256 (P-256) key for locally issued tokens, read from `auth.signing_key_file` (default
  <repo>/.data/keys/jwt-es256.pem, SEC1 "EC PRIVATE KEY" PEM like C# ExportECPrivateKeyPem) and created on first use.
  The key id is the first 16 hex digits of SHA-256(SubjectPublicKeyInfo DER), so a key file shared with the C# API gives
  the same `kid` and JWKS.
* `DevIdentityProvider`: client registry from `auth.clients_file` (default data/seed/demo/partners.json: partners[].clients
  and admin) with PBKDF2-hashed secrets; `issue()` mints client-credentials tokens after checking tenant.api_client
  (status REVOKED or missing -> invalid_client), `issue_sdk_session()` mints SDK session tokens bound to cart, user, store
  and market.
* `TokenValidator`: JwtBearer validation (issuer, audience, lifetime with 30 s clock skew, ES256 only). With
  `auth.authority` set, tokens are validated against the authority's OpenID configuration and JWKS instead; SDK session
  tokens minted by this API (local issuer, token_use "sdk") are still accepted with the local key.
* `Principal` / `to_partner_context`: the claims as the API sees them (C# PrincipalExtensions).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from base64 import urlsafe_b64encode
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from healthswap.domain.primitives import iso
from healthswap.platform.db import Db, DbRole
from healthswap.platform.errors import HsError
from healthswap.platform.services.infrastructure import ClientSecret
from healthswap.platform.settings import AuthOptions
from healthswap.platform.tenancy import PartnerContext

log = logging.getLogger(__name__)


class Scopes:
    """OAuth scopes of the partner API (C# Scopes)."""

    ADMIN = "hs.admin"
    SDK_ALLOWED: tuple[str, ...] = ("carts.evaluate", "events.write", "products.read")
    ALL: tuple[str, ...] = (
        "catalog.write", "catalog.read", "inventory.write", "products.read", "carts.evaluate", "events.write", "orders.write", "orders.read",
        "users.write", "users.read", "billing.read", "billing.write", "sdk.sessions.write", ADMIN,
    )
    # The "mcp" authorization policy: either scope is enough.
    MCP: tuple[str, ...] = ("carts.evaluate", "products.read")


def _b64url(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------------------------------------------------
# Signing key
# ---------------------------------------------------------------------------------------------------------------------


class TokenKeys:
    """ES256 signing key for locally issued tokens (persisted so tokens survive restarts during manual testing)."""

    ALGORITHM = "ES256"

    def __init__(self, options: AuthOptions, repo_root: Path) -> None:
        path = Path(options.signing_key_file) if options.signing_key_file is not None else Path(repo_root) / ".data" / "keys" / "jwt-es256.pem"
        self.path = path
        self.private_key = self._load_or_create(path)
        self.public_key = self.private_key.public_key()
        spki = self.public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.kid = hashlib.sha256(spki).hexdigest()[:16]

    @staticmethod
    def _load_or_create(path: Path) -> ec.EllipticCurvePrivateKey:
        if not path.is_file():
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as f:  # another process may create it at the same time: keep the first one
                    f.write(pem)
                if os.name == "posix":
                    path.chmod(0o600)
                log.info("Created the token signing key %s", path)
            except FileExistsError:
                pass
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError(f"{path} must hold a P-256 (secp256r1) EC private key")
        return key

    def jwks(self) -> dict[str, Any]:
        """The JSON Web Key Set with the public key (GET /.well-known/jwks.json)."""
        n = self.public_key.public_numbers()
        return {"keys": [{
            "kty": "EC", "crv": "P-256", "use": "sig", "alg": self.ALGORITHM, "kid": self.kid,
            "x": _b64url(n.x.to_bytes(32, "big")), "y": _b64url(n.y.to_bytes(32, "big")),
        }]}

    def sign(self, payload: Mapping[str, Any]) -> str:
        return jwt.encode(dict(payload), self.private_key, algorithm=self.ALGORITHM, headers={"kid": self.kid})


# ---------------------------------------------------------------------------------------------------------------------
# Development identity provider
# ---------------------------------------------------------------------------------------------------------------------


def _invalid_client(detail: str) -> HsError:
    return HsError(401, "invalid_client", "Invalid client", detail)


class DevIdentityProvider:
    """Development OAuth 2.0 client-credentials server (RFC 6749 section 4.4). Production deployments use an external
    authority (auth.authority) and mTLS-bound tokens (RFC 8705); the API then only validates tokens."""

    _CLIENT_SQL = (
        "select c.client_id, c.partner_id, c.scopes, c.status, p.geography from tenant.api_client c join tenant.partner p using (partner_id) "
        "where c.client_id = @clientId"
    )

    def __init__(self, db: Db, keys: TokenKeys, options: AuthOptions, repo_root: Path) -> None:
        self.db = db
        self.keys = keys
        self.options = options
        self._secrets: dict[str, ClientSecret] = {}
        self._admin_clients: set[str] = set()
        file = Path(options.clients_file) if options.clients_file is not None else Path(repo_root) / "data" / "seed" / "demo" / "partners.json"
        if not file.is_file():
            return
        root = json.loads(file.read_text(encoding="utf-8"))
        for partner in _items(root, "partners"):
            for client in _items(partner, "clients"):
                self._secrets[_field(client, "clientId")] = ClientSecret.hash_secret(_field(client, "secret"))
        admin = _member(root, "admin")
        if isinstance(admin, Mapping):
            admin_id = _field(admin, "clientId")
            self._secrets[admin_id] = ClientSecret.hash_secret(_field(admin, "secret"))
            self._admin_clients.add(admin_id)

    @property
    def client_ids(self) -> frozenset[str]:
        return frozenset(self._secrets)

    def _verify(self, client_id: str, secret: str) -> bool:
        s = self._secrets.get(client_id)
        return s is not None and s.verify(secret)

    def issue(self, client_id: str | None, client_secret: str | None, requested_scope: str | None) -> dict[str, Any]:
        """TokenResponse {"access_token", "token_type": "Bearer", "expires_in", "scope"}. Raises HsError invalid_client (401)
        or invalid_scope (400); the token endpoint turns them into RFC 6749 error responses."""
        if client_id is None or client_secret is None or not self._verify(client_id, client_secret):
            raise _invalid_client("Client authentication failed")
        claims: dict[str, Any] = {"client_id": client_id, "token_use": "partner"}
        if client_id in self._admin_clients:
            granted = [Scopes.ADMIN]
            claims["token_use"] = "admin"
        else:
            row = self.db.in_tx(DbRole.OPS, None, lambda u: u.one(self._CLIENT_SQL, {"clientId": client_id}))
            if row is None or row["status"] == "REVOKED":
                raise _invalid_client("Client is not registered or revoked")
            granted = list(row["scopes"] or ())
            claims["partner_id"] = str(row["partner_id"])
            claims["geo"] = row["geography"]
        if requested_scope is not None and requested_scope.strip():
            requested = [s for s in requested_scope.split(" ") if s]
            if any(s not in granted for s in requested):
                raise HsError(400, "invalid_scope", "Invalid scope", "Requested scope exceeds the client's grant")
            granted = requested
        claims["scope"] = " ".join(granted)
        lifetime = timedelta(minutes=self.options.access_token_minutes)
        return {
            "access_token": self._create(client_id, claims, lifetime), "token_type": "Bearer", "expires_in": int(lifetime.total_seconds()),
            "scope": claims["scope"],
        }

    def issue_sdk_session(self, partner: PartnerContext, req: Mapping[str, Any]) -> dict[str, Any]:
        """SdkSession {"token", "tokenType", "expiresAt", "scopes"} for an SdkSessionRequest (camelCase mapping)."""
        requested = req.get("scopes")
        scopes = list(dict.fromkeys(requested if requested else Scopes.SDK_ALLOWED))
        if any(s not in Scopes.SDK_ALLOWED for s in scopes):
            raise HsError.invalid("SDK sessions may only carry carts.evaluate, events.write and products.read")
        maximum = self.options.sdk_session_max_seconds
        ttl_value = req.get("ttlSeconds")
        ttl = min(max(int(ttl_value) if ttl_value is not None else maximum, 60), maximum)
        claims: dict[str, Any] = {
            "client_id": partner.client_id, "partner_id": str(partner.partner_id), "token_use": "sdk", "scope": " ".join(scopes),
            "cart_ref": req.get("cartRef"), "market": req.get("market"),
        }
        if req.get("userRef") is not None:
            claims["user_ref"] = req["userRef"]
        if req.get("storeId") is not None:
            claims["store_id"] = req["storeId"]
        expires = datetime.now(UTC) + timedelta(seconds=ttl)
        token = self._create("sdk:" + partner.client_id, claims, timedelta(seconds=ttl))
        return {"token": token, "tokenType": "Bearer", "expiresAt": iso(expires), "scopes": scopes}

    def _create(self, subject: str, claims: dict[str, Any], lifetime: timedelta) -> str:
        now = time.time()
        payload = {
            "aud": self.options.audience, "iss": self.options.issuer, "exp": int(now + lifetime.total_seconds()), "iat": int(now),
            "nbf": int(now - 5), **claims, "sub": subject, "jti": uuid.uuid4().hex,
        }
        return self.keys.sign(payload)


def _member(obj: Any, name: str) -> Any:
    """Case-insensitive property lookup (HsJson.Options reads PropertyNameCaseInsensitive)."""
    if not isinstance(obj, Mapping):
        return None
    if name in obj:
        return obj[name]
    lower = name.lower()
    return next((v for k, v in obj.items() if isinstance(k, str) and k.lower() == lower), None)


def _items(obj: Any, name: str) -> list[Any]:
    value = _member(obj, name)
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []


def _field(obj: Any, name: str) -> str:
    value = _member(obj, name)
    if not isinstance(value, str):
        raise ValueError(f"clients file: '{name}' must be a string")
    return value


# ---------------------------------------------------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------------------------------------------------


class InvalidToken(Exception):
    """A bearer token that failed validation; `description` is the RFC 6750 error_description (C# ErrorDescription)."""

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


def _dotnet_time(seconds: Any) -> str:
    """A NumericDate as C# DateTime.ToString(CultureInfo.InvariantCulture) writes it ("MM/dd/yyyy HH:mm:ss")."""
    try:
        return datetime.fromtimestamp(float(seconds), UTC).strftime("%m/%d/%Y %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(seconds)


class TokenValidator:
    """Bearer token validation like the C# JwtBearer configuration (MapInboundClaims = false)."""

    CLOCK_SKEW_SECONDS = 30
    AUTHORITY_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")

    def __init__(self, options: AuthOptions, keys: TokenKeys, http: httpx.Client | None = None) -> None:
        self.options = options
        self.keys = keys
        self._http = http
        self._lock = threading.Lock()
        self._authority_issuer: str | None = None
        self._jwks: jwt.PyJWKClient | None = None

    @property
    def uses_authority(self) -> bool:
        return bool(self.options.authority)

    def validate(self, token: str) -> dict[str, Any]:
        """The token's claims. Raises InvalidToken with a C#-style description."""
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.InvalidTokenError:
            raise InvalidToken("The token is malformed") from None
        try:
            if not self.uses_authority or (unverified.get("iss") == self.options.issuer and unverified.get("token_use") == "sdk"):
                return self._decode(token, self.keys.public_key, (TokenKeys.ALGORITHM,), self.options.issuer)
            issuer, jwks = self._authority()
            signing_key = jwks.get_signing_key_from_jwt(token)
            return self._decode(token, signing_key.key, self.AUTHORITY_ALGORITHMS, issuer)
        except jwt.ExpiredSignatureError:
            raise InvalidToken(f"The token expired at '{_dotnet_time(unverified.get('exp'))}'") from None
        except jwt.ImmatureSignatureError:
            raise InvalidToken(f"The token is not valid before '{_dotnet_time(unverified.get('nbf'))}'") from None
        except jwt.InvalidAudienceError:
            aud = unverified.get("aud")
            raise InvalidToken(f"The audience '{', '.join(aud) if isinstance(aud, list) else aud}' is invalid") from None
        except jwt.InvalidIssuerError:
            raise InvalidToken(f"The issuer '{unverified.get('iss')}' is invalid") from None
        except jwt.MissingRequiredClaimError as ex:
            claim = ex.claim
            raise InvalidToken({"exp": "The token has no expiration", "aud": "The audience '(null)' is invalid",
                                "iss": "The issuer '(null)' is invalid"}.get(claim, f"The token has no '{claim}' claim")) from None
        except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError):
            raise InvalidToken("The signature is invalid") from None
        except jwt.PyJWKClientError:
            raise InvalidToken("The signature key was not found") from None
        except jwt.InvalidTokenError:
            raise InvalidToken("The token is malformed") from None

    def _decode(self, token: str, key: Any, algorithms: tuple[str, ...], issuer: str) -> dict[str, Any]:
        return jwt.decode(
            token, key, algorithms=list(algorithms), audience=self.options.audience, issuer=issuer, leeway=self.CLOCK_SKEW_SECONDS,
            options={"require": ["exp"], "verify_iat": False},
        )

    def _authority(self) -> tuple[str, jwt.PyJWKClient]:
        """OpenID configuration of auth.authority (issuer + JWKS client), fetched once."""
        with self._lock:
            if self._jwks is None or self._authority_issuer is None:
                url = str(self.options.authority).rstrip("/") + "/.well-known/openid-configuration"
                http = self._http or httpx.Client(timeout=10.0)
                try:
                    doc = http.get(url).raise_for_status().json()
                finally:
                    if self._http is None:
                        http.close()
                self._authority_issuer = str(doc["issuer"])
                self._jwks = jwt.PyJWKClient(str(doc["jwks_uri"]), cache_keys=True)
            return self._authority_issuer, self._jwks


# ---------------------------------------------------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------------------------------------------------


def _claim(claims: Mapping[str, Any], name: str) -> str | None:
    """C# FindFirst(name)?.Value: the first value of the claim as a string."""
    value = claims.get(name)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else json.dumps(value)


def scopes_of(claims: Mapping[str, Any]) -> frozenset[str]:
    """Every space-separated value of every "scope" claim."""
    value = claims.get("scope")
    values = value if isinstance(value, list) else [] if value is None else [value]
    return frozenset(s for v in values if isinstance(v, str) for s in v.split(" ") if s)


def to_partner_context(claims: Mapping[str, Any]) -> PartnerContext | None:
    """The partner the token belongs to, or None when it has no valid partner_id claim (admin tokens)."""
    raw = _claim(claims, "partner_id")
    try:
        partner_id = uuid.UUID(raw) if raw else None
    except ValueError:
        partner_id = None
    if partner_id is None:
        return None
    sdk = _claim(claims, "token_use") == "sdk"
    return PartnerContext(
        partner_id=partner_id, client_id=_claim(claims, "client_id") or "", scopes=scopes_of(claims), is_sdk_session=sdk,
        sdk_cart_ref=_claim(claims, "cart_ref") if sdk else None, sdk_user_ref=_claim(claims, "user_ref") if sdk else None,
        sdk_store_id=_claim(claims, "store_id") if sdk else None, sdk_market=_claim(claims, "market") if sdk else None,
    )


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller (C# ClaimsPrincipal with MapInboundClaims = false)."""

    claims: Mapping[str, Any]
    scopes: frozenset[str] = field(default_factory=frozenset)
    partner: PartnerContext | None = None

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> Principal:
        return cls(dict(claims), scopes_of(claims), to_partner_context(claims))

    @property
    def client_id(self) -> str | None:
        return _claim(self.claims, "client_id")

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_any_scope(self, scopes: tuple[str, ...]) -> bool:
        return any(s in self.scopes for s in scopes)
