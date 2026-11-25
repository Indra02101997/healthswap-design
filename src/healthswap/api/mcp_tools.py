"""McpTools.cs ported: the MCP tools for shopping agents (HLD 7.6) on stateless streamable HTTP at /mcp.

Every recommendation carries the HealthSwap label; agents must show it and report SHOWN before ACCEPTED / ADDED_TO_CART so
RA-1 add fees are only charged for disclosed, labeled recommendations.

Parity with the C# tools: the same names, descriptions, parameters (input schemas as the C# SDK publishes them),
annotations and output (`structuredContent` {"result": ...} plus the same object as compact JSON text; nulls omitted).
The platform services are synchronous, so each tool runs on a worker thread (anyio.to_thread.run_sync).

This module deliberately has no `from __future__ import annotations`: the MCP SDK reads the tool signatures at runtime.
"""

import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from healthswap.api.auth import Scopes
from healthswap.api.http import PRINCIPAL, ApiState, authorize
from healthswap.domain.primitives import new_id
from healthswap.domain.recommendation.explanations import ExplanationTemplates, FactSheets
from healthswap.domain.recommendation.labels import ShopperCopy
from healthswap.platform import jsonutil, netjson
from healthswap.platform.api_mapper import ApiMapper
from healthswap.platform.errors import HsError
from healthswap.platform.services.events import SuggestionEvent
from healthswap.platform.tenancy import PartnerContext

log = logging.getLogger(__name__)

_INSTRUCTIONS = ("Show every alternative with its label. Call record_swap_decision with SHOWN when you present it, then ACCEPTED or ADDED_TO_CART "
                 "when the shopper adds it.")
_DECISIONS = {"SHOWN": "IMPRESSION", "ACCEPTED": "ACCEPT", "ADDED_TO_CART": "ADD_TO_CART", "DISMISSED": "DISMISS", "UNDONE": "UNDO"}


class AgentCartLine(BaseModel):
    """A cart line as an agent sends it (C# AgentCartLine)."""

    partnerSku: Annotated[str, Field(description="Partner SKU of the cart line")]
    quantity: Annotated[Decimal, Field(description="Quantity in the cart")]
    unitPrice: Annotated[str, Field(description='Unit price as a decimal string, for example "56.00"')]
    gtin: Annotated[str | None, Field(description="Optional GTIN when the SKU may not be synced yet")] = None


# ---------------------------------------------------------------------------------------------------------------------
# Schemas exactly as the C# SDK publishes them (tools/list)
# ---------------------------------------------------------------------------------------------------------------------


def _p(description: str, type_: str | list[str], *, fmt: str | None = None, default: Any = ..., **extra: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {"description": description, "type": type_}
    if fmt is not None:
        schema["format"] = fmt
    schema.update(extra)
    if default is not ...:
        schema["default"] = default
    return schema


def _optional(description: str, *, fmt: str | None = None) -> dict[str, Any]:
    return _p(description, ["string", "null"], fmt=fmt, default=None)


def _input(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "$defs": {"output": True},
    "anyOf": [{"$ref": "#/$defs/output"}, {"properties": {"result": {"$ref": "#/$defs/output"}}, "required": ["result"]}],
}

_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "evaluate_cart": _input({
        "market": _p("ISO 3166-1 alpha-2 market, for example IN or US", "string"),
        "currency": _p("ISO 4217 currency, for example INR", "string"),
        "lines": _p("Cart lines", "array", items={
            "type": "object",
            "properties": {
                "partnerSku": _p("Partner SKU of the cart line", "string"),
                "quantity": _p("Quantity in the cart", "number"),
                "unitPrice": _p('Unit price as a decimal string, for example "56.00"', "string"),
                "gtin": _optional("Optional GTIN when the SKU may not be synced yet"),
            },
            "required": ["partnerSku", "quantity", "unitPrice"],
        }),
        "cartRef": _p("Partner cart reference; reuse it for the whole conversation", "string"),
        "storeId": _optional("Store id for availability"),
        "locale": _optional("BCP 47 locale such as en-IN or hi-IN"),
    }, ["market", "currency", "lines", "cartRef"]),
    "get_product_health": _input({
        "market": _p("Market code, for example IN", "string"),
        "productId": _optional("Canonical product id", fmt="uuid"),
        "gtin": _optional("GTIN-8/12/13/14"),
        "partnerSku": _optional("Partner SKU"),
        "locale": _optional("Locale for labels"),
    }, ["market"]),
    "find_healthier_alternatives": _input({
        "productId": _p("Canonical product id", "string", fmt="uuid"),
        "market": _p("Market code", "string"),
        "cartRef": _optional("Cart reference for the conversation (enables add tracking)"),
        "storeId": _optional("Store id for availability"),
        "limit": _p("Maximum alternatives (1-10)", "integer", default=3),
        "locale": _optional("Locale"),
    }, ["productId", "market"]),
    "explain_swap": _input({
        "fromProductId": _p("Original product id", "string", fmt="uuid"),
        "toProductId": _p("Alternative product id", "string", fmt="uuid"),
        "market": _p("Market code", "string"),
        "locale": _optional("Locale"),
    }, ["fromProductId", "toProductId", "market"]),
    "record_swap_decision": _input({
        "suggestionId": _p("suggestionId from evaluate_cart or find_healthier_alternatives", "string", fmt="uuid"),
        "decision": _p("SHOWN, ACCEPTED, ADDED_TO_CART, DISMISSED or UNDONE", "string"),
        "alternativePartnerSku": _optional("Partner SKU of the alternative (required for ACCEPTED and ADDED_TO_CART)"),
        "eventId": _optional("Client-generated id for deduplication; reuse it when retrying", fmt="uuid"),
    }, ["suggestionId", "decision"]),
}

_DESCRIPTIONS = {
    "evaluate_cart": ("Evaluates a shopping cart and returns healthier, in-stock, constraint-safe swap suggestions. Each alternative is labeled "
                      "'Recommended by HealthSwap'; show the label and the disclosure to the shopper."),
    "get_product_health": ("Returns the health profile of a product (HSI, jurisdiction score, flags, allergens, additives) by productId, GTIN or "
                           "partner SKU."),
    "find_healthier_alternatives": ("Lists healthier alternatives for one product. With a cartRef a suggestionId is returned for record_swap_decision. "
                                    "Alternatives are labeled 'Recommended by HealthSwap'."),
    "explain_swap": ("Explains, with verified facts only, why product B is a healthier alternative to product A (per 100 g/ml comparisons, additives, "
                     "processing)."),
    "record_swap_decision": ("Records what happened with a suggestion: SHOWN (label displayed), ACCEPTED (swap accepted and added), ADDED_TO_CART "
                             "(recommended item added), DISMISSED or UNDONE. ACCEPTED and ADDED_TO_CART may create an RA-1 add fee for the retailer."),
}

_ANNOTATIONS = {
    "evaluate_cart": ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
    "get_product_health": ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    "find_healthier_alternatives": ToolAnnotations(read_only_hint=False, idempotent_hint=False, open_world_hint=False),
    "explain_swap": ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    "record_swap_decision": ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
}


# ---------------------------------------------------------------------------------------------------------------------
# Tool plumbing
# ---------------------------------------------------------------------------------------------------------------------


def _partner(ctx: Context) -> PartnerContext:
    """The calling partner (C# HealthSwapTools.Partner)."""
    request = ctx.request_context.request
    principal = request.scope.get(PRINCIPAL) if request is not None else None
    partner = principal.partner if principal is not None else None
    if partner is None:
        raise HsError(401, "HS-AUTH-INVALID", "Unauthorized", "A partner token is required")
    if "carts.evaluate" not in partner.scopes and "products.read" not in partner.scopes:
        raise HsError.forbidden("The token needs carts.evaluate or products.read")
    return partner


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


async def _run(name: str, work: Callable[[], Any]) -> CallToolResult:
    """Runs a tool body on a worker thread and shapes the result like the C# SDK (UseStructuredContent = true)."""
    try:
        value = await anyio.to_thread.run_sync(work)
    except HsError as ex:
        # C# reports every failure as "An error occurred invoking '<tool>'."; the problem code and detail are safe to
        # show and tell the agent what to fix.
        return _error(f"{ex.code}: {ex.detail if ex.detail is not None else ex.title}")
    except Exception:
        log.exception("MCP tool %s failed", name)
        return _error(f"An error occurred invoking '{name}'.")
    text = netjson.serialize_web(jsonutil.to_plain(value, keep_decimal=True))
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content={"result": jsonutil.to_plain(value)})


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _get(d: Any, *path: str) -> Any:
    """d[path[0]][path[1]]... with None for anything missing (the C# objects' members)."""
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


# ---------------------------------------------------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------------------------------------------------


def create_tools(state: ApiState) -> list[Tool]:
    c = state.container

    async def evaluate_cart(
        ctx: Context,
        market: Annotated[str, Field(description="ISO 3166-1 alpha-2 market, for example IN or US")],
        currency: Annotated[str, Field(description="ISO 4217 currency, for example INR")],
        lines: Annotated[list[AgentCartLine], Field(description="Cart lines")],
        cartRef: Annotated[str, Field(description="Partner cart reference; reuse it for the whole conversation")],
        storeId: Annotated[str | None, Field(description="Store id for availability")] = None,
        locale: Annotated[str | None, Field(description="BCP 47 locale such as en-IN or hi-IN")] = None,
    ) -> CallToolResult:
        def work() -> Any:
            result = c.evaluation_service.evaluate(_partner(ctx), {
                "cartRef": cartRef, "market": market, "currency": currency, "storeId": storeId, "locale": locale,
                "context": {"placement": "AGENT", "channel": "AGENT"},
                "lines": [
                    {"lineId": str(i + 1), "partnerSku": line.partnerSku, "gtin": line.gtin, "quantity": line.quantity,
                     "unitPrice": {"amount": line.unitPrice, "currency": currency}}
                    for i, line in enumerate(lines)
                ],
            })
            return {
                "evaluationId": result.get("evaluationId"), "degraded": result.get("degraded"), "summary": result.get("summary"),
                "disclosure": _get(result, "disclosures", "feeDisclosure"),
                "suggestions": [
                    {
                        "suggestionId": s.get("suggestionId"), "lineId": s.get("lineId"),
                        "original": {
                            "partnerSku": _get(s, "original", "partnerSku"), "title": _get(s, "original", "title"),
                            "hsi": _get(s, "original", "health", "hsi"), "hsiGrade": _get(s, "original", "health", "hsiGrade"),
                        },
                        "alternatives": [
                            {
                                "rank": a.get("rank"), "partnerSku": _get(a, "product", "partnerSku"), "title": _get(a, "product", "title"),
                                "hsi": _get(a, "product", "health", "hsi"), "healthDelta": a.get("healthDelta"), "unitPrice": a.get("unitPrice"),
                                "label": _get(a, "recommendation", "text"), "disclosure": _get(a, "recommendation", "disclosure"),
                                "explanation": _get(a, "explanation", "short"), "requiresAvailabilityCheck": a.get("requiresAvailabilityCheck"),
                            }
                            for a in s.get("alternatives") or []
                        ],
                    }
                    for s in result.get("suggestions") or []
                ],
                "instructions": _INSTRUCTIONS,
            }

        return await _run("evaluate_cart", work)

    async def get_product_health(
        ctx: Context,
        market: Annotated[str, Field(description="Market code, for example IN")],
        productId: Annotated[uuid.UUID | None, Field(description="Canonical product id")] = None,
        gtin: Annotated[str | None, Field(description="GTIN-8/12/13/14")] = None,
        partnerSku: Annotated[str | None, Field(description="Partner SKU")] = None,
        locale: Annotated[str | None, Field(description="Locale for labels")] = None,
    ) -> CallToolResult:
        def work() -> Any:
            partner = _partner(ctx)
            if productId is not None:
                p = c.product_service.get(partner, productId, market, locale)
            else:
                p = c.product_service.lookup(partner, market, gtin, partnerSku, locale)
            return {
                "productId": p.get("productId"), "displayName": p.get("displayName"), "brand": p.get("brand"), "hsi": p.get("hsi"),
                "hsiGrade": p.get("hsiGrade"), "primaryScore": p.get("primaryScore"), "novaGroup": p.get("novaGroup"), "flags": p.get("flags"),
                "badges": [b.get("label") for b in p.get("badges") or []],
                "allergens": [f"{_text(a.get('code'))}:{_text(a.get('presence'))}" for a in p.get("allergens") or []],
                "additives": [f"INS {_text(a.get('insNumber'))} {_text(a.get('name'))} (tier {_text(a.get('concernTier'))})" for a in p.get("additives") or []],
                "dataSufficiency": p.get("dataSufficiency"), "recalled": p.get("recalled"),
            }

        return await _run("get_product_health", work)

    async def find_healthier_alternatives(
        ctx: Context,
        productId: Annotated[uuid.UUID, Field(description="Canonical product id")],
        market: Annotated[str, Field(description="Market code")],
        cartRef: Annotated[str | None, Field(description="Cart reference for the conversation (enables add tracking)")] = None,
        storeId: Annotated[str | None, Field(description="Store id for availability")] = None,
        limit: Annotated[int, Field(description="Maximum alternatives (1-10)")] = 3,
        locale: Annotated[str | None, Field(description="Locale")] = None,
    ) -> CallToolResult:
        def work() -> Any:
            r = c.evaluation_service.alternatives(_partner(ctx), productId, market, storeId, cartRef, None, "AGENT", None, locale, max(1, min(limit, 10)))
            return {
                "suggestionId": r.get("suggestionId"), "expiresAt": r.get("expiresAt"), "disclosure": _get(r, "disclosures", "feeDisclosure"),
                "alternatives": [
                    {
                        "rank": a.get("rank"), "partnerSku": _get(a, "product", "partnerSku"), "productId": _get(a, "product", "productId"),
                        "title": _get(a, "product", "title"), "healthDelta": a.get("healthDelta"), "unitPrice": a.get("unitPrice"),
                        "label": _get(a, "recommendation", "text"), "explanation": _get(a, "explanation", "short"), "reasonCodes": a.get("reasonCodes"),
                    }
                    for a in r.get("alternatives") or []
                ],
            }

        return await _run("find_healthier_alternatives", work)

    async def explain_swap(
        ctx: Context,
        fromProductId: Annotated[uuid.UUID, Field(description="Original product id")],
        toProductId: Annotated[uuid.UUID, Field(description="Alternative product id")],
        market: Annotated[str, Field(description="Market code")],
        locale: Annotated[str | None, Field(description="Locale")] = None,
    ) -> CallToolResult:
        def work() -> Any:
            _partner(ctx)
            loc = ApiMapper.resolve_locale(market, locale)
            a = c.hot_cache.get_profile(market, fromProductId)
            if a is None:
                raise HsError.not_found(f"Product {fromProductId}")
            b = c.hot_cache.get_profile(market, toProductId)
            if b is None:
                raise HsError.not_found(f"Product {toProductId}")
            pack = c.refs.get_pack(market)
            sheet = FactSheets.build(a, b, pack)
            selected = FactSheets.select(sheet)
            cached = c.hot_cache.get_explanation(market, loc, fromProductId, toProductId, sheet.hash)
            text = cached.short if cached is not None and cached.short is not None else None
            if text is None:
                text = ExplanationTemplates.render(selected if len(selected) > 0 else list(sheet.facts[:1]), loc).short
            zero = Decimal(0)
            return {
                "summary": text, "healthDelta": (b.hsi if b.hsi is not None else zero) - (a.hsi if a.hsi is not None else zero),
                "from": {"name": a.name, "hsi": a.hsi, "hsiGrade": a.hsi_grade}, "to": {"name": b.name, "hsi": b.hsi, "hsiGrade": b.hsi_grade},
                "facts": ApiMapper.details(sheet.facts), "disclaimer": ShopperCopy.label_disclaimer(loc, b.nutrition.is_liquid),
                "methodologyUrl": pack.methodology_url,
            }

        return await _run("explain_swap", work)

    async def record_swap_decision(
        ctx: Context,
        suggestionId: Annotated[uuid.UUID, Field(description="suggestionId from evaluate_cart or find_healthier_alternatives")],
        decision: Annotated[str, Field(description="SHOWN, ACCEPTED, ADDED_TO_CART, DISMISSED or UNDONE")],
        alternativePartnerSku: Annotated[
            str | None, Field(description="Partner SKU of the alternative (required for ACCEPTED and ADDED_TO_CART)")] = None,
        eventId: Annotated[uuid.UUID | None, Field(description="Client-generated id for deduplication; reuse it when retrying")] = None,
    ) -> CallToolResult:
        def work() -> Any:
            event_type = _DECISIONS.get(decision)
            if event_type is None:
                raise HsError.invalid("decision must be SHOWN, ACCEPTED, ADDED_TO_CART, DISMISSED or UNDONE")
            result = c.event_service.ingest(_partner(ctx), [SuggestionEvent(
                event_id=eventId if eventId is not None else new_id(), type=event_type, suggestion_id=suggestionId, occurred_at=c.clock.utc_now(),
                alternative_partner_sku=alternativePartnerSku, metadata={"channel": "AGENT"},
            )])
            charges = result.get("charges")
            return {
                "accepted": result.get("accepted"), "duplicates": result.get("duplicates"), "rejected": result.get("rejected"),
                "charges": None if charges is None else [{"chargeId": ch.get("chargeId"), "state": ch.get("state"), "amount": ch.get("amount")} for ch in charges],
            }

        return await _run("record_swap_decision", work)

    tools = []
    for fn in (evaluate_cart, get_product_health, find_healthier_alternatives, explain_swap, record_swap_decision):
        name = fn.__name__
        tool = Tool.from_function(fn, name=name, description=_DESCRIPTIONS[name], annotations=_ANNOTATIONS[name], structured_output=False)
        tool.parameters = _INPUT_SCHEMAS[name]
        tool.fn_metadata.output_schema = _OUTPUT_SCHEMA
        tools.append(tool)
    return tools


def create_mcp_server(state: ApiState) -> MCPServer:
    """The "healthswap" MCP server (stateless streamable HTTP, SSE responses like the C# SDK)."""
    mcp = MCPServer(name="healthswap", version="1.0.0", tools=create_tools(state))
    # Creates mcp.session_manager; the Starlette app it returns is not used (McpEndpoint is mounted instead).
    mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=False,
                            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return mcp


class McpEndpoint:
    """/mcp: a bearer token with carts.evaluate or products.read (the C# "mcp" policy), then the MCP transport. A missing or
    invalid token is a 401 problem whose WWW-Authenticate names the RFC 9728 protected resource metadata."""

    def __init__(self, mcp: MCPServer) -> None:
        self.app = StreamableHTTPASGIApp(mcp.session_manager)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        authorize(scope, Scopes.MCP)
        await self.app(scope, receive, send)
