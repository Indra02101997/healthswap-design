"""PartnerEndpoints.cs ported: every /v1 operation of api/openapi.yaml plus the pre-signed feed upload and the feed error
report. Each route is named with its operationId (the contract validator and the tests find routes by name) and carries
the same scope requirement as C#; `BackgroundJobs` is the asynchronous partner work queue (catalog enrichment, feeds).
"""

from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import anyio
from fastapi import Depends, FastAPI
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from healthswap.api.http import (
    ApiState,
    base_url,
    body_of,
    clamp,
    empty_response,
    idempotent,
    json_response,
    partner_of,
    pipeline,
    query_int,
    query_str,
    query_uuid,
)
from healthswap.platform.errors import HsError
from healthswap.platform.services.catalog import PreferenceService
from healthswap.platform.workers import BackgroundWorker

_PLACEMENTS = (None, "PDP", "SEARCH")
_CHARGE_STATES = (None, "PENDING", "FINAL", "VOID", "REJECTED", "CREDITED")


class BackgroundJobs(BackgroundWorker):
    """Background work queue for asynchronous partner work (C# BackgroundJobs hosted service)."""

    def __init__(self) -> None:
        super().__init__("BackgroundJobs")
        self._queue: queue.Queue[tuple[str, Callable[[], object]]] = queue.Queue()
        self._pending = 0
        self._count = threading.Lock()

    @property
    def pending(self) -> int:
        return self._pending

    def enqueue(self, name: str, work: Callable[[], object]) -> None:
        with self._count:
            self._pending += 1
        self._queue.put((name, work))

    def execute(self) -> None:
        for name, work in self.read_all(self._queue):
            try:
                work()
            except Exception:
                self.log.exception("Background job %s failed", name)
            finally:
                with self._count:
                    self._pending -= 1


def _limit(limit: int | None, maximum: int = 500) -> int:
    return clamp(100 if limit is None else limit, 1, maximum)


def register(app: FastAPI, state: ApiState) -> None:
    """Maps the partner API (C# PartnerEndpoints.Map)."""
    c = state.container

    def add(method: str, path: str, name: str, endpoint: Callable[[Request], Any], scope: str | None) -> None:
        app.add_api_route(
            path, endpoint, methods=[method], name=name, include_in_schema=False,
            dependencies=[Depends(pipeline(name, None if scope is None else (scope,)))],
        )

    # ---------- SDK sessions ----------

    def create_sdk_session(request: Request) -> Response:
        req = body_of(request)

        def handler() -> tuple[int, Any]:
            ctx = partner_of(request)
            if ctx.is_sdk_session:
                raise HsError.forbidden("SDK sessions cannot mint other sessions")
            if req.get("userRef") is not None:
                PreferenceService.validate_user_ref(req["userRef"])
            partner = c.partners.get(ctx.partner_id)
            if req.get("market") not in partner.markets:
                raise HsError.unprocessable("HS-UNSUPPORTED-MARKET", f"Market {req.get('market')} is not enabled for this partner")
            return 201, state.idp.issue_sdk_session(ctx, req)

        return idempotent(request, False, handler)

    add("POST", "/v1/sdk/sessions", "createSdkSession", create_sdk_session, "sdk.sessions.write")

    # ---------- Catalog ----------

    def upsert_catalog_items(request: Request) -> Response:
        req = body_of(request)

        def handler() -> tuple[int, Any]:
            ctx = partner_of(request)
            outcome = c.catalog_service.upsert(ctx.partner_id, req.get("items") or [], None)
            if outcome.needs_enrichment:
                needs = outcome.needs_enrichment
                state.jobs.enqueue("catalog-enrichment", lambda: c.catalog_enrichment.enrich(ctx.partner_id, needs))
            return 200, outcome.result

        return idempotent(request, False, handler)

    add("POST", "/v1/catalog/item-batches", "upsertCatalogItems", upsert_catalog_items, "catalog.write")

    def create_catalog_feed(request: Request) -> Response:
        req = body_of(request)

        def handler() -> tuple[int, Any]:
            ctx = partner_of(request)
            root = base_url(request)
            feed = c.feed_service.create(
                ctx.partner_id, req, lambda feed_id, token: f"{root}/v1/catalog/feeds/{feed_id}/upload?token={quote(token, safe='')}")
            return 201, feed

        return idempotent(request, False, handler)

    add("POST", "/v1/catalog/feeds", "createCatalogFeed", create_catalog_feed, "catalog.write")

    async def upload_catalog_feed(request: Request) -> Response:
        """Pre-signed upload target (stands in for object storage). Authorized by the signed token, not by OAuth."""
        feed_id: uuid.UUID = request.path_params["feedId"]
        token = query_str(request, "token", required=True)
        stream = request.stream().__aiter__()

        async def next_chunk() -> bytes | None:
            try:
                return await stream.__anext__()
            except StopAsyncIteration:
                return None

        def chunks() -> Any:
            while (chunk := anyio.from_thread.run(next_chunk)) is not None:
                if chunk:
                    yield chunk

        partner_id, accepted_id = await anyio.to_thread.run_sync(lambda: c.feed_service.accept_upload(feed_id, token, chunks()))
        state.jobs.enqueue("feed-processing", lambda: c.feed_service.process(partner_id, accepted_id))
        return empty_response(202)

    add("PUT", "/v1/catalog/feeds/{feedId:uuid}/upload", "uploadCatalogFeed", upload_catalog_feed, None)

    def get_catalog_feed(request: Request) -> Response:
        root = base_url(request)
        feed_id = request.path_params["feedId"]
        return json_response(c.feed_service.get(partner_of(request).partner_id, feed_id, lambda fid: f"{root}/v1/catalog/feeds/{fid}/errors"))

    add("GET", "/v1/catalog/feeds/{feedId:uuid}", "getCatalogFeed", get_catalog_feed, "catalog.read")

    def get_catalog_feed_errors(request: Request) -> Response:
        feed_id = request.path_params["feedId"]
        c.feed_service.get(partner_of(request).partner_id, feed_id, None)  # tenant check
        path = c.feed_service.error_report_path(feed_id)
        return FileResponse(path, media_type="text/plain") if path is not None else empty_response(404)

    add("GET", "/v1/catalog/feeds/{feedId:uuid}/errors", "getCatalogFeedErrors", get_catalog_feed_errors, "catalog.read")

    def get_catalog_item(request: Request) -> Response:
        return json_response(c.catalog_service.get(partner_of(request).partner_id, request.path_params["partnerSku"]))

    add("GET", "/v1/catalog/items/{partnerSku}", "getCatalogItem", get_catalog_item, "catalog.read")

    def delist_catalog_item(request: Request) -> Response:
        c.catalog_service.delist(partner_of(request).partner_id, request.path_params["partnerSku"])
        return empty_response(204)

    add("DELETE", "/v1/catalog/items/{partnerSku}", "delistCatalogItem", delist_catalog_item, "catalog.write")

    def override_catalog_match(request: Request) -> Response:
        return json_response(c.catalog_service.override_match(partner_of(request).partner_id, request.path_params["partnerSku"], body_of(request)))

    add("PUT", "/v1/catalog/items/{partnerSku}/match", "overrideCatalogMatch", override_catalog_match, "catalog.write")

    # ---------- Inventory ----------

    def upsert_store_inventory(request: Request) -> Response:
        req = body_of(request)
        store_id = request.path_params["storeId"]
        return idempotent(request, False, lambda: (202, c.inventory_service.upsert(partner_of(request).partner_id, store_id, req)))

    add("POST", "/v1/stores/{storeId}/inventory-batches", "upsertStoreInventory", upsert_store_inventory, "inventory.write")

    # ---------- Products ----------

    def lookup_product(request: Request) -> Response:
        market = query_str(request, "market", required=True)
        gtin, sku, locale = query_str(request, "gtin"), query_str(request, "partnerSku"), query_str(request, "locale")
        return json_response(c.product_service.lookup(partner_of(request), market, gtin, sku, locale))

    add("GET", "/v1/products/lookup", "lookupProduct", lookup_product, "products.read")

    def get_product_health_profile(request: Request) -> Response:
        market = query_str(request, "market", required=True)
        locale = query_str(request, "locale")
        return json_response(c.product_service.get(partner_of(request), request.path_params["productId"], market, locale))

    add("GET", "/v1/products/{productId:uuid}", "getProductHealthProfile", get_product_health_profile, "products.read")

    def list_product_alternatives(request: Request) -> Response:
        market = query_str(request, "market", required=True)
        store_id, cart_ref, user_ref = query_str(request, "storeId"), query_str(request, "cartRef"), query_str(request, "userRef")
        placement, currency, locale = query_str(request, "placement"), query_str(request, "currency"), query_str(request, "locale")
        limit = query_int(request, "limit")
        if placement not in _PLACEMENTS:
            raise HsError.invalid("placement must be PDP or SEARCH")
        if user_ref is not None:
            PreferenceService.validate_user_ref(user_ref)
        return json_response(c.evaluation_service.alternatives(
            partner_of(request), request.path_params["productId"], market, store_id, cart_ref, user_ref, placement, currency, locale,
            clamp(5 if limit is None else limit, 1, 10),
        ))

    add("GET", "/v1/products/{productId:uuid}/alternatives", "listProductAlternatives", list_product_alternatives, "products.read")

    # ---------- Cart evaluations ----------

    def evaluate_cart(request: Request) -> Response:
        req = body_of(request)

        def handler() -> tuple[int, Any]:
            if req.get("userRef") is not None:
                PreferenceService.validate_user_ref(req["userRef"])
            return 200, c.evaluation_service.evaluate(partner_of(request), req)

        return idempotent(request, False, handler)

    add("POST", "/v1/cart-evaluations", "evaluateCart", evaluate_cart, "carts.evaluate")

    def get_cart_evaluation(request: Request) -> Response:
        ctx = partner_of(request)
        evaluation_id = request.path_params["evaluationId"]
        evaluation = c.evaluation_service.get(ctx.partner_id, evaluation_id)
        if ctx.is_sdk_session and evaluation.get("cartRef") != ctx.sdk_cart_ref:
            raise HsError.not_found(f"Cart evaluation {evaluation_id}")
        return json_response(evaluation)

    add("GET", "/v1/cart-evaluations/{evaluationId:uuid}", "getCartEvaluation", get_cart_evaluation, "carts.evaluate")

    # ---------- Events ----------

    def report_suggestion_events(request: Request) -> Response:
        return json_response(c.event_service.ingest(partner_of(request), body_of(request)), 202)

    add("POST", "/v1/events", "reportSuggestionEvents", report_suggestion_events, "events.write")

    # ---------- Orders ----------

    def create_order(request: Request) -> Response:
        req = body_of(request)

        def handler() -> tuple[int, Any]:
            if req.get("userRef") is not None:
                PreferenceService.validate_user_ref(req["userRef"])
            return 201, c.order_service.create(partner_of(request), req)

        return idempotent(request, True, handler)

    add("POST", "/v1/orders", "createOrder", create_order, "orders.write")

    def get_order(request: Request) -> Response:
        return json_response(c.order_service.get(partner_of(request).partner_id, request.path_params["orderId"]))

    add("GET", "/v1/orders/{orderId}", "getOrder", get_order, "orders.read")

    def update_order_status(request: Request) -> Response:
        return json_response(c.order_service.update_status(partner_of(request).partner_id, request.path_params["orderId"], body_of(request)))

    add("PATCH", "/v1/orders/{orderId}", "updateOrderStatus", update_order_status, "orders.write")

    def create_order_adjustments(request: Request) -> Response:
        req = body_of(request)
        order_id = request.path_params["orderId"]
        return idempotent(request, False, lambda: (202, c.order_service.adjust(partner_of(request).partner_id, order_id, req)))

    add("POST", "/v1/orders/{orderId}/adjustments", "createOrderAdjustments", create_order_adjustments, "orders.write")

    def get_order_attribution(request: Request) -> Response:
        return json_response(c.order_service.attribution_summary(partner_of(request).partner_id, request.path_params["orderId"]))

    add("GET", "/v1/orders/{orderId}/attribution", "getOrderAttribution", get_order_attribution, "orders.read")

    # ---------- Shoppers ----------

    def get_shopper_preferences(request: Request) -> Response:
        return json_response(c.preference_service.get(partner_of(request).partner_id, request.path_params["userRef"]))

    add("GET", "/v1/users/{userRef}/preferences", "getShopperPreferences", get_shopper_preferences, "users.read")

    def put_shopper_preferences(request: Request) -> Response:
        return json_response(c.preference_service.put(partner_of(request).partner_id, request.path_params["userRef"], body_of(request)))

    add("PUT", "/v1/users/{userRef}/preferences", "putShopperPreferences", put_shopper_preferences, "users.write")

    def erase_shopper(request: Request) -> Response:
        return json_response(c.privacy_service.erase(partner_of(request).partner_id, request.path_params["userRef"]), 202)

    add("DELETE", "/v1/users/{userRef}", "eraseShopper", erase_shopper, "users.write")

    # ---------- Billing ----------

    def list_statements(request: Request) -> Response:
        period, status, cursor = query_str(request, "period"), query_str(request, "status"), query_str(request, "cursor")
        limit = query_int(request, "limit")
        return json_response(c.billing_service.list(partner_of(request).partner_id, period, status, cursor, _limit(limit)))

    add("GET", "/v1/billing/statements", "listStatements", list_statements, "billing.read")

    def get_statement(request: Request) -> Response:
        return json_response(c.billing_service.get(partner_of(request).partner_id, request.path_params["statementId"]))

    add("GET", "/v1/billing/statements/{statementId:uuid}", "getStatement", get_statement, "billing.read")

    def list_statement_lines(request: Request) -> Response:
        cursor, limit = query_str(request, "cursor"), query_int(request, "limit")
        return json_response(c.billing_service.lines(partner_of(request).partner_id, request.path_params["statementId"], cursor, _limit(limit)))

    add("GET", "/v1/billing/statements/{statementId:uuid}/lines", "listStatementLines", list_statement_lines, "billing.read")

    def list_statement_recommendation_charges(request: Request) -> Response:
        cursor, limit = query_str(request, "cursor"), query_int(request, "limit")
        return json_response(c.reco_charge_service.list(
            partner_of(request).partner_id, None, None, None, None, request.path_params["statementId"], cursor, _limit(limit, 200)))

    add("GET", "/v1/billing/statements/{statementId:uuid}/recommendation-charges", "listStatementRecommendationCharges",
        list_statement_recommendation_charges, "billing.read")

    def list_recommendation_charges(request: Request) -> Response:
        period, state_, cart_ref = query_str(request, "period"), query_str(request, "state"), query_str(request, "cartRef")
        suggestion_id = query_uuid(request, "suggestionId")
        cursor, limit = query_str(request, "cursor"), query_int(request, "limit")
        if state_ not in _CHARGE_STATES:
            raise HsError.invalid(f"Unknown state {state_}")
        return json_response(c.reco_charge_service.list(
            partner_of(request).partner_id, period, state_, cart_ref, suggestion_id, None, cursor, _limit(limit, 200)))

    add("GET", "/v1/billing/recommendation-charges", "listRecommendationCharges", list_recommendation_charges, "billing.read")

    def get_recommendation_charge(request: Request) -> Response:
        return json_response(c.reco_charge_service.get(partner_of(request).partner_id, request.path_params["chargeId"]))

    add("GET", "/v1/billing/recommendation-charges/{chargeId:uuid}", "getRecommendationCharge", get_recommendation_charge, "billing.read")

    def create_dispute(request: Request) -> Response:
        req = body_of(request)
        return idempotent(request, False, lambda: (201, c.billing_service.open_dispute(partner_of(request).partner_id, req)))

    add("POST", "/v1/billing/disputes", "createDispute", create_dispute, "billing.write")

    def get_dispute(request: Request) -> Response:
        return json_response(c.billing_service.get_dispute(partner_of(request).partner_id, request.path_params["disputeId"]))

    add("GET", "/v1/billing/disputes/{disputeId:uuid}", "getDispute", get_dispute, "billing.read")

    # ---------- Transparency (public) ----------

    def list_scoring_models(request: Request) -> Response:
        market = query_str(request, "market")
        if market is not None and (len(market) != 2 or not all("A" <= ch <= "Z" for ch in market)):
            raise HsError.invalid("market must be an ISO 3166-1 alpha-2 code")
        return json_response(c.transparency_service.models(market))

    add("GET", "/v1/scoring-models", "listScoringModels", list_scoring_models, None)

    def get_service_status(request: Request) -> Response:
        return json_response(c.transparency_service.status())

    add("GET", "/v1/status", "getServiceStatus", get_service_status, None)


__all__ = ["BackgroundJobs", "register"]
