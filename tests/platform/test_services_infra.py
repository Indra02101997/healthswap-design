"""Unit tests for the services infrastructure port (Services/Infrastructure.cs, the pure parts of CatalogServices.cs and
ReadServices.cs), the container and the CLI. No database: units that touch PostgreSQL run against small fakes, and every
Settings object points at a closed local port so nothing can reach (let alone write to) the development database."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from healthswap import cli
from healthswap.domain.commerce.ledger import Postings
from healthswap.domain.primitives import iso, utc_now
from healthswap.platform import netjson
from healthswap.platform.container import Container, service
from healthswap.platform.db import DbRole
from healthswap.platform.errors import FieldError, HsError
from healthswap.platform.hot_cache import ShopperPrefsDoc
from healthswap.platform.services.catalog import CatalogService, FeedService, PreferenceService, read_catalog_item
from healthswap.platform.services.infrastructure import (
    ClientSecret,
    CryptographicError,
    DataProtectionProvider,
    IdempotencyService,
    IdempotentResponse,
    LedgerService,
    SecretProtector,
    UrlPolicy,
    WebhookDispatcher,
    WebhookFanoutConsumer,
    WebhookSignature,
    WebhookTypes,
)
from healthswap.platform.settings import FeedOptions, Settings, WebhookOptions, load_settings
from healthswap.platform.workers import BackgroundWorker

PARTNER = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01")
# A closed local port: no test in this module may reach a real database (in particular not the dev DB on 55432).
UNREACHABLE_DB = "postgresql://postgres:postgres@127.0.0.1:9/postgres?sslmode=disable&connect_timeout=2"


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Any:
    """cli.main configures the root logger (stderr handler, INFO); keep that from leaking into other tests."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
    for h in handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)


# ---------------------------------------------------------------------------------------------------------------------
# Webhook signatures
# ---------------------------------------------------------------------------------------------------------------------

BODY = '{"id":"evt_1","type":"catalog.match.review_required","data":{"x":"Bhujia – ₹"}}'
TS = 1_766_000_000
NOW = datetime.fromtimestamp(TS, UTC)
FIVE_MIN = timedelta(minutes=5)


def test_signature_header_format_and_hmac() -> None:
    header = WebhookSignature.sign("whsec_test", TS, BODY)
    expected = hmac.new(b"whsec_test", f"{TS}.{BODY}".encode(), hashlib.sha256).hexdigest()
    assert header == f"t={TS},v1={expected}"
    assert WebhookSignature.HEADER == "HealthSwap-Signature"


def test_signature_verifies_within_tolerance() -> None:
    header = WebhookSignature.sign("whsec_test", TS, BODY)
    assert WebhookSignature.verify("whsec_test", header, BODY, NOW, FIVE_MIN)
    assert WebhookSignature.verify("whsec_test", header, BODY, NOW + FIVE_MIN, FIVE_MIN)
    assert WebhookSignature.verify("whsec_test", header, BODY, NOW - FIVE_MIN, FIVE_MIN)
    # Whitespace around parameters and unknown parameters are tolerated.
    v1 = header.split("v1=")[1]
    assert WebhookSignature.verify("whsec_test", f" t = {TS} , v0=abc, v1 = {v1} ", BODY, NOW, FIVE_MIN)


@pytest.mark.parametrize("mutate",
                         [lambda h: h, lambda h: h.replace("t=", "t= "), lambda h: h + ",v0=ignored"])
def test_signature_rejects_tampered_body_and_wrong_secret(mutate: Any) -> None:
    header = mutate(WebhookSignature.sign("whsec_test", TS, BODY))
    assert WebhookSignature.verify("whsec_test", header, BODY, NOW, FIVE_MIN)
    assert not WebhookSignature.verify("whsec_test", header, BODY.replace("evt_1", "evt_2"), NOW, FIVE_MIN)
    assert not WebhookSignature.verify("whsec_other", header, BODY, NOW, FIVE_MIN)


def test_signature_rejects_stale_future_duplicate_and_malformed_headers() -> None:
    header = WebhookSignature.sign("whsec_test", TS, BODY)
    v1 = header.split("v1=")[1]
    late = NOW + FIVE_MIN + timedelta(seconds=1)
    assert not WebhookSignature.verify("whsec_test", header, BODY, late, FIVE_MIN)
    assert not WebhookSignature.verify("whsec_test", header, BODY, NOW - FIVE_MIN - timedelta(seconds=1), FIVE_MIN)
    for bad in (
        f"t={TS},t={TS},v1={v1}",  # duplicate parameter (C# ToDictionary threw -> invalid)
        f"t={TS},v1={v1},v1={v1}",
        f"t={TS}",
        f"v1={v1}",
        f"t=abc,v1={v1}",
        f"t={TS}.5,v1={v1}",
        f"t=99999999999999999999,v1={v1}",
        f"t={TS},v1={v1[:-1]}",
        f"t={TS},v1={v1.upper()}",
        "",
    ):
        assert not WebhookSignature.verify("whsec_test", bad, BODY, NOW, FIVE_MIN), bad


# ---------------------------------------------------------------------------------------------------------------------
# URL policy (SSRF guard)
# ---------------------------------------------------------------------------------------------------------------------

LOOPBACK = "Loopback hosts are not allowed"
HTTPS = "Webhook URLs must use https"
PRIVATE = "Private network addresses are not allowed"
NOT_ABSOLUTE = "URL is not absolute"
CREDENTIALS = "URL must not contain credentials"


@pytest.mark.parametrize(
    ("url", "allow_localhost", "problem"),
    [
        ("https://hooks.partner.example/healthswap", False, None),
        ("https://hooks.partner.example:8443/x?y=1", False, None),
        ("HTTPS://Hooks.Partner.Example/x", False, None),
        ("https://8.8.8.8/hook", False, None),
        ("https://[2001:4860:4860::8888]/hook", False, None),
        ("http://hooks.partner.example/healthswap", False, HTTPS),
        ("http://hooks.partner.example/healthswap", True, HTTPS),
        ("http://localhost:9000/hook", True, None),
        ("https://localhost/hook", True, None),
        ("http://localhost:9000/hook", False, LOOPBACK),
        ("http://LOCALHOST./hook", False, LOOPBACK),
        ("https://api.localhost/hook", False, LOOPBACK),
        ("http://127.0.0.1:8080/hook", False, LOOPBACK),
        ("http://127.0.0.1:8080/hook", True, None),
        ("https://127.1/hook", False, LOOPBACK),
        ("https://2130706433/hook", False, LOOPBACK),
        ("https://0x7f000001/hook", False, LOOPBACK),
        ("https://[::1]/hook", False, LOOPBACK),
        ("https://[::ffff:127.0.0.1]/hook", False, LOOPBACK),
        ("ftp://localhost/hook", True, HTTPS),
        ("https://10.0.0.1/hook", False, PRIVATE),
        ("https://10.0.0.1/hook", True, PRIVATE),
        ("https://172.16.4.2/hook", False, PRIVATE),
        ("https://172.31.255.255/hook", False, PRIVATE),
        ("https://192.168.1.10/hook", False, PRIVATE),
        ("https://169.254.169.254/latest/meta-data", False, PRIVATE),
        ("https://0.0.0.0/hook", False, PRIVATE),
        ("https://100.64.0.1/hook", False, PRIVATE),
        ("https://[fd00::1]/hook", False, PRIVATE),
        ("https://[fe80::1]/hook", False, PRIVATE),
        ("https://[::ffff:10.0.0.1]/hook", False, PRIVATE),
        ("https://[2002:a00:1::1]/hook", False, PRIVATE),  # 6to4 of 10.0.0.1
        ("https://012.0.0.1/hook", False, PRIVATE),  # octal 10.0.0.1
        ("https://user:pass@hooks.partner.example/x", False, CREDENTIALS),
        ("https://user@hooks.partner.example/x", False, CREDENTIALS),
        ("hooks.partner.example/x", False, NOT_ABSOLUTE),
        ("/relative/path", False, NOT_ABSOLUTE),
        ("", False, NOT_ABSOLUTE),
        ("https://", False, NOT_ABSOLUTE),
        ("https://hooks.partner.example:99999/x", False, NOT_ABSOLUTE),
        ("https://hooks partner.example/x", False, NOT_ABSOLUTE),
        ("https://hooks.partner.example\\@10.0.0.1/x", False, NOT_ABSOLUTE),
        ("javascript:alert(1)", False, NOT_ABSOLUTE),
        ("file:///etc/passwd", False, NOT_ABSOLUTE),
    ],
)
def test_url_policy(url: str, allow_localhost: bool, problem: str | None) -> None:
    assert UrlPolicy.problem(url, allow_localhost) == problem


# ---------------------------------------------------------------------------------------------------------------------
# Data protection: webhook secrets and time-limited tokens
# ---------------------------------------------------------------------------------------------------------------------


def test_secret_protector_round_trip_and_key_persistence(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    protector = SecretProtector(DataProtectionProvider(keys))
    a, b = protector.protect("whsec_quickbasket"), protector.protect("whsec_quickbasket")
    assert a.startswith("hs1.") and b.startswith("hs1.") and a != b  # random nonce
    assert "whsec" not in a
    assert protector.unprotect(a) == "whsec_quickbasket"
    key_file = keys / DataProtectionProvider.MASTER_KEY_FILE
    assert key_file.is_file()
    if os.name == "posix":
        assert key_file.stat().st_mode & 0o777 == 0o600
    # Another provider instance (another process) on the same directory reuses the persisted key.
    assert SecretProtector(DataProtectionProvider(keys)).unprotect(a) == "whsec_quickbasket"
    # Unicode secrets survive.
    assert protector.unprotect(protector.protect("ключ-✓-😀")) == "ключ-✓-😀"


def _tamper(token: str) -> str:
    """Changes the second-to-last base64url character (always 6 data bits, unlike the last one)."""
    return token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]


def test_secret_protector_rejects_other_keys_purposes_and_tampering(tmp_path: Path) -> None:
    provider = DataProtectionProvider(tmp_path / "keys")
    ciphertext = SecretProtector(provider).protect("whsec_1")
    with pytest.raises(CryptographicError):
        SecretProtector(DataProtectionProvider(tmp_path / "other-keys")).unprotect(ciphertext)
    with pytest.raises(CryptographicError):
        provider.create_protector("HealthSwap.FeedUpload.v1").unprotect(ciphertext)
    for bad in (_tamper(ciphertext), ciphertext[:-4], "hs1.", "hs1.!!!!", "CfDJ8AAAAAAAAAAAAAAAAAAAAAA", ""):
        with pytest.raises(CryptographicError):
            SecretProtector(provider).unprotect(bad)


def test_corrupt_key_file_is_reported(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / DataProtectionProvider.MASTER_KEY_FILE).write_text("not base64 !!")
    provider = DataProtectionProvider(keys)
    with pytest.raises(CryptographicError):
        SecretProtector(provider).protect("x")


def test_time_limited_tokens(tmp_path: Path) -> None:
    provider = DataProtectionProvider(tmp_path / "keys")
    clock = {"now": datetime(2026, 1, 1, 12, 0, tzinfo=UTC)}
    protector = provider.create_time_limited_protector("HealthSwap.FeedUpload.v1")
    protector._now = lambda: clock["now"]  # injectable wall clock
    expires = datetime(2026, 1, 1, 13, 0, 0, 123_456, tzinfo=UTC)
    token = protector.protect(f"{PARTNER.hex}.{uuid.UUID(int=7).hex}", expires)
    assert token.startswith("hs1t.")
    payload, expiration = protector.unprotect(token)
    assert payload == f"{PARTNER.hex}.{uuid.UUID(int=7).hex}"
    assert expiration == datetime(2026, 1, 1, 13, 0, 0, 123_000, tzinfo=UTC)  # millisecond precision
    clock["now"] = datetime(2026, 1, 1, 13, 0, 1, tzinfo=UTC)
    with pytest.raises(CryptographicError, match="expired"):
        protector.unprotect(token)
    clock["now"] = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    other = provider.create_time_limited_protector("HealthSwap.Other.v1")
    other._now = protector._now
    with pytest.raises(CryptographicError):
        other.unprotect(token)
    with pytest.raises(CryptographicError):  # a plain protector's payload is not a token
        protector.unprotect(provider.create_protector("HealthSwap.FeedUpload.v1").protect("x"))
    with pytest.raises(CryptographicError):
        protector.unprotect(_tamper(token))


def test_client_secret_pbkdf2() -> None:
    hashed = ClientSecret.hash_secret("qb-dev-secret")
    assert len(hashed.salt) == 16 and len(hashed.hash) == 32
    assert hashed.verify("qb-dev-secret")
    assert not hashed.verify("qb-dev-secret ")
    assert ClientSecret.hash_secret("qb-dev-secret").salt != hashed.salt
    fixed = ClientSecret.hash_secret("s3cr3t", bytes(range(16)))
    assert fixed.hash == hashlib.pbkdf2_hmac("sha256", b"s3cr3t", bytes(range(16)), 100_000, 32)


# ---------------------------------------------------------------------------------------------------------------------
# Webhook fan-out request and delivery
# ---------------------------------------------------------------------------------------------------------------------


class RecordingUow:
    def __init__(self, role: DbRole = DbRole.APP) -> None:
        self.role = role
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> int:
        self.calls.append((sql, dict(params or {})))
        return 1


def test_webhook_request_goes_to_the_outbox() -> None:
    u = RecordingUow()
    WebhookFanoutConsumer.request(u, PARTNER, WebhookTypes.ERASURE_COMPLETED, {"requestId": uuid.UUID(int=1), "completedAt": NOW})
    [(sql, params)] = u.calls
    assert "insert into txn.outbox" in sql
    assert params["topic"] == WebhookFanoutConsumer.TOPIC == "hs.webhook-requests.v1" and params["key"] == str(PARTNER)
    payload = json.loads(params["payload"])
    assert list(payload) == ["type", "partnerId", "data", "id"]
    assert payload["type"] == "user.erasure.completed" and payload["partnerId"] == str(PARTNER)
    assert payload["data"] == {"requestId": str(uuid.UUID(int=1)), "completedAt": iso(NOW)}
    assert payload["id"].startswith("evt_") and len(payload["id"]) == 36


class DispatchDb:
    """Fake Db for WebhookDispatcher: the claim query returns `rows`; updates are recorded."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.leases: list[dict[str, Any]] = []
        self.updates: dict[uuid.UUID, dict[str, Any]] = {}

    def in_tx(self, role: DbRole, partner_id: Any, work: Any) -> Any:
        assert role == DbRole.OPS
        return work(self)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        assert "for update of d skip locked" in sql
        return [dict(r) for r in self.rows]

    def execute(self, sql: str, params: dict[str, Any]) -> int:
        if "set next_attempt_at = @lease" in sql:
            self.leases.append(params)
        else:
            self.updates[params["id"]] = params
        return 1


def test_webhook_dispatcher_signs_delivers_and_backs_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = SecretProtector(DataProtectionProvider(tmp_path / "keys"))
    now = utc_now()
    # The dispatcher resolves the endpoint host and connects to the vetted address (no real DNS in unit tests).
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])

    def row(n: int, url: str, created: datetime = now, secret: str | None = None, attempts: int = 0) -> dict[str, Any]:
        return {
            "delivery_id": uuid.UUID(int=n), "webhook_id": f"evt_{n}", "partner_id": PARTNER, "payload": f'{{"id":"evt_{n}","data":{{"n":{n}}}}}',
            "attempts": attempts, "created_at": created, "url": url, "secret_ciphertext": secret or secrets.protect(f"whsec_{n}"),
        }

    db = DispatchDb([
        row(1, "https://hooks.partner.example/ok"),
        row(2, "https://10.0.0.5/hook"),
        row(3, "https://hooks.partner.example/fail", attempts=2),
        row(4, "https://hooks.partner.example/fail", created=now - timedelta(hours=25)),
        row(5, "https://hooks.partner.example/ok", secret="CfDJ8-written-by-the-dotnet-data-protection"),
        row(6, "https://hooks.partner.example/redirect"),
    ])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/fail":
            return httpx.Response(500)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        return httpx.Response(204)

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    dispatcher = WebhookDispatcher(db, http, secrets, WebhookOptions())
    assert dispatcher.dispatch_due() == 6

    assert [str(r.url) for r in seen] == [
        "https://93.184.216.34/ok", "https://93.184.216.34/fail", "https://93.184.216.34/fail", "https://93.184.216.34/redirect",
    ]
    assert {(r.headers["Host"], r.extensions["sni_hostname"]) for r in seen} == {("hooks.partner.example", "hooks.partner.example")}
    first = seen[0]
    assert first.method == "POST" and first.headers["Content-Type"] == "application/json; charset=utf-8"
    assert first.headers["HealthSwap-Webhook-Id"] == "evt_1"
    assert first.content == b'{"id":"evt_1","data":{"n":1}}'
    assert WebhookSignature.verify("whsec_1", first.headers["HealthSwap-Signature"], first.content.decode(), utc_now(), FIVE_MIN)
    assert len(db.leases) == 1 and len(db.leases[0]["ids"]) == 6

    u = db.updates
    ok = u[uuid.UUID(int=1)]
    assert (ok["delivered"], ok["dead"], ok["status"], ok["error"], ok["attempts"]) == (True, False, 204, None, 1)
    private = u[uuid.UUID(int=2)]
    assert (private["delivered"], private["status"], private["error"]) == (False, None, "Private network addresses are not allowed")
    assert timedelta(seconds=9) < private["next"] - utc_now() <= timedelta(seconds=10)
    failed = u[uuid.UUID(int=3)]
    assert (failed["delivered"], failed["dead"], failed["status"], failed["error"], failed["attempts"]) == (False, False, 500, "HTTP 500", 3)
    assert timedelta(seconds=119) < failed["next"] - utc_now() <= timedelta(seconds=120)  # third attempt: 120 s backoff
    dead = u[uuid.UUID(int=4)]
    assert (dead["delivered"], dead["dead"]) == (False, True)
    foreign = u[uuid.UUID(int=5)]
    assert foreign["delivered"] is False and foreign["error"].startswith("CryptographicError")
    redirect = u[uuid.UUID(int=6)]
    assert (redirect["delivered"], redirect["status"], redirect["error"]) == (False, 302, "HTTP 302")  # redirects are not followed


# ---------------------------------------------------------------------------------------------------------------------
# Idempotency keys
# ---------------------------------------------------------------------------------------------------------------------


class IdempotencyDb:
    """In-memory txn.idempotency_key for IdempotencyService."""

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}

    def in_tx(self, role: DbRole, partner_id: Any, work: Any) -> Any:
        assert role == DbRole.APP and partner_id == PARTNER
        return work(self)

    def execute(self, sql: str, p: dict[str, Any]) -> int:
        k = (p["partnerId"], p["key"])
        if "insert into txn.idempotency_key" in sql:
            if k in self.rows:
                return 0
            self.rows[k] = {"method": p["method"], "path": p["path"], "request_hash": p["hash"], "expires_at": p["exp"], "status_code": None, "response_body": None}
            return 1
        if sql.startswith("update txn.idempotency_key"):
            self.rows[k].update(status_code=p["status"], response_body=p["body"])
            return 1
        if "expires_at < @now" in sql:
            if k in self.rows and self.rows[k]["expires_at"] < p["now"]:
                del self.rows[k]
                return 1
            return 0
        assert sql.startswith("delete from txn.idempotency_key")
        return 1 if self.rows.pop(k, None) is not None else 0

    def single(self, sql: str, p: dict[str, Any]) -> dict[str, Any]:
        return dict(self.rows[(p["partnerId"], p["key"])])


class FixedNow:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def utc_now(self) -> datetime:
        return self.now


def test_idempotency_replays_and_conflicts() -> None:
    db = IdempotencyDb()
    clock = FixedNow(datetime(2026, 3, 1, tzinfo=UTC))
    service_ = IdempotencyService(db, clock)
    calls: list[int] = []

    def ok() -> IdempotentResponse:
        calls.append(1)
        return IdempotentResponse(201, '{"orderId":"o-1"}')

    first = service_.execute(PARTNER, "key-1", "POST", "/v1/orders", b'{"a":1}', ok)
    again = service_.execute(PARTNER, "key-1", "POST", "/v1/orders", b'{"a":1}', ok)
    assert first == again == IdempotentResponse(201, '{"orderId":"o-1"}') and len(calls) == 1
    assert db.rows[(PARTNER, "key-1")]["request_hash"] == hashlib.sha256(b'{"a":1}').digest() == IdempotencyService.request_hash(b'{"a":1}')

    for method, path, body in (("POST", "/v1/orders", b'{"a":2}'), ("PUT", "/v1/orders", b'{"a":1}'), ("POST", "/v1/other", b'{"a":1}')):
        with pytest.raises(HsError) as err:
            service_.execute(PARTNER, "key-1", method, path, body, ok)
        assert (err.value.status, err.value.code, err.value.detail) == (409, "HS-IDEMPOTENCY-CONFLICT", "This Idempotency-Key was used with a different request")

    db.rows[(PARTNER, "busy")] = {"method": "POST", "path": "/p", "request_hash": hashlib.sha256(b"").digest(), "expires_at": clock.now + timedelta(hours=1),
                                  "status_code": None, "response_body": None}
    with pytest.raises(HsError, match="still being processed"):
        service_.execute(PARTNER, "busy", "POST", "/p", b"", ok)

    # Expired keys are forgotten and the request runs again.
    db.rows[(PARTNER, "key-1")]["expires_at"] = clock.now - timedelta(seconds=1)
    service_.execute(PARTNER, "key-1", "POST", "/v1/orders", b'{"a":2}', ok)
    assert len(calls) == 2


def test_idempotency_stores_client_errors_and_forgets_server_errors() -> None:
    db = IdempotencyDb()
    service_ = IdempotencyService(db, FixedNow(datetime(2026, 3, 1, tzinfo=UTC)))

    def invalid() -> IdempotentResponse:
        raise HsError.invalid("Bad cart", [FieldError("/lines/0/qty", "must be positive", "HS-QTY")])

    with pytest.raises(HsError):
        service_.execute(PARTNER, "k", "POST", "/v1/carts", b"{}", invalid)
    replay = service_.execute(PARTNER, "k", "POST", "/v1/carts", b"{}", lambda: pytest.fail("must replay"))
    assert replay.status == 400
    assert replay.body == (
        '{"type":"https://healthswap.example/problems/hs-req-invalid","title":"Request validation failed","status":400,"detail":"Bad cart",'
        '"code":"HS-REQ-INVALID","errors":[{"field":"/lines/0/qty","message":"must be positive","code":"HS-QTY"}]}'
    )
    assert IdempotencyService.problem_json(HsError.conflict("HS-X", "nope")) == (
        '{"type":"https://healthswap.example/problems/hs-x","title":"Conflict","status":409,"detail":"nope","code":"HS-X"}'
    )

    def unavailable() -> IdempotentResponse:
        raise HsError(503, "HS-UNAVAILABLE", "Service unavailable")

    def crash() -> IdempotentResponse:
        raise RuntimeError("boom")

    for handler in (unavailable, crash):
        with pytest.raises((HsError, RuntimeError)):
            service_.execute(PARTNER, "k2", "POST", "/v1/carts", b"{}", handler)
        assert (PARTNER, "k2") not in db.rows
    assert service_.execute(PARTNER, "k2", "POST", "/v1/carts", b"{}", lambda: IdempotentResponse(200, "{}")).status == 200


@pytest.mark.parametrize("key", ["", "x" * 256, "😀" * 128])
def test_idempotency_key_length_is_counted_in_utf16_units(key: str) -> None:
    with pytest.raises(HsError) as err:
        IdempotencyService(IdempotencyDb(), FixedNow(utc_now())).execute(PARTNER, key, "POST", "/p", b"", lambda: IdempotentResponse(200, ""))
    assert err.value.status == 400 and err.value.detail == "Idempotency-Key must be 1-255 characters"


def test_idempotency_accepts_255_utf16_units() -> None:
    db = IdempotencyDb()
    response = IdempotencyService(db, FixedNow(utc_now())).execute(PARTNER, "😀" * 127 + "x", "POST", "/p", b"", lambda: IdempotentResponse(200, "{}"))
    assert response.status == 200


# ---------------------------------------------------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------------------------------------------------


class LedgerUow(RecordingUow):
    def __init__(self, role: DbRole, posted: bool = False) -> None:
        super().__init__(role)
        self.posted = posted

    def scalar(self, sql: str, params: dict[str, Any]) -> bool:
        return self.posted

    def single(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"account_id": uuid.uuid5(uuid.NAMESPACE_OID, params["code"])}


def test_ledger_requires_the_ledger_writer_role_and_numbers_lines() -> None:
    posting = Postings.reco_fee(uuid.UUID(int=9), Decimal("2.50"))
    with pytest.raises(RuntimeError, match="hs_ledger_writer"):
        LedgerService(FixedNow(NOW)).post(LedgerUow(DbRole.APP), PARTNER, "INR", posting, "test")
    assert not LedgerService(FixedNow(NOW)).post(LedgerUow(DbRole.LEDGER_WRITER, posted=True), PARTNER, "INR", posting, "test")
    u = LedgerUow(DbRole.LEDGER_WRITER)
    assert LedgerService(FixedNow(NOW)).post(u, PARTNER, "INR", posting, "test")
    entries = [p for s, p in u.calls if "insert into billing.journal_entry" in s]
    lines = [p for s, p in u.calls if "insert into billing.journal_line" in s]
    assert len(entries) == 1 and entries[0]["key"] == posting.idempotency_key and entries[0]["at"] == NOW
    assert [p["lineNo"] for p in lines] == list(range(1, len(posting.lines) + 1))
    assert sum(p["debit"] for p in lines) == sum(p["credit"] for p in lines) == Decimal("2.50")


# ---------------------------------------------------------------------------------------------------------------------
# Catalog items (System.Text.Json reader), CSV feeds, candidates, preferences
# ---------------------------------------------------------------------------------------------------------------------


def test_read_catalog_item_like_system_text_json() -> None:
    item = read_catalog_item({
        "TITLE": "Masala Oats 500 g", "partnersku": "SKU-1", "unknown": {"ignored": True}, "netQuantity": {"unit": "g"},
        "nutrition": [{"energyKcal": 389, "sodiumMg": Decimal("410.50"), "other": [{"code": "IRON"}]}], "isListed": None,
        "attributes": {"aisle": "7"},
    })
    assert list(item) == ["partnerSku", "title", "netQuantity", "nutrition", "attributes"]  # C# property order, nulls omitted
    assert item["netQuantity"] == {"value": Decimal(0), "unit": "g"}  # NetQuantityDto(decimal Value, ...) default
    assert item["nutrition"] == [{"basis": "PER_100G", "energyKcal": Decimal(389), "sodiumMg": Decimal("410.50"),
                                  "other": [{"code": "IRON", "amount": Decimal(0)}]}]
    assert netjson.serialize_hs(item) == (
        '{"partnerSku":"SKU-1","title":"Masala Oats 500 g","netQuantity":{"value":0,"unit":"g"},'
        '"nutrition":[{"basis":"PER_100G","energyKcal":389,"sodiumMg":410.50,"other":[{"code":"IRON","amount":0}]}],"attributes":{"aisle":"7"}}'
    )
    assert read_catalog_item(item) == item  # idempotent
    assert read_catalog_item(None) is None
    # JSONL numbers are Decimals: "2.0" is not an Int32 for System.Text.Json either.
    with pytest.raises(ValueError, match=r"Int32\]. Path: \$.packCount"):
        read_catalog_item({"partnerSku": "S", "title": "T", "packCount": Decimal("2.0")})


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"title": "T"}, "missing required properties including: 'partnerSku'"),
        ({"partnerSku": "S"}, "missing required properties including: 'title'"),
        ({}, "including: 'partnerSku', 'title'"),
        ({"partnerSku": None, "title": "T"}, "partnerSku is required"),
        ({"partnerSku": "S", "title": None}, "title is required"),
        ({"partnerSku": 5, "title": "T"}, "System.String. Path: $.partnerSku"),
        ({"partnerSku": "S", "title": "T", "packCount": 2.5}, "Nullable`1[System.Int32]. Path: $.packCount"),
        ({"partnerSku": "S", "title": "T", "packCount": 2**31}, "Path: $.packCount"),
        ({"partnerSku": "S", "title": "T", "isListed": "yes"}, "Boolean]. Path: $.isListed"),
        ({"partnerSku": "S", "title": "T", "netQuantity": {"value": None}}, "System.Decimal. Path: $.netQuantity.value"),
        ({"partnerSku": "S", "title": "T", "markets": "IN"}, "List`1[System.String]. Path: $.markets"),
        ({"partnerSku": "S", "title": "T", "nutrition": {}}, "Path: $.nutrition"),
        ("SKU-1", "HealthSwap.Contracts.CatalogItem. Path: $"),
    ],
)
def test_read_catalog_item_errors(value: Any, message: str) -> None:
    with pytest.raises(ValueError) as err:
        read_catalog_item(value)
    assert message in str(err.value)


@pytest.mark.parametrize(
    ("line", "cells"),
    [
        ("a,b,c", ["a", "b", "c"]),
        ("", [""]),
        (",", ["", ""]),
        ('"x, y",z', ["x, y", "z"]),
        ('"say ""hi""",2', ['say "hi"', "2"]),
        ('a"b,c', ["ab,c"]),  # a quote opens a quoted section anywhere (C# parser)
        ('"unterminated,x', ["unterminated,x"]),
        ("  padded ,x", ["  padded ", "x"]),
    ],
)
def test_parse_csv_line(line: str, cells: list[str]) -> None:
    assert FeedService.parse_csv_line(line) == cells


def test_csv_rows_become_catalog_items() -> None:
    header = FeedService.parse_csv_line("partnerSku,gtin,title,brand,categoryPath,netQuantityValue,netQuantityUnit,packCount,isPrivateLabel,isListed")
    item = FeedService._from_csv(header, FeedService.parse_csv_line('SKU-9,8901234567890,"Rusk, Elaichi",Local Bakery,Bakery|Rusk,300.0,,2, True ,FALSE'))
    assert item == {
        "partnerSku": "SKU-9", "gtin": "8901234567890", "title": "Rusk, Elaichi", "brand": "Local Bakery", "categoryPath": ["Bakery", "Rusk"],
        "netQuantity": {"value": Decimal("300.0"), "unit": "g"}, "packCount": 2, "isPrivateLabel": True, "isListed": False,
    }
    assert FeedService._from_csv(header, ["SKU-10", "", "Plain"]) == {"partnerSku": "SKU-10", "title": "Plain"}
    with pytest.raises(ValueError, match="partnerSku is required"):
        FeedService._from_csv(header, ["", "", "T"])
    with pytest.raises(ValueError, match="title is required"):
        FeedService._from_csv(header, ["S"])
    with pytest.raises(ValueError):
        FeedService._from_csv(header, FeedService.parse_csv_line("S,,T,,,,,two"))
    with pytest.raises(ValueError):
        FeedService._from_csv(header, FeedService.parse_csv_line("S,,T,,,,,,yes"))


def test_to_candidate_maps_partner_label_data() -> None:
    item = {
        "partnerSku": "SKU-LASSI", "gtin": "8901234500001", "title": "Sweet Lassi 200 ml", "brand": "Dairy Co", "netQuantity": {"value": 200, "unit": "ml"},
        "nutrition": [
            {"basis": "PER_SERVING", "energyKcal": 150},
            {"basis": "PER_100ML", "servingSize": {"value": 200, "unit": "ml"}, "energyKcal": Decimal("75.0"), "sodiumMg": 40, "totalSugarsG": 11},
        ],
        "ingredientsText": "Milk, sugar", "dietaryMarks": ["VEGETARIAN"], "attributes": {"k": "v"},
    }
    now = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    c = CatalogService.to_candidate(PARTNER, item, "IN", now)
    assert (c.source_code, c.external_id, c.raw_gtin, c.name, c.brand, c.markets) == (
        "PARTNER_FEED", f"{PARTNER.hex}:SKU-LASSI", "8901234500001", "Sweet Lassi 200 ml", "Dairy Co", ("IN",))
    assert (c.net_value, c.net_unit, c.pack_count, c.physical_state, c.serving_value, c.serving_unit) == (Decimal(200), "ml", 1, "LIQUID", Decimal(200), "ml")
    assert c.nutrition is not None
    assert (c.nutrition.basis, c.nutrition.carb_definition, c.nutrition.energy_kcal, c.nutrition.sodium_mg) == ("PER_100ML", "AVAILABLE", Decimal("75.0"), Decimal(40))
    assert (c.sodium_declared, c.salt_declared, c.is_private_label, c.verification, c.observed_at, c.partner_id) == (True, False, False, "UNVERIFIED", now, PARTNER)
    assert c.attributes == {"k": "v"}
    assert c.raw_json == netjson.serialize_hs(read_catalog_item(item))
    us = CatalogService.to_candidate(PARTNER, {"partnerSku": "S", "title": "T", "nutrition": [{"basis": "PER_SERVING", "saltG": 1}]}, "US", now)
    assert (us.nutrition.basis, us.nutrition.carb_definition, us.physical_state, us.salt_declared) == ("PER_100G", "TOTAL_INCL_FIBRE", "SOLID", True)


@pytest.mark.parametrize("ref", ["usr_12345", "A" * 128, "abc-DEF_123"])
def test_user_ref_valid(ref: str) -> None:
    PreferenceService.validate_user_ref(ref)


def test_feed_upload_tokens_and_size_limit(tmp_path: Path) -> None:
    provider = DataProtectionProvider(tmp_path / "keys")
    storage = tmp_path / "feeds"
    # No database: every case below is decided before the feed row is touched.
    feeds = FeedService(None, None, None, provider, FeedOptions(storage_root=storage, max_upload_bytes=10), FixedNow(utc_now()))  # type: ignore[arg-type]
    protector = provider.create_time_limited_protector(FeedService.UPLOAD_PURPOSE)
    feed = uuid.UUID(int=42)
    soon = utc_now() + timedelta(minutes=5)
    token = protector.protect(f"{PARTNER.hex}.{feed.hex}", soon)

    with pytest.raises(HsError) as err:
        feeds.accept_upload(feed, token, iter([b"0123456789", b"x"]))
    assert (err.value.status, err.value.code, err.value.detail) == (400, "HS-REQ-TOO-LARGE", "Feed uploads are limited to 10 bytes")
    assert list(storage.iterdir()) == []  # the partial upload was removed

    for bad_feed, bad_token, detail in (
        (uuid.UUID(int=43), token, "The upload URL does not match this feed"),
        (feed, protector.protect("no-dot", soon), "The upload URL does not match this feed"),
        (feed, protector.protect(f"{PARTNER.hex}.{feed.hex}", utc_now() - timedelta(seconds=1)), "The upload URL is invalid or expired"),
        (feed, provider.create_time_limited_protector("HealthSwap.Other.v1").protect(f"{PARTNER.hex}.{feed.hex}", soon), "The upload URL is invalid or expired"),
        (feed, "not-a-token", "The upload URL is invalid or expired"),
    ):
        with pytest.raises(HsError) as err:
            feeds.accept_upload(bad_feed, bad_token, b"{}")
        assert (err.value.status, err.value.code, err.value.detail) == (403, "HS-FEED-UPLOAD-DENIED", detail)
    assert not any(storage.iterdir())


@pytest.mark.parametrize("ref", ["short", "A" * 129, "has space1", "émile-123", "user@example.com"])
def test_user_ref_invalid(ref: str) -> None:
    with pytest.raises(HsError) as err:
        PreferenceService.validate_user_ref(ref)
    assert err.value.status == 400


def test_preferences_to_api_shape() -> None:
    doc = ShopperPrefsDoc(
        diet_type="VEGETARIAN", allergens=("PEANUT",), strict_may_contain=True, goals=(), avoid_ingredients=("PALM_OIL",), excluded_brands=(),
        religious_preferences=(), locale=None, is_minor=None, health_data_consent=True, personalization_consent=False, consent_notice_version="2026-01",
        updated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    assert PreferenceService.to_api(doc) == {
        "dietType": "VEGETARIAN", "allergens": ["PEANUT"], "strictMayContain": True, "goals": [], "avoidIngredients": ["PALM_OIL"], "excludedBrands": [],
        "religiousPreferences": [], "healthDataConsent": True, "personalizationConsent": False, "consentNoticeVersion": "2026-01",
        "updatedAt": "2026-01-02T03:04:05.000000Z",
    }


# ---------------------------------------------------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------------------------------------------------


def _settings(tmp_path: Path, **extra: Any) -> Settings:
    return load_settings(
        overrides={
            "database.url": UNREACHABLE_DB, "lake.root": str(tmp_path / "lake"), "data_protection.keys_directory": str(tmp_path / "keys"),
            "feeds.storage_root": str(tmp_path / "feeds"), **extra,
        },
        env={},
    )


def test_container_wires_singletons_lazily(tmp_path: Path) -> None:
    with Container(_settings(tmp_path)) as c:
        assert "db" not in c.__dict__
        assert c.secret_protector is c.secret_protector
        assert "db" not in c.__dict__  # building the secret protector needs no database
        assert c.refs is c.reference_data_repository and c.models is c.model_registry and c.partners is c.partner_directory
        assert c.bus is c.in_process_event_bus and c.data_protection is c.data_protection_provider and c.clock is c.offset_clock
        assert c.catalog_service.cache is c.hot_cache and c.catalog_service.partners is c.partners and c.catalog_service.db is c.db
        assert c.demo_seeder.catalog is c.catalog_service and c.demo_seeder.secrets is c.secret_protector and c.demo_seeder.refs is c.refs
        assert c.feed_service.enrichment is c.catalog_enrichment and c.catalog_enrichment.catalog is c.catalog_service
        assert c.bus_consumers == (c.webhook_fanout_consumer, c.lake_sink) and c.bus.consumers == c.bus_consumers
        assert c.outbox_relay.bus is c.bus and c.webhook_dispatcher.http is c.webhook_http_client
        assert c.llm_gateway.provider is c.llm_provider and type(c.llm_provider).__name__ == "MockLlmProvider"
        assert "llm_http_client" not in c.__dict__  # only the Azure OpenAI provider uses it
        assert c.repo_root == c.settings.repo_root and str(c.data_protection_provider.keys_directory).startswith(str(tmp_path))
        assert isinstance(type(c).__dict__["hot_cache"], service)


def test_container_resolves_each_service_once_across_threads(tmp_path: Path) -> None:
    c = Container(_settings(tmp_path))
    seen: list[Any] = []
    barrier = threading.Barrier(8)

    def resolve() -> None:
        barrier.wait()
        seen.append(c.transparency_service)

    threads = [threading.Thread(target=resolve) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(s) for s in seen}) == 1
    c.close()


def test_container_azure_openai_provider_is_case_insensitive(tmp_path: Path) -> None:
    with Container(_settings(tmp_path, **{"llm.provider": "azureopenai", "llm.endpoint": "https://example.openai.azure.com"})) as c:
        assert type(c.llm_provider).__name__ == "AzureOpenAiProvider" and c.llm_provider.http is c.llm_http_client
        assert c.llm_http_client.timeout.read == 60.0
    assert c.llm_http_client.is_closed


class _FakeWorker(BackgroundWorker):
    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__(name)
        self.events = log

    def execute(self) -> None:
        self.stopping.wait()

    def stop(self, timeout: float | None = 10.0) -> bool:
        self.events.append("stop:" + self.name)
        return super().stop(timeout)


class _WorkerContainer(Container):
    """Workers replaced by fakes; also defines the services later porters will add."""

    events: list[str]

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.events = []

    @service
    def in_process_event_bus(self) -> Any:
        return _FakeWorker("bus", self.events)

    @service
    def outbox_relay(self) -> Any:
        return _FakeWorker("relay", self.events)

    @service
    def evaluation_recorder(self) -> Any:
        return _FakeWorker("recorder", self.events)

    @service
    def webhook_dispatcher(self) -> Any:
        return _FakeWorker("webhooks", self.events)

    @service
    def finalizer_worker(self) -> Any:
        return _FakeWorker("finalizer", self.events)

    @service
    def cache_warmer(self) -> Any:
        return _FakeWorker("warmer", self.events)


def test_container_starts_workers_in_csharp_order_and_stops_in_reverse(tmp_path: Path) -> None:
    c = _WorkerContainer(_settings(tmp_path))
    c.start_workers()
    assert [w.name for w in c.workers] == ["bus", "relay", "recorder", "webhooks", "finalizer", "warmer"]
    assert all(w.is_running for w in c.workers)
    c.start_workers()  # idempotent
    assert len(c.workers) == 6
    c.stop_workers()
    assert c.events == ["stop:warmer", "stop:finalizer", "stop:webhooks", "stop:recorder", "stop:relay", "stop:bus"]
    assert c.workers == ()

    c.events.clear()
    c.start_workers(include_scheduler=False, include_cache_warmer=False, include_webhooks=False)  # the CLI's set
    assert [w.name for w in c.workers] == ["bus", "relay", "recorder"]
    c.close()
    c.close()  # idempotent
    assert c.events == ["stop:recorder", "stop:relay", "stop:bus"]
    with pytest.raises(RuntimeError):
        c.start_workers()


def test_container_wires_orders_billing_and_ml_data(tmp_path: Path) -> None:
    with Container(_settings(tmp_path)) as c:
        assert c.finalizer_worker.attribution is c.attribution_engine and c.finalizer_worker.reco is c.reco_charge_service
        assert c.attribution_engine.ledger is c.ledger_service and c.attribution_engine.db is c.db and c.attribution_engine.clock is c.clock
        assert c.order_service.attribution is c.attribution_engine and c.order_service.suggestions is c.suggestion_store
        assert c.order_service.cache is c.hot_cache and c.order_service.partners is c.partners
        assert c.billing_service.ledger is c.ledger_service and c.billing_service.partners is c.partners
        assert c.ml_data.evaluations is c.evaluation_service and c.ml_data.pregen is c.explanation_pregen and c.ml_data.refs is c.refs
        assert c.ml_data.provider == "mock"
        assert "db" in c.__dict__ and not c.db._opened  # wiring never connects


def test_container_starts_the_finalizer_only_when_the_scheduler_is_enabled(tmp_path: Path) -> None:
    c = _WorkerContainer(_settings(tmp_path, **{"scheduler.enabled": False}))
    c.start_workers()
    assert [w.name for w in c.workers] == ["bus", "relay", "recorder", "webhooks", "warmer"]
    c.close()


# ---------------------------------------------------------------------------------------------------------------------
# CLI (no database)
# ---------------------------------------------------------------------------------------------------------------------


def _run_cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(args)
    out, err = capsys.readouterr()
    return code, out, err


@pytest.mark.parametrize("args", [[], ["help"], ["--help"], ["-h"]])
def test_cli_help(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _run_cli(args, capsys)
    assert code == 0 and out.startswith("hs - HealthSwap operations CLI") and "hs kb score | build-graph | explain | rebuild" in out


def test_cli_parses_the_jobs_billing_ml_and_llm_commands() -> None:
    parser = cli._parser()
    assert parser.parse_args(["jobs", "finalize"]).command == "jobs finalize"
    ns = parser.parse_args(["billing", "month-close", "--period", "2026-01", "--partner", str(PARTNER)])
    assert (ns.command, ns.period, ns.partner) == ("billing month-close", "2026-01", PARTNER)
    ns = parser.parse_args(["billing", "month-close"])
    assert (ns.period, ns.partner) == (None, None)
    ns = parser.parse_args(["ml", "export", "--out", "x"])
    assert (ns.command, ns.out) == ("ml export", "x")
    ns = parser.parse_args(["ml", "simulate"])
    assert (ns.command, ns.partner, ns.sessions, ns.out, ns.seed) == ("ml simulate", PARTNER, 2000, None, 7)
    ns = parser.parse_args(["ml", "simulate", "--sessions", "5", "--seed", "3", "--partner", str(uuid.UUID(int=1))])
    assert (ns.sessions, ns.seed, ns.partner) == (5, 3, uuid.UUID(int=1))
    ns = parser.parse_args(["llm", "eval"])
    assert (ns.command, ns.input, ns.max) == ("llm eval", None, 200)
    assert {"jobs finalize", "billing month-close", "ml export", "ml simulate", "llm eval"} <= set(cli.COMMANDS)
    assert "not yet ported" not in cli.USAGE


class _Recorder:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = result

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.result


def test_cli_operations_commands_call_the_services_like_the_csharp_cli(tmp_path: Path) -> None:
    finalize_reco, finalize_attr = _Recorder((2, 1)), _Recorder(3)
    close, export, simulate, evaluate = _Recorder([]), _Recorder({"products": 1}), _Recorder(7), _Recorder("report")
    warm, flush = _Recorder(), _Recorder(True)
    c: Any = type("C", (), {})()
    c.repo_root = tmp_path
    c.reco_charge_service = type("R", (), {"finalize_due": staticmethod(finalize_reco)})()
    c.attribution_engine = type("A", (), {"finalize_due": staticmethod(finalize_attr)})()
    c.billing_service = type("B", (), {"close_month": staticmethod(close)})()
    c.ml_data = type("M", (), {"export": staticmethod(export), "simulate": staticmethod(simulate), "evaluate_explanations": staticmethod(evaluate),
                               "provider": "mock"})()
    c.cache_warmer = type("W", (), {"warm_all": staticmethod(warm)})()
    c.evaluation_recorder = type("E", (), {"flush": staticmethod(flush)})()
    parser = cli._parser()

    def run(*args: str) -> Any:
        ns = parser.parse_args(list(args))
        return cli.COMMANDS[ns.command](c, ns)

    assert run("jobs", "finalize") == {"recoCharges": {"final": 2, "rejected": 1}, "attributionsFinal": 3}
    run("billing", "month-close")
    run("billing", "month-close", "--period", "2026-02", "--partner", str(PARTNER))
    assert close.calls[0][1] is None and len(close.calls[0][0]) == 7 and close.calls[1] == ("2026-02", PARTNER)
    run("ml", "export")
    assert export.calls == [(tmp_path / "ml" / "data",)]
    assert run("ml", "simulate", "--sessions", "5") == {"rows": 7}
    assert len(warm.calls) == 1 and flush.calls == [(60.0,)]
    assert simulate.calls == [(PARTNER, 5, tmp_path / "ml" / "data" / "ranker_events.jsonl", 7)]
    assert run("llm", "eval", "--max", "3") == {"provider": "mock", "report": "report"}
    assert evaluate.calls == [(tmp_path / "ml" / "data" / "explanation_facts.jsonl", 3)]


@pytest.mark.parametrize(
    "args",
    [["nope"], ["kb"], ["kb", "ingest"], ["kb", "ingest", "--source", "xyz", "--file", "f"], ["db", "wait", "--timeout", "x"],
     ["billing", "month-close", "--partner", "not-a-uuid"], ["ml", "simulate", "--sessions", "many"]],
)
def test_cli_usage_errors(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _run_cli(args, capsys)
    assert code == 2 and "hs help" in err


def test_cli_global_options_anywhere() -> None:
    parser = cli._parser()
    for args in (
        ["--env", "Development", "--database-url", "u", "kb", "score"],
        ["kb", "score", "--env", "Development", "--database-url", "u"],
        ["--env", "Development", "kb", "score", "--database-url", "u"],
    ):
        ns = parser.parse_args(args)
        assert (ns.command, ns.env, ns.database_url) == ("kb score", "Development", "u")
    ns = parser.parse_args(["kb", "ingest", "--file", "x.json"])
    assert (ns.command, ns.source, ns.file, getattr(ns, "env", None)) == ("kb ingest", "brand", "x.json", None)
    assert parser.parse_args(["seed", "--demo", "--verbose"]).demo is True


def test_cli_db_wait_reports_unreachable_database(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run_cli(["db", "wait", "--timeout", "0", "--database-url", UNREACHABLE_DB], capsys)
    assert (code, out) == (1, "") and "Database not reachable or schema missing" in err


def test_cli_logging_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    cli._configure_logging(False)
    logging.getLogger("healthswap.test").info("hello %s", "world")
    out, err = capsys.readouterr()
    assert out == "" and err.rstrip().endswith("info: healthswap.test hello world")


def test_cli_prints_reports_as_indented_json(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print({"matched": 1, "steps": [{"market": "IN", "value": Decimal("0.50"), "id": uuid.UUID(int=1), "none": None}]})
    out = capsys.readouterr().out
    assert json.loads(out) == {"matched": 1, "steps": [{"market": "IN", "value": 0.5, "id": "00000000-0000-0000-0000-000000000001"}]}
    assert out.startswith('{\n  "matched": 1,')
