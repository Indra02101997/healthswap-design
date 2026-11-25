"""Services/EventServices.cs ported: suggestion snapshots (SuggestionStore), the suggestion event collector (EventService), the
RA-1 charge lifecycle (RecoChargeService), opaque keyset/offset cursors (Cursor) and the background FinalizerWorker.

Requests and results are camelCase dicts shaped like api/openapi.yaml (EventBatchRequest -> EventBatchResult, RecommendationCharge,
RecommendationChargePage): ids are strings, timestamps ISO 8601 strings, amounts Money dicts, None members omitted.

Port notes:
    * SQL, ordering, rejection codes/messages, dedupe on txn.event_key, the RA-1 evidence chain and every outbox payload are
      those of the C#. Charge acks are returned to server-to-server callers only (ctx.is_server), as in the C# service.
    * FinalizerWorker takes the FP-1 attribution engine (services/orders.py AttributionEngine) as an optional argument: when
      it is None only RA-1 charges are finalized. The attribution interval uses a monotonic clock (C# DateTime.UtcNow).
    * Cursor.decode returns 400 "Invalid cursor" for every malformed cursor, including tick values outside the Int64 range (the
      C# OverflowException escaped as a 500).
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from healthswap.domain.commerce.fees import RecoAddFeeRule, RecoChargeEvidence, RecoChargeReasons, RecoFeeSnapshot
from healthswap.domain.commerce.ledger import Postings
from healthswap.domain.knowledge.products import decimal_max
from healthswap.domain.primitives import UTC, Clock, as_utc, iso, new_id, parse_iso, period_try_parse, to_decimal
from healthswap.platform import netjson
from healthswap.platform.api_mapper import ApiMapper
from healthswap.platform.bus import Outbox, Topics
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.errors import HsError
from healthswap.platform.hot_cache import AlternativeSnapshot, HotCache, SuggestionSnapshot
from healthswap.platform.knowledge.reference_data import ReferenceDataRepository
from healthswap.platform.services.infrastructure import LedgerService
from healthswap.platform.settings import SchedulerOptions
from healthswap.platform.tenancy import PartnerContext, PartnerDirectory
from healthswap.platform.workers import BackgroundWorker

__all__ = [
    "Cursor", "EventService", "FinalizerWorker", "RecoChargeService", "SchedulerOptions", "SuggestionEvent", "SuggestionStore",
]

log = logging.getLogger(__name__)

_ONE = Decimal(1)
_MIN_QUANTITY = Decimal("0.001")
_CHARGEABLE_TYPES = ("ACCEPT", "ADD_TO_CART")


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drops None members (JsonIgnoreCondition.WhenWritingNull)."""
    return {k: v for k, v in d.items() if v is not None}


def _uuid(value: uuid.UUID | str | None, what: str) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except ValueError:
        raise HsError.invalid(f"{what} must be a UUID") from None


# ---------------------------------------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SuggestionEvent:
    """Contracts.SuggestionEvent."""

    event_id: uuid.UUID
    type: str
    suggestion_id: uuid.UUID
    occurred_at: datetime
    alternative_partner_sku: str | None = None
    quantity: Decimal | None = None
    metadata: Mapping[str, str] | None = None

    @staticmethod
    def from_api(e: Any, what: str = "event") -> SuggestionEvent:
        if not isinstance(e, Mapping):
            raise HsError.invalid(f"{what} must be an object")

        def required(key: str) -> Any:
            value = e.get(key)
            if value is None:
                raise HsError.invalid(f"{what}.{key} is required")
            return value

        event_type = required("type")
        sku = e.get("alternativePartnerSku")
        quantity = e.get("quantity")
        metadata = e.get("metadata")
        occurred = required("occurredAt")
        if not isinstance(event_type, str) or (sku is not None and not isinstance(sku, str)):
            raise HsError.invalid(f"{what}.type and alternativePartnerSku must be strings")
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, (int, float, Decimal))):
            raise HsError.invalid(f"{what}.quantity must be a number")
        if metadata is not None and (not isinstance(metadata, Mapping) or not all(isinstance(v, str) for v in metadata.values())):
            raise HsError.invalid(f"{what}.metadata must be an object of strings")
        if isinstance(occurred, datetime):
            occurred_at = as_utc(occurred)
        else:
            try:
                occurred_at = parse_iso(str(occurred))
            except ValueError:
                raise HsError.invalid(f"{what}.occurredAt must be an RFC 3339 timestamp") from None
        return SuggestionEvent(
            event_id=_uuid(required("eventId"), f"{what}.eventId"), type=event_type, suggestion_id=_uuid(required("suggestionId"), f"{what}.suggestionId"),
            occurred_at=occurred_at, alternative_partner_sku=sku, quantity=None if quantity is None else to_decimal(quantity), metadata=metadata,
        )

    @staticmethod
    def batch_from_api(req: Any) -> list[SuggestionEvent]:
        """EventBatchRequest {"events": [...]}."""
        events = req.get("events") if isinstance(req, Mapping) else None
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise HsError.invalid("request.events is required")
        return [SuggestionEvent.from_api(e, f"events[{i}]") for i, e in enumerate(events)]


# ---------------------------------------------------------------------------------------------------------------------
# SuggestionStore
# ---------------------------------------------------------------------------------------------------------------------


class SuggestionStore:
    """Suggestion snapshots: hot cache first (written at serve time), PostgreSQL second (txn.suggestion, written asynchronously)."""

    SELECT_SQL = """
        select suggestion_id, created_at, evaluation_id, partner_id, cart_ref, user_ref, market, store_id, placement, line_id, from_partner_sku, from_product_id,
               from_version_id, from_qty, from_unit_price, from_net_quantity, from_net_unit, from_tax_rate, currency, alternatives::text as alternatives,
               reco_fee::text as reco_fee, rules_version, graph_version, expires_at, attribution_expires_at
        from txn.suggestion
        """

    def __init__(self, db: Db, cache: HotCache) -> None:
        self.db = db
        self.cache = cache

    def get(self, partner_id: uuid.UUID, suggestion_id: uuid.UUID) -> SuggestionSnapshot | None:
        cached = self.cache.get_suggestion(suggestion_id)
        if cached is not None:
            return cached if cached.partner_id == partner_id else None
        row = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.one(self.SELECT_SQL + " where suggestion_id = @suggestionId", {"suggestionId": suggestion_id}))
        return None if row is None else self.map(row)

    @staticmethod
    def load_in_tx(u: Uow, suggestion_id: uuid.UUID) -> SuggestionSnapshot | None:
        row = u.one(SuggestionStore.SELECT_SQL + " where suggestion_id = @suggestionId", {"suggestionId": suggestion_id})
        return None if row is None else SuggestionStore.map(row)

    @staticmethod
    def map(r: Mapping[str, Any]) -> SuggestionSnapshot:
        alternatives = HotCache.from_json(list[AlternativeSnapshot], r["alternatives"]) or []
        return SuggestionSnapshot(
            suggestion_id=r["suggestion_id"], created_at=r["created_at"], evaluation_id=r["evaluation_id"], partner_id=r["partner_id"],
            cart_ref=r["cart_ref"], user_ref=r["user_ref"], market=r["market"], store_id=r["store_id"], placement=r["placement"], line_id=r["line_id"],
            from_partner_sku=r["from_partner_sku"], from_product_id=r["from_product_id"], from_version_id=int(r["from_version_id"]), from_qty=r["from_qty"],
            from_unit_price=r["from_unit_price"], from_net_quantity=r["from_net_quantity"], from_net_unit=r["from_net_unit"], from_tax_rate=r["from_tax_rate"],
            currency=r["currency"].strip(), alternatives=tuple(alternatives),
            reco_fee=None if r["reco_fee"] is None else HotCache.from_json(RecoFeeSnapshot, r["reco_fee"]), rules_version=r["rules_version"],
            graph_version=r["graph_version"], expires_at=r["expires_at"], attribution_expires_at=r["attribution_expires_at"],
        )


# ---------------------------------------------------------------------------------------------------------------------
# EventService
# ---------------------------------------------------------------------------------------------------------------------


class EventService:
    """
    Suggestion event collector (HLD 7.2): dedupe on eventId, evidence for attribution, RA-1 charges for ACCEPT / ADD_TO_CART of a
    HealthSwap-recommended alternative, UNDO voids within the grace period, DISMISS feeds the R8 fatigue store, line labels.
    """

    def __init__(self, db: Db, cache: HotCache, suggestions: SuggestionStore, charges: RecoChargeService, partners: PartnerDirectory,
                 refs: ReferenceDataRepository, clock: Clock) -> None:
        self.db = db
        self.cache = cache
        self.suggestions = suggestions
        self.charges = charges
        self.partners = partners
        self.refs = refs
        self.clock = clock

    @staticmethod
    def ordered(events: Sequence[SuggestionEvent]) -> list[SuggestionEvent]:
        """Processing order: by occurredAt, then by position in the batch."""
        return sorted(events, key=lambda e: e.occurred_at)

    @staticmethod
    def validate(ctx: PartnerContext, e: SuggestionEvent, s: SuggestionSnapshot | None, now: datetime) -> tuple[str, str] | None:
        """(code, message) when the event must be rejected, otherwise None."""
        if s is None:
            return "HS-EVENT-UNKNOWN-SUGGESTION", "Unknown or expired suggestionId"
        if ctx.is_sdk_session and s.cart_ref != ctx.sdk_cart_ref:
            return "HS-AUTH-SESSION-SCOPE", "The suggestion belongs to another cart session"
        if e.occurred_at > now + timedelta(minutes=5):
            return "HS-EVENT-TIME", "occurredAt is in the future"
        if e.type in _CHARGEABLE_TYPES and (e.alternative_partner_sku is None or all(a.partner_sku != e.alternative_partner_sku for a in s.alternatives)):
            return "HS-EVENT-SKU-NOT-RECOMMENDED", "alternativePartnerSku must be one of the suggestion's recommended alternatives"
        return None

    def ingest(self, ctx: PartnerContext, req: Mapping[str, Any] | Sequence[SuggestionEvent]) -> dict[str, Any]:
        """EventBatchRequest dict (or parsed events) -> EventBatchResult dict."""
        events = SuggestionEvent.batch_from_api(req) if isinstance(req, Mapping) else list(req)
        now = self.clock.utc_now()
        partner = self.partners.get(ctx.partner_id)
        snapshots: dict[uuid.UUID, SuggestionSnapshot | None] = {}
        for sid in dict.fromkeys(e.suggestion_id for e in events):
            snapshots[sid] = self.suggestions.get(ctx.partner_id, sid)

        rejected: list[dict[str, Any]] = []
        acks: list[dict[str, Any]] = []
        label_adds: list[tuple[str, str, uuid.UUID, datetime]] = []
        dismissals: list[tuple[str, uuid.UUID, uuid.UUID]] = []
        counts = {"accepted": 0, "duplicates": 0}
        cooldown_days = 7
        if len(partner.markets) > 0:
            cooldown_days = self.refs.get_pack(partner.markets[0]).rules.dismiss_cooldown_days

        def work(u: Uow) -> None:
            for e in self.ordered(events):
                s = snapshots[e.suggestion_id]
                problem = self.validate(ctx, e, s, now)
                if problem is not None:
                    rejected.append({"eventId": str(e.event_id), "code": problem[0], "message": problem[1]})
                    continue
                assert s is not None
                inserted = u.execute(
                    "insert into txn.event_key (partner_id, event_id, occurred_at) values (@p, @id, @at) on conflict do nothing",
                    {"p": ctx.partner_id, "id": e.event_id, "at": e.occurred_at},
                )
                if inserted == 0:
                    counts["duplicates"] += 1
                    if ctx.is_server:
                        existing = self.charges.ack_for_event(u, e.event_id)
                        if existing is not None:
                            acks.append(existing)
                    continue
                u.execute(
                    """
                    insert into txn.suggestion_event (event_id, occurred_at, received_at, partner_id, suggestion_id, event_type, alternative_partner_sku, quantity, metadata)
                    values (@id, @at, @now, @p, @sid, @type, @sku, @qty, @meta::jsonb)
                    """,
                    {
                        "id": e.event_id, "at": e.occurred_at, "now": now, "p": ctx.partner_id, "sid": e.suggestion_id, "type": e.type,
                        "sku": e.alternative_partner_sku, "qty": e.quantity, "meta": netjson.to_json_string(dict(e.metadata) if e.metadata is not None else {}),
                    },
                )
                counts["accepted"] += 1

                if e.type in _CHARGEABLE_TYPES:
                    label_adds.append((s.cart_ref, e.alternative_partner_sku or "", s.suggestion_id, s.attribution_expires_at))
                    if s.reco_fee is not None and RecoAddFeeRule.is_chargeable_event(e.type, s.reco_fee):
                        ack = self.charges.create_pending(u, s, e, now)
                        if ctx.is_server:
                            acks.append(ack)
                elif e.type == "UNDO":
                    # Labels stay on the cart line: earlier adds may still be in the cart, and lineLabels only lists lines present in the cart.
                    self.charges.void_on_undo(u, s, e, now)
                elif e.type == "DISMISS":
                    key = s.user_ref if s.user_ref is not None else "cart:" + s.cart_ref
                    for a in s.alternatives:
                        dismissals.append((key, s.from_product_id, a.product_id))
                rank = next((a.rank for a in s.alternatives if a.partner_sku == e.alternative_partner_sku), None)
                Outbox.enqueue(u, ctx.partner_id, Topics.SUGGESTION_EVENTS, str(e.suggestion_id), {
                    "eventId": e.event_id, "type": e.type, "suggestionId": e.suggestion_id, "alternativePartnerSku": e.alternative_partner_sku,
                    "quantity": e.quantity, "occurredAt": e.occurred_at, "receivedAt": now, "placement": s.placement, "market": s.market,
                    "fromProductId": s.from_product_id, "rank": rank,
                })

        self.db.in_tx(DbRole.APP, ctx.partner_id, work)

        for cart_ref, sku, sid, until in label_adds:
            self.cache.add_cart_reco_label(ctx.partner_id, cart_ref, sku, sid, until - now if until - now > timedelta(0) else timedelta(hours=1))
        for key, from_, to in dismissals:
            self.cache.set_dismissed(ctx.partner_id, key, from_, to, timedelta(days=cooldown_days))

        return _compact({
            "accepted": counts["accepted"], "duplicates": counts["duplicates"], "rejected": rejected if rejected else None, "charges": acks if acks else None,
        })


# ---------------------------------------------------------------------------------------------------------------------
# RecoChargeService
# ---------------------------------------------------------------------------------------------------------------------


class RecoChargeService:
    """RA-1 charge lifecycle: PENDING at the add, VOID on a timely UNDO, FINAL (ledger RECO_FEE) or REJECTED by the finalizer."""

    COLUMNS = """
        charge_id, partner_id, event_id, event_type, suggestion_id, cart_ref, placement, alternative_partner_sku, quantity, basis, unit_amount, amount, currency,
        rule_version, state, rejection_reason, occurred_at, received_at, finalizes_at, finalized_at, statement_id, credited_amount
        """

    def __init__(self, db: Db, ledger: LedgerService, clock: Clock) -> None:
        self.db = db
        self.ledger = ledger
        self.clock = clock

    def create_pending(self, u: Uow, s: SuggestionSnapshot, e: SuggestionEvent, received_at: datetime) -> dict[str, Any]:
        """Inserts the PENDING charge of an ACCEPT / ADD_TO_CART event and returns its RecommendationChargeAck."""
        fee = s.reco_fee
        if fee is None:
            raise ValueError("The suggestion has no RA-1 fee snapshot")
        amount = RecoAddFeeRule.amount(fee, e.quantity)
        finalizes_at = RecoAddFeeRule.finalizes_at(e.occurred_at, received_at, fee)
        charge_id = new_id()
        alt = next((a for a in s.alternatives if a.partner_sku == e.alternative_partner_sku), None)
        if alt is None:
            raise ValueError("Sequence contains no matching element")
        u.execute(
            """
            insert into txn.reco_charge (charge_id, partner_id, event_id, event_type, suggestion_id, cart_ref, user_ref, market, placement, alternative_partner_sku,
                alternative_product_id, quantity, basis, unit_amount, amount, currency, rule_version, state, occurred_at, received_at, finalizes_at)
            values (@chargeId, @partnerId, @eventId, @type, @sid, @cartRef, @userRef, @market, @placement, @sku, @product, @qty, @basis, @unit, @amount, @currency,
                @rule, 'PENDING', @occurred, @received, @finalizesAt)
            on conflict (partner_id, event_id) do nothing
            """,
            {
                "chargeId": charge_id, "partnerId": s.partner_id, "eventId": e.event_id, "type": e.type, "sid": s.suggestion_id, "cartRef": s.cart_ref,
                "userRef": s.user_ref, "market": s.market, "placement": s.placement, "sku": alt.partner_sku, "product": alt.product_id,
                "qty": decimal_max(_MIN_QUANTITY, e.quantity if e.quantity is not None else _ONE), "basis": fee.basis, "unit": fee.amount, "amount": amount,
                "currency": fee.currency, "rule": fee.rule, "occurred": e.occurred_at, "received": received_at, "finalizesAt": finalizes_at,
            },
        )
        Outbox.enqueue(u, s.partner_id, Topics.RECO_CHARGES, str(charge_id), {
            "chargeId": charge_id, "state": "PENDING", "amount": amount, "currency": fee.currency, "suggestionId": s.suggestion_id, "eventType": e.type,
        })
        return self._ack(e.event_id, charge_id, "PENDING", amount, fee.currency, finalizes_at)

    def ack_for_event(self, u: Uow, event_id: uuid.UUID) -> dict[str, Any] | None:
        """The RecommendationChargeAck of an earlier event (duplicate replay), or None when the event created no charge."""
        r = u.one(f"select {self.COLUMNS} from txn.reco_charge where event_id = @eventId", {"eventId": event_id})
        if r is None:
            return None
        return self._ack(r["event_id"], r["charge_id"], r["state"], r["amount"], r["currency"].strip(), r["finalizes_at"] if r["state"] == "PENDING" else None)

    def void_on_undo(self, u: Uow, s: SuggestionSnapshot, undo: SuggestionEvent, now: datetime) -> str | None:
        """Voids the latest PENDING add of the suggestion when the UNDO is within the grace period. Returns the affected SKU."""
        fee = s.reco_fee
        if fee is None:
            return None
        latest = u.one(
            f"select {self.COLUMNS} from txn.reco_charge where suggestion_id = @sid and state = 'PENDING' and (@sku::text is null or alternative_partner_sku = @sku) "
            "and occurred_at <= @at order by occurred_at desc limit 1",
            {"sid": s.suggestion_id, "sku": undo.alternative_partner_sku, "at": undo.occurred_at},
        )
        if latest is None:
            return None
        if not RecoAddFeeRule.undo_voids(latest["occurred_at"], undo.occurred_at, fee):
            return latest["alternative_partner_sku"]
        u.execute(
            "update txn.reco_charge set state = 'VOID', rejection_reason = @reason, finalized_at = @now, user_ref = null where charge_id = @id and state = 'PENDING'",
            {"reason": RecoChargeReasons.UNDONE_WITHIN_GRACE, "now": now, "id": latest["charge_id"]},
        )
        Outbox.enqueue(u, s.partner_id, Topics.RECO_CHARGES, str(latest["charge_id"]), {
            "chargeId": latest["charge_id"], "state": "VOID", "reason": RecoChargeReasons.UNDONE_WITHIN_GRACE,
        })
        return latest["alternative_partner_sku"]

    def finalize_due(self) -> tuple[int, int]:
        """Finalizer: applies the RA-1 evidence chain to due PENDING charges and posts RECO_FEE for FINAL ones. Returns (final, rejected)."""
        now = self.clock.utc_now()
        partner_ids = [
            r["partner_id"] for r in self.db.in_tx(DbRole.OPS, None, lambda u: u.query(
                "select distinct partner_id from txn.reco_charge where state = 'PENDING' and finalizes_at <= @now", {"now": now}))
        ]
        final = rejected = 0
        for partner_id in partner_ids:
            f, r = self.db.in_tx(DbRole.LEDGER_WRITER, partner_id, lambda u, pid=partner_id: self._finalize_partner(u, pid, now))
            final += f
            rejected += r
        if final + rejected > 0:
            log.info("RA-1 finalizer: %d FINAL, %d REJECTED", final, rejected)
        return final, rejected

    def _finalize_partner(self, u: Uow, partner_id: uuid.UUID, now: datetime) -> tuple[int, int]:
        fc = rc = 0
        due = u.query(
            f"select {self.COLUMNS} from txn.reco_charge where state = 'PENDING' and finalizes_at <= @now order by occurred_at limit 500 for update skip locked",
            {"now": now},
        )
        for c in due:
            s = SuggestionStore.load_in_tx(u, c["suggestion_id"])
            if s is None and now - c["received_at"] < timedelta(hours=1):
                continue  # the asynchronous suggestion write may still be in flight
            fee = s.reco_fee if s is not None and s.reco_fee is not None else RecoFeeSnapshot(
                currency=c["currency"].strip(), amount=c["unit_amount"], basis=c["basis"],
            )
            first_impression = u.scalar(
                "select min(occurred_at) from txn.suggestion_event where suggestion_id = @sid and event_type = 'IMPRESSION'", {"sid": c["suggestion_id"]},
            )
            final_for_suggestion = u.scalar(
                "select count(*)::int from txn.reco_charge where suggestion_id = @sid and state in ('FINAL', 'CREDITED')", {"sid": c["suggestion_id"]},
            )
            occurred = as_utc(c["occurred_at"])
            month_start = datetime(occurred.year, occurred.month, 1, tzinfo=UTC)
            month_end = datetime(occurred.year + (occurred.month == 12), occurred.month % 12 + 1, 1, tzinfo=UTC)
            month_to_date = u.scalar(
                "select coalesce(sum(amount), 0) from txn.reco_charge where state in ('FINAL', 'CREDITED') and occurred_at >= @from and occurred_at < @to",
                {"from": month_start, "to": month_end},
            )
            evidence = RecoChargeEvidence(
                suggestion_served=s is not None,
                sku_recommended=s is not None and any(a.partner_sku == c["alternative_partner_sku"] for a in s.alternatives),
                first_impression_at=first_impression,
                suggestion_created_at=s.created_at if s is not None else c["occurred_at"],
                attribution_expires_at=s.attribution_expires_at if s is not None else c["occurred_at"],
                final_charges_for_suggestion=int(final_for_suggestion or 0), month_to_date_amount=to_decimal(month_to_date or 0),
            )
            state, reason = RecoAddFeeRule.decide(c["occurred_at"], c["amount"], fee, evidence)
            u.execute(
                "update txn.reco_charge set state = @state, rejection_reason = @reason, finalized_at = @now, user_ref = null where charge_id = @id and state = 'PENDING'",
                {"state": state, "reason": reason, "now": now, "id": c["charge_id"]},
            )
            if state == "FINAL":
                self.ledger.post(u, partner_id, c["currency"].strip(), Postings.reco_fee(c["charge_id"], c["amount"]), "reco-charge-finalizer", now)
                fc += 1
            else:
                rc += 1
            Outbox.enqueue(u, partner_id, Topics.RECO_CHARGES, str(c["charge_id"]), {
                "chargeId": c["charge_id"], "state": state, "reason": reason, "amount": c["amount"],
            })
        return fc, rc

    def list(self, partner_id: uuid.UUID, period: str | None = None, state: str | None = None, cart_ref: str | None = None,
             suggestion_id: uuid.UUID | str | None = None, statement_id: uuid.UUID | str | None = None, cursor: str | None = None,
             limit: int = 100) -> dict[str, Any]:
        """RecommendationChargePage {"items": [...], "nextCursor": ...}: newest first, keyset-paginated."""
        start = end = None
        if period is not None:
            parsed = period_try_parse(period)
            if parsed is None:
                raise HsError.invalid("period must be YYYY-MM")
            start, end = parsed
        sid = _uuid(suggestion_id, "suggestionId")
        statement = _uuid(statement_id, "statementId")
        c_at, c_id = Cursor.decode(cursor)
        limit = min(max(limit, 1), 200)

        def work(u: Uow) -> list[dict[str, Any]]:
            if statement is not None and not u.scalar(
                "select exists (select 1 from billing.statement where statement_id = @statementId)", {"statementId": statement},
            ):
                raise HsError.not_found(f"Statement {statement}")
            return u.query(
                f"""
                select {self.COLUMNS} from txn.reco_charge
                where (@from::timestamptz is null or occurred_at >= @from) and (@to::timestamptz is null or occurred_at < @to)
                  and (@state::text is null or state = @state) and (@cartRef::text is null or cart_ref = @cartRef)
                  and (@sid::uuid is null or suggestion_id = @sid) and (@statementId::uuid is null or statement_id = @statementId)
                  and (@cAt::timestamptz is null or (occurred_at, charge_id) < (@cAt::timestamptz, @cId::uuid))
                order by occurred_at desc, charge_id desc limit @take
                """,
                {
                    "from": start, "to": end, "state": state, "cartRef": cart_ref, "sid": sid, "statementId": statement, "cAt": c_at, "cId": c_id,
                    "take": limit + 1,
                },
            )

        rows = self.db.in_tx(DbRole.APP, partner_id, work)
        next_cursor = Cursor.encode(rows[limit - 1]["occurred_at"], rows[limit - 1]["charge_id"]) if len(rows) > limit else None
        return _compact({"items": [self.to_api(r) for r in rows[:limit]], "nextCursor": next_cursor})

    def get(self, partner_id: uuid.UUID, charge_id: uuid.UUID) -> dict[str, Any]:
        row = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.one(f"select {self.COLUMNS} from txn.reco_charge where charge_id = @chargeId",
                                                                     {"chargeId": charge_id}))
        if row is None:
            raise HsError.not_found(f"Recommendation charge {charge_id}")
        return self.to_api(row)

    @staticmethod
    def to_api(r: Mapping[str, Any]) -> dict[str, Any]:
        """txn.reco_charge row -> RecommendationCharge."""
        currency = r["currency"].strip()
        return _compact({
            "chargeId": str(r["charge_id"]), "eventId": str(r["event_id"]), "eventType": r["event_type"], "suggestionId": str(r["suggestion_id"]),
            "cartRef": r["cart_ref"], "placement": r["placement"], "alternativePartnerSku": r["alternative_partner_sku"], "quantity": r["quantity"],
            "basis": r["basis"], "amount": ApiMapper.money(r["amount"], currency), "state": r["state"], "rejectionReason": r["rejection_reason"],
            "ruleVersion": r["rule_version"], "occurredAt": iso(r["occurred_at"]), "finalizesAt": iso(r["finalizes_at"]) if r["state"] == "PENDING" else None,
            "finalizedAt": iso(r["finalized_at"]), "statementId": None if r["statement_id"] is None else str(r["statement_id"]),
            "creditedAmount": ApiMapper.money(r["credited_amount"], currency) if r["credited_amount"] > 0 else None,
        })

    @staticmethod
    def _ack(event_id: uuid.UUID, charge_id: uuid.UUID, state: str, amount: Decimal, currency: str, finalizes_at: datetime | None) -> dict[str, Any]:
        """RecommendationChargeAck."""
        return _compact({
            "eventId": str(event_id), "chargeId": str(charge_id), "state": state, "amount": ApiMapper.money(amount, currency), "finalizesAt": iso(finalizes_at),
        })


# ---------------------------------------------------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------------------------------------------------

_TICKS_EPOCH = datetime(1, 1, 1, tzinfo=UTC)
_MAX_TICKS = 3_155_378_975_999_999_999  # DateTime.MaxValue.Ticks
_INTEGER_RX = re.compile(r"[ \t\n\v\f\r]*([+-]?[0-9]+)[ \t\n\v\f\r]*")  # NumberStyles.Integer


def _parse_integer(text: str, bits: int) -> int:
    """long.Parse / int.Parse(text, CultureInfo.InvariantCulture); ValueError when malformed or out of range."""
    m = _INTEGER_RX.fullmatch(text)
    if m is None:
        raise ValueError(text)
    value = int(m.group(1))
    if not -(1 << (bits - 1)) <= value < 1 << (bits - 1):
        raise ValueError(text)
    return value


def _b64decode(text: str) -> str:
    """Convert.FromBase64String + Encoding.UTF8.GetString (invalid UTF-8 becomes U+FFFD)."""
    return base64.b64decode(text, validate=True).decode("utf-8", errors="replace")


class Cursor:
    """Opaque keyset cursors: base64url("ticks|id"), ticks = 100 ns intervals since 0001-01-01 (DateTime.Ticks)."""

    @staticmethod
    def ticks(at: datetime) -> int:
        delta = as_utc(at) - _TICKS_EPOCH
        return (delta.days * 86_400 + delta.seconds) * 10_000_000 + delta.microseconds * 10

    @staticmethod
    def encode(at: datetime, id: uuid.UUID) -> str:
        raw = base64.b64encode(f"{Cursor.ticks(at)}|{id}".encode()).decode("ascii")
        return raw.rstrip("=").replace("+", "-").replace("/", "_")

    @staticmethod
    def decode(cursor: str | None) -> tuple[datetime | None, uuid.UUID | None]:
        if not cursor:
            return None, None
        try:
            b64 = cursor.replace("-", "+").replace("_", "/")
            b64 = b64 + "=" * ((4 - len(b64) % 4) % 4)
            parts = _b64decode(b64).split("|")
            ticks = _parse_integer(parts[0], 64)
            if not 0 <= ticks <= _MAX_TICKS:
                raise ValueError(parts[0])
            at = _TICKS_EPOCH + timedelta(microseconds=ticks // 10)
            return at, uuid.UUID(parts[1].strip())
        except (ValueError, IndexError, binascii.Error):
            raise HsError.invalid("Invalid cursor") from None

    @staticmethod
    def encode_offset(offset: int) -> str:
        return base64.b64encode(f"o|{offset}".encode()).decode("ascii").rstrip("=")

    @staticmethod
    def decode_offset(cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            s = _b64decode(cursor + "=" * ((4 - len(cursor) % 4) % 4))
            if not s.startswith("o|"):
                raise ValueError(s)
            return _parse_integer(s[2:], 32)
        except (ValueError, binascii.Error):
            raise HsError.invalid("Invalid cursor") from None


# ---------------------------------------------------------------------------------------------------------------------
# FinalizerWorker
# ---------------------------------------------------------------------------------------------------------------------


class AttributionFinalizer(Protocol):
    """What FinalizerWorker needs from the FP-1 AttributionEngine (services/orders.py)."""

    def finalize_due(self) -> Any: ...


class FinalizerWorker(BackgroundWorker):
    """Background schedulers: RA-1 charge finalization and FP-1 return-window finalization (skipped while `attribution` is None)."""

    def __init__(self, reco: RecoChargeService, attribution: AttributionFinalizer | None, options: SchedulerOptions) -> None:
        super().__init__()
        self.reco = reco
        self.attribution = attribution
        self.options = options

    def execute(self) -> None:
        if not self.options.enabled:
            return
        last_attribution: float | None = None
        while not self.stopping.is_set():
            try:
                self.reco.finalize_due()
                if self.attribution is not None and (
                    last_attribution is None or time.monotonic() - last_attribution > self.options.attribution_finalizer_seconds
                ):
                    self.attribution.finalize_due()
                    last_attribution = time.monotonic()
            except Exception:
                self.log.warning("Finalizer iteration failed", exc_info=True)
            if not self.sleep(self.options.reco_finalizer_seconds):
                break
