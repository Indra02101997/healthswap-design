"""Services/Infrastructure.cs ported: webhook types, signatures, the SSRF URL policy, webhook fan-out and delivery, secret
protection, idempotency keys and the double-entry ledger writer.

Port notes:
    * Webhook delivery is infrastructure: it runs on wall-clock time (`utc_now()`), not on the time-travel capable business
      clock, exactly like the C# implementation.
    * ASP.NET Core Data Protection is replaced by `DataProtectionProvider`: a random 256-bit master key persisted in the keys
      directory (settings.data_protection.keys_directory, created with owner-only permissions where the OS supports it),
      one sub-key per purpose derived with HKDF-SHA256, AES-256-GCM ciphertexts with a versioned prefix ("hs1.") and
      HMAC-SHA256 signed time-limited tokens ("hs1t.") for pre-signed upload URLs. Ciphertexts written by the C#
      implementation (Data Protection "CfDJ8..." payloads) cannot be read and raise CryptographicError.
    * `UrlPolicy.problem` keeps the C# messages and order of checks and is stricter where .NET's Uri parsing was lenient:
      only http/https URLs, any non-global IP literal (IPv4, IPv6, IPv4-mapped IPv6, and integer/hex/octal IPv4 forms
      such as http://2130706433/) counts as private, and "*.localhost" names count as loopback.
    * The development identity provider's client-secret hashing (HealthSwap.Api/Auth.cs DevIdentityProvider: PBKDF2-SHA256,
      100 000 iterations, 16-byte salt, 32-byte hash, constant-time verify) lives here as `ClientSecret` so the API port can
      reuse it; secrets are never stored in the database (tenant.api_client holds metadata only).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import socket
import struct
import threading
import time
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from healthswap.domain.commerce.ledger import LedgerPosting
from healthswap.domain.primitives import UTC, Clock, as_utc, iso, new_id, utc_now
from healthswap.domain.text import _utf8
from healthswap.platform import jsonutil, netjson
from healthswap.platform.bus import BusMessage, Outbox, Topics
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.errors import HsError
from healthswap.platform.settings import WebhookOptions
from healthswap.platform.workers import BackgroundWorker

log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _unix_seconds(value: datetime) -> int:
    """DateTimeOffset.ToUnixTimeSeconds (floor)."""
    return (as_utc(value) - _EPOCH) // timedelta(seconds=1)


def _utf16_len(s: str) -> int:
    """C# string.Length (UTF-16 code units)."""
    return len(s) if s.isascii() else len(s.encode("utf-16-le", "surrogatepass")) // 2


class WebhookTypes:
    CATALOG_REVIEW = "catalog.match.review_required"
    PROFILE_CHANGED = "product.health_profile.changed"
    STATEMENT_ISSUED = "billing.statement.issued"
    DISPUTE_UPDATED = "billing.dispute.updated"
    ERASURE_COMPLETED = "user.erasure.completed"


# ---------------------------------------------------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------------------------------------------------

_LONG = re.compile(r"[ \t\n\v\f\r]*[+-]?[0-9]+[ \t\n\v\f\r]*")
_INT64 = (-(2**63), 2**63 - 1)


class WebhookSignature:
    """HMAC-SHA256 signatures: `HealthSwap-Signature: t=<unix>,v1=<hex>` over "t.body" (doc 04 webhook section)."""

    HEADER = "HealthSwap-Signature"

    @staticmethod
    def sign(secret: str, timestamp: int, body: str) -> str:
        return f"t={timestamp},v1={WebhookSignature._mac(secret, timestamp, body)}"

    @staticmethod
    def _mac(secret: str, timestamp: int, body: str) -> str:
        return hmac.new(_utf8(secret), _utf8(f"{timestamp}.{body}"), hashlib.sha256).hexdigest()

    @staticmethod
    def verify(secret: str, header: str, body: str, now: datetime, tolerance: timedelta) -> bool:
        """True when `header` carries a v1 signature of `body` made with `secret` at most `tolerance` away from `now`.

        Duplicate parameters make the header invalid (the C# ToDictionary threw on them)."""
        parts: dict[str, str] = {}
        for p in header.split(","):
            kv = p.split("=", 1)
            if len(kv) != 2:
                continue
            key = kv[0].strip()
            if key in parts:
                return False
            parts[key] = kv[1].strip()
        t, v1 = parts.get("t"), parts.get("v1")
        if t is None or v1 is None or not _LONG.fullmatch(t):
            return False
        ts = int(t)
        if not _INT64[0] <= ts <= _INT64[1]:
            return False
        if abs(_unix_seconds(now) - ts) > tolerance.total_seconds():
            return False
        return hmac.compare_digest(WebhookSignature._mac(secret, ts, body).encode("ascii"), v1.encode("utf-8", "surrogatepass"))


# ---------------------------------------------------------------------------------------------------------------------
# Data protection (ASP.NET Core Data Protection replacement)
# ---------------------------------------------------------------------------------------------------------------------


class CryptographicError(Exception):
    """C# CryptographicException: the payload was tampered with, expired, or protected with another key or purpose."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    if not text or not re.fullmatch(r"[A-Za-z0-9_-]+", text) or len(text) % 4 == 1:
        raise CryptographicError("The payload was invalid.")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise CryptographicError("The payload was invalid.") from None


class DataProtectionProvider:
    """Purpose-bound protectors over one persisted master key (C# IDataProtectionProvider with PersistKeysToFileSystem).

    The 256-bit master key is created on first use in `keys_directory/MASTER_KEY_FILE` (O_EXCL, mode 0600; the directory is
    created with mode 0700 - on Windows the user profile's ACLs apply). Every purpose gets its own key derived with
    HKDF-SHA256 (salt = application name, info = purpose chain), so a payload protected for one purpose never unprotects for
    another. Thread-safe.
    """

    MASTER_KEY_FILE = "healthswap-dataprotection.key"
    KEY_BYTES = 32

    def __init__(self, keys_directory: str | os.PathLike[str], application_name: str = "HealthSwap") -> None:
        self.keys_directory = Path(keys_directory)
        self.application_name = application_name
        self._lock = threading.Lock()
        self._master: bytes | None = None

    @property
    def key_path(self) -> Path:
        return self.keys_directory / self.MASTER_KEY_FILE

    def _master_key(self) -> bytes:
        key = self._master
        if key is None:
            with self._lock:
                if self._master is None:
                    self._master = self._load_or_create()
                key = self._master
        return key

    def _load_or_create(self) -> bytes:
        path = self.key_path
        if not self.keys_directory.is_dir():
            os.makedirs(self.keys_directory, mode=0o700, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            return self._read_existing(path)
        key = os.urandom(self.KEY_BYTES)
        try:
            os.write(fd, base64.b64encode(key) + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        log.info("Created data protection master key %s", path)
        return key

    def _read_existing(self, path: Path) -> bytes:
        # Another process may be writing the key right now: retry briefly before declaring the file corrupt.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                key = base64.b64decode(path.read_bytes().strip(), validate=True)
                if len(key) == self.KEY_BYTES:
                    return key
            except (binascii.Error, ValueError):
                pass
            if time.monotonic() > deadline:
                raise CryptographicError(f"The data protection key file {path} is invalid")
            time.sleep(0.05)

    @property
    def key_id(self) -> bytes:
        """4-byte identifier of the master key (written into every payload to detect a different key ring)."""
        return hashlib.sha256(self._master_key()).digest()[:4]

    def derive_key(self, *purposes: str) -> bytes:
        info = b"\x00".join(_utf8(p) for p in purposes)
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=_utf8(self.application_name), info=info).derive(self._master_key())

    def create_protector(self, purpose: str) -> DataProtector:
        return DataProtector(self, (purpose,))

    def create_time_limited_protector(self, purpose: str) -> TimeLimitedDataProtector:
        """C# CreateProtector(purpose).ToTimeLimitedDataProtector()."""
        return TimeLimitedDataProtector(self, (purpose, "TimeLimitedDataProtector.v1"))


class DataProtector:
    """AES-256-GCM: "hs1." + base64url(key id (4) | nonce (12) | ciphertext | tag (16)); the header is authenticated."""

    PREFIX = "hs1."

    def __init__(self, provider: DataProtectionProvider, purposes: tuple[str, ...]) -> None:
        self.provider = provider
        self.purposes = purposes
        self._aead: AESGCM | None = None

    def _cipher(self) -> AESGCM:
        if self._aead is None:
            self._aead = AESGCM(self.provider.derive_key(*self.purposes))
        return self._aead

    def protect(self, plaintext: str) -> str:
        key_id = self.provider.key_id
        nonce = os.urandom(12)
        aad = self.PREFIX.encode("ascii") + key_id
        return self.PREFIX + _b64url(key_id + nonce + self._cipher().encrypt(nonce, _utf8(plaintext), aad))

    def unprotect(self, protected: str) -> str:
        if not isinstance(protected, str) or not protected.startswith(self.PREFIX):
            raise CryptographicError("The provided payload cannot be decrypted because it was not protected with this protection provider.")
        raw = _b64url_decode(protected[len(self.PREFIX):])
        if len(raw) < 4 + 12 + 16:
            raise CryptographicError("The payload was invalid.")
        key_id, nonce, body = raw[:4], raw[4:16], raw[16:]
        if not hmac.compare_digest(key_id, self.provider.key_id):
            raise CryptographicError(f"The key {key_id.hex()} was not found in the key ring.")
        try:
            plain = self._cipher().decrypt(nonce, body, self.PREFIX.encode("ascii") + key_id)
        except InvalidTag:
            raise CryptographicError("The payload was invalid.") from None
        return plain.decode("utf-8")


class TimeLimitedDataProtector:
    """HMAC-SHA256 signed tokens with an expiry (C# ITimeLimitedDataProtector; the payload is signed, not encrypted):
    "hs1t." + base64url(key id (4) | expiry unix ms (8, big-endian) | UTF-8 payload | HMAC (32)). Expiry uses wall time."""

    PREFIX = "hs1t."

    def __init__(self, provider: DataProtectionProvider, purposes: tuple[str, ...], now: Callable[[], datetime] = utc_now) -> None:
        self.provider = provider
        self.purposes = purposes
        self._now = now
        self._key: bytes | None = None

    def _mac(self, data: bytes) -> bytes:
        if self._key is None:
            self._key = self.provider.derive_key(*self.purposes)
        return hmac.new(self._key, self.PREFIX.encode("ascii") + data, hashlib.sha256).digest()

    def protect(self, plaintext: str, expiration: datetime) -> str:
        ms = (as_utc(expiration) - _EPOCH) // timedelta(milliseconds=1)
        data = self.provider.key_id + struct.pack(">q", ms) + _utf8(plaintext)
        return self.PREFIX + _b64url(data + self._mac(data))

    def unprotect(self, protected: str) -> tuple[str, datetime]:
        """The payload and its expiration; CryptographicError when the token is invalid, signed with another key or
        purpose, or expired."""
        if not isinstance(protected, str) or not protected.startswith(self.PREFIX):
            raise CryptographicError("The provided payload cannot be decrypted because it was not protected with this protection provider.")
        raw = _b64url_decode(protected[len(self.PREFIX):])
        if len(raw) < 4 + 8 + 32:
            raise CryptographicError("The payload was invalid.")
        data, mac = raw[:-32], raw[-32:]
        if not hmac.compare_digest(data[:4], self.provider.key_id):
            raise CryptographicError(f"The key {data[:4].hex()} was not found in the key ring.")
        if not hmac.compare_digest(mac, self._mac(data)):
            raise CryptographicError("The payload was invalid.")
        (ms,) = struct.unpack(">q", data[4:12])
        try:
            expiration = _EPOCH + timedelta(milliseconds=ms)
        except OverflowError:
            raise CryptographicError("The payload was invalid.") from None
        if expiration < self._now():
            raise CryptographicError(f"The payload expired at {iso(expiration)}.")
        try:
            return data[12:].decode("utf-8"), expiration
        except UnicodeDecodeError:
            raise CryptographicError("The payload was invalid.") from None


class SecretProtector:
    """Encrypts webhook signing secrets at rest (purpose "HealthSwap.WebhookSecrets.v1"; KMS/Key Vault in production)."""

    PURPOSE = "HealthSwap.WebhookSecrets.v1"

    def __init__(self, provider: DataProtectionProvider) -> None:
        self._protector = provider.create_protector(self.PURPOSE)

    def protect(self, secret: str) -> str:
        return self._protector.protect(secret)

    def unprotect(self, ciphertext: str) -> str:
        return self._protector.unprotect(ciphertext)


@dataclass(frozen=True, slots=True)
class ClientSecret:
    """A client secret kept only as a PBKDF2-SHA256 hash (HealthSwap.Api/Auth.cs DevIdentityProvider.ClientSecret)."""

    salt: bytes
    hash: bytes

    ITERATIONS = 100_000

    @staticmethod
    def hash_secret(secret: str, salt: bytes | None = None) -> ClientSecret:
        """Rfc2898DeriveBytes.Pbkdf2(UTF8(secret), salt (16 random bytes), 100 000, SHA256, 32)."""
        salt = os.urandom(16) if salt is None else salt
        return ClientSecret(salt, hashlib.pbkdf2_hmac("sha256", _utf8(secret), salt, ClientSecret.ITERATIONS, 32))

    def verify(self, secret: str) -> bool:
        """Constant-time comparison (CryptographicOperations.FixedTimeEquals)."""
        return hmac.compare_digest(self.hash, hashlib.pbkdf2_hmac("sha256", _utf8(secret), self.salt, ClientSecret.ITERATIONS, 32))


# ---------------------------------------------------------------------------------------------------------------------
# URL policy (SSRF guard)
# ---------------------------------------------------------------------------------------------------------------------

_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")
_IPV4_LENIENT = re.compile(r"(?:0[xX][0-9a-fA-F]*|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]*|[0-9]+)){0,3}")


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP address a host literal denotes, including the inet_aton forms resolvers accept (127.1, 0x7f.0.0.1, 2130706433)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if _IPV4_LENIENT.fullmatch(host):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


class UrlPolicy:
    @staticmethod
    def problem(url: str, allow_http_localhost: bool) -> str | None:
        """Rejects non-https URLs, credentials in URLs and private/loopback IP literals (SSRF guard); None when acceptable."""
        text = url.strip() if isinstance(url, str) else ""
        try:
            parts = urlsplit(text)
            host = parts.hostname
            _ = parts.port  # raises ValueError for an invalid port
        except ValueError:
            return "URL is not absolute"
        if not parts.scheme or not parts.netloc or not host or _CONTROL_OR_SPACE.search(text) or "\\" in text:
            return "URL is not absolute"
        if "@" in parts.netloc:
            return "URL must not contain credentials"
        scheme = parts.scheme.lower()
        ip = _ip_literal(host.rstrip("."))
        mapped = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
        loopback = (ip is not None and (ip.is_loopback or (mapped is not None and mapped.is_loopback)))
        name = host.rstrip(".").lower()
        if loopback or name == "localhost" or name.endswith(".localhost"):
            if not allow_http_localhost:
                return "Loopback hosts are not allowed"
            return None if scheme in ("http", "https") else "Webhook URLs must use https"
        if scheme != "https":
            return "Webhook URLs must use https"
        if ip is not None and UrlPolicy.is_private(ip):
            return "Private network addresses are not allowed"
        return None

    @staticmethod
    def is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        """Any address that is not globally routable (private, loopback, link-local, CGNAT, multicast, reserved, ...)."""
        if isinstance(ip, ipaddress.IPv6Address):
            for embedded in (ip.ipv4_mapped, ip.sixtofour):
                if embedded is not None and UrlPolicy.is_private(embedded):
                    return True
            if ip.teredo is not None:
                return True
        return (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            or not ip.is_global
        )

    @staticmethod
    def resolve(host: str, port: int, allow_loopback: bool) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """Every address `host` resolves to (socket.getaddrinfo, in resolver order), all of them vetted: any private, loopback,
        link-local, multicast, reserved or unspecified address refuses the host (loopback is accepted when `allow_loopback`).
        Connect to these addresses only, never to the name again (a second lookup could be rebound). Raises WebhookTargetRefused."""
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as ex:
            raise WebhookTargetRefused(f"Host {host} could not be resolved: {ex}") from ex
        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for info in infos:
            text = str(info[4][0]).split("%", 1)[0]  # IPv6 zone index
            try:
                ip = ipaddress.ip_address(text)
            except ValueError as ex:
                raise WebhookTargetRefused(f"Host {host} resolved to an invalid address {text!r}") from ex
            if ip not in addresses:
                addresses.append(ip)
        if not addresses:
            raise WebhookTargetRefused(f"Host {host} did not resolve to any address")
        for ip in addresses:
            mapped = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
            if allow_loopback and (ip.is_loopback or (mapped is not None and mapped.is_loopback)):
                continue
            if UrlPolicy.is_private(ip):
                raise WebhookTargetRefused(f"Private network addresses are not allowed: {host} resolves to {ip}")
        return addresses


class WebhookTargetRefused(Exception):
    """The webhook endpoint must not be called: its host does not resolve to public addresses only (SSRF guard)."""


# ---------------------------------------------------------------------------------------------------------------------
# Webhook fan-out and delivery
# ---------------------------------------------------------------------------------------------------------------------


def _ordinal(s: str) -> bytes:
    """Sort key of StringComparer.Ordinal (UTF-16 code units)."""
    return s.encode("utf-16-be", "surrogatepass")


class WebhookFanoutConsumer:
    """Turns domain events into per-endpoint webhook deliveries (hs_ops: needs endpoint and catalog lookups across roles).

    Webhook delivery is infrastructure: it runs on wall-clock time, not on the (time-travel capable) business clock.
    """

    TOPIC = "hs.webhook-requests.v1"

    def __init__(self, db: Db) -> None:
        self.db = db
        self.topics: Collection[str] = (self.TOPIC, Topics.KB_PRODUCT_CHANGES)

    @staticmethod
    def request(u: Uow, partner_id: uuid.UUID, type_: str, data: Any) -> None:
        """Requests a webhook from inside any unit of work (written to the outbox; delivered asynchronously)."""
        Outbox.enqueue(u, partner_id, WebhookFanoutConsumer.TOPIC, str(partner_id),
                       {"type": type_, "partnerId": partner_id, "data": data, "id": "evt_" + new_id().hex})

    def handle(self, m: BusMessage) -> None:
        payload = m.payload
        if m.topic == Topics.KB_PRODUCT_CHANGES:
            kind = payload.get("kind") if isinstance(payload, Mapping) else None
            if kind != "PROFILE_CHANGED":
                return
            product_id = uuid.UUID(str(payload["productId"]))
            market = payload["market"]
            changes = list(payload["changes"])
            if changes == ["CREATED"]:
                return  # initial scoring is not a change for partners
            skus = self.db.in_tx(DbRole.OPS, None, lambda u: u.query(
                "select c.partner_id, c.partner_sku from tenant.catalog_item c join tenant.partner p using (partner_id) where c.product_id = @productId and @market = any(p.markets)",
                {"productId": product_id, "market": market},
            ))
            groups: dict[uuid.UUID, list[str]] = {}
            for s in skus:
                groups.setdefault(s["partner_id"], []).append(s["partner_sku"])
            previous = payload.get("previousProfileVersion")
            for partner_id, partner_skus in groups.items():
                self._create_deliveries(partner_id, WebhookTypes.PROFILE_CHANGED, "evt_" + new_id().hex, {
                    "productId": product_id, "market": market, "partnerSkus": sorted(partner_skus, key=_ordinal),
                    "previousProfileVersion": previous if isinstance(previous, str) else None,
                    "profileVersion": payload["profileVersion"], "changes": changes,
                })
            return
        partner_id = uuid.UUID(str(payload["partnerId"]))
        self._create_deliveries(partner_id, payload["type"], payload["id"], payload["data"])

    def _create_deliveries(self, partner_id: uuid.UUID, type_: str, webhook_id: str, data: Any) -> None:
        envelope = {
            "id": webhook_id, "type": type_, "createdAt": iso(utc_now()), "apiVersion": "1.1", "partnerId": str(partner_id),
            "data": jsonutil.to_plain(data),
        }
        payload = jsonutil.dumps(envelope)

        def work(u: Uow) -> int:
            endpoints = [
                e for e in u.query(
                    "select endpoint_id, partner_id, event_types from tenant.webhook_endpoint where partner_id = @partnerId and status = 'ACTIVE'",
                    {"partnerId": partner_id},
                )
                if len(e["event_types"]) == 0 or type_ in e["event_types"]
            ]
            for e in endpoints:
                u.execute(
                    "insert into txn.webhook_delivery (webhook_id, partner_id, endpoint_id, event_type, payload, next_attempt_at) values (@webhookId, @partnerId, @endpointId, @type, @payload::jsonb, @now)",
                    {"webhookId": webhook_id, "partnerId": partner_id, "endpointId": e["endpoint_id"], "type": type_, "payload": payload, "now": utc_now()},
                )
            return len(endpoints)

        n = self.db.in_tx(DbRole.OPS, None, work)
        log.info("Webhook %s %s queued for %d endpoint(s) of partner %s", type_, webhook_id, n, partner_id)


class WebhookDispatcher(BackgroundWorker):
    """At-least-once signed delivery with exponential backoff for up to 24 hours, then DEAD (HLD 8.4).

    `http` must not follow redirects (the platform registers an httpx.Client with follow_redirects=False); None creates one.
    """

    BACKOFF_SECONDS = (10, 30, 120, 600, 1800, 3600, 7200, 14400)
    LEASE_SECONDS = 60

    def __init__(self, db: Db, http: httpx.Client | None, secrets: SecretProtector, options: WebhookOptions) -> None:
        super().__init__()
        self.db = db
        self.http = http if http is not None else httpx.Client(follow_redirects=False, timeout=float(options.timeout_seconds))
        self.secrets = secrets
        self.options = options

    def dispatch_due(self) -> int:
        """Sends every due delivery once; returns how many were attempted."""
        now = utc_now()

        # Claim a batch with a short lease, then send outside the transaction (never hold a transaction during HTTP calls).
        def claim(u: Uow) -> list[dict[str, Any]]:
            rows = u.query(
                """
                select d.delivery_id, d.webhook_id, d.partner_id, d.payload::text as payload, d.attempts, d.created_at, e.url, e.secret_ciphertext
                from txn.webhook_delivery d join tenant.webhook_endpoint e on e.endpoint_id = d.endpoint_id
                where d.status = 'PENDING' and d.next_attempt_at <= @now order by d.next_attempt_at limit 50 for update of d skip locked
                """,
                {"now": now},
            )
            if rows:
                u.execute(
                    "update txn.webhook_delivery set next_attempt_at = @lease where delivery_id = any(@ids)",
                    {"lease": now + timedelta(seconds=self.LEASE_SECONDS), "ids": [r["delivery_id"] for r in rows]},
                )
            return rows

        due = self.db.in_tx(DbRole.OPS, None, claim)
        for d in due:
            status: int | None = None
            error = UrlPolicy.problem(d["url"], self.options.allow_http_localhost)
            if error is None:
                try:
                    secret = self.secrets.unprotect(d["secret_ciphertext"])
                    ts = _unix_seconds(utc_now())
                    res = self.post(
                        d["url"], _utf8(d["payload"]),
                        {
                            "Content-Type": "application/json; charset=utf-8", WebhookSignature.HEADER: WebhookSignature.sign(secret, ts, d["payload"]),
                            "HealthSwap-Webhook-Id": d["webhook_id"],
                        },
                    )
                    status = res.status_code
                    if not res.is_success:
                        error = f"HTTP {status}"
                except WebhookTargetRefused as ex:
                    error = str(ex)
                except (httpx.HTTPError, CryptographicError) as ex:
                    error = f"{type(ex).__name__}: {ex}"

            attempts = d["attempts"] + 1
            delivered = error is None
            dead = not delivered and (utc_now() - as_utc(d["created_at"])) / timedelta(hours=1) >= self.options.max_attempt_hours
            next_at = utc_now() + timedelta(seconds=self.BACKOFF_SECONDS[min(attempts - 1, len(self.BACKOFF_SECONDS) - 1)])
            self.db.in_tx(DbRole.OPS, None, lambda u, d=d, attempts=attempts, status=status, error=error, delivered=delivered, dead=dead, next_at=next_at: u.execute(
                """
                update txn.webhook_delivery set attempts = @attempts, last_status_code = @status, last_error = @error,
                    status = case when @delivered then 'DELIVERED' when @dead then 'DEAD' else 'PENDING' end,
                    delivered_at = case when @delivered then @now else null end, next_attempt_at = @next
                where delivery_id = @id
                """,
                {
                    "attempts": attempts, "status": status, "error": error, "delivered": delivered, "dead": dead, "now": utc_now(), "next": next_at,
                    "id": d["delivery_id"],
                },
            ))
            if not delivered:
                self.log.warning("Webhook %s attempt %d failed: %s", d["webhook_id"], attempts, error)
        return len(due)

    def post(self, url: str, content: bytes, headers: Mapping[str, str]) -> httpx.Response:
        """POSTs to `url` pinned to a vetted address of its host (UrlPolicy.resolve): the request goes to the IP literal with the
        original Host header and, for https, TLS SNI and certificate verification on the real host name, so a second DNS answer
        (rebinding) cannot redirect it. Tries the vetted addresses in resolver order until one accepts the connection; redirects
        are never followed. Raises WebhookTargetRefused or httpx.HTTPError."""
        try:
            target = httpx.URL(url)
            host = target.raw_host.decode("ascii")  # IDNA-encoded name or bare IP literal
        except (httpx.InvalidURL, UnicodeError) as ex:
            raise WebhookTargetRefused(f"URL is not valid: {ex}") from ex
        addresses = UrlPolicy.resolve(host, target.port or (443 if target.scheme == "https" else 80), self.options.allow_http_localhost)
        # Connection: close keeps the pinned connection out of the pool (a pooled IP origin could serve another host name).
        pinned = {**headers, "Host": httpx.Request("POST", target).headers["Host"], "Connection": "close"}
        extensions = {"sni_hostname": host} if target.scheme == "https" and _ip_literal(host) is None else {}
        failure: httpx.ConnectError | None = None
        for ip in addresses:
            try:
                return self.http.post(
                    target.copy_with(host=str(ip)), content=content, headers=pinned, timeout=float(self.options.timeout_seconds),
                    follow_redirects=False, extensions=extensions,
                )
            except httpx.ConnectError as ex:
                failure = ex
        assert failure is not None
        raise failure

    def execute(self) -> None:
        while not self.stopping.is_set():
            try:
                self.dispatch_due()
            except Exception:
                self.log.warning("Webhook dispatch iteration failed", exc_info=True)
            if not self.sleep(1.0):
                break


# ---------------------------------------------------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdempotentResponse:
    status: int
    body: str


class IdempotencyService:
    """Idempotency-Key handling (HLD 8.2): same key + same payload replays the stored response; a different payload is 409."""

    def __init__(self, db: Db, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    @staticmethod
    def request_hash(body: bytes) -> bytes:
        """SHA-256 of the raw request body (stored in txn.idempotency_key.request_hash)."""
        return hashlib.sha256(body).digest()

    @staticmethod
    def problem_json(ex: HsError) -> str:
        """The RFC 9457 problem document stored (and replayed) for a 4xx HsError (C# Problem with HsJson.Options)."""
        return netjson.serialize_hs({
            "type": "https://healthswap.example/problems/" + ex.code.lower(), "title": ex.title, "status": ex.status, "detail": ex.detail,
            "code": ex.code, "errors": None if ex.errors is None else [e.to_api() for e in ex.errors],
        })

    def execute(self, partner_id: uuid.UUID, key: str, method: str, path: str, body: bytes,
                handler: Callable[[], IdempotentResponse]) -> IdempotentResponse:
        if _utf16_len(key) == 0 or _utf16_len(key) > 255:
            raise HsError.invalid("Idempotency-Key must be 1-255 characters")
        request_hash = self.request_hash(body)
        now = self.clock.utc_now()

        def claim(u: Uow) -> dict[str, Any] | None:
            u.execute("delete from txn.idempotency_key where partner_id = @partnerId and key = @key and expires_at < @now", {"partnerId": partner_id, "key": key, "now": now})
            inserted = u.execute(
                """
                insert into txn.idempotency_key (partner_id, key, method, path, request_hash, expires_at) values (@partnerId, @key, @method, @path, @hash, @exp)
                on conflict (partner_id, key) do nothing
                """,
                {"partnerId": partner_id, "key": key, "method": method, "path": path, "hash": request_hash, "exp": now + timedelta(hours=24)},
            )
            return None if inserted == 1 else u.single(
                "select method, path, request_hash, status_code, response_body from txn.idempotency_key where partner_id = @partnerId and key = @key",
                {"partnerId": partner_id, "key": key},
            )

        existing = self.db.in_tx(DbRole.APP, partner_id, claim)
        if existing is not None:
            if existing["method"] != method or existing["path"] != path or bytes(existing["request_hash"]) != request_hash:
                raise HsError.conflict("HS-IDEMPOTENCY-CONFLICT", "This Idempotency-Key was used with a different request")
            if existing["status_code"] is None:
                raise HsError.conflict("HS-IDEMPOTENCY-CONFLICT", "A request with this Idempotency-Key is still being processed")
            return IdempotentResponse(existing["status_code"], existing["response_body"] or "")

        try:
            response = handler()
        except HsError as ex:
            if ex.status < 500:
                self._store(partner_id, key, ex.status, self.problem_json(ex))
            else:
                self._forget(partner_id, key)
            raise
        except BaseException:
            self._forget(partner_id, key)
            raise
        self._store(partner_id, key, response.status, response.body)
        return response

    def _forget(self, partner_id: uuid.UUID, key: str) -> None:
        self.db.in_tx(DbRole.APP, partner_id, lambda u: u.execute(
            "delete from txn.idempotency_key where partner_id = @partnerId and key = @key", {"partnerId": partner_id, "key": key}))

    def _store(self, partner_id: uuid.UUID, key: str, status: int, body: str) -> None:
        self.db.in_tx(DbRole.APP, partner_id, lambda u: u.execute(
            "update txn.idempotency_key set status_code = @status, response_body = @body where partner_id = @partnerId and key = @key",
            {"partnerId": partner_id, "key": key, "status": status, "body": body},
        ))


# ---------------------------------------------------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------------------------------------------------


class LedgerService:
    """Double-entry ledger writer (HLD 9.6). Callers run inside a unit of work with the hs_ledger_writer role."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def post(self, u: Uow, partner_id: uuid.UUID, currency: str, posting: LedgerPosting, created_by: str,
             effective_at: datetime | None = None) -> bool:
        """Writes the journal entry and its lines; False when the posting's idempotency key was already posted."""
        if u.role != DbRole.LEDGER_WRITER:
            raise RuntimeError("Ledger postings require the hs_ledger_writer role")
        if u.scalar("select exists (select 1 from billing.journal_entry where idempotency_key = @key)", {"key": posting.idempotency_key}):
            return False
        entry_id = new_id()
        u.execute(
            """
            insert into billing.journal_entry (entry_id, partner_id, entry_kind, reference_type, reference_id, effective_at, created_by, idempotency_key)
            values (@entryId, @partnerId, @kind, @refType, @refId, @at, @by, @key)
            """,
            {
                "entryId": entry_id, "partnerId": partner_id, "kind": posting.kind, "refType": posting.reference_type, "refId": posting.reference_id,
                "at": self.clock.utc_now() if effective_at is None else effective_at, "by": created_by, "key": posting.idempotency_key,
            },
        )
        for line_no, line in enumerate(posting.lines, start=1):
            account = self._account(u, partner_id, line.account, currency)
            u.execute(
                "insert into billing.journal_line (entry_id, line_no, partner_id, account_id, debit, credit) values (@entryId, @lineNo, @partnerId, @account, @debit, @credit)",
                {"entryId": entry_id, "lineNo": line_no, "partnerId": partner_id, "account": account, "debit": line.debit, "credit": line.credit},
            )
        return True

    @staticmethod
    def _account(u: Uow, partner_id: uuid.UUID, code: str, currency: str) -> uuid.UUID:
        u.execute(
            "insert into billing.account (partner_id, code, currency) values (@partnerId, @code, @currency) on conflict (partner_id, code, currency) do nothing",
            {"partnerId": partner_id, "code": code, "currency": currency},
        )
        return u.single(
            "select account_id from billing.account where partner_id = @partnerId and code = @code and currency = @currency",
            {"partnerId": partner_id, "code": code, "currency": currency},
        )["account_id"]
