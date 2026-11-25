"""AdminEndpoints.cs and DemoEndpoints ported.

* /admin (scope hs.admin): tenant config, kill switches, jobs, time travel for testing (Development only), ledger, curation,
  disputes, payments, webhook deliveries, the LLM audit, model reload and swap diagnostics.
* /demo (Development or demo.enabled): an SDK-session backend for the demo shopper UI, the RA-1 charges of a demo shop cart
  (the partner view of web/demo), a signed-webhook receiver and, when the directories exist, the demo UI (<repo>/web/demo)
  and the web SDK (<repo>/sdk/web) as static files.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles

from healthswap.api.auth import Scopes
from healthswap.api.http import (
    ApiState,
    BadRequest,
    body_of,
    clamp,
    empty_response,
    json_response,
    pipeline,
    principal_of,
    query_int,
    query_str,
    query_uuid,
    read_body,
)
from healthswap.domain.commerce.partner_config import PartnerConfig
from healthswap.domain.primitives import iso, period_of, try_parse_amount, utc_now
from healthswap.platform import jsonutil, netjson
from healthswap.platform.db import DbRole
from healthswap.platform.errors import FieldError, HsError
from healthswap.platform.jsonutil import JsonElement
from healthswap.platform.services.billing import BillingService
from healthswap.platform.services.infrastructure import WebhookSignature
from healthswap.platform.tenancy import PartnerContext, PartnerDirectory

log = logging.getLogger("Admin")

_ADMIN = (Scopes.ADMIN,)


def _actor(request: Request) -> str:
    principal = principal_of(request)
    client_id = principal.client_id if principal is not None else None
    return "admin:" + (client_id or "unknown")


def _rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Dapper rows as the C# row classes serialize: camelCase property names, nulls omitted."""
    return [{jsonutil.camel(k): v for k, v in r.items()} for r in rows]


def _record(request: Request, parameter: str, fields: Mapping[str, Callable[[Any], Any]]) -> dict[str, Any]:
    """Binds a C# record from the JSON body like minimal APIs do (case-insensitive names, unknown members ignored);
    a member of the wrong JSON type fails the binding."""
    document = body_of(request)
    if not isinstance(document, dict):
        raise BadRequest(f'Failed to read parameter "{parameter}" from the request body as JSON.')
    names = {name.lower(): name for name in fields}
    bound: dict[str, Any] = {}
    for key, value in document.items():
        name = names.get(key.lower())
        if name is None:
            continue
        try:
            bound[name] = fields[name](value)
        except (TypeError, ValueError):
            raise BadRequest(f'Failed to read parameter "{parameter}" from the request body as JSON.') from None
    return bound


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected a JSON boolean")
    return value


def _double(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError("expected a JSON number")
    return float(value)


def _string(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError("expected a JSON string")
    return value


def _strings(value: Any) -> list[str | None] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise TypeError("expected a JSON array")
    return [_string(v) for v in value]


def _update_config(state: ApiState, partner_id: uuid.UUID, text: str, actor: str, action: str) -> None:
    def work(u: Any) -> None:
        updated = u.execute("update tenant.partner set config = @json::jsonb where partner_id = @partnerId", {"json": text, "partnerId": partner_id})
        if updated == 0:
            raise HsError.not_found(f"Partner {partner_id}")
        u.execute(
            "insert into audit.change_log (actor, action, entity_type, entity_id, details) values (@actor, @action, 'PARTNER_CONFIG', @id, @details::jsonb)",
            {"actor": actor, "action": action, "id": str(partner_id), "details": text},
        )

    state.container.db.in_tx(DbRole.OPS, None, work)


def register(app: FastAPI, state: ApiState) -> None:
    """Maps the admin plane (C# AdminEndpoints.Map)."""
    c = state.container

    def add(method: str, path: str, endpoint: Callable[[Request], Any], body: str | None = None, allow_null_body: bool = False) -> None:
        app.add_api_route(
            "/admin" + path, endpoint, methods=[method], name="admin:" + method + " " + path, include_in_schema=False,
            dependencies=[Depends(pipeline(None, _ADMIN, body, allow_null_body))],
        )

    def list_partners(request: Request) -> Response:
        return json_response([
            {
                "partnerId": p.partner_id, "displayName": p.display_name, "geography": p.geography, "markets": list(p.markets), "status": p.status,
                "config": JsonElement.parse(p.config_json),
            }
            for p in c.partners.list_all()
        ])

    add("GET", "/partners", list_partners)

    def put_config(request: Request) -> Response:
        partner_id: uuid.UUID = request.path_params["partnerId"]
        document = body_of(request)
        if document is None:
            raise HsError.invalid("Empty config")
        if not isinstance(document, Mapping):
            raise BadRequest("The JSON value could not be converted to HealthSwap.Domain.Commerce.PartnerConfig. Path: $")
        try:
            cfg = PartnerConfig.from_dict(document)
        except (ValueError, TypeError, KeyError) as ex:
            raise BadRequest(str(ex)) from None
        problems = cfg.validate()
        if problems:
            raise HsError.invalid("Invalid partner configuration", [FieldError("/", p) for p in problems])
        raw: bytes = request.state.raw_body
        _update_config(state, partner_id, raw.decode("utf-8-sig"), _actor(request), "CONFIG_REPLACED")
        c.partners.invalidate(partner_id)
        p = c.partners.get(partner_id)
        return json_response({"partnerId": p.partner_id, "config": JsonElement.parse(p.config_json)})

    add("PUT", "/partners/{partnerId:uuid}/config", put_config, "JsonElement body", allow_null_body=True)

    def kill_switch(request: Request) -> Response:
        partner_id: uuid.UUID = request.path_params["partnerId"]
        req = _record(request, "KillSwitchRequest req", {"enabled": _bool, "disabledPlacements": _strings})
        enabled = req.get("enabled", False)
        placements = req.get("disabledPlacements")
        current = c.partners.get(partner_id)
        node = jsonutil.loads(current.config_json)
        node["killSwitch"] = enabled
        if placements is not None:
            node["disabledPlacements"] = placements
        _update_config(state, partner_id, netjson.to_json_string(node), _actor(request), "KILL_SWITCH_ON" if enabled else "KILL_SWITCH_OFF")
        c.partners.invalidate(partner_id)
        return json_response({
            "partnerId": partner_id, "killSwitch": enabled, "disabledPlacements": placements,
            "effectiveWithinSeconds": PartnerDirectory.ttl.total_seconds(),
        })

    add("POST", "/partners/{partnerId:uuid}/kill-switch", kill_switch, "KillSwitchRequest req")

    def run_job(request: Request) -> Response:
        job: str = request.path_params["job"]
        period = query_str(request, "period")
        partner_id = query_uuid(request, "partnerId")
        c.evaluation_recorder.flush(10.0)
        result: Any
        if job == "finalize-reco-charges":
            final, rejected = c.reco_charge_service.finalize_due()
            result = {"final": final, "rejected": rejected}
        elif job == "finalize-attributions":
            result = {"final": c.attribution_engine.finalize_due()}
        elif job == "month-close":
            result = c.billing_service.close_month(period if period is not None else period_of(c.clock.utc_now()), partner_id)
        elif job == "dispatch-webhooks":
            result = {"relayed": c.outbox_relay.pump(), "dispatched": c.webhook_dispatcher.dispatch_due()}
        elif job == "kb-rebuild":
            result = {
                "scoring": c.scoring_job.run_all(), "graphs": c.swap_graph_builder.build_all(), "explanations": c.explanation_pregen.run_all(),
                "cache": c.cache_warmer.warm_all(),
            }
        elif job == "warm-cache":
            result = c.cache_warmer.warm_all()
        else:
            raise HsError.not_found(f"Job {job}")
        return json_response({"job": job, "result": result})

    add("POST", "/jobs/{job}", run_job)

    def clock_view() -> dict[str, Any]:
        return {"utcNow": iso(c.clock.utc_now()), "offsetSeconds": c.offset_clock.offset.total_seconds()}

    def get_clock(request: Request) -> Response:
        return json_response(clock_view())

    add("GET", "/clock", get_clock)

    def set_clock(request: Request) -> Response:
        req = _record(request, "ClockRequest req", {"offsetSeconds": _double, "advanceSeconds": _double})
        if not state.settings.is_development:
            raise HsError.forbidden("Time travel is available in Development only")
        clock = c.offset_clock
        if req.get("offsetSeconds") is not None:
            clock.offset = timedelta(seconds=req["offsetSeconds"])
        if req.get("advanceSeconds") is not None:
            clock.offset = clock.offset + timedelta(seconds=req["advanceSeconds"])
        log.warning("%s set the clock offset to %s", _actor(request), clock.offset)
        return json_response(clock_view())

    add("POST", "/clock", set_clock, "ClockRequest req")

    def trial_balance(request: Request) -> Response:
        rows = c.billing_service.trial_balance(query_uuid(request, "partnerId"))
        return json_response({"rows": rows, "balanced": BillingService.balanced(rows)})

    add("GET", "/ledger/trial-balance", trial_balance)

    def curation_tasks(request: Request) -> Response:
        status = query_str(request, "status")
        rows = c.db.in_tx(DbRole.OPS, None, lambda u: u.query(
            "select task_id, kind, priority, status, product_id, rule_code, payload::text as payload, created_at from kb.curation_task "
            "where (@status::text is null or status = @status) order by priority, created_at limit 500",
            {"status": status},
        ))
        return json_response(_rows(rows))

    add("GET", "/curation-tasks", curation_tasks)

    def resolve_dispute(request: Request) -> Response:
        return json_response(c.billing_service.resolve_dispute(request.path_params["disputeId"], body_of(request), _actor(request)))

    add("POST", "/disputes/{disputeId:uuid}/resolve", resolve_dispute, "DisputeResolution req")

    def record_payment(request: Request) -> Response:
        req = _record(request, "PaymentRequest req", {"amount": _string, "paymentRef": _string})
        amount = try_parse_amount(req.get("amount"))
        if amount is None:
            raise HsError.invalid("amount must be a decimal string")
        return json_response(c.billing_service.record_payment(request.path_params["statementId"], amount, req.get("paymentRef"), _actor(request)))

    add("POST", "/statements/{statementId:uuid}/payments", record_payment, "PaymentRequest req")

    def webhook_deliveries(request: Request) -> Response:
        partner_id = query_uuid(request, "partnerId")
        rows = c.db.in_tx(DbRole.OPS, None, lambda u: u.query(
            "select delivery_id, webhook_id, partner_id, event_type, status, attempts, last_status_code, last_error, created_at, delivered_at "
            "from txn.webhook_delivery where (@partnerId::uuid is null or partner_id = @partnerId) order by created_at desc limit 200",
            {"partnerId": partner_id},
        ))
        return json_response(_rows(rows))

    add("GET", "/webhook-deliveries", webhook_deliveries)

    def llm_audit(request: Request) -> Response:
        task = query_str(request, "task")
        limit = query_int(request, "limit")

        def work(u: Any) -> dict[str, Any]:
            summary = u.query(
                "select task, prompt_version, model_ref, count(*)::int as calls, count(*) filter (where accepted)::int as accepted, "
                "coalesce(sum(tokens_in), 0)::int as tokens_in, coalesce(sum(tokens_out), 0)::int as tokens_out from rec.llm_audit group by 1, 2, 3 order by 1")
            recent = u.query(
                "select task, prompt_version, model_ref, accepted, validation::text as validation, output::text as output, latency_ms, created_at "
                "from rec.llm_audit where (@task::text is null or task = @task) order by created_at desc limit @limit",
                {"task": task, "limit": clamp(20 if limit is None else limit, 1, 200)},
            )
            return {"summary": _rows(summary), "recent": _rows(recent)}

        return json_response(c.db.in_tx(DbRole.OPS, None, work))

    add("GET", "/llm-audit", llm_audit)

    def reload_models(request: Request) -> Response:
        models = c.models
        models.reload()
        return json_response({
            "ranker": models.ranker_version, "substitutability": models.substitutability_version, "entityResolution": models.entity_resolution_version,
        })

    add("POST", "/models/reload", reload_models)

    def diagnose_swaps(request: Request) -> Response:
        market = query_str(request, "market", required=True)
        product_id = query_uuid(request, "productId", required=True)
        return json_response(c.swap_graph_builder.diagnose(market, product_id))

    add("GET", "/diagnostics/swaps", diagnose_swaps)


# ---------------------------------------------------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------------------------------------------------


def _get(m: Any, name: str) -> Any:
    """Case-insensitive member lookup (HsJson reads partners.json case-insensitively)."""
    if not isinstance(m, Mapping):
        return None
    for key, value in m.items():
        if isinstance(key, str) and key.lower() == name.lower():
            return value
    return None


class DemoLoopbackTransport(httpx.BaseTransport):
    """Delivers webhooks addressed to this API's own demo receiver (seeded as http://localhost:8080/demo/webhooks/...)
    to the port the API actually listens on, so the local demo works on any port."""

    _LOOPBACK = ("localhost", "127.0.0.1", "::1")

    def __init__(self, host: str, port: int, inner: httpx.BaseTransport | None = None) -> None:
        self.host = "127.0.0.1" if host in ("", "localhost", "0.0.0.0", "::", "*", "+") else host
        self.port = port
        self.inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host in self._LOOPBACK and url.path.startswith("/demo/webhooks/") and (url.port or 80) != self.port:
            request.url = url.copy_with(scheme="http", host=self.host, port=self.port)
            request.headers["Host"] = f"{self.host}:{self.port}"
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


# A demo shop cart (web/demo/demo.js creates "c_demo_" + 12 hex digits per page load): the demo charge view reads nothing else.
DEMO_CART_REF = re.compile(r"c_demo_[a-z0-9]{8,32}")


class DemoStaticFiles(StaticFiles):
    """The demo UI and the web SDK as static files: explicit JavaScript / CSS / HTML content types (the operating system's MIME
    registry can map .js to text/plain, which browsers refuse for module scripts), a Content-Security-Policy that only allows
    same-origin scripts, styles and requests, and no caching so a changed SDK is picked up on reload."""

    MEDIA_TYPES = {
        ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml",
    }
    HEADERS = {
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                                   "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-cache",
    }

    def file_response(self, full_path: Any, stat_result: Any, scope: Any, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        media_type = self.MEDIA_TYPES.get(Path(str(full_path)).suffix.lower())
        if media_type is not None and "content-type" in response.headers:
            response.headers["Content-Type"] = media_type
        for name, value in self.HEADERS.items():
            response.headers[name] = value
        return response


def register_demo(app: FastAPI, state: ApiState) -> None:
    """Maps the local demo (C# DemoEndpoints.Map) and its static files. Call it after every other route: the static
    mounts match any path under /demo."""
    repo_root = state.settings.repo_root
    file = repo_root / "data" / "seed" / "demo" / "partners.json"
    partners: list[Any] = []
    if file.is_file():
        partners = _get(json.loads(file.read_text(encoding="utf-8-sig")), "partners") or []

    def secret(name: str) -> str | None:
        for p in partners:
            webhook = _get(p, "webhook")
            url = _get(webhook, "url")
            if isinstance(url, str) and url.lower().endswith("/" + name.lower()):
                value = _get(webhook, "secret")
                return value if isinstance(value, str) else None
        return None

    quick_basket = partners[0] if partners else None
    received: dict[str, deque[dict[str, Any]]] = {}
    lock = threading.Lock()

    async def demo_index(request: Request) -> Response:
        return RedirectResponse("/demo/index.html", status_code=302)

    app.add_api_route("/demo", demo_index, methods=["GET"], name="demo:index", include_in_schema=False)

    def demo_session(request: Request) -> Response:
        """Stands in for the partner backend: mints an SDK session for the first demo partner (never expose this in production)."""
        if quick_basket is None:
            raise HsError.not_found("Demo partner")
        req = _record(request, "DemoSessionRequest req", {"cartRef": _string, "userRef": _string, "storeId": _string, "market": _string})
        ctx = PartnerContext(partner_id=uuid.UUID(str(_get(quick_basket, "partnerId"))), client_id="demo-backend", scopes=frozenset(Scopes.SDK_ALLOWED))
        session = state.idp.issue_sdk_session(ctx, {"cartRef": req.get("cartRef"), "userRef": req.get("userRef"), "storeId": req.get("storeId"),
                                                    "market": req.get("market")})
        return json_response(session, 201)

    app.add_api_route("/demo/api/session", demo_session, methods=["POST"], name="demo:session", include_in_schema=False,
                      dependencies=[Depends(pipeline(None, None, "DemoSessionRequest req"))])

    def demo_charges(request: Request) -> Response:
        """Stands in for the partner's back office: the RA-1 charges of one demo shop cart, read server-side with the first
        demo partner's identity (the browser never holds partner credentials). Only carts of the demo page (c_demo_...)."""
        if quick_basket is None:
            raise HsError.not_found("Demo partner")
        cart_ref = query_str(request, "cartRef", required=True) or ""
        if DEMO_CART_REF.fullmatch(cart_ref) is None:
            raise HsError.invalid("cartRef must be a demo shop cart: c_demo_ followed by 8 to 32 lowercase letters or digits")
        page = state.container.reco_charge_service.list(uuid.UUID(str(_get(quick_basket, "partnerId"))), cart_ref=cart_ref, limit=200)
        return json_response({"cartRef": cart_ref, "items": page["items"]}, headers={"Cache-Control": "no-store"})

    app.add_api_route("/demo/api/charges", demo_charges, methods=["GET"], name="demo:charges", include_in_schema=False)

    async def receive_webhook(request: Request) -> Response:
        name: str = request.path_params["name"]
        body = (await read_body(request)).decode("utf-8-sig", errors="replace")
        key = secret(name)
        signature = request.headers.get(WebhookSignature.HEADER)
        valid = key is not None and signature is not None and WebhookSignature.verify(key, signature, body, utc_now(), timedelta(minutes=5))
        try:
            document = jsonutil.loads(body)
        except (ValueError, RecursionError):
            document = {}
        event_type = document.get("type") if isinstance(document, dict) else None
        entry = {"receivedAt": iso(utc_now()), "signatureValid": valid, "type": event_type if isinstance(event_type, str) else None,
                 "body": JsonElement(document)}
        with lock:
            received.setdefault(name, deque(maxlen=200)).append(entry)
        return empty_response(204 if valid else 401)

    app.add_api_route("/demo/webhooks/{name}", receive_webhook, methods=["POST"], name="demo:webhooks:receive", include_in_schema=False)

    def list_webhooks(request: Request) -> Response:
        with lock:
            items = list(reversed(received.get(request.path_params["name"], ())))
        return json_response(items)

    app.add_api_route("/demo/webhooks/{name}", list_webhooks, methods=["GET"], name="demo:webhooks:list", include_in_schema=False)

    sdk = repo_root / "sdk" / "web"
    if sdk.is_dir():
        app.mount("/demo/sdk", DemoStaticFiles(directory=sdk), name="demo:sdk")
    ui = repo_root / "web" / "demo"
    if ui.is_dir():
        app.mount("/demo", DemoStaticFiles(directory=ui), name="demo:ui")
