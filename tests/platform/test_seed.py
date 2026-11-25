"""Demo seed on PostgreSQL (`integration`): `DemoSeeder.seed(repo_root, demo_tenants=True)` through the platform Container,
compared with the C# `hs seed --demo` on a fresh database, then the catalog, inventory, preference, privacy, product,
transparency, feed and cache-warmer services on the seeded state.

Runs on a fresh dev database of its own (the `fresh_database_url` fixture in tests/conftest.py). The module seeds once (the
tests run in file order and later ones change the state). The expected step results and table contents are those of the C#
seeder on a fresh database (two runs, identical apart from ids and timestamps).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from healthswap.domain.primitives import gtin_with_check_digit
from healthswap.platform import jsonutil, netjson
from healthswap.platform.container import Container
from healthswap.platform.db import convert_sql
from healthswap.platform.errors import HsError
from healthswap.platform.seeding.demo_seeder import SeedReport
from healthswap.platform.services.catalog import read_catalog_item
from healthswap.platform.settings import load_settings
from healthswap.platform.tenancy import PartnerContext

pytestmark = pytest.mark.integration

QB = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01")  # QuickBasket (IN)
HG = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e02")  # Hometown Grocer (US)

# C# `hs seed --demo` on a fresh database (graph versions carry the build time and are checked by pattern).
EXPECTED_STEPS: dict[str, Any] = {
    "referenceData": "applied",
    "brandPortal": {"received": 47, "created": 47, "newVersions": 47, "unchanged": 0, "rejected": 0, "curationTasks": 1},
    "usdaFdc": {"received": 2, "created": 1, "newVersions": 2, "unchanged": 0, "rejected": 0, "curationTasks": 1},
    "openFoodFacts": {"received": 3, "created": 1, "newVersions": 3, "unchanged": 0, "rejected": 0, "curationTasks": 0},
    "recalls": 1,
    "scoring": [
        {"market": "IN", "products": 33, "eligible": 32, "changed": 33, "categories": 12},
        {"market": "US", "products": 16, "eligible": 15, "changed": 16, "categories": 7},
    ],
    "catalog:QuickBasket": {"accepted": 34, "rejected": 0, "states": {"AUTO_MATCHED": 32, "NEEDS_REVIEW": 1, "UNMATCHED": 1}},
    "inventory:QuickBasket:BLR-KOR-012": {"accepted": 32, "rejected": 0},
    "inventory:QuickBasket:BLR-IND-004": {"accepted": 7, "rejected": 0},
    "catalog:Hometown Grocer": {"accepted": 15, "rejected": 0, "states": {"AUTO_MATCHED": 15}},
    "inventory:Hometown Grocer:NYC-BKN-101": {"accepted": 15, "rejected": 0},
    "swapGraphs": [
        {"market": "IN", "status": "ACTIVE", "products": 11, "edges": 15, "validation": {
            "edges": 15, "productsWithEdges": 11, "edgesIntoIneligible": 0, "edgesBelowDeltaMin": 0, "llmJudged": 4, "substitutabilityModel": "heuristic",
            "rulesVersion": "R-1.3"}},
        {"market": "US", "status": "ACTIVE", "products": 6, "edges": 7, "validation": {
            "edges": 7, "productsWithEdges": 6, "edgesIntoIneligible": 0, "edgesBelowDeltaMin": 0, "llmJudged": 1, "substitutabilityModel": "heuristic",
            "rulesVersion": "R-1.3"}},
    ],
    "explanations": [
        {"market": "IN", "pairs": 15, "rendered": 30, "llmAccepted": 26, "llmRejected": 4},
        {"market": "US", "pairs": 7, "rendered": 12, "llmAccepted": 10, "llmRejected": 2},
    ],
}

# Table sizes after the C# seed (the bus drained: 102 kb product changes, 6 tenant messages, 1 webhook delivery).
EXPECTED_COUNTS = {
    "kb.product": 49, "kb.product_version": 52, "kb.source_record": 52, "kb.curation_task": 3, "rec.health_profile": 49, "rec.product_score": 82,
    "rec.swap_edge": 22, "rec.explanation": 42, "rec.llm_audit": 47, "tenant.partner": 2, "tenant.api_client": 2, "tenant.store": 3,
    "tenant.webhook_endpoint": 2, "tenant.catalog_item": 49, "tenant.store_inventory": 54, "txn.outbox": 108, "txn.webhook_delivery": 1,
    "audit.change_log": 2,
}


def _drain(c: Container) -> None:
    while c.outbox_relay.pump() > 0:
        pass
    assert c.bus.wait_idle(60)


@pytest.fixture(scope="module")
def seeded(fresh_database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Container, SeedReport]]:
    tmp = tmp_path_factory.mktemp("seed")
    settings = load_settings(
        overrides={
            "database.url": fresh_database_url, "database.max_pool_size": 4, "llm.provider": "Mock", "llm.mock_fault_every": 7,
            "llm.enable_explanation_polish": True, "graph.llm_judge_uncertain": True, "lake.root": str(tmp / "lake"),
            "data_protection.keys_directory": str(tmp / "keys"), "feeds.storage_root": str(tmp / "feeds"),
        },
        env={},
    )
    with Container(settings) as c:
        # The CLI's worker set: bus + outbox relay (webhook fan-out and lake sink), no dispatcher, scheduler or warmer.
        c.start_workers(include_scheduler=False, include_cache_warmer=False, include_webhooks=False)
        report = c.demo_seeder.seed(c.repo_root, True)
        _drain(c)
        yield c, report


def _rows(c: Container, sql: str, **params: Any) -> list[dict[str, Any]]:
    """Test-side reads on the privileged raw connection (the application roles cannot read every table, e.g. audit)."""
    with c.db.raw() as conn:
        return conn.execute(convert_sql(sql) if params else sql, params or None).fetchall()


def test_seed_report_matches_the_csharp_seeder(seeded: tuple[Container, SeedReport]) -> None:
    _, report = seeded
    steps = jsonutil.to_plain(report.steps)
    assert list(steps) == list(EXPECTED_STEPS)
    graphs = steps["swapGraphs"]
    for g in graphs:
        assert re.fullmatch(rf"{g['market']}-\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ", g.pop("graphVersion"))
    assert steps == EXPECTED_STEPS
    assert list(steps["catalog:QuickBasket"]["states"]) == ["AUTO_MATCHED", "NEEDS_REVIEW", "UNMATCHED"]  # GroupBy: first appearance


def test_seeded_tables_match_the_csharp_seeder(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    counts = {t: _rows(c, f"select count(*) as n from {t}")[0]["n"] for t in EXPECTED_COUNTS}
    topics = {r["topic"]: (r["n"], r["published"]) for r in _rows(
        c, "select topic, count(*) as n, count(published_at) as published from txn.outbox group by topic")}
    assert counts == EXPECTED_COUNTS
    assert topics == {"hs.catalog-changes.v1": (2, 2), "hs.inventory-updates.v1": (3, 3), "hs.kb-product-changes.v1": (102, 102), "hs.webhook-requests.v1": (1, 1)}


def test_partners_clients_stores_and_webhooks(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    partners = _rows(c, "select partner_id, display_name, legal_name, geography, markets, status, config->'fee'->>'rate' as rate, config::text as config from tenant.partner order by partner_id")
    assert [(p["display_name"], p["geography"], p["markets"], p["status"], p["rate"]) for p in partners] == [
        ("QuickBasket", "IN", ["IN"], "LIVE", "0.10"), ("Hometown Grocer", "US", ["US"], "LIVE", "0.10")]  # decimal scale kept
    demo = jsonutil.loads((c.repo_root / "data" / "seed" / "demo" / "partners.json").read_text(encoding="utf-8"))
    for row, source in zip(partners, demo["partners"], strict=True):
        assert jsonutil.loads(row["config"]) == source["config"]
    assert c.partners.get(QB).config.currency == "INR" and c.partners.get(HG).markets == ("US",)

    clients = _rows(c, "select client_id, partner_id, scopes, status from tenant.api_client order by client_id")
    assert [(r["client_id"], r["partner_id"], len(r["scopes"]), r["status"]) for r in clients] == [
        ("hometown-server", HG, 13, "ACTIVE"), ("quickbasket-server", QB, 13, "ACTIVE")]
    # Client secrets are never stored (tenant.api_client holds metadata only, exactly like the C# seeder).
    columns = {r["column_name"] for r in _rows(c, "select column_name from information_schema.columns where table_schema = 'tenant' and table_name = 'api_client'")}
    assert not any("secret" in name or "hash" in name for name in columns)

    stores = _rows(c, "select partner_id, store_id, market, postal_code, timezone, is_active from tenant.store order by partner_id, store_id")
    assert [(s["partner_id"], s["store_id"], s["market"], s["postal_code"], s["timezone"], s["is_active"]) for s in stores] == [
        (QB, "BLR-IND-004", "IN", "560038", "Asia/Kolkata", True), (QB, "BLR-KOR-012", "IN", "560034", "Asia/Kolkata", True),
        (HG, "NYC-BKN-101", "US", "11201", "America/New_York", True)]

    hooks = _rows(c, "select partner_id, url, event_types, status, secret_ciphertext from tenant.webhook_endpoint order by partner_id")
    assert [(h["partner_id"], h["url"], h["event_types"], h["status"]) for h in hooks] == [
        (QB, "http://localhost:8080/demo/webhooks/quickbasket", [], "ACTIVE"), (HG, "http://localhost:8080/demo/webhooks/hometown", [], "ACTIVE")]
    assert [c.secret_protector.unprotect(h["secret_ciphertext"]) for h in hooks] == ["dev-only-webhook-secret-quickbasket", "dev-only-webhook-secret-hometown"]
    assert all("dev-only" not in h["secret_ciphertext"] for h in hooks)

    log = _rows(c, "select actor, action, entity_type, entity_id, details::text as details from audit.change_log order by id")
    assert [(r["actor"], r["action"], r["entity_type"], r["entity_id"], r["details"]) for r in log] == [
        ("seed", "PARTNER_UPSERT", "PARTNER", str(QB), "{}"), ("seed", "PARTNER_UPSERT", "PARTNER", str(HG), "{}")]


def test_catalog_matches_and_inventory(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    items = _rows(c, """
        select c.partner_id, c.partner_sku, c.match_method, c.match_confidence, c.match_state, c.is_listed, c.raw_payload_uri, p.primary_gtin
        from tenant.catalog_item c left join kb.product p on p.product_id = c.product_id order by c.partner_id, c.partner_sku""")
    summary: dict[tuple[Any, ...], int] = {}
    for i in items:
        key = (i["partner_id"], i["match_method"], str(i["match_confidence"]), i["match_state"])
        summary[key] = summary.get(key, 0) + 1
    assert summary == {
        (QB, "FUZZY", "0.773", "NEEDS_REVIEW"): 1, (QB, "GTIN_EXACT", "1.000", "AUTO_MATCHED"): 32, (QB, "NONE", "0.000", "UNMATCHED"): 1,
        (HG, "GTIN_NORMALIZED", "0.990", "AUTO_MATCHED"): 15,
    }
    by_sku = {i["partner_sku"]: i for i in items}
    assert by_sku["SKU-NDL-280-PROMO"]["primary_gtin"] == "08909990000026"  # Swaad Atta Masala Noodles 4-pack 280 g
    assert by_sku["SKU-LOC-RUSK"]["primary_gtin"] is None
    assert all(i["is_listed"] and i["raw_payload_uri"] is None for i in items)

    inventory = _rows(c, "select partner_id, store_id, count(*) as n, count(*) filter (where not in_stock) as out_of_stock, min(currency) as currency "
                         "from tenant.store_inventory group by partner_id, store_id order by partner_id, store_id")
    assert [(r["partner_id"], r["store_id"], r["n"], r["out_of_stock"], r["currency"]) for r in inventory] == [
        (QB, "BLR-IND-004", 7, 1, "INR"), (QB, "BLR-KOR-012", 32, 1, "INR"), (HG, "NYC-BKN-101", 15, 0, "USD")]
    price = _rows(c, "select price::text as price, mrp::text as mrp from tenant.store_inventory where partner_id = @p and store_id = 'BLR-KOR-012' and partner_sku = 'SKU-NDL-280'",
                  p=QB)
    assert price == [{"price": "56.00", "mrp": "56.00"}]


def test_outbox_and_review_webhook(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    tenant = _rows(c, "select topic, msg_key, payload::text as payload from txn.outbox where topic <> 'hs.kb-product-changes.v1' order by outbox_id")
    assert [(r["topic"], r["msg_key"]) for r in tenant] == [
        ("hs.catalog-changes.v1", str(QB)), ("hs.webhook-requests.v1", str(QB)), ("hs.inventory-updates.v1", f"{QB}:BLR-KOR-012"),
        ("hs.inventory-updates.v1", f"{QB}:BLR-IND-004"), ("hs.catalog-changes.v1", str(HG)), ("hs.inventory-updates.v1", f"{HG}:NYC-BKN-101")]
    changes = json.loads(tenant[0]["payload"])
    assert list(changes) == ["items"] and len(changes["items"]) == 34  # feedId null is omitted
    assert changes["items"][0]["sku"] == "SKU-NDL-280" and changes["items"][0]["state"] == "AUTO_MATCHED"
    inventory = json.loads(tenant[2]["payload"])
    assert {k: v for k, v in inventory.items() if k != "snapshotAt"} == {"mode": "FULL_SNAPSHOT", "storeId": "BLR-KOR-012", "accepted": 32, "markedOutOfStock": 0}

    [delivery] = _rows(c, "select webhook_id, partner_id, event_type, payload::text as payload, status, attempts from txn.webhook_delivery")
    assert (delivery["partner_id"], delivery["event_type"], delivery["status"], delivery["attempts"]) == (QB, "catalog.match.review_required", "PENDING", 0)
    envelope = json.loads(delivery["payload"])
    assert envelope["id"] == delivery["webhook_id"] and envelope["id"].startswith("evt_")
    assert (envelope["type"], envelope["apiVersion"], envelope["partnerId"]) == ("catalog.match.review_required", "1.1", str(QB))
    fuzzy, unmatched = envelope["data"]["items"]
    assert "feedId" not in envelope["data"]
    assert fuzzy["partnerSku"] == "SKU-NDL-280-PROMO" and fuzzy["match"]["state"] == "NEEDS_REVIEW" and fuzzy["match"]["method"] == "FUZZY"
    assert fuzzy["match"]["confidence"] == 0.773 and fuzzy["match"]["dataSufficiency"] == "SUFFICIENT"
    assert unmatched == {"partnerSku": "SKU-LOC-RUSK", "match": {"state": "UNMATCHED", "method": "NONE", "confidence": 0, "dataSufficiency": "INSUFFICIENT"}}
    lake = list((c.settings.lake.root / "bronze").glob("hs.catalog-changes.v1/dt=*/part-0000.jsonl"))
    assert lake and sum(len(p.read_text(encoding="utf-8").splitlines()) for p in lake) == 2


def test_cache_warmer_and_read_services(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    counts = c.cache_warmer.warm_all()
    assert counts == {"profiles": 49, "edges": 22, "explanations": 42, "graphs": 2, "skuMaps": 47, "inventory": 54, "preferences": 0, "lineLabels": 0}
    assert c.cache_warmer.last_warm_at is not None

    ctx = PartnerContext(partner_id=QB)
    by_sku = c.product_service.lookup(ctx, "IN", None, "SKU-NDL-280")
    by_gtin = c.product_service.lookup(ctx, "IN", "8901234567890", None)
    assert by_sku == by_gtin and by_sku["productId"] == c.product_service.get(ctx, uuid.UUID(by_sku["productId"]), "IN")["productId"]
    for call, status, code in (
        (lambda: c.product_service.lookup(ctx, "US", "8901234567890", None), 422, "HS-UNSUPPORTED-MARKET"),
        (lambda: c.product_service.lookup(ctx, "IN", "8901234567891", None), 400, "HS-REQ-INVALID"),
        (lambda: c.product_service.lookup(ctx, "IN", None, None), 400, "HS-REQ-INVALID"),
        (lambda: c.product_service.lookup(ctx, "IN", None, "SKU-LOC-RUSK"), 404, "HS-NOT-FOUND"),
        (lambda: c.product_service.get(ctx, uuid.UUID(int=5), "IN"), 404, "HS-NOT-FOUND"),
        (lambda: c.product_service.lookup(PartnerContext(partner_id=QB, is_sdk_session=True, sdk_market="US"), "IN", "8901234567890", None), 403, "HS-AUTH-SCOPE"),
    ):
        with pytest.raises(HsError) as err:
            call()
        assert (err.value.status, err.value.code) == (status, code)

    status = c.transparency_service.status()
    assert status["status"] == "OPERATIONAL" and status["regions"] == [{"region": "IN", "status": "OPERATIONAL"}, {"region": "US", "status": "OPERATIONAL"}]
    models = c.transparency_service.models()["items"]
    assert models == sorted(models, key=lambda m: (m["market"], m["code"])) and {m["market"] for m in models} == {"IN", "US"}
    assert {"HSI", "IN_INR_DRAFT"} <= {m["code"] for m in models if m["market"] == "IN"}
    assert all(m["documentationUrl"] and m["status"] for m in models)
    assert c.transparency_service.models("US")["items"] == [m for m in models if m["market"] == "US"]


def test_catalog_item_status_override_and_delist(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    status = c.catalog_service.get(QB, "SKU-NDL-280")
    assert status["isListed"] and status["match"]["state"] == "AUTO_MATCHED" and status["match"]["method"] == "GTIN_EXACT" and "health" in status
    with pytest.raises(HsError) as err:
        c.catalog_service.get(QB, "SKU-NOPE")
    assert err.value.status == 404

    for req, code in (({"action": "ASSIGN"}, "HS-MATCH-PRODUCT-REQUIRED"), ({"action": "ASSIGN", "productId": str(uuid.UUID(int=1))}, "HS-UNKNOWN-PRODUCT"),
                      ({"action": "CONFIRM"}, "HS-MATCH-NOTHING-TO-CONFIRM")):
        with pytest.raises(HsError) as err:
            c.catalog_service.override_match(QB, "SKU-LOC-RUSK" if code == "HS-MATCH-NOTHING-TO-CONFIRM" else "SKU-NDL-280-PROMO", req)
        assert (err.value.status, err.value.code) == (422, code)

    confirmed = c.catalog_service.override_match(QB, "SKU-NDL-280-PROMO", {"action": "CONFIRM", "comment": "promo pack of the atta noodles"})
    assert (confirmed["match"]["state"], confirmed["match"]["method"], confirmed["match"]["confidence"]) == ("CONFIRMED", "MANUAL", Decimal("1.000"))
    ctx = PartnerContext(partner_id=QB)
    assert c.product_service.lookup(ctx, "IN", None, "SKU-NDL-280-PROMO")["productId"] == confirmed["match"]["productId"]
    [audit] = _rows(c, "select actor, action, entity_type, entity_id, details::text as details from audit.change_log where action like 'MATCH_%'")
    assert (audit["actor"], audit["action"], audit["entity_type"], audit["entity_id"]) == (f"partner:{QB}", "MATCH_CONFIRM", "CATALOG_ITEM", "SKU-NDL-280-PROMO")
    assert json.loads(audit["details"]) == {"Comment": "promo pack of the atta noodles", "ProductId": None}

    # A confirmed match is sticky: re-sending the catalog item keeps it.
    item = {"partnerSku": "SKU-NDL-280-PROMO", "title": "Swaad Masala Noodles 280g Pack of 4 (promo pack)", "brand": "Swaad", "netQuantity": {"value": 280, "unit": "g"}}
    outcome = c.catalog_service.upsert(QB, [item])
    assert outcome.result["results"][0]["match"]["state"] == "CONFIRMED" and outcome.needs_enrichment == []

    c.catalog_service.delist(QB, "SKU-NDL-280-PROMO")
    assert c.catalog_service.get(QB, "SKU-NDL-280-PROMO")["isListed"] is False
    with pytest.raises(HsError) as err:
        c.product_service.lookup(ctx, "IN", None, "SKU-NDL-280-PROMO")
    assert err.value.status == 404


def test_catalog_upsert_rejections_and_enrichment(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    new_gtin = gtin_with_check_digit("890999900077")
    items = [
        {"partnerSku": "SKU-BAD-GTIN", "gtin": "8901234567891", "title": "Broken"},
        {"partnerSku": "SKU-US-ONLY", "title": "Wrong market", "markets": ["US"]},
        {"partnerSku": "SKU-RAGI-COOKIE", "gtin": new_gtin, "title": "Test Ragi Millet Cookies 200 g", "brand": "TestBake", "categoryPath": ["Biscuits"],
         "netQuantity": {"value": 200, "unit": "g"}, "ingredientsText": "Ragi flour (40%), wheat flour, sugar, edible vegetable oil (palm), salt",
         "nutrition": [{"basis": "PER_100G", "energyKcal": 470, "proteinG": 7, "carbohydrateG": 66, "totalSugarsG": 22, "totalFatG": 19,
                        "saturatedFatG": 9, "sodiumMg": 310, "dietaryFibreG": Decimal("4.5")}]},
    ]
    outcome = c.catalog_service.upsert(QB, items)
    results = outcome.result["results"]
    assert (outcome.result["received"], outcome.result["accepted"], outcome.result["rejected"]) == (3, 1, 2)
    assert results[0] == {"partnerSku": "SKU-BAD-GTIN", "status": "REJECTED", "errors": [{"field": "/gtin", "message": "Invalid GTIN check digit or length", "code": "HS-INVALID-GTIN"}]}
    assert results[1]["errors"] == [{"field": "/markets", "message": "Market not enabled for this partner", "code": "HS-UNSUPPORTED-MARKET"}]
    assert results[2]["match"]["state"] == "PENDING_ENRICHMENT" and [i["partnerSku"] for i, _ in outcome.needs_enrichment] == ["SKU-RAGI-COOKIE"]

    c.catalog_enrichment.enrich(QB, outcome.needs_enrichment)
    status = c.catalog_service.get(QB, "SKU-RAGI-COOKIE")
    assert (status["match"]["state"], status["match"]["method"]) == ("AUTO_MATCHED", "GTIN_EXACT") and "health" in status
    [source] = _rows(c, "select r.external_id, r.partner_id, r.payload_sha256, r.payload_uri from kb.source_record r join kb.source s using (source_id) "
                        "where s.code = 'PARTNER_FEED'")
    raw = netjson.serialize_hs(read_catalog_item(items[2]))  # the C# JsonSerializer.Serialize(item, HsJson.Options)
    assert (source["external_id"], source["partner_id"]) == (f"{QB.hex}:SKU-RAGI-COOKIE", QB)
    assert bytes(source["payload_sha256"]) == hashlib.sha256(raw.encode("utf-8")).digest()
    bronze = c.settings.lake.root / source["payload_uri"].removeprefix("lake://")
    assert bronze.read_text(encoding="utf-8") == raw and json.loads(raw)["nutrition"][0]["dietaryFibreG"] == 4.5


def test_inventory_delta_and_errors(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    ack = c.inventory_service.upsert(QB, "BLR-KOR-012", {
        "snapshotAt": "2099-01-01T00:00:00Z", "mode": "DELTA",
        "entries": [
            {"partnerSku": "SKU-NDL-280", "inStock": False, "price": {"amount": "57.00", "currency": "INR"}, "availableQuantity": 0},
            {"partnerSku": "SKU-MLT-256", "inStock": True, "price": {"amount": "1.00", "currency": "USD"}},
            {"partnerSku": "SKU-ATT-280", "inStock": True, "price": {"amount": "-1", "currency": "INR"}},
        ],
    })
    assert ack == {"accepted": 1, "rejected": 2, "errors": [
        {"partnerSku": "SKU-MLT-256", "code": "HS-CURRENCY-MISMATCH", "message": "Price currency must be INR"},
        {"partnerSku": "SKU-ATT-280", "code": "HS-INVALID-PRICE", "message": "Price must be a non-negative decimal"}]}
    cached = c.hot_cache.get_inventory(QB, "BLR-KOR-012", ["SKU-NDL-280"])["SKU-NDL-280"]
    assert (cached.in_stock, cached.price, cached.available_quantity) == (False, Decimal("57.00"), 0)
    with pytest.raises(HsError) as err:
        c.inventory_service.upsert(QB, "NOPE-STORE", {"snapshotAt": "2099-01-01T00:00:00Z", "mode": "DELTA", "entries": []})
    assert (err.value.status, err.value.code) == (422, "HS-UNKNOWN-STORE")


def test_preferences_consent_and_erasure(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    user = "usr_test_0001"
    with pytest.raises(HsError) as err:
        c.preference_service.get(QB, user)
    assert err.value.status == 404

    without = c.preference_service.put(QB, user, {"dietType": "VEGETARIAN", "allergens": ["PEANUT"], "goals": ["LOW_SUGAR"], "excludedBrands": ["Swaad"]})
    assert (without["allergens"], without["goals"], without["excludedBrands"], without["healthDataConsent"]) == ([], [], ["Swaad"], False)
    granted = c.preference_service.put(QB, user, {"dietType": "VEGETARIAN", "allergens": ["PEANUT"], "goals": ["LOW_SUGAR"], "healthDataConsent": True,
                                                  "consentNoticeVersion": "2026-01", "locale": "en-IN"})
    assert (granted["allergens"], granted["goals"], granted["healthDataConsent"], granted["locale"]) == (["PEANUT"], ["LOW_SUGAR"], True, "en-IN")
    assert c.preference_service.get(QB, user) == granted
    assert c.preference_service.load_for_evaluation(QB, user).allergens == ("PEANUT",)
    consents = _rows(c, "select purpose, granted, notice_version from tenant.consent_record where user_ref = @u order by captured_at, purpose", u=user)
    assert [(r["purpose"], r["granted"], r["notice_version"]) for r in consents] == [
        ("HEALTH_PREFERENCES", False, "unspecified"), ("PERSONALIZATION", False, "unspecified"), ("HEALTH_PREFERENCES", True, "2026-01")]

    erased = c.privacy_service.erase(QB, user)
    assert erased["status"] == "COMPLETED" and uuid.UUID(erased["requestId"])
    with pytest.raises(HsError):
        c.preference_service.get(QB, user)
    assert c.preference_service.load_for_evaluation(QB, user) is None
    assert _rows(c, "select count(*) as n from tenant.consent_record where user_ref = @u", u=user) == [{"n": 0}]
    [dsar] = _rows(c, "select status, request_type, user_ref_hash from tenant.dsar_request where request_id = @r", r=uuid.UUID(erased["requestId"]))
    assert (dsar["status"], dsar["request_type"]) == ("COMPLETED", "ERASURE") and len(bytes(dsar["user_ref_hash"])) == 32
    _drain(c)
    erasure = _rows(c, "select payload::text as payload from txn.webhook_delivery where event_type = 'user.erasure.completed'")
    assert len(erasure) == 1 and json.loads(erasure[0]["payload"])["data"]["requestId"] == erased["requestId"]


def test_feed_upload_and_processing(seeded: tuple[Container, SeedReport]) -> None:
    c, _ = seeded
    urls: dict[str, str] = {}

    def upload_url(feed_id: uuid.UUID, token: str) -> str:
        urls[str(feed_id)] = token
        return f"http://localhost:8080/v1/catalog/feeds/{feed_id}/upload?token={token}"

    with pytest.raises(HsError) as err:
        c.feed_service.create(QB, {"format": "PARQUET", "mode": "DELTA"}, upload_url)
    assert (err.value.status, err.value.code) == (422, "HS-FEED-FORMAT-UNSUPPORTED")

    feed = c.feed_service.create(QB, {"format": "JSONL", "mode": "DELTA", "compression": "GZIP"}, upload_url)
    feed_id = uuid.UUID(feed["feedId"])
    assert feed["status"] == "AWAITING_UPLOAD" and feed["uploadUrl"].endswith(urls[feed["feedId"]])
    assert c.feed_service.get(QB, feed_id)["status"] == "AWAITING_UPLOAD"
    lines = [
        json.dumps({"partnerSku": "SKU-MLT-256", "gtin": "8909990000019", "title": "MilletMagic Millet Masala Noodles 4-pack 256 g"}),
        "{not json",
        json.dumps({"partnerSku": "SKU-FEED-BAD", "gtin": "123", "title": "Bad GTIN"}),
        "",
    ]
    body = gzip.compress(("\ufeff" + "\r\n".join(lines)).encode("utf-8"))
    with pytest.raises(HsError) as err:
        c.feed_service.accept_upload(feed_id, urls[feed["feedId"]] + "x", body)
    assert (err.value.status, err.value.code) == (403, "HS-FEED-UPLOAD-DENIED")
    assert c.feed_service.accept_upload(feed_id, urls[feed["feedId"]], io_chunks(body)) == (QB, feed_id)
    with pytest.raises(HsError) as err:
        c.feed_service.accept_upload(feed_id, urls[feed["feedId"]], body)
    assert (err.value.status, err.value.code) == (409, "HS-FEED-STATE")

    c.feed_service.process(QB, feed_id)
    done = c.feed_service.get(QB, feed_id, lambda f: f"http://localhost:8080/v1/catalog/feeds/{f}/errors")
    assert done["status"] == "PARTIAL"
    assert done["stats"] == {"received": 3, "accepted": 1, "rejected": 2, "autoMatched": 1, "needsReview": 0, "unmatched": 0, "pendingEnrichment": 0}
    assert done["errorReportUrl"].endswith("/errors") and "completedAt" in done and "uploadUrlExpiresAt" not in done
    report = Path(c.feed_service.error_report_path(feed_id)).read_text(encoding="utf-8").splitlines()
    assert report[0].startswith("line 2: ") and report[1] == "SKU-FEED-BAD: Invalid GTIN check digit or length"
    assert c.catalog_service.get(QB, "SKU-MLT-256")["match"]["state"] == "AUTO_MATCHED"


def io_chunks(body: bytes) -> Iterator[bytes]:
    for i in range(0, len(body), 7):
        yield body[i:i + 7]
