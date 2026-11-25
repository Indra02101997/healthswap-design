"""HealthSwap Partner API client for server-side Python (depends only on httpx).

Usage::

    from healthswap_client import HealthSwapClient, HealthSwapError, events, verify_webhook

    with HealthSwapClient("https://api.in.healthswap.example", CLIENT_ID, CLIENT_SECRET) as hs:  # secrets from a vault
        evaluation = hs.evaluate_cart({
            "cartRef": "c_7f3a91", "market": "IN", "storeId": "BLR-KOR-012", "currency": "INR", "locale": "en-IN",
            "lines": [{"lineId": "1", "partnerSku": "SKU-NDL-280", "quantity": 2, "unitPrice": {"amount": "56.00", "currency": "INR"}}],
        })  # fails open within 150 ms: {"suggestions": [], "degraded": True, "failOpen": {"reason": "TIMEOUT", ...}}
        for suggestion in evaluation["suggestions"]:
            alternative = suggestion["alternatives"][0]  # render alternative["recommendation"]["text"] and the fee disclosure
            hs.report_events([events.impression(suggestion["suggestionId"]),
                              events.accept(suggestion["suggestionId"], alternative["product"]["partnerSku"], quantity=2)])
        try:
            order = hs.create_order({...})  # the required Idempotency-Key is generated (and reused on retries)
        except HealthSwapError as e:
            print(e.status, e.code, e.detail, e.errors)
        for charge in hs.iter_recommendation_charges(cart_ref="c_7f3a91"):  # follows nextCursor
            print(charge["state"], charge["amount"])

    # Webhook receiver: verify the raw body before parsing it.
    if not verify_webhook(WEBHOOK_SECRET, request.headers.get("HealthSwap-Signature"), raw_body):
        return 401

Every operation of api/openapi.yaml is a method named after its operationId in snake_case (dicts in, dicts out), plus
`report_events`, `iter_statements`, `iter_statement_lines`, `iter_recommendation_charges`,
`iter_statement_recommendation_charges` and the low-level `request`.
"""

from healthswap_client import events
from healthswap_client.client import VERSION, HealthSwapClient
from healthswap_client.errors import HealthSwapError
from healthswap_client.resilience import CircuitBreaker
from healthswap_client.webhooks import SIGNATURE_HEADER, sign_webhook, verify_webhook

__version__ = VERSION
__all__ = ["SIGNATURE_HEADER", "CircuitBreaker", "HealthSwapClient", "HealthSwapError", "events", "sign_webhook", "verify_webhook"]
