"""Builders for SuggestionEvent dicts (api/openapi.yaml components/schemas/SuggestionEvent).

Send them with `HealthSwapClient.report_events`. Each builder creates a fresh eventId (the server deduplicates on it, so
re-sending the same dict is safe) and stamps occurredAt with the current UTC time unless you pass one.

* `impression` - the suggestion was at least 50% visible for at least 1 second (count it once).
* `accept` - the shopper accepted the swap and the alternative was added to the cart (one RA-1 add).
* `add_to_cart` - any other add of a HealthSwap-recommended alternative (re-adding it, "+" on the cart line, adding it
  from a product page): one RA-1 add per event.
* `undo` - the shopper reverted the swap or removed the added product; voids the latest add of the suggestion within the
  undo grace period.
* `dismiss` - the shopper kept the original ("Keep original").
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

__all__ = ["accept", "add_to_cart", "dismiss", "event", "impression", "timestamp", "undo"]

EVENT_TYPES = ("IMPRESSION", "ACCEPT", "ADD_TO_CART", "DISMISS", "UNDO", "DETAILS_VIEWED")


def timestamp(value: datetime | None = None) -> str:
    """RFC 3339 UTC timestamp with milliseconds ("2026-11-04T10:15:30.123Z"); naive datetimes are rejected."""
    moment = datetime.now(UTC) if value is None else value
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware (use datetime.now(UTC))")
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event(event_type: str, suggestion_id: str | uuid.UUID, *, alternative_partner_sku: str | None = None, quantity: float | Decimal | None = None,
          occurred_at: datetime | str | None = None, event_id: str | uuid.UUID | None = None, metadata: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A SuggestionEvent dict of any type."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of {', '.join(EVENT_TYPES)}")
    if quantity is not None and (isinstance(quantity, bool) or not quantity > 0):
        raise ValueError("quantity must be a positive number")
    if metadata is not None and (len(metadata) > 10 or not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())):
        raise ValueError("metadata holds at most 10 string values")
    result: dict[str, Any] = {
        "eventId": str(event_id or uuid.uuid4()),
        "type": event_type,
        "suggestionId": str(suggestion_id),
        "occurredAt": occurred_at if isinstance(occurred_at, str) else timestamp(occurred_at),
    }
    if alternative_partner_sku is not None:
        result["alternativePartnerSku"] = alternative_partner_sku
    if quantity is not None:
        result["quantity"] = quantity
    if metadata:
        result["metadata"] = dict(metadata)
    return result


def impression(suggestion_id: str | uuid.UUID, *, occurred_at: datetime | str | None = None, event_id: str | uuid.UUID | None = None,
               metadata: Mapping[str, str] | None = None) -> dict[str, Any]:
    """IMPRESSION: the suggestion was at least 50% visible for at least 1 second."""
    return event("IMPRESSION", suggestion_id, occurred_at=occurred_at, event_id=event_id, metadata=metadata)


def accept(suggestion_id: str | uuid.UUID, alternative_partner_sku: str, *, quantity: float | Decimal | None = None,
           occurred_at: datetime | str | None = None, event_id: str | uuid.UUID | None = None, metadata: Mapping[str, str] | None = None) -> dict[str, Any]:
    """ACCEPT: the swap was accepted and the alternative added to the cart (one RA-1 add; required for FP-1 attribution)."""
    return event("ACCEPT", suggestion_id, alternative_partner_sku=alternative_partner_sku, quantity=quantity, occurred_at=occurred_at,
                 event_id=event_id, metadata=metadata)


def add_to_cart(suggestion_id: str | uuid.UUID, alternative_partner_sku: str, *, quantity: float | Decimal | None = None,
                occurred_at: datetime | str | None = None, event_id: str | uuid.UUID | None = None, metadata: Mapping[str, str] | None = None) -> dict[str, Any]:
    """ADD_TO_CART: any other add of a HealthSwap-recommended alternative (one RA-1 add per event)."""
    return event("ADD_TO_CART", suggestion_id, alternative_partner_sku=alternative_partner_sku, quantity=quantity, occurred_at=occurred_at,
                 event_id=event_id, metadata=metadata)


def undo(suggestion_id: str | uuid.UUID, alternative_partner_sku: str | None = None, *, occurred_at: datetime | str | None = None,
         event_id: str | uuid.UUID | None = None, metadata: Mapping[str, str] | None = None) -> dict[str, Any]:
    """UNDO: the swap was reverted or the added product removed (voids the latest add within the undo grace period)."""
    return event("UNDO", suggestion_id, alternative_partner_sku=alternative_partner_sku, occurred_at=occurred_at, event_id=event_id,
                 metadata=metadata)


def dismiss(suggestion_id: str | uuid.UUID, *, occurred_at: datetime | str | None = None, event_id: str | uuid.UUID | None = None,
            metadata: Mapping[str, str] | None = None) -> dict[str, Any]:
    """DISMISS: the shopper kept the original (the alternatives are not suggested again for the cool-down period)."""
    return event("DISMISS", suggestion_id, occurred_at=occurred_at, event_id=event_id, metadata=metadata)
