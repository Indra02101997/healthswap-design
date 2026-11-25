"""Program.cs ported: the ASGI application factory.

Pipeline (outermost first, C# Program.cs order with CORS hoisted so problems carry CORS headers):
CORS ("sdk" policy) -> ProblemMiddleware -> AuthenticationMiddleware -> RateLimitMiddleware -> routes, where every API route
runs the `pipeline()` dependency (authorization, then contract validation / JSON binding) before its endpoint.

Routes: POST /oauth2/token, GET /.well-known/jwks.json, GET /.well-known/oauth-protected-resource, GET /openapi.yaml,
GET /healthz, GET /readyz, /v1/* (partner_routes), /admin/* (admin_routes), /mcp (mcp_tools) and, in Development or
with demo.enabled, /demo/*. FastAPI's generated /docs, /redoc and /openapi.json are disabled (the contract is api/openapi.yaml).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import unquote

import anyio
import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from healthswap.api import admin_routes, partner_routes
from healthswap.api.admin_routes import DemoLoopbackTransport
from healthswap.api.auth import TokenKeys, TokenValidator
from healthswap.api.contract import OpenApiContract
from healthswap.api.http import (
    ApiState,
    AuthenticationMiddleware,
    ProblemMiddleware,
    RateLimitMiddleware,
    base_url,
    json_response,
    problem_response,
)
from healthswap.api.mcp_tools import McpEndpoint, create_mcp_server
from healthswap.api.partner_routes import BackgroundJobs
from healthswap.platform.container import Container
from healthswap.platform.errors import FieldError, HsError
from healthswap.platform.settings import Settings, load_settings

log = logging.getLogger(__name__)

__all__ = ["create_app"]


def _form_value(form: FormData, name: str) -> str | None:
    """StringValues -> string: None when absent, multiple values joined with ','. File parts are not form values."""
    values = [v for v in form.getlist(name) if isinstance(v, str)]
    return ",".join(values) if values else None


def _route_demo_webhooks(container: Container, settings: Settings) -> None:
    """Demo partners are seeded with webhook URLs on http://localhost:8080; deliver them to this server's port instead."""
    client = httpx.Client(follow_redirects=False, timeout=Container.WEBHOOK_HTTP_TIMEOUT_SECONDS,
                          transport=DemoLoopbackTransport(settings.server.host, settings.server.port))
    try:
        container.override("webhook_http_client", client)
    except RuntimeError:  # the caller already built (or supplied) the webhook client
        client.close()


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    """The HealthSwap API. Without arguments the settings come from config/*.toml and the environment (HS_ENV,
    DATABASE_URL, HS_<SECTION>__<KEY>). The container's workers run for the application's lifetime and the container is
    closed on shutdown."""
    if settings is None:
        settings = container.settings if container is not None else load_settings()
    if container is None:
        container = Container(settings)
    contract = OpenApiContract.load(settings.repo_root / "api" / "openapi.yaml")
    keys = TokenKeys(settings.auth, settings.repo_root)
    validator = TokenValidator(settings.auth, keys)
    jobs = BackgroundJobs()
    state = ApiState(settings, container, contract, keys, validator, jobs)
    demo = settings.is_development or settings.demo.enabled
    if demo:
        _route_demo_webhooks(container, settings)
    mcp = create_mcp_server(state)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await anyio.to_thread.run_sync(lambda: container.start_workers(
            include_scheduler=settings.scheduler.enabled, include_cache_warmer=settings.cache.warm_on_startup))
        jobs.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await anyio.to_thread.run_sync(jobs.stop)
            await anyio.to_thread.run_sync(container.close)

    app = FastAPI(
        title="HealthSwap API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
        middleware=[
            Middleware(CORSMiddleware, allow_origins=list(settings.cors.origins), allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "traceparent"]),
            Middleware(ProblemMiddleware, is_development=settings.is_development),
            Middleware(AuthenticationMiddleware, validator=validator),
            Middleware(RateLimitMiddleware, options=settings.rate_limits),
        ],
    )
    app.state.hs = state

    async def http_error(request: Request, exc: Exception) -> Response:
        """Routing failures (404, 405) are empty responses, like ASP.NET Core endpoint routing."""
        assert isinstance(exc, StarletteHTTPException)
        return Response(status_code=exc.status_code, headers=exc.headers)

    async def validation_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RequestValidationError)
        errors = [FieldError("/" + "/".join(str(p) for p in e.get("loc", ())), str(e.get("msg", ""))) for e in exc.errors()]
        return problem_response(request.scope, 400, "HS-REQ-INVALID", "Request validation failed", "The request is not valid", errors)

    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)

    # ---------- OAuth 2.0 and discovery ----------

    async def oauth_token(request: Request) -> Response:
        """Development token endpoint (client credentials; HTTP Basic or form credentials)."""
        if settings.auth.authority:
            return Response(status_code=404)
        media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if media not in ("application/x-www-form-urlencoded", "multipart/form-data"):
            return json_response({"error": "invalid_request", "error_description": "Use application/x-www-form-urlencoded"}, 400)
        form = await request.form()
        if [v for v in form.getlist("grant_type") if isinstance(v, str)] != ["client_credentials"]:
            return json_response({"error": "unsupported_grant_type"}, 400)
        client_id, secret, scope = _form_value(form, "client_id"), _form_value(form, "client_secret"), _form_value(form, "scope")
        basic = request.headers.get("authorization")
        if basic is not None and basic[:6].lower() == "basic ":
            try:
                decoded = base64.b64decode("".join(basic[6:].split()), validate=True).decode("utf-8", errors="replace")
            except (binascii.Error, ValueError):
                return json_response({"error": "invalid_request", "error_description": "Malformed Basic credentials"}, 400)
            parts = decoded.split(":", 1)
            if len(parts) == 2:
                client_id, secret = unquote(parts[0]), unquote(parts[1])
        try:
            token = await anyio.to_thread.run_sync(lambda: state.idp.issue(client_id, secret, scope))
        except HsError as ex:
            if ex.code in ("invalid_client", "invalid_scope"):
                return json_response({"error": ex.code, "error_description": ex.detail}, ex.status)
            raise
        return json_response(token, headers={"Cache-Control": "no-store"})

    async def jwks(request: Request) -> Response:
        return json_response(keys.jwks())

    async def protected_resource(request: Request) -> Response:
        root = base_url(request)
        return json_response({
            "resource": root + "/mcp", "authorization_servers": [settings.auth.authority or root + "/oauth2"],
            "scopes_supported": ["carts.evaluate", "products.read", "events.write"], "bearer_methods_supported": ["header"],
        })

    async def openapi_yaml(request: Request) -> Response:
        return Response(contract.yaml, media_type="application/yaml")

    async def healthz(request: Request) -> Response:
        return json_response({"status": "ok"})

    def readyz(request: Request) -> Response:
        with container.db.raw():
            pass
        return json_response({"status": "ready"})

    def route(method: str, path: str, endpoint: Any, name: str) -> None:
        app.add_api_route(path, endpoint, methods=[method], name=name, include_in_schema=False)

    route("POST", "/oauth2/token", oauth_token, "oauth2:token")
    route("GET", "/.well-known/jwks.json", jwks, "jwks")
    route("GET", "/.well-known/oauth-protected-resource", protected_resource, "oauth-protected-resource")
    route("GET", "/openapi.yaml", openapi_yaml, "openapi.yaml")
    route("GET", "/healthz", healthz, "healthz")
    route("GET", "/readyz", readyz, "readyz")

    # ---------- APIs ----------

    partner_routes.register(app, state)
    admin_routes.register(app, state)
    app.add_route("/mcp", McpEndpoint(mcp), methods=["GET", "POST", "DELETE"], name="mcp", include_in_schema=False)
    if demo:
        admin_routes.register_demo(app, state)
    return app
