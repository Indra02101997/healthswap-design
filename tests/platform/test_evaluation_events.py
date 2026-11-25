"""Unit tests for services/evaluation.py and services/events.py (no database): cursors, budget and degradation helpers,
request parsing, policy checks, event ordering and validation, ingest with fakes, charge mapping and the recorder."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from healthswap.domain.commerce.fees import RecoFeeSnapshot
from healthswap.domain.commerce.partner_config import ExperimentConfig, PartnerConfig
from healthswap.domain.primitives import UTC, FixedClock
from healthswap.platform.errors import HsError
from healthswap.platform.hot_cache import AlternativeSnapshot, SuggestionSnapshot
from healthswap.platform.services.evaluation import (
    CartEvaluationRequest,
    CartLine,
    EvaluationRecord,
    EvaluationRecorder,
    EvaluationService,
    ModelVersions,
)
from healthswap.platform.services.events import Cursor, EventService, FinalizerWorker, RecoChargeService, SuggestionEvent
from healthswap.platform.settings import SchedulerOptions
from healthswap.platform.tenancy import PartnerContext, PartnerInfo

PID = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01")
SID = uuid.UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
ALT_PRODUCT = uuid.UUID("11111111-2222-3333-4444-555555555555")
FROM_PRODUCT = uuid.UUID("66666666-7777-8888-9999-000000000000")
NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)


def _invalid(fn: Any, *args: Any) -> HsError:
    with pytest.raises(HsError) as ei:
        fn(*args)
    assert ei.value.status == 400 and ei.value.code == "HS-REQ-INVALID"
    return ei.value


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------------------------------------------------


def test_cursor_ticks_match_datetime_ticks() -> None:
    assert Cursor.ticks(datetime(1970, 1, 1, tzinfo=UTC)) == 621_355_968_000_000_000  # DateTime.UnixEpoch.Ticks
    assert Cursor.ticks(datetime(1, 1, 1, tzinfo=UTC)) == 0
    assert Cursor.ticks(datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=UTC)) == 621_355_968_000_000_010


def test_cursor_round_trip_is_base64url_without_padding() -> None:
    at = datetime(2026, 11, 3, 10, 0, 1, 123456, tzinfo=UTC)
    cid = uuid.UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
    cursor = Cursor.encode(at, cid)
    assert "=" not in cursor and "+" not in cursor and "/" not in cursor
    padded = cursor + "=" * (-len(cursor) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == f"{Cursor.ticks(at)}|{cid}"
    assert Cursor.decode(cursor) == (at, cid)
    assert Cursor.decode(cursor)[0].tzinfo is not None
    assert Cursor.decode(None) == (None, None) and Cursor.decode("") == (None, None)


@pytest.mark.parametrize("text", [
    "abc",                                                    # no separator
    "123|not-a-guid",
    "-5|0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",                # before DateTime.MinValue
    "3155378976000000000|0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",  # after DateTime.MaxValue
    "99999999999999999999|0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",  # outside Int64
    "12x|0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
])
def test_cursor_decode_rejects_malformed_payloads(text: str) -> None:
    assert _invalid(Cursor.decode, _b64url(text)).detail == "Invalid cursor"


def test_cursor_decode_rejects_invalid_base64() -> None:
    _invalid(Cursor.decode, "!!!!")
    _invalid(Cursor.decode, "a")


def test_cursor_decode_accepts_integer_whitespace_and_sign() -> None:
    at, cid = Cursor.decode(_b64url(" +621355968000000000 |0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"))
    assert at == datetime(1970, 1, 1, tzinfo=UTC) and cid == SID


def test_offset_cursor_round_trip() -> None:
    assert Cursor.encode_offset(25) == "b3wyNQ"
    assert Cursor.decode_offset("b3wyNQ") == 25
    assert Cursor.decode_offset(Cursor.encode_offset(0)) == 0
    assert Cursor.decode_offset(None) == 0 and Cursor.decode_offset("") == 0
    for bad in ["eHl6", base64.b64encode(b"o|x").decode(), base64.b64encode(b"o|2147483648").decode(), "!!"]:
        _invalid(Cursor.decode_offset, bad)
    assert Cursor.decode_offset(base64.b64encode(b"o|-3").decode()) == -3


# ---------------------------------------------------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------------------------------------------------


def test_soft_budget_always_serves_one_line() -> None:
    assert not EvaluationService.budget_exceeded(0, 10_000, 100)
    assert not EvaluationService.budget_exceeded(1, 100, 100)
    assert EvaluationService.budget_exceeded(1, 101, 100)
    assert EvaluationService.budget_exceeded(3, 500, 100)


def test_degradation_reasons_are_ordinal_sorted_or_absent() -> None:
    assert EvaluationService.degradation_reasons(set()) is None
    assert EvaluationService.degradation_reasons({"TIMEOUT_BUDGET", "KILL_SWITCH", "INVENTORY_STALE", "NO_DATA"}) == [
        "INVENTORY_STALE", "KILL_SWITCH", "NO_DATA", "TIMEOUT_BUDGET",
    ]


def test_placement_policy_maps_to_kill_switch() -> None:
    cfg = PartnerConfig()
    assert not EvaluationService.placement_disabled(cfg, "CART")
    assert EvaluationService.placement_disabled(dataclasses.replace(cfg, kill_switch=True), "CART")
    assert EvaluationService.placement_disabled(dataclasses.replace(cfg, disabled_placements=("CART",)), "CART")
    assert EvaluationService.placement_disabled(dataclasses.replace(cfg, placements=("PDP",)), "CART")
    assert EvaluationService.placement_disabled(cfg, "EMAIL")


def test_holdout_bucket_uses_the_first_four_hash_bytes_little_endian() -> None:
    assert not EvaluationService.in_holdout(dataclasses.replace(PartnerConfig(), experiment=ExperimentConfig(name="x", holdout_percent=0)), "u1")
    always = dataclasses.replace(PartnerConfig(), experiment=ExperimentConfig(name="x", holdout_percent=100))
    assert all(EvaluationService.in_holdout(always, f"user-{i}") for i in range(50))
    cfg = dataclasses.replace(PartnerConfig(), experiment=ExperimentConfig(name="pilot-2026-11", holdout_percent=10))
    for key in ["user-1", "cart:abc", "u-\u0939\u093f"]:
        bucket = int.from_bytes(hashlib.sha256(("pilot-2026-11:" + key).encode()).digest()[:4], "little") % 100
        assert EvaluationService.in_holdout(cfg, key) is (bucket < 10)
    share = sum(EvaluationService.in_holdout(cfg, f"user-{i}") for i in range(2000)) / 2000
    assert 0.07 < share < 0.13


def test_local_hour_by_market() -> None:
    assert EvaluationService.local_hour("IN", datetime(2026, 1, 1, 20, 0, tzinfo=UTC)) == 1  # 01:30 IST
    assert EvaluationService.local_hour("US", datetime(2026, 7, 1, 12, 0, tzinfo=UTC)) == 8  # EDT
    assert EvaluationService.local_hour("US", datetime(2026, 1, 15, 12, 0, tzinfo=UTC)) == 7  # EST
    assert EvaluationService.local_hour("GB", datetime(2026, 1, 15, 12, 0, tzinfo=UTC)) == 12


def _line(**kw: Any) -> CartLine:
    m = {"lineId": "1", "partnerSku": "SKU-NDL-280", "quantity": 2, "unitPrice": {"amount": "56.00", "currency": "INR"}, **kw}
    return CartLine.from_api(m)


def test_unit_price_spreads_the_line_discount() -> None:
    assert str(EvaluationService.unit_price(_line())) == "56.00"
    assert EvaluationService.unit_price(_line(lineDiscount={"amount": "10.00", "currency": "INR"})) == Decimal("51.00")
    assert EvaluationService.unit_price(_line(quantity=Decimal("0.5"), lineDiscount={"amount": "10.00", "currency": "INR"})) == Decimal("46.00")
    assert EvaluationService.unit_price(_line(lineDiscount={"amount": "n/a", "currency": "INR"})) == Decimal("56.00")
    assert EvaluationService.unit_price(_line(unitPrice={"amount": " 56.00", "currency": "INR"})) is None
    assert EvaluationService.unit_price(_line(unitPrice={"amount": "abc", "currency": "INR"})) is None


def test_summary_weights_hsi_by_quantity_and_counts_flags_in_order() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    profiles: Any = {a: SimpleNamespace(hsi=Decimal("30.0"), flags=("HIGH_SODIUM", "HIGH_SATFAT")), b: SimpleNamespace(hsi=Decimal("61.0"), flags=("HIGH_SODIUM",))}
    lines = [_line(lineId="1", quantity=2), _line(lineId="2", quantity=1)]
    s = EvaluationService.summary(3, {"1": a, "2": b}, profiles, lines, 0)
    assert s == {
        "linesTotal": 3, "linesResolved": 2, "linesScored": 2, "linesWithSuggestions": 0, "cartHsi": Decimal("40.3"),
        "flagCounts": {"HIGH_SODIUM": 2, "HIGH_SATFAT": 1},
    }
    assert list(s["flagCounts"]) == ["HIGH_SODIUM", "HIGH_SATFAT"]
    assert EvaluationService.summary(1, {}, {}, [], 0) == {"linesTotal": 1, "linesResolved": 0, "linesScored": 0, "linesWithSuggestions": 0}


def test_cart_request_parsing() -> None:
    r = CartEvaluationRequest.from_api({
        "cartRef": "c1", "market": "IN", "currency": "INR", "userRef": "u1",
        "context": {"placement": "CHECKOUT", "maxSuggestions": 2, "isMinor": False},
        "lines": [{"lineId": "1", "partnerSku": "S", "quantity": 1.5, "unitPrice": {"amount": "1.00", "currency": "INR"}, "isSubstitutable": False}],
    })
    assert (r.placement, r.max_suggestions, r.is_minor, r.lines[0].quantity, r.lines[0].is_substitutable) == ("CHECKOUT", 2, False, Decimal("1.5"), False)
    assert r.experiment_arm is None and r.preferences is None
    _invalid(CartEvaluationRequest.from_api, {"cartRef": "c1", "market": "IN", "currency": "INR"})
    _invalid(CartEvaluationRequest.from_api, {"cartRef": "c1", "market": "IN", "currency": "INR", "lines": [{"lineId": "1", "partnerSku": "S", "quantity": 1}]})
    _invalid(CartEvaluationRequest.from_api, {
        "cartRef": "c1", "market": "IN", "currency": "INR", "lines": [{"lineId": "1", "partnerSku": "S", "quantity": "2", "unitPrice": {"amount": "1", "currency": "INR"}}],
    })
    _invalid(CartEvaluationRequest.from_api, {"market": "IN", "currency": "INR", "lines": []})


def test_model_versions_api_shape() -> None:
    assert ModelVersions("NUTRI@1+HSI@2", "R-1", "g1").to_api() == {"scoring": "NUTRI@1+HSI@2", "rules": "R-1", "swapGraph": "g1"}
    assert ModelVersions("s", "r", "none", "heuristic-L2").to_api()["ranker"] == "heuristic-L2"


def _partner(**kw: Any) -> PartnerInfo:
    base: dict[str, Any] = {
        "partner_id": PID, "display_name": "QuickBasket", "geography": "IN", "markets": ("IN",), "status": "ACTIVE", "config": PartnerConfig(), "config_json": "{}",
    }
    base.update(kw)
    return PartnerInfo(**base)


def _ctx(sdk: bool = False, cart: str | None = None, **kw: Any) -> PartnerContext:
    base: dict[str, Any] = {
        "partner_id": PID, "client_id": "quickbasket-server", "scopes": frozenset({"carts.evaluate", "events.write"}), "is_sdk_session": sdk,
        "sdk_user_ref": None, "sdk_cart_ref": cart, "sdk_store_id": None, "sdk_market": None,
    }
    base.update(kw)
    return PartnerContext(**base)


def test_authorize_checks_market_status_and_sdk_bindings() -> None:
    EvaluationService.authorize(_ctx(), _partner(), "IN", "c1", "u1", "S1")
    with pytest.raises(HsError) as ei:
        EvaluationService.authorize(_ctx(), _partner(), "US", "c1", None, None)
    assert (ei.value.status, ei.value.code) == (422, "HS-UNSUPPORTED-MARKET")
    with pytest.raises(HsError) as ei:
        EvaluationService.authorize(_ctx(), _partner(status="SUSPENDED"), "IN", "c1", None, None)
    assert ei.value.status == 403
    sdk = _ctx(sdk=True, cart="c1", sdk_user_ref="u1", sdk_store_id="S1", sdk_market="IN")
    EvaluationService.authorize(sdk, _partner(), "IN", "c1", "u1", "S1")
    EvaluationService.authorize(sdk, _partner(), "IN", None, None, None)
    for args, message in [
        (("IN", "c2", None, None), "The SDK session is bound to another cart"),
        (("IN", "c1", "u2", None), "The SDK session is bound to another user"),
        (("IN", "c1", None, "S2"), "The SDK session is bound to another store"),
    ]:
        with pytest.raises(HsError) as ei:
            EvaluationService.authorize(sdk, _partner(), *args)
        assert (ei.value.status, ei.value.code, ei.value.detail) == (403, "HS-AUTH-SCOPE", message)
    with pytest.raises(HsError) as ei:
        EvaluationService.authorize(_ctx(sdk=True, cart="c1", sdk_market="IN"), _partner(markets=("IN", "US")), "US", "c1", None, None)
    assert ei.value.detail == "The SDK session is bound to another market"


# ---------------------------------------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------------------------------------


def _snapshot(**kw: Any) -> SuggestionSnapshot:
    base: dict[str, Any] = {
        "suggestion_id": SID, "partner_id": PID, "cart_ref": "cart-1", "user_ref": None, "market": "IN", "from_partner_sku": "SKU-NDL-280",
        "from_product_id": FROM_PRODUCT, "currency": "INR", "rules_version": "R-1", "graph_version": "g1",
        "alternatives": (AlternativeSnapshot(1, "SKU-MLT-256", ALT_PRODUCT, 3, Decimal("89.00"), Decimal("256"), "g", Decimal("21.5"), False, [0.5, 1.0]),),
        "reco_fee": RecoFeeSnapshot(currency="INR", amount=Decimal("1.00")), "created_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=29), "attribution_expires_at": NOW + timedelta(hours=72),
    }
    base.update(kw)
    return SuggestionSnapshot(**base)


def _event(event_type: str, at: datetime, sku: str | None = "SKU-MLT-256", sid: uuid.UUID = SID, eid: uuid.UUID | None = None, qty: Any = None) -> SuggestionEvent:
    return SuggestionEvent(event_id=eid or uuid.uuid4(), type=event_type, suggestion_id=sid, occurred_at=at, alternative_partner_sku=sku,
                           quantity=None if qty is None else Decimal(str(qty)))


def test_event_parsing() -> None:
    eid = uuid.uuid4()
    e = SuggestionEvent.from_api({"eventId": str(eid), "type": "ACCEPT", "suggestionId": str(SID), "alternativePartnerSku": "S", "quantity": 2,
                                  "occurredAt": "2026-11-03T10:00:00.5+05:30", "metadata": {"screen": "cart"}})
    assert (e.event_id, e.suggestion_id, e.quantity, e.metadata) == (eid, SID, Decimal(2), {"screen": "cart"})
    assert e.occurred_at == datetime(2026, 11, 3, 4, 30, 0, 500000, tzinfo=UTC)
    assert SuggestionEvent.batch_from_api({"events": []}) == []
    _invalid(SuggestionEvent.batch_from_api, {})
    _invalid(SuggestionEvent.from_api, {"eventId": "x", "type": "ACCEPT", "suggestionId": str(SID), "occurredAt": "2026-11-03T10:00:00Z"})
    _invalid(SuggestionEvent.from_api, {"eventId": str(eid), "type": "ACCEPT", "suggestionId": str(SID), "occurredAt": "yesterday"})
    _invalid(SuggestionEvent.from_api, {"eventId": str(eid), "type": "ACCEPT", "suggestionId": str(SID)})
    _invalid(SuggestionEvent.from_api, {"eventId": str(eid), "type": "ACCEPT", "suggestionId": str(SID), "occurredAt": "2026-11-03T10:00:00Z", "quantity": "2"})


def test_events_are_processed_by_occurrence_then_batch_position() -> None:
    a, b, c = _event("ACCEPT", NOW), _event("IMPRESSION", NOW - timedelta(seconds=5)), _event("ADD_TO_CART", NOW)
    assert EventService.ordered([a, b, c]) == [b, a, c]


def test_event_validation_order_and_messages() -> None:
    s = _snapshot()
    server = _ctx()
    assert EventService.validate(server, _event("ACCEPT", NOW), None, NOW) == ("HS-EVENT-UNKNOWN-SUGGESTION", "Unknown or expired suggestionId")
    assert EventService.validate(_ctx(sdk=True, cart="other"), _event("ACCEPT", NOW + timedelta(hours=1)), s, NOW) == (
        "HS-AUTH-SESSION-SCOPE", "The suggestion belongs to another cart session")
    assert EventService.validate(_ctx(sdk=True, cart="cart-1"), _event("ACCEPT", NOW), s, NOW) is None
    assert EventService.validate(server, _event("IMPRESSION", NOW + timedelta(minutes=5)), s, NOW) is None
    assert EventService.validate(server, _event("IMPRESSION", NOW + timedelta(minutes=5, microseconds=1)), s, NOW) == (
        "HS-EVENT-TIME", "occurredAt is in the future")
    for bad in [_event("ACCEPT", NOW, sku="SKU-CRM-500"), _event("ADD_TO_CART", NOW, sku=None)]:
        assert EventService.validate(server, bad, s, NOW) == (
            "HS-EVENT-SKU-NOT-RECOMMENDED", "alternativePartnerSku must be one of the suggestion's recommended alternatives")
    assert EventService.validate(server, _event("UNDO", NOW, sku=None), s, NOW) is None
    assert EventService.validate(server, _event("DISMISS", NOW, sku="SKU-CRM-500"), s, NOW) is None


class _FakeUow:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.event_keys: set[uuid.UUID] = set()

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        params = params or {}
        self.statements.append((" ".join(sql.split()), params))
        if "txn.event_key" in sql:
            if params["id"] in self.event_keys:
                return 0
            self.event_keys.add(params["id"])
        return 1

    def outbox(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [(p["topic"], p["key"], json.loads(p["payload"])) for sql, p in self.statements if "txn.outbox" in sql]


class _FakeDb:
    def __init__(self) -> None:
        self.uow = _FakeUow()
        self.calls: list[tuple[Any, Any]] = []

    def in_tx(self, role: Any, partner_id: Any, work: Any) -> Any:
        self.calls.append((role, partner_id))
        return work(self.uow)


class _FakeCharges:
    def __init__(self) -> None:
        self.pending: list[uuid.UUID] = []
        self.voided: list[uuid.UUID] = []

    def create_pending(self, u: Any, s: SuggestionSnapshot, e: SuggestionEvent, received_at: datetime) -> dict[str, Any]:
        self.pending.append(e.event_id)
        return {"eventId": str(e.event_id), "chargeId": f"charge-{len(self.pending)}", "state": "PENDING"}

    def ack_for_event(self, u: Any, event_id: uuid.UUID) -> dict[str, Any] | None:
        return {"eventId": str(event_id), "chargeId": "existing", "state": "PENDING"}

    def void_on_undo(self, u: Any, s: SuggestionSnapshot, e: SuggestionEvent, now: datetime) -> str | None:
        self.voided.append(e.event_id)
        return None


class _FakeCache:
    def __init__(self) -> None:
        self.labels: list[tuple[Any, ...]] = []
        self.dismissed: list[tuple[Any, ...]] = []

    def add_cart_reco_label(self, partner: uuid.UUID, cart_ref: str, sku: str, sid: uuid.UUID, ttl: timedelta) -> None:
        self.labels.append((partner, cart_ref, sku, sid, ttl))

    def set_dismissed(self, partner: uuid.UUID, key: str, from_: uuid.UUID, to: uuid.UUID, ttl: timedelta) -> None:
        self.dismissed.append((partner, key, from_, to, ttl))


def _event_service(snapshots: dict[uuid.UUID, SuggestionSnapshot]) -> tuple[EventService, _FakeDb, _FakeCharges, _FakeCache]:
    db, charges, cache = _FakeDb(), _FakeCharges(), _FakeCache()
    store = SimpleNamespace(get=lambda partner_id, sid: snapshots.get(sid))
    partners = SimpleNamespace(get=lambda partner_id: _partner())
    refs = SimpleNamespace(get_pack=lambda market: SimpleNamespace(rules=SimpleNamespace(dismiss_cooldown_days=14)))
    svc = EventService(db, cache, store, charges, partners, refs, FixedClock(NOW))  # type: ignore[arg-type]
    return svc, db, charges, cache


def test_ingest_dedupes_charges_and_orders_events() -> None:
    svc, db, charges, cache = _event_service({SID: _snapshot()})
    accept_id = uuid.uuid4()
    unknown = uuid.uuid4()
    batch = [
        _event("ACCEPT", NOW - timedelta(seconds=3), eid=accept_id, qty=2),
        _event("IMPRESSION", NOW - timedelta(seconds=5), sku=None),
        _event("ACCEPT", NOW - timedelta(seconds=2), eid=accept_id),  # replay of the same eventId
        _event("ADD_TO_CART", NOW, sku="SKU-CRM-500"),
        _event("IMPRESSION", NOW, sid=unknown),
    ]
    result = svc.ingest(_ctx(), batch)
    assert result["accepted"] == 2 and result["duplicates"] == 1
    assert [r["code"] for r in result["rejected"]] == ["HS-EVENT-SKU-NOT-RECOMMENDED", "HS-EVENT-UNKNOWN-SUGGESTION"]
    assert result["charges"] == [
        {"eventId": str(accept_id), "chargeId": "charge-1", "state": "PENDING"}, {"eventId": str(accept_id), "chargeId": "existing", "state": "PENDING"},
    ]
    assert charges.pending == [accept_id]
    outbox = db.uow.outbox()
    assert [p["type"] for _, _, p in outbox] == ["IMPRESSION", "ACCEPT"]
    assert outbox[1][1] == str(SID) and outbox[1][2]["rank"] == 1 and outbox[1][2]["quantity"] == 2 and "rank" not in outbox[0][2]
    events = [p for sql, p in db.uow.statements if "txn.suggestion_event" in sql]
    assert [p["type"] for p in events] == ["IMPRESSION", "ACCEPT"] and events[0]["meta"] == "{}"
    assert cache.labels == [(PID, "cart-1", "SKU-MLT-256", SID, timedelta(hours=72))]


def test_ingest_gives_sdk_sessions_no_charges_and_rejects_foreign_suggestions() -> None:
    other = uuid.uuid4()
    svc, _, charges, _ = _event_service({SID: _snapshot(), other: _snapshot(suggestion_id=other, cart_ref="cart-2")})
    sdk = _ctx(sdk=True, cart="cart-1")
    result = svc.ingest(sdk, [_event("ADD_TO_CART", NOW), _event("ADD_TO_CART", NOW, sid=other)])
    assert result["accepted"] == 1 and "charges" not in result and len(charges.pending) == 1
    assert result["rejected"][0]["code"] == "HS-AUTH-SESSION-SCOPE"


def test_ingest_undo_and_dismiss() -> None:
    svc, _, charges, cache = _event_service({SID: _snapshot(user_ref="u-1"), uuid.UUID(int=1): _snapshot(suggestion_id=uuid.UUID(int=1))})
    undo, dismiss = _event("UNDO", NOW, sku=None), _event("DISMISS", NOW, sku=None, sid=uuid.UUID(int=1))
    result = svc.ingest(_ctx(), {"events": [
        {"eventId": str(undo.event_id), "type": "UNDO", "suggestionId": str(SID), "occurredAt": "2026-11-03T10:00:00Z"},
        {"eventId": str(dismiss.event_id), "type": "DISMISS", "suggestionId": str(uuid.UUID(int=1)), "occurredAt": "2026-11-03T10:00:00Z"},
    ]})
    assert result == {"accepted": 2, "duplicates": 0}
    assert charges.voided == [undo.event_id] and cache.labels == []
    assert cache.dismissed == [(PID, "cart:cart-1", FROM_PRODUCT, ALT_PRODUCT, timedelta(days=14))]


# ---------------------------------------------------------------------------------------------------------------------
# Charges and the recorder
# ---------------------------------------------------------------------------------------------------------------------


def _charge_row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "charge_id": uuid.UUID(int=7), "partner_id": PID, "event_id": uuid.UUID(int=8), "event_type": "ADD_TO_CART", "suggestion_id": SID, "cart_ref": "cart-1",
        "placement": "CART", "alternative_partner_sku": "SKU-MLT-256", "quantity": Decimal("2.000"), "basis": "PER_ADD_EVENT", "unit_amount": Decimal("1.0000"),
        "amount": Decimal("1.0000"), "currency": "INR", "rule_version": "RA-1", "state": "PENDING", "rejection_reason": None, "occurred_at": NOW,
        "received_at": NOW, "finalizes_at": NOW + timedelta(minutes=10), "finalized_at": None, "statement_id": None, "credited_amount": Decimal("0.0000"),
    }
    base.update(kw)
    return base


def test_charge_api_shape() -> None:
    pending = RecoChargeService.to_api(_charge_row())
    assert pending == {
        "chargeId": str(uuid.UUID(int=7)), "eventId": str(uuid.UUID(int=8)), "eventType": "ADD_TO_CART", "suggestionId": str(SID), "cartRef": "cart-1",
        "placement": "CART", "alternativePartnerSku": "SKU-MLT-256", "quantity": Decimal("2.000"), "basis": "PER_ADD_EVENT",
        "amount": {"amount": "1.00", "currency": "INR"}, "state": "PENDING", "ruleVersion": "RA-1", "occurredAt": "2026-11-03T10:00:00.000000Z",
        "finalizesAt": "2026-11-03T10:10:00.000000Z",
    }
    final = RecoChargeService.to_api(_charge_row(state="CREDITED", currency="INR", finalized_at=NOW, statement_id=uuid.UUID(int=9),
                                                 credited_amount=Decimal("1.0000"), rejection_reason=None))
    assert "finalizesAt" not in final and final["finalizedAt"] == "2026-11-03T10:00:00.000000Z"
    assert final["statementId"] == str(uuid.UUID(int=9)) and final["creditedAmount"] == {"amount": "1.00", "currency": "INR"}


def test_recorder_writes_evaluation_suggestions_and_training_rows() -> None:
    db = _FakeDb()
    recorder = EvaluationRecorder(db)  # type: ignore[arg-type]
    eid = uuid.uuid4()
    record = EvaluationRecord(PID, eid, "cart-1", None, "BLR-KOR-012", "IN", "CART", 2, False, 12, "pilot-2026-11", ModelVersions("s", "r", "g1", "heuristic-L2"),
                              (_snapshot(evaluation_id=eid),), ({"suggestionId": SID, "alternatives": [{"rank": 1, "features": [0.5]}]},), NOW)
    pdp = dataclasses.replace(record, evaluation_id=None, suggestions=(_snapshot(suggestion_id=uuid.UUID(int=3)),), training_rows=())
    recorder.start()
    try:
        recorder.enqueue(record)
        recorder.enqueue(pdp)
        assert recorder.flush(5)
    finally:
        recorder.stop()
    sql = [s for s, _ in db.uow.statements]
    assert sum("txn.cart_evaluation" in s for s in sql) == 1 and sum("txn.suggestion " in s for s in sql) == 2
    evaluation = next(p for s, p in db.uow.statements if "txn.cart_evaluation" in s)
    assert json.loads(evaluation["versions"]) == {"scoring": "s", "rules": "r", "swapGraph": "g1", "ranker": "heuristic-L2"}
    assert (evaluation["lines"], evaluation["suggestions"], evaluation["latency"]) == (2, 1, 12)
    suggestion = next(p for s, p in db.uow.statements if "txn.suggestion " in s)
    assert json.loads(suggestion["alternatives"])[0]["partner_sku"] == "SKU-MLT-256" and json.loads(suggestion["recoFee"])["currency"] == "INR"
    outbox = db.uow.outbox()
    assert [(t, k) for t, k, _ in outbox] == [("hs.cart-evaluations.v1", str(eid)), ("hs.cart-evaluations.v1", str(uuid.UUID(int=3)))]
    assert outbox[0][2]["versions"]["swapGraph"] == "g1" and outbox[0][2]["rows"][0]["suggestionId"] == str(SID)
    assert "evaluationId" not in outbox[1][2]


def test_recorder_drops_the_oldest_record_when_full() -> None:
    class Small(EvaluationRecorder):
        CAPACITY = 2

    recorder = Small(_FakeDb())  # type: ignore[arg-type]
    records = [EvaluationRecord(PID, uuid.UUID(int=i), "c", None, None, "IN", "CART", 1, False, 0, None, ModelVersions("s", "r", "g"), (), (), NOW)
               for i in range(3)]
    for r in records:
        recorder.enqueue(r)
    assert recorder.enqueued == 3 and recorder.written == 0
    assert [recorder._queue.get_nowait().evaluation_id for _ in range(2)] == [uuid.UUID(int=1), uuid.UUID(int=2)]
    assert not recorder.flush(0.05)


def test_finalizer_worker_skips_attribution_when_absent_and_honours_disabled() -> None:
    calls: list[str] = []
    reco = SimpleNamespace(finalize_due=lambda: calls.append("reco") or (0, 0))
    disabled = FinalizerWorker(reco, None, SchedulerOptions(enabled=False))  # type: ignore[arg-type]
    disabled.execute()
    assert calls == []

    attribution = SimpleNamespace(finalize_due=lambda: calls.append("attribution"))
    worker = FinalizerWorker(reco, attribution, SchedulerOptions(enabled=True, reco_finalizer_seconds=3600, attribution_finalizer_seconds=60))  # type: ignore[arg-type]
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        assert worker.stop(5)
    assert calls[:2] == ["reco", "attribution"]

    calls.clear()
    worker = FinalizerWorker(reco, None, SchedulerOptions(enabled=True, reco_finalizer_seconds=3600))  # type: ignore[arg-type]
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        assert worker.stop(5)
    assert calls == ["reco"]
