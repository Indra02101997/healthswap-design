"""SSRF guard of the webhook dispatcher: the endpoint host is resolved (every address) before delivery, any non-public address
refuses the delivery (recorded as a failed attempt with the usual backoff), and the request is pinned to the vetted IP so a
second DNS answer cannot redirect it. The resolver is monkeypatched; no network access."""

from __future__ import annotations

import ipaddress
import socket
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from healthswap.domain.primitives import utc_now
from healthswap.platform.db import DbRole
from healthswap.platform.services.infrastructure import (
    DataProtectionProvider,
    SecretProtector,
    UrlPolicy,
    WebhookDispatcher,
    WebhookTargetRefused,
)
from healthswap.platform.settings import WebhookOptions

PARTNER = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01")
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _resolver(table: dict[str, list[str]], calls: list[str] | None = None) -> Callable[..., list[tuple[Any, ...]]]:
    """socket.getaddrinfo over a fixed table (host -> addresses in resolver order); unknown hosts fail like NXDOMAIN."""

    def getaddrinfo(host: str, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> list[tuple[Any, ...]]:
        if calls is not None:
            calls.append(host)
        if host not in table:
            raise socket.gaierror(11001, "No such host is known")
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)) if ":" in ip else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in table[host]
        ]

    return getaddrinfo


class _Db:
    """Fake Db for WebhookDispatcher: the claim returns `rows`; the attempt updates are recorded per delivery."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.updates: dict[uuid.UUID, dict[str, Any]] = {}

    def in_tx(self, role: DbRole, partner_id: Any, work: Any) -> Any:
        return work(self)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows]

    def execute(self, sql: str, params: dict[str, Any]) -> int:
        if "set next_attempt_at = @lease" not in sql:
            self.updates[params["id"]] = params
        return 1


@pytest.fixture()
def secrets(tmp_path: Path) -> SecretProtector:
    return SecretProtector(DataProtectionProvider(tmp_path / "keys"))


def _dispatch(secrets: SecretProtector, urls: list[str], handler: Callable[[httpx.Request], httpx.Response],
              allow_http_localhost: bool = False) -> tuple[_Db, list[httpx.Request]]:
    rows = [
        {
            "delivery_id": uuid.UUID(int=n), "webhook_id": f"evt_{n}", "partner_id": PARTNER, "payload": '{"id":"evt"}', "attempts": 0,
            "created_at": utc_now(), "url": url, "secret_ciphertext": secrets.protect("whsec_test"),
        }
        for n, url in enumerate(urls, start=1)
    ]
    db = _Db(rows)
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(record), follow_redirects=False)
    dispatcher = WebhookDispatcher(db, http, secrets, WebhookOptions(allow_http_localhost=allow_http_localhost))
    assert dispatcher.dispatch_due() == len(urls)
    return db, seen


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(204)


def test_public_address_is_delivered_pinned_to_the_vetted_ip(secrets: SecretProtector, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({
        "hooks.partner.example": [PUBLIC_V4, PUBLIC_V6], "xn--bcher-kva.example": [PUBLIC_V6],
    }, calls))
    db, seen = _dispatch(secrets, ["https://hooks.partner.example/hs?x=1", "https://bücher.example:8443/hooks"], _ok)

    assert [str(r.url) for r in seen] == [f"https://{PUBLIC_V4}/hs?x=1", f"https://[{PUBLIC_V6}]:8443/hooks"]
    assert [r.headers["Host"] for r in seen] == ["hooks.partner.example", "xn--bcher-kva.example:8443"]
    # TLS SNI and certificate verification use the real host name; the connection is not pooled for another host.
    assert [r.extensions["sni_hostname"] for r in seen] == ["hooks.partner.example", "xn--bcher-kva.example"]
    assert all(r.headers["Connection"] == "close" for r in seen)
    assert calls == ["hooks.partner.example", "xn--bcher-kva.example"]  # one lookup per delivery, IDNA-encoded
    assert all(u["delivered"] and u["status"] == 204 and u["error"] is None for u in db.updates.values())


@pytest.mark.parametrize(
    ("addresses", "refused"),
    [
        (["10.0.0.7"], "10.0.0.7"),
        ([PUBLIC_V4, "192.168.1.4"], "192.168.1.4"),  # mixed: one bad answer refuses the host
        (["169.254.169.254"], "169.254.169.254"),  # cloud metadata (link-local)
        (["127.0.0.1"], "127.0.0.1"),  # loopback without webhooks.allow_http_localhost
        (["::ffff:10.1.2.3"], "::ffff:10.1.2.3"),
        (["fe80::1%12"], "fe80::1"),
        (["224.0.0.1"], "224.0.0.1"),
        (["0.0.0.0"], "0.0.0.0"),
        (["240.0.0.1"], "240.0.0.1"),
    ],
)
def test_private_resolution_is_refused_and_backed_off(addresses: list[str], refused: str, secrets: SecretProtector,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"rebind.partner.example": addresses}))
    db, seen = _dispatch(secrets, ["https://rebind.partner.example/hook"], _ok)
    assert seen == []
    [update] = db.updates.values()
    assert (update["delivered"], update["dead"], update["status"], update["attempts"]) == (False, False, None, 1)
    assert update["error"] == f"Private network addresses are not allowed: rebind.partner.example resolves to {refused}"
    assert timedelta(seconds=9) < update["next"] - utc_now() <= timedelta(seconds=10)  # the regular first backoff step


def test_unresolvable_host_is_a_failed_attempt(secrets: SecretProtector, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({}))
    db, seen = _dispatch(secrets, ["https://gone.partner.example/hook"], _ok)
    [update] = db.updates.values()
    assert seen == [] and update["delivered"] is False
    assert update["error"].startswith("Host gone.partner.example could not be resolved: ")


def test_loopback_only_with_allow_http_localhost(secrets: SecretProtector, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"localhost": ["::1", "127.0.0.1"], "dev.partner.example": ["127.0.0.1"]}))
    urls = ["http://localhost:8080/demo/webhooks/quickbasket", "https://dev.partner.example/hook"]

    db, seen = _dispatch(secrets, urls, _ok, allow_http_localhost=False)
    assert seen == []
    assert [u["error"] for u in db.updates.values()] == [
        "Loopback hosts are not allowed", "Private network addresses are not allowed: dev.partner.example resolves to 127.0.0.1",
    ]

    def ipv4_only(request: httpx.Request) -> httpx.Response:
        if request.url.host == "::1":  # nothing listens on [::1]: the next vetted address is tried
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200)

    db, seen = _dispatch(secrets, urls, ipv4_only, allow_http_localhost=True)
    assert [str(r.url) for r in seen] == ["http://[::1]:8080/demo/webhooks/quickbasket", "http://127.0.0.1:8080/demo/webhooks/quickbasket",
                                          "https://127.0.0.1/hook"]
    assert [r.headers["Host"] for r in seen] == ["localhost:8080", "localhost:8080", "dev.partner.example"]
    assert "sni_hostname" not in seen[0].extensions and seen[2].extensions["sni_hostname"] == "dev.partner.example"
    assert all(u["delivered"] for u in db.updates.values())


def test_redirects_are_not_followed_after_pinning(secrets: SecretProtector, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"hooks.partner.example": [PUBLIC_V4]}))
    db, seen = _dispatch(secrets, ["https://hooks.partner.example/hook"],
                         lambda r: httpx.Response(307, headers={"Location": "http://169.254.169.254/latest/meta-data"}))
    assert len(seen) == 1
    [update] = db.updates.values()
    assert (update["delivered"], update["status"], update["error"]) == (False, 307, "HTTP 307")


def test_resolve_returns_every_vetted_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"a.example": [PUBLIC_V6, PUBLIC_V4, PUBLIC_V6], "lo.example": ["127.0.0.1", "::1"]}))
    assert UrlPolicy.resolve("a.example", 443, allow_loopback=False) == [ipaddress.ip_address(PUBLIC_V6), ipaddress.ip_address(PUBLIC_V4)]
    assert UrlPolicy.resolve("lo.example", 80, allow_loopback=True) == [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]
    with pytest.raises(WebhookTargetRefused, match="lo.example resolves to 127.0.0.1"):
        UrlPolicy.resolve("lo.example", 80, allow_loopback=False)
