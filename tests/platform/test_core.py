"""Platform core: cache stores, HotCache, settings, event bus, background workers and tenancy (no database needed except the
`integration` tests at the end, which get a fresh dev database from the `fresh_database_url` fixture)."""

from __future__ import annotations

import json
import logging
import queue
import re
import textwrap
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from healthswap.domain.commerce.fees import RecoFeeSnapshot
from healthswap.domain.commerce.partner_config import PartnerConfig
from healthswap.domain.knowledge.products import AllergenPresence, Nutrition, ProductData
from healthswap.domain.primitives import NetQuantity, new_id, utc_now
from healthswap.domain.scoring.health_profiles import HealthProfileBuilder, HealthProfileData, MarketPack
from healthswap.domain.scoring.models import AdditiveRegulation, FopBand, PointsModelParams
from healthswap.domain.text import to_float32
from healthswap.platform.bus import BusMessage, InProcessEventBus, LakeSink, Outbox, OutboxRelay, Topics
from healthswap.platform.cache import MemoryCacheStore, RedisCacheStore, create_cache_store, redis_glob_escape, redis_url
from healthswap.platform.db import DEFAULT_DATABASE_URL, Db, DbOptions, DbRole
from healthswap.platform.errors import HsError
from healthswap.platform.hot_cache import (
    AlternativeSnapshot,
    CachedExplanation,
    HotCache,
    InventoryItem,
    ShopperPrefsDoc,
    SkuMapEntry,
    SuggestionSnapshot,
    SwapEdge,
    dumps,
    loads,
)
from healthswap.platform.settings import CacheOptions, LakeOptions, Settings, load_settings
from healthswap.platform.tenancy import PartnerContext, PartnerDirectory
from healthswap.platform.workers import BackgroundWorker

PARTNER = uuid.UUID("6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01")
PARTNER_HEX = "6f1d0c2a5b7e4c1a9d3e1a2b3c4d5e01"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------------------------------------------------
# A fake redis.Redis (the subset RedisCacheStore uses; decode_responses=True semantics)
# ---------------------------------------------------------------------------------------------------------------------


def _redis_glob(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "[" and pattern.find("]", i + 1) > 0:
            j = pattern.find("]", i + 1)
            out.append("[" + re.escape(pattern[i + 1:j]) + "]")
            i = j + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out), re.S)


class FakePipeline:
    def __init__(self, r: FakeRedis) -> None:
        self.r, self.ops = r, []

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: self.ops.append((name, a, k))

    def execute(self) -> list[Any]:
        return [getattr(self.r, name)(*a, **k) for name, a, k in self.ops]


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttl_ms: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        v = self.data.get(key)
        return v if isinstance(v, str) else None

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.get(k) for k in keys]

    def set(self, key: str, value: str, px: int | None = None) -> bool:
        self.data[key] = value
        self.ttl_ms.pop(key, None)
        if px is not None:
            self.ttl_ms[key] = px
        return True

    def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            n += self.data.pop(k, None) is not None
            self.ttl_ms.pop(k, None)
        return n

    def hset(self, key: str, mapping: dict[str, str]) -> int:
        self.data.setdefault(key, {}).update(mapping)
        return len(mapping)

    def hmget(self, key: str, fields: list[str]) -> list[str | None]:
        return [self.data.get(key, {}).get(f) for f in fields]

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.data.get(key, {}))

    def hdel(self, key: str, *fields: str) -> int:
        h = self.data.get(key, {})
        n = sum(h.pop(f, None) is not None for f in fields)
        if not h:
            self.delete(key)
        return n

    def sadd(self, key: str, *members: str) -> int:
        self.data.setdefault(key, set()).update(members)
        return len(members)

    def smembers(self, key: str) -> set[str]:
        return set(self.data.get(key, set()))

    def srem(self, key: str, *members: str) -> int:
        s = self.data.get(key, set())
        s.difference_update(members)
        if not s:
            self.delete(key)
        return len(members)

    def pexpire(self, key: str, ms: int) -> bool:
        if key not in self.data:
            return False
        if ms <= 0:
            self.delete(key)
        else:
            self.ttl_ms[key] = ms
        return True

    def scan_iter(self, match: str = "*", count: int | None = None) -> Iterator[str]:
        rx = _redis_glob(match)
        return iter([k for k in list(self.data) if rx.fullmatch(k)])

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


# ---------------------------------------------------------------------------------------------------------------------
# Cache stores
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture(params=["memory", "redis"])
def store(request: pytest.FixtureRequest) -> MemoryCacheStore | RedisCacheStore:
    return MemoryCacheStore() if request.param == "memory" else RedisCacheStore(FakeRedis())


class TestCacheStoreContract:
    def test_strings(self, store: Any) -> None:
        assert store.get("a") is None
        store.set("a", "1")
        store.set("b", "two")
        assert store.get("a") == "1"
        assert store.m_get(["a", "missing", "b"]) == ["1", None, "two"]
        assert store.mget(["b"]) == ["two"]
        assert store.m_get([]) == []
        assert store.delete("a") is True
        assert store.delete("a") is False
        assert store.get("a") is None

    def test_hashes(self, store: Any) -> None:
        store.h_set("h", {"x": "1", "y": "2"})
        store.h_set("h", [("z", "3"), ("x", "9")])
        assert store.hm_get("h", ["x", "nope", "z"]) == ["9", None, "3"]
        assert store.hmget("h", ["y"]) == ["2"]
        assert store.hm_get("h", []) == []
        assert store.h_get_all("h") == {"x": "9", "y": "2", "z": "3"}
        store.h_del("h", ["x", "nope"])
        assert store.hgetall("h") == {"y": "2", "z": "3"}
        assert store.h_get_all("missing") == {}
        assert store.hm_get("missing", ["a"]) == [None]

    def test_sets(self, store: Any) -> None:
        store.s_add("s", ["a", "b"])
        store.s_add("s", ["b", "c"])
        assert sorted(store.s_members("s")) == ["a", "b", "c"]
        store.s_rem("s", ["a", "zz"])
        assert sorted(store.smembers("s")) == ["b", "c"]
        assert store.s_members("missing") == []
        store.s_add("s", [])
        store.s_rem("s", [])

    def test_count_and_delete_by_prefix_match_literally(self, store: Any) -> None:
        for key in ("swap:IN:g1:a", "swap:IN:g1:b", "swap:IN:g10:c", "dismiss:p:*:x", "dismiss:p:u1:x"):
            store.set(key, "1")
        store.h_set("swap:IN:g1:h", {"f": "1"})
        assert store.count("swap:IN:g1:") == 3
        assert store.count("swap:IN:") == 4
        assert store.delete_by_prefix("dismiss:p:*:") == 1  # a user ref "*" must not wipe every user's keys
        assert store.get("dismiss:p:u1:x") == "1"
        assert store.delete_by_prefix("swap:IN:g1:") == 3
        assert store.count("swap:") == 1
        assert store.delete_by_prefix("nothing:") == 0


class TestMemoryCacheStore:
    def test_ttl_expiry(self) -> None:
        clock = FakeClock()
        s = MemoryCacheStore(clock)
        s.set("a", "1", timedelta(seconds=10))
        s.set("b", "2", 2.5)
        s.set("c", "3")
        clock.advance(9.9)
        assert s.m_get(["a", "b", "c"]) == ["1", None, "3"]
        assert s.count("") == 2
        clock.advance(0.1)
        assert s.get("a") is None
        assert s.count("") == 1
        s.set("d", "4", timedelta(0))
        assert s.get("d") is None

    def test_hash_ttl_is_refreshed_only_when_given(self) -> None:
        clock = FakeClock()
        s = MemoryCacheStore(clock)
        s.h_set("h", {"a": "1"}, timedelta(seconds=10))
        clock.advance(6)
        s.h_set("h", {"b": "2"})  # no TTL: keeps the expiry
        clock.advance(5)
        assert s.h_get_all("h") == {}
        s.h_set("h", {"a": "1"}, 10)
        clock.advance(6)
        s.h_set("h", {"b": "2"}, 10)  # TTL: refreshed
        clock.advance(5)
        assert s.h_get_all("h") == {"a": "1", "b": "2"}

    def test_expired_set_starts_fresh_and_types_are_replaced(self) -> None:
        clock = FakeClock()
        s = MemoryCacheStore(clock)
        s.s_add("s", ["a"], 1)
        clock.advance(1)
        s.s_add("s", ["b"])
        assert s.s_members("s") == ["b"]
        s.set("k", "v")
        s.h_set("k", {"f": "1"})  # like C#: a hash write replaces a value of another type
        assert s.get("k") is None
        assert s.h_get_all("k") == {"f": "1"}
        assert s.s_members("k") == []

    def test_delete_by_prefix_removes_expired_keys_too(self) -> None:
        clock = FakeClock()
        s = MemoryCacheStore(clock)
        s.set("p:1", "x", 1)
        s.set("p:2", "x")
        clock.advance(2)
        assert s.count("p:") == 1
        assert s.delete_by_prefix("p:") == 2

    def test_thread_safety(self) -> None:
        s = MemoryCacheStore()

        def work(n: int) -> None:
            for i in range(200):
                s.h_set("h", {f"{n}:{i}": "1"})
                s.s_add("s", [f"{n}:{i}"])

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(s.h_get_all("h")) == 1600
        assert len(s.s_members("s")) == 1600


class TestRedisCacheStore:
    def test_ttls_are_sent_in_milliseconds(self) -> None:
        r = FakeRedis()
        s = RedisCacheStore(r)
        s.set("a", "1", timedelta(seconds=1.5))
        s.set("b", "1", 0)
        s.set("c", "1")
        s.h_set("h", {"f": "v"}, timedelta(days=30))
        s.s_add("s", ["m"], 60)
        assert r.ttl_ms == {"a": 1500, "b": 1, "h": 30 * 86_400_000, "s": 60_000}
        s.h_set("h2", [], 10)  # nothing to write: no key is created
        assert "h2" not in r.data

    def test_bytes_responses_are_decoded(self) -> None:
        class BytesRedis(FakeRedis):
            def get(self, key: str) -> Any:
                v = super().get(key)
                return None if v is None else v.encode()

        s = RedisCacheStore(BytesRedis())
        s.set("k", "välue")
        assert s.get("k") == "välue"

    def test_glob_escape(self) -> None:
        assert redis_glob_escape("dismiss:p:[a]*?\\:") == "dismiss:p:\\[a\\]\\*\\?\\\\:"

    @pytest.mark.parametrize(
        ("configuration", "url"),
        [
            ("redis://cache:6379/2", "redis://cache:6379/2"),
            ("localhost", "redis://localhost:6379/0"),
            ("localhost:6380", "redis://localhost:6380/0"),
            ("hs.eastus.redis.azure.net:10000,password=p@ss/w0rd,ssl=True,abortConnect=False", "rediss://:p%40ss%2Fw0rd@hs.eastus.redis.azure.net:10000/0"),
            ("10.0.0.5:6379,user=app,password=pw,defaultDatabase=3", "redis://app:pw@10.0.0.5:6379/3"),
            ("[::1]:6381", "redis://[::1]:6381/0"),
        ],
    )
    def test_stackexchange_configuration_strings(self, configuration: str, url: str) -> None:
        assert redis_url(configuration) == url

    def test_create_cache_store(self) -> None:
        assert isinstance(create_cache_store(CacheOptions()), MemoryCacheStore)
        assert isinstance(create_cache_store(None), MemoryCacheStore)
        store = create_cache_store(CacheOptions(redis="cache.example:6390,password=secret,defaultDatabase=1"))
        assert isinstance(store, RedisCacheStore)
        kwargs = store.client.connection_pool.connection_kwargs  # the client connects lazily: no server needed
        assert (kwargs["host"], kwargs["port"], kwargs["password"], kwargs["db"]) == ("cache.example", 6390, "secret", 1)


# ---------------------------------------------------------------------------------------------------------------------
# HotCache
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def in_pack(root: Path) -> MarketPack:
    def load(p: Path) -> Any:
        return json.loads(p.read_text(encoding="utf-8"), parse_float=D)

    packs = root / "data" / "packs"
    models = [PointsModelParams.from_dict(load(f)) for f in sorted((packs / "models").glob("*.json"))]
    additives = AdditiveRegulation.from_additives_file(load(packs / "additives.json"), date(2026, 11, 1))
    return MarketPack.from_pack_file(load(packs / "IN.json"), models, additives)


def noodles(pack: MarketPack) -> HealthProfileData:
    p = ProductData(
        product_id=uuid.uuid4(), version_id=42, gtin="08901234567893", name="Masala noodles", brand="Brand", category_code="noodles.instant",
        category_id=uuid.uuid4(), net_quantity=NetQuantity(D(280), "g"),
        nutrition=Nutrition(
            energy_kcal=D(440), total_sugars_g=D("2.5"), added_sugars_g=D("2.5"), saturated_fat_g=D("8.5"), total_fat_g=D(17), trans_fat_g=D(0),
            sodium_mg=D(1200), fibre_g=D(2), protein_g=D("8.5"), carbohydrate_g=D(60),
        ),
        nova=4, nova_confidence=D("0.9"), additives=("621", "951"), data_sufficiency="SUFFICIENT", confidence=D("0.95"), diet_class="VEG",
        allergens={"GLUTEN": AllergenPresence.CONTAINS, "MILK": AllergenPresence.MAY_CONTAIN}, allergens_verified=True, allergen_confidence=D(1),
        form="NOODLE", flavour_family="MASALA", use_occasion="MEAL", marks=("VEG_MARK",),
    )
    return HealthProfileBuilder.build(p, pack, utc_now(), category_name="Instant noodles", sources=("BRAND_PORTAL",))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cache(clock: FakeClock) -> HotCache:
    return HotCache(MemoryCacheStore(clock))


class TestHotCache:
    def test_profile_round_trip_drops_only_the_json_ignored_score(self, cache: HotCache, in_pack: MarketPack) -> None:
        hp = noodles(in_pack)
        assert hp.fop_bands and hp.primary is not None and hp.primary.normalized is not None
        cache.set_profile(hp)
        raw = cache.store.get(f"hp:IN:{hp.product_id.hex}")
        assert raw is not None and "normalized" not in raw and '"product_id"' in raw
        back = cache.get_profile("IN", hp.product_id)
        expected = replace(hp, primary=replace(hp.primary, normalized=None), scores=tuple(replace(s, normalized=None) for s in hp.scores))
        assert back == expected
        assert all(type(b) is FopBand for b in back.fop_bands.values())
        assert all(type(a) is AllergenPresence for a in back.allergens.values())
        assert type(back.nutrition.energy_kcal) is D and back.computed_at == hp.computed_at

    def test_get_profiles_dedupes_and_skips_missing(self, cache: HotCache, in_pack: MarketPack) -> None:
        a, b = noodles(in_pack), noodles(in_pack)
        cache.set_profile(a)
        cache.set_profile(b)
        missing = uuid.uuid4()
        got = cache.get_profiles("IN", [b.product_id, missing, a.product_id, b.product_id])
        assert list(got) == [b.product_id, a.product_id]
        assert cache.get_profiles("US", [a.product_id]) == {}
        assert cache.get_profile("IN", missing) is None

    def test_key_formats_match_the_csharp_implementation(self) -> None:
        product, other = uuid.UUID(int=1), uuid.UUID(int=2)
        assert HotCache.sku_map_key(PARTNER, "SKU-1") == f"skumap:{PARTNER_HEX}:SKU-1"
        assert HotCache.p2_sku_key(PARTNER, product) == f"p2sku:{PARTNER_HEX}:{'0' * 31}1"
        assert HotCache.profile_key("IN", product) == f"hp:IN:{'0' * 31}1"
        assert HotCache.edges_key("IN", "IN-g7", product) == f"swap:IN:IN-g7:{'0' * 31}1"
        assert HotCache.inventory_key(PARTNER, "BLR-01") == f"inv:{PARTNER_HEX}:BLR-01"
        assert HotCache.explanation_key("IN", "hi-IN", product, other, "abc") == f"expl:IN:hi-IN:{'0' * 31}1:{'0' * 31}2:abc"
        assert HotCache.prefs_key(PARTNER, "u1") == f"pref:{PARTNER_HEX}:u1"
        assert HotCache.dismiss_key(PARTNER, "u1", product, other) == f"dismiss:{PARTNER_HEX}:u1:{'0' * 31}1:{'0' * 31}2"
        assert HotCache.cart_reco_key(PARTNER, "cart-9") == f"reco:{PARTNER_HEX}:cart-9"
        assert HotCache.sku_map_key(str(PARTNER), "x") == f"skumap:{PARTNER_HEX}:x"  # string ids are accepted

    def test_sku_maps_partner_skus_and_gtins(self, cache: HotCache) -> None:
        product = uuid.uuid4()
        cache.set_sku_map(PARTNER, "SKU-1", SkuMapEntry(product, D("0.97"), "AUTO_MATCHED"))
        assert cache.get_sku_maps(PARTNER, ["SKU-1", "SKU-2"]) == [SkuMapEntry(product, D("0.97"), "AUTO_MATCHED"), None]
        cache.remove_sku_map(PARTNER, "SKU-1")
        assert cache.get_sku_maps(PARTNER, ["SKU-1"]) == [None]
        cache.add_partner_sku(PARTNER, product, "SKU-1")
        cache.add_partner_sku(PARTNER, product, "SKU-1B")
        assert sorted(cache.get_partner_skus(PARTNER, product)) == ["SKU-1", "SKU-1B"]
        cache.remove_partner_sku(PARTNER, product, "SKU-1")
        assert cache.get_partner_skus(PARTNER, product) == ["SKU-1B"]
        cache.set_gtin("08901234567893", product)
        assert cache.store.get("gtin:08901234567893") == str(product)
        assert cache.get_product_by_gtin("08901234567893") == product
        cache.store.set("gtin:00000000000000", "not-a-guid")
        assert cache.get_product_by_gtin("00000000000000") is None
        assert cache.get_product_by_gtin("missing") is None

    def test_swap_graph(self, cache: HotCache) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        edges = [
            SwapEdge(b, D("16.50"), D("0.91"), ("HIGHER_FIBRE", "LESS_SODIUM"), 4, 7, "noodles.instant", ()),
            SwapEdge(c, D("9.1"), D("0.80"), (), 4, 2, "noodles.instant", ("951",)),
        ]
        cache.set_active_graph("IN", "IN-g7")
        cache.set_edges("IN", "IN-g7", a, edges)
        assert cache.get_active_graph("IN") == "IN-g7"
        assert cache.get_active_graph("US") is None
        assert cache.get_edges("IN", "IN-g7", [a, a, b]) == {a: edges}
        cache.set_edges("IN", "IN-g7", b, [])
        assert cache.get_edges("IN", "IN-g7", [b]) == {b: []}
        assert cache.delete_graph("IN", "IN-g7") == 2
        assert cache.get_edges("IN", "IN-g7", [a]) == {}

    def test_inventory_hash_format(self, cache: HotCache) -> None:
        observed = datetime(2026, 11, 23, 10, 15, 30, 999_000, tzinfo=UTC)
        cache.set_inventory(PARTNER, "BLR-01", [
            ("SKU-1", InventoryItem(True, D("56.00"), D("60"), observed, 12)),
            ("SKU-2", InventoryItem(False, D("-0.00"), None, observed, None)),
        ])
        unix = int(observed.timestamp())
        raw = cache.store.h_get_all(HotCache.inventory_key(PARTNER, "BLR-01"))
        assert raw == {"SKU-1": f"1|56.00|60|{unix}|12", "SKU-2": f"0|0.00||{unix}|"}
        got = cache.get_inventory(PARTNER, "BLR-01", ["SKU-2", "SKU-1", "SKU-3"])
        whole_second = observed.replace(microsecond=0)
        assert got == {
            "SKU-2": InventoryItem(False, D("0.00"), None, whole_second, None),
            "SKU-1": InventoryItem(True, D("56.00"), D("60"), whole_second, 12),
        }
        assert sorted(cache.get_inventory_skus(PARTNER, "BLR-01")) == ["SKU-1", "SKU-2"]
        assert HotCache.parse_inventory("1|2|3") is None
        with pytest.raises(ValueError):
            HotCache.parse_inventory("1|abc||0|")

    def test_explanations_prefs_and_dismissals(self, cache: HotCache, clock: FakeClock) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        e = CachedExplanation("Less sodium, more fibre", ("31% less sodium per 100 g",), "TEMPLATE", False, None)
        cache.set_explanation("IN", "en-IN", a, b, "f00d", e)
        assert cache.get_explanation("IN", "en-IN", a, b, "f00d") == e
        assert cache.get_explanation("IN", "hi-IN", a, b, "f00d") is None

        prefs = ShopperPrefsDoc(diet_type="VEG", allergens=("PEANUT",), goals=("LOWER_SODIUM",), locale="hi-IN", is_minor=False,
                                health_data_consent=True, updated_at=datetime(2026, 11, 1, tzinfo=UTC))
        cache.set_prefs(PARTNER, "u1", prefs)
        assert cache.get_prefs(PARTNER, "u1") == prefs
        clock.advance(timedelta(days=30).total_seconds())
        assert cache.get_prefs(PARTNER, "u1") is None  # 30-day TTL
        cache.set_prefs(PARTNER, "u1", prefs)
        cache.delete_prefs(PARTNER, "u1")
        assert cache.get_prefs(PARTNER, "u1") is None

        cache.set_dismissed(PARTNER, "u1", a, b, timedelta(days=7))
        cache.set_dismissed(PARTNER, "u2", a, c, timedelta(days=7))
        assert cache.get_dismissed(PARTNER, "u1", [(a, b), (a, c)]) == {(a, b)}
        assert cache.get_dismissed(PARTNER, "u1", []) == set()
        assert cache.delete_user_keys(PARTNER, "u1") == 1
        assert cache.get_dismissed(PARTNER, "u1", [(a, b)]) == set()
        assert cache.get_dismissed(PARTNER, "u2", [(a, c)]) == {(a, c)}
        clock.advance(timedelta(days=7).total_seconds())
        assert cache.get_dismissed(PARTNER, "u2", [(a, c)]) == set()

    def test_cart_reco_labels(self, cache: HotCache, clock: FakeClock) -> None:
        s1, s2 = uuid.uuid4(), uuid.uuid4()
        cache.add_cart_reco_label(PARTNER, "cart-1", "SKU-1", s1, timedelta(hours=24))
        cache.add_cart_reco_label(PARTNER, "cart-1", "SKU-2", s2, timedelta(hours=24))
        cache.store.h_set(HotCache.cart_reco_key(PARTNER, "cart-1"), {"SKU-3": "garbage"})
        assert cache.get_cart_reco_labels(PARTNER, "cart-1") == {"SKU-1": s1, "SKU-2": s2}
        cache.remove_cart_reco_label(PARTNER, "cart-1", "SKU-1")
        assert cache.get_cart_reco_labels(PARTNER, "cart-1") == {"SKU-2": s2}
        clock.advance(timedelta(hours=24).total_seconds())
        assert cache.get_cart_reco_labels(PARTNER, "cart-1") == {}

    def test_suggestion_snapshot_round_trip(self, cache: HotCache, clock: FakeClock) -> None:
        now = utc_now()
        s = SuggestionSnapshot(
            suggestion_id=new_id(), partner_id=PARTNER, evaluation_id=new_id(), cart_ref="cart-1", user_ref="u1", market="IN", store_id="BLR-01",
            line_id="l1", from_partner_sku="SKU-1", from_product_id=uuid.uuid4(), from_version_id=4, from_qty=D(2), from_unit_price=D("56.00"),
            from_net_quantity=D(280), from_net_unit="g", from_tax_rate=D("0.12"), currency="INR",
            alternatives=(
                AlternativeSnapshot(1, "SKU-9", uuid.uuid4(), 7, D("89.00"), D(256), "g", D("16.5"), False, [to_float32(0.1), 1.0, to_float32(-2.7)]),
                AlternativeSnapshot(2, "SKU-8", uuid.uuid4(), 3, None, None, None, D(9), False),
            ),
            reco_fee=RecoFeeSnapshot(amount=D("1.00"), currency="INR", monthly_cap=D("5000.00")),
            rules_version="R-1.3", graph_version="IN-g7", created_at=now, expires_at=now + timedelta(minutes=30),
            attribution_expires_at=now + timedelta(hours=24),
        )
        cache.set_suggestion(s, timedelta(minutes=30))
        assert cache.get_suggestion(s.suggestion_id) == s
        clock.advance(1800)
        assert cache.get_suggestion(s.suggestion_id) is None

    def test_evaluations_and_categories(self, cache: HotCache) -> None:
        evaluation = uuid.uuid4()
        cache.set_evaluation_json(PARTNER, evaluation, '{"evaluationId":"x"}', timedelta(minutes=30))
        assert cache.store.get(f"eval:{PARTNER_HEX}:{evaluation.hex}") == '{"evaluationId":"x"}'
        assert cache.get_evaluation_json(PARTNER, evaluation) == '{"evaluationId":"x"}'

        cats = [uuid.uuid4() for _ in range(7)]
        cache.set_category_ceiling("IN", cats[0], D("61.25"))
        for cat, raw in zip(cats[1:], ["abc", "1,234.5", " 7 ", "5-", "+5-", "1e3"], strict=True):
            cache.store.set(f"cat:IN:{cat.hex}", raw)  # decimal.TryParse(NumberStyles.Number) semantics
        assert cache.store.get(f"cat:IN:{cats[0].hex}") == "61.25"
        assert cache.get_category_ceilings("IN", [*cats, cats[0], uuid.uuid4()]) == {
            cats[0]: D("61.25"), cats[2]: D("1234.5"), cats[3]: D(7), cats[4]: D(-5),
        }
        cache.set_category_map("IN", "noodles.instant", ["noodles.instant", "pasta.dry"])
        assert sorted(cache.get_category_map("IN", "noodles.instant")) == ["noodles.instant", "pasta.dry"]
        assert cache.get_category_map("IN", "chips") == []

    def test_json_helpers(self) -> None:
        edge = SwapEdge(uuid.uuid4(), D("10.50"), D("0.9"), ("A",), 1, 2, "x", ())
        text = HotCache.to_json([edge])
        assert json.loads(text)[0]["health_delta"] == "10.50"
        assert HotCache.from_json(list[SwapEdge], text) == [edge]
        assert loads(list[SwapEdge], dumps([edge])) == [edge]
        assert HotCache.from_json(SkuMapEntry, None) is None
        assert HotCache.from_json(SkuMapEntry, "null") is None
        assert json.loads(HotCache.to_json({"basis": "PER_100G", "facts": [edge]}))["facts"][0]["to_version"] == 2

    def test_works_on_the_redis_store(self, in_pack: MarketPack) -> None:
        cache = HotCache(RedisCacheStore(FakeRedis()))
        hp = noodles(in_pack)
        cache.set_profile(hp)
        assert cache.get_profiles("IN", [hp.product_id])[hp.product_id].hsi == hp.hsi
        cache.add_cart_reco_label(PARTNER, "c", "SKU-1", hp.product_id, timedelta(hours=1))
        assert cache.get_cart_reco_labels(PARTNER, "c") == {"SKU-1": hp.product_id}


# ---------------------------------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------------------------------


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


class TestSettings:
    def test_code_defaults_and_derived_paths(self, tmp_path: Path) -> None:
        s = load_settings(env={}, overrides={"repo_root": tmp_path})
        root = tmp_path.resolve()
        assert (s.environment, s.is_development, s.repo_root) == ("Production", False, root)
        assert s.database.url == DEFAULT_DATABASE_URL and s.database.use_roles and s.database.max_pool_size == 16
        assert s.cache == CacheOptions(redis=None, warm_on_startup=True)
        assert s.llm.provider == "Mock" and s.llm.api_key is None and s.llm.mock_fault_every == 0 and s.llm.temperature == 0.0
        assert s.llm.deployments == {"T3": "hs-t3-classifier", "T4": "hs-t4-judge", "T5": "hs-t5-explainer", "T7": "hs-embeddings"}
        assert (s.llm.max_calls_per_task_per_run, s.llm.enable_explanation_polish) == (5000, True)
        assert s.models.directory == root / "models"
        assert (s.lake.enabled, s.lake.root) == (True, root / ".data" / "lake")
        assert (s.feeds.storage_root, s.feeds.upload_url_minutes, s.feeds.max_upload_bytes) == (root / ".data" / "feeds", 60, 200 * 1024 * 1024)
        assert s.data_protection.keys_directory == root / ".data" / "keys"
        assert s.auth.signing_key_file == root / ".data" / "keys" / "jwt-es256.pem"
        assert s.auth.clients_file == root / "data" / "seed" / "demo" / "partners.json"
        assert (s.auth.authority, s.auth.issuer, s.auth.audience) == (None, "http://localhost:8080/oauth2", "healthswap-api")
        assert (s.auth.access_token_minutes, s.auth.sdk_session_max_seconds) == (10, 900)
        assert (s.graph.llm_judge_uncertain, s.graph.uncertain_low, s.graph.uncertain_high) == (True, D("0.60"), D("0.80"))
        assert (s.evaluation.internal_budget_ms, s.evaluation.suggestion_ttl_minutes, s.evaluation.max_alternatives_cap) == (100, 30, 5)
        assert (s.webhooks.allow_http_localhost, s.webhooks.max_attempt_hours, s.webhooks.timeout_seconds) == (False, 24, 10)
        assert (s.scheduler.enabled, s.scheduler.reco_finalizer_seconds, s.scheduler.attribution_finalizer_seconds) == (True, 30, 60)
        assert (s.rate_limits.enabled, s.rate_limits.cart_evaluations_per_second, s.rate_limits.default_per_second) == (True, 50, 20)
        assert s.rate_limits.burst_multiplier == 2
        assert s.cors.origins == ["http://localhost:8080"]
        assert s.demo.enabled is False
        assert s.server.base_url == "http://localhost:8080"

    def test_settings_constructor_resolves_relative_paths(self, tmp_path: Path) -> None:
        s = Settings(repo_root=tmp_path, lake=LakeOptions(root=Path("lake")))
        assert s.lake.root == tmp_path.resolve() / "lake"
        assert s.models.directory == tmp_path.resolve() / "models"

    def test_toml_layers_and_environment_selection(self, tmp_path: Path) -> None:
        write(tmp_path / "config" / "settings.toml", """
            [llm]
            mock_fault_every = 7
            [llm.deployments]
            T5 = "hs-t5-ft"
            [graph]
            uncertain_low = 0.55
            [cors]
            origins = ["https://shop.example", "https://m.shop.example"]
            [lake]
            root = "custom/lake"
            """)
        write(tmp_path / "config" / "settings.development.toml", """
            [llm]
            mock_fault_every = 3
            [demo]
            enabled = true
            """)
        root = {"repo_root": tmp_path}
        prod = load_settings(env={}, overrides=root)
        assert prod.llm.mock_fault_every == 7 and not prod.demo.enabled
        assert prod.llm.deployments["T5"] == "hs-t5-ft" and prod.llm.deployments["T3"] == "hs-t3-classifier"
        assert prod.graph.uncertain_low == D("0.55") and type(prod.graph.uncertain_low) is D
        assert prod.cors.origins == ["https://shop.example", "https://m.shop.example"]
        assert prod.lake.root == tmp_path.resolve() / "custom" / "lake"
        dev = load_settings("Development", env={}, overrides=root)
        assert (dev.environment, dev.is_development, dev.llm.mock_fault_every, dev.demo.enabled) == ("Development", True, 3, True)
        assert dev.graph.uncertain_low == D("0.55")
        via_env = load_settings(env={"HS_ENV": "development"}, overrides=root)
        assert via_env.is_development and via_env.demo.enabled
        assert load_settings("Staging", env={}, overrides=root).llm.mock_fault_every == 7  # a missing environment file is fine

    def test_environment_variables_and_overrides(self, tmp_path: Path) -> None:
        write(tmp_path / "config" / "settings.toml", """
            [scheduler]
            reco_finalizer_seconds = 10
            [llm]
            provider = "Mock"
            """)
        env = {
            "HS_LLM__PROVIDER": "AzureOpenAI",
            "hs_llm__endpoint": "https://example.openai.azure.com",
            "HS_LLM__API_KEY": "sk-test-secret",
            "HS_LLM__DEPLOYMENTS__t4": "judge-ft",
            "HS_LLM__TEMPERATURE": "-1",
            "HS_SCHEDULER__ENABLED": "false",
            "HS_SCHEDULER__RECO_FINALIZER_SECONDS": "5",
            "HS_RATELIMITS__BURSTMULTIPLIER": "3",
            "HS_GRAPH__UNCERTAIN_HIGH": "0.85",
            "HS_FEEDS__MAX_UPLOAD_BYTES": "1048576",
            "HS_CORS__ORIGINS": '["https://a.example"]',
            "HS_CACHE__REDIS": "",
            "HS_WEBHOOKS__ALLOW_HTTP_LOCALHOST": "1",
            "HS_LAKE__ROOT": str(tmp_path / "abs-lake"),
            "DATABASE_URL": "postgresql://u:p@db:5432/hs",
            "HS_TEST_DATABASE_URL": "not a setting",
            "PATH": "ignored",
        }
        s = load_settings(env=env, overrides={
            "repo_root": tmp_path, "scheduler.enabled": True, "llm.deployments.T3": "classifier-ft", "rate_limits": {"default_per_second": 7},
        })
        assert (s.llm.provider, s.llm.endpoint, s.llm.api_key, s.llm.temperature) == ("AzureOpenAI", "https://example.openai.azure.com", "sk-test-secret", -1.0)
        assert "sk-test-secret" not in repr(s)
        assert s.llm.deployments == {"T3": "classifier-ft", "T4": "judge-ft", "T5": "hs-t5-explainer", "T7": "hs-embeddings"}
        assert s.scheduler.enabled is True and s.scheduler.reco_finalizer_seconds == 5
        assert (s.rate_limits.burst_multiplier, s.rate_limits.default_per_second) == (3, 7)
        assert s.graph.uncertain_high == D("0.85") and s.feeds.max_upload_bytes == 1_048_576
        assert s.cors.origins == ["https://a.example"] and s.cache.redis is None and s.webhooks.allow_http_localhost is True
        assert s.lake.root == tmp_path / "abs-lake"
        assert s.database.url == "postgresql://u:p@db:5432/hs"
        assert load_settings(env={**env, "HS_DATABASE__URL": "postgresql://specific"}, overrides={"repo_root": tmp_path}).database.url == "postgresql://specific"
        assert load_settings(env={"HS_CORS__ORIGINS": "https://a.example, https://b.example"}, overrides={"repo_root": tmp_path}).cors.origins == [
            "https://a.example", "https://b.example",
        ]

    def test_repo_root_from_environment(self, tmp_path: Path) -> None:
        s = load_settings(env={"HEALTHSWAP_REPO_ROOT": str(tmp_path)})
        assert s.repo_root == tmp_path.resolve() and s.feeds.storage_root == tmp_path.resolve() / ".data" / "feeds"

    def test_repository_config_files(self, root: Path) -> None:
        dev = load_settings("Development", env={})
        assert dev.repo_root == root
        assert dev.database.url == "postgresql://postgres:postgres@127.0.0.1:55432/postgres?sslmode=disable&connect_timeout=30"
        assert (dev.llm.mock_fault_every, dev.webhooks.allow_http_localhost, dev.demo.enabled) == (7, True, True)
        prod = load_settings("Production", env={})
        assert (prod.llm.mock_fault_every, prod.webhooks.allow_http_localhost, prod.demo.enabled) == (7, False, False)

    @pytest.mark.parametrize(
        ("env", "overrides", "message"),
        [
            ({"HS_LLM__PROVDER": "x"}, {}, "llm.PROVDER"),
            ({"HS_NOSUCH__KEY": "x"}, {}, "NOSUCH.KEY"),
            ({"HS_SCHEDULER__ENABLED__X": "x"}, {}, "scheduler.enabled.X"),
            ({}, {"scheduler.enabeld": False}, "scheduler.enabeld"),
            ({}, {"nosuch": 1}, "nosuch"),
            ({}, {"cors": {"origin": ["x"]}}, "cors.origin"),
        ],
    )
    def test_unknown_keys_are_errors(self, tmp_path: Path, env: dict[str, str], overrides: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match="Unknown setting '" + re.escape(message) + "'"):
            load_settings(env=env, overrides={"repo_root": tmp_path, **overrides})

    def test_unknown_toml_keys_are_errors(self, tmp_path: Path) -> None:
        write(tmp_path / "config" / "settings.toml", "[llm]\nprovder = \"x\"\n")
        with pytest.raises(ValueError, match=r"Unknown setting 'llm\.provder' \(.*settings\.toml\)"):
            load_settings(env={}, overrides={"repo_root": tmp_path})
        write(tmp_path / "config" / "settings.toml", "debug = true\n")
        with pytest.raises(ValueError, match="Unknown setting 'debug'"):
            load_settings(env={}, overrides={"repo_root": tmp_path})

    def test_secrets_are_rejected_in_toml(self, tmp_path: Path) -> None:
        write(tmp_path / "config" / "settings.development.toml", "[llm]\napi_key = \"sk-leaked\"\n")
        with pytest.raises(ValueError, match="secret.*HS_LLM__API_KEY") as e:
            load_settings("Development", env={}, overrides={"repo_root": tmp_path})
        assert "sk-leaked" not in str(e.value)

    @pytest.mark.parametrize(
        ("env", "overrides", "message"),
        [
            ({"HS_SCHEDULER__ENABLED": "maybe"}, {}, "scheduler.enabled"),
            ({"HS_EVALUATION__INTERNAL_BUDGET_MS": "fast"}, {}, "evaluation.internal_budget_ms"),
            ({"HS_GRAPH__UNCERTAIN_LOW": "NaN"}, {}, "graph.uncertain_low"),
            ({"HS_LLM__DEPLOYMENTS": "[1]"}, {}, "llm.deployments"),
            ({}, {"llm.provider": None}, "llm.provider"),
            ({}, {"rate_limits.enabled": 2}, "rate_limits.enabled"),
            ({}, {"evaluation.internal_budget_ms": True}, "evaluation.internal_budget_ms"),
        ],
    )
    def test_invalid_values_are_errors(self, tmp_path: Path, env: dict[str, str], overrides: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match="Invalid value .* for setting '" + re.escape(message) + "'"):
            load_settings(env=env, overrides={"repo_root": tmp_path, **overrides})

    def test_secret_values_are_not_echoed_and_environment_names_are_validated(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"Invalid value \*\*\* for setting 'llm.api_key'"):
            load_settings(env={}, overrides={"repo_root": tmp_path, "llm.api_key": 12345})
        with pytest.raises(ValueError, match="Invalid environment name"):
            load_settings("../secrets", env={}, overrides={"repo_root": tmp_path})


# ---------------------------------------------------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------------------------------------------------


class Recorder:
    def __init__(self, *topics: str) -> None:
        self.topics = topics
        self.messages: list[BusMessage] = []

    def handle(self, message: BusMessage) -> None:
        self.messages.append(message)


class Failing:
    topics = ("*",)

    def handle(self, message: BusMessage) -> None:
        raise RuntimeError("consumer bug")


def message(outbox_id: int, topic: str, key: str, payload: Any, partner: uuid.UUID | None = PARTNER) -> BusMessage:
    return BusMessage(outbox_id, topic, key, payload, partner, datetime(2026, 11, 23, 18, 30, 5, 123_000, tzinfo=UTC))


class TestEventBus:
    def test_publish_subscribe_and_lake(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        orders, late = Recorder(Topics.ORDERS), Recorder(Topics.KB_PRODUCT_CHANGES, Topics.ORDERS)
        lake = LakeSink(LakeOptions(root=tmp_path / "lake"))
        bus = InProcessEventBus([Failing(), orders, lake])
        bus.start()
        try:
            bus.publish(message(1, Topics.ORDERS, "o-1", {"orderId": "o-1", "total": D("12.50"), "coupon": None}))
            bus.publish(message(2, Topics.KB_PRODUCT_CHANGES, "p-1", {"productId": "p-1"}, partner=None))
            assert bus.wait_idle(5)
            bus.subscribe(late)
            bus.publish(message(3, Topics.ORDERS, "o-2", {"orderId": "o-2"}))
            bus.publish(message(4, Topics.BILLING, "b-1", {}))
            assert bus.wait_idle(5) and bus.pending == 0
        finally:
            assert bus.stop(5)
        assert [m.key for m in orders.messages] == ["o-1", "o-2"]
        assert [m.key for m in late.messages] == ["o-2"]
        assert caplog.text.count("Consumer Failing failed") == 4

        order_lines = (tmp_path / "lake" / "bronze" / Topics.ORDERS / "dt=2026-11-23" / "part-0000.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(order_lines[0])
        assert len(order_lines) == 2
        assert first == {
            "outboxId": 1, "topic": "hs.orders.v1", "key": "o-1", "partnerId": str(PARTNER), "createdAt": "2026-11-23T18:30:05.123000Z",
            "payload": {"orderId": "o-1", "total": 12.5, "coupon": None},
        }
        kb_line = (tmp_path / "lake" / "bronze" / Topics.KB_PRODUCT_CHANGES / "dt=2026-11-23" / "part-0000.jsonl").read_text(encoding="utf-8")
        assert "partnerId" not in json.loads(kb_line)

    def test_dispatch_and_disabled_lake(self, tmp_path: Path) -> None:
        rec = Recorder("*")
        bus = InProcessEventBus([rec, LakeSink(LakeOptions(enabled=False, root=tmp_path))])
        bus.dispatch(message(1, Topics.BILLING, "k", {}))
        assert [m.key for m in rec.messages] == ["k"]
        assert not any(tmp_path.iterdir())
        bus.unsubscribe(rec)
        bus.dispatch(message(2, Topics.BILLING, "k2", {}))
        assert len(rec.messages) == 1

    def test_publish_fails_fast_once_the_bus_stops_with_a_full_queue(self) -> None:
        bus = InProcessEventBus(capacity=1)
        bus.publish(message(1, Topics.ORDERS, "a", {}))
        bus.stopping.set()
        with pytest.raises(RuntimeError, match="stopping"):
            bus.publish(message(2, Topics.ORDERS, "b", {}))

    def test_outbox_enqueue(self) -> None:
        u = FakeUow()
        Outbox.enqueue(u, PARTNER, Topics.ORDERS, "o-1", {"orderId": "o-1", "total": D("12.50"), "coupon": None, "at": datetime(2026, 11, 23, tzinfo=UTC)})
        sql, params = u.calls[0]
        assert sql == "insert into txn.outbox (partner_id, topic, msg_key, payload) values (@partnerId, @topic, @key, @payload::jsonb)"
        assert params == {
            "partnerId": PARTNER, "topic": "hs.orders.v1", "key": "o-1",
            "payload": '{"orderId":"o-1","total":12.5,"at":"2026-11-23T00:00:00.000000Z"}',
        }

    def test_outbox_relay_pump(self) -> None:
        created = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
        u = FakeUow(rows=[
            {"outbox_id": 7, "partner_id": PARTNER, "topic": Topics.ORDERS, "msg_key": "o-1", "payload": '{"orderId": "o-1", "total": 12.50}', "created_at": created},
            {"outbox_id": 8, "partner_id": None, "topic": Topics.KB_PRODUCT_CHANGES, "msg_key": "p", "payload": "{}", "created_at": created},
        ])
        db = FakeDb(u)
        bus = InProcessEventBus()
        assert OutboxRelay(db, bus).pump() == 2
        assert db.roles == [(DbRole.OPS, None)]
        assert "for update skip locked" in u.calls[0][0]
        assert u.calls[1] == ("update txn.outbox set published_at = now() where outbox_id = any(@ids)", {"ids": [7, 8]})
        assert bus.pending == 2
        rec = Recorder("*")
        bus.subscribe(rec)
        bus.start()
        try:
            assert bus.wait_idle(5)
        finally:
            bus.stop(5)
        assert [(m.outbox_id, m.payload, m.partner_id, m.created_at) for m in rec.messages] == [
            (7, {"orderId": "o-1", "total": D("12.50")}, PARTNER, created), (8, {}, None, created),
        ]
        empty = FakeUow(rows=[])
        assert OutboxRelay(FakeDb(empty), bus).pump() == 0
        assert len(empty.calls) == 1


class FakeUow:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> int:
        self.calls.append((sql, params))
        return 1

    def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        return self.rows


class FakeDb:
    def __init__(self, uow: Any) -> None:
        self.uow = uow
        self.roles: list[tuple[DbRole, Any]] = []

    @contextmanager
    def tx(self, role: DbRole, partner_id: uuid.UUID | None = None) -> Iterator[Any]:
        self.roles.append((role, partner_id))
        yield self.uow


# ---------------------------------------------------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------------------------------------------------


class Ticker(BackgroundWorker):
    def __init__(self) -> None:
        super().__init__()
        self.ticks = 0
        self.started = threading.Event()

    def execute(self) -> None:
        self.started.set()
        while self.sleep(0.01):
            self.ticks += 1


class Flaky(BackgroundWorker):
    restart_delay_seconds = 0.01

    def __init__(self) -> None:
        super().__init__("flaky-worker")
        self.runs = 0
        self.recovered = threading.Event()

    def execute(self) -> None:
        self.runs += 1
        if self.runs == 1:
            raise RuntimeError("boom")
        self.recovered.set()
        while self.sleep(0.01):
            pass


class Drainer(BackgroundWorker):
    def __init__(self) -> None:
        super().__init__()
        self.inbox: queue.Queue[int] = queue.Queue()
        self.seen: list[int] = []

    def execute(self) -> None:
        for item in self.read_all(self.inbox, poll_seconds=0.01):
            if item < 0:
                raise ValueError("negative")
            self.seen.append(item)


class TestBackgroundWorker:
    def test_start_stop_restart(self) -> None:
        w = Ticker()
        assert not w.is_running and w.stop(1)  # stopping a worker that never started is fine
        w.start()
        assert w.started.wait(2) and w.is_running
        thread = w._thread
        w.start()  # already running: no second thread
        assert w._thread is thread and thread is not None and thread.daemon and thread.name == "Ticker"
        assert w.stop(2) and not w.is_running and w.stopping.is_set()
        w.started.clear()
        w.start()
        assert w.started.wait(2) and not w.stopping.is_set()
        assert w.stop(2)

    def test_sleep_returns_false_when_stopping(self) -> None:
        w = Ticker()
        assert w.sleep(0) is True
        w.stopping.set()
        t0 = time.monotonic()
        assert w.sleep(30) is False
        assert time.monotonic() - t0 < 1

    def test_failure_is_logged_and_the_loop_restarts(self, caplog: pytest.LogCaptureFixture) -> None:
        w = Flaky()
        with caplog.at_level(logging.ERROR):
            w.start()
            assert w.recovered.wait(2)
            assert w.stop(2)
        assert w.runs == 2
        assert "flaky-worker failed; restarting" in caplog.text and "boom" in caplog.text

    def test_returning_from_execute_ends_the_thread(self) -> None:
        class Once(BackgroundWorker):
            def execute(self) -> None:
                return

        w = Once()
        w.start()
        assert w._thread is not None
        w._thread.join(2)
        assert not w.is_running and not w.stopping.is_set()

    def test_read_all_marks_items_done_even_when_handling_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        w = Drainer()
        w.restart_delay_seconds = 0.01
        w.start()
        try:
            for item in (1, 2, -1, 3):
                w.inbox.put(item)
            deadline = time.monotonic() + 5
            while w.inbox.unfinished_tasks and time.monotonic() < deadline:
                time.sleep(0.01)
            assert w.inbox.unfinished_tasks == 0
        finally:
            assert w.stop(2)
        assert w.seen == [1, 2, 3]
        assert "negative" in caplog.text


# ---------------------------------------------------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------------------------------------------------


def partner_row(pid: uuid.UUID, name: str, status: str = "LIVE", config: str = '{"currency": "USD", "markets": ["US"], "fee": {"rate": 0.12}}') -> dict[str, Any]:
    return {"partner_id": pid, "display_name": name, "geography": "US", "markets": ["US"], "status": status, "config": config}


class TenantDb:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[DbRole, Any]] = []

    @contextmanager
    def tx(self, role: DbRole, partner_id: uuid.UUID | None = None) -> Iterator[Any]:
        self.calls.append((role, partner_id))
        rows = self.rows

        class U:
            def one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
                assert "from tenant.partner where partner_id = @partnerId" in sql
                return next((dict(r) for r in rows if r["partner_id"] == params["partnerId"]), None)

            def query(self, sql: str) -> list[dict[str, Any]]:
                assert sql.endswith("from tenant.partner order by display_name")
                return sorted((dict(r) for r in rows), key=lambda r: r["display_name"])

        yield U()


class TestTenancy:
    def test_partner_context(self) -> None:
        ctx = PartnerContext(partner_id=PARTNER, client_id="qb-server", scopes=frozenset({"carts.evaluate"}))
        assert ctx.is_server and not ctx.is_sdk_session and "carts.evaluate" in ctx.scopes
        sdk = replace(ctx, is_sdk_session=True, sdk_user_ref="u1", sdk_cart_ref="c1", sdk_store_id="BLR-01", sdk_market="IN")
        assert not sdk.is_server
        assert PartnerContext().partner_id == uuid.UUID(int=0) and PartnerContext().scopes == frozenset()

    def test_directory_caches_until_invalidated_or_expired(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        db = TenantDb([partner_row(a, "Zeta"), partner_row(b, "Alpha", config="null")])
        d = PartnerDirectory(db)
        info = d.get(a)
        assert (info.partner_id, info.display_name, info.geography, info.markets, info.status) == (a, "Zeta", "US", ("US",), "LIVE")
        assert info.config.currency == "USD" and info.config.fee.rate == D("0.12") and info.config.markets == ("US",)
        assert info.config_json == '{"currency": "USD", "markets": ["US"], "fee": {"rate": 0.12}}'
        assert db.calls == [(DbRole.APP, a)]
        db.rows[0]["status"] = "SUSPENDED"
        assert d.get(str(a)) is info and len(db.calls) == 1  # cached for 15 s
        d.invalidate(a)
        assert d.get(a).status == "SUSPENDED" and len(db.calls) == 2
        d.ttl = timedelta(0)
        d.get(a)
        assert len(db.calls) == 3
        assert d.get(b).config == PartnerConfig()  # JSON null -> defaults
        assert [p.display_name for p in d.list_all()] == ["Alpha", "Zeta"] and db.calls[-1] == (DbRole.OPS, None)
        assert PartnerDirectory.ttl == PartnerDirectory.TTL == timedelta(seconds=15)

    def test_unknown_partner_is_unauthorized(self) -> None:
        with pytest.raises(HsError) as e:
            PartnerDirectory(TenantDb([])).get(uuid.uuid4())
        assert (e.value.status, e.value.code, e.value.title) == (401, "HS-AUTH-INVALID", "Unknown partner")

    def test_parse_config(self) -> None:
        assert PartnerDirectory.parse_config(None) == PartnerConfig()
        assert PartnerDirectory.parse_config('{"fee": {"recommendationAdd": {"amount": "2.00"}}}').fee.recommendation_add.amount == D("2.00")
        assert PartnerDirectory.parse_config({"killSwitch": True}).kill_switch is True
        with pytest.raises(ValueError):
            PartnerDirectory.parse_config('{"taxRate": "abc"}')


# ---------------------------------------------------------------------------------------------------------------------
# Integration (PostgreSQL): a fresh dev database of this module's own (the `fresh_database_url` fixture in tests/conftest.py).
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_db(fresh_database_url: str) -> Iterator[Db]:
    db = Db(DbOptions(url=fresh_database_url, max_pool_size=4))
    db.wait_ready(30)
    yield db
    db.close()


@pytest.mark.integration
def test_partner_directory_on_postgres(test_db: Db) -> None:
    pid = new_id()
    with test_db.tx(DbRole.OPS) as u:
        u.execute(
            "insert into tenant.partner (partner_id, display_name, legal_name, geography, markets, status, config) "
            "values (@id, @name, @name, 'IN', @markets, 'SANDBOX', @config::jsonb)",
            {"id": pid, "name": f"zz-test-{pid}", "markets": ["IN"], "config": '{"currency": "INR", "fee": {"rate": 0.12, "caps": {"perLine": "40.00"}}}'},
        )
    try:
        d = PartnerDirectory(test_db)
        info = d.get(pid)
        assert (info.display_name, info.markets, info.status) == (f"zz-test-{pid}", ("IN",), "SANDBOX")
        assert info.config.fee.rate == D("0.12") and info.config.fee.caps.per_line == D("40.00")
        assert json.loads(info.config_json)["fee"]["caps"]["perLine"] == "40.00"
        assert any(p.partner_id == pid for p in d.list_all())
        with test_db.tx(DbRole.OPS) as u:
            u.execute("update tenant.partner set status = 'LIVE' where partner_id = @id", {"id": pid})
        assert d.get(pid).status == "SANDBOX"
        d.invalidate(pid)
        assert d.get(pid).status == "LIVE"
        with pytest.raises(HsError):
            d.get(new_id())
    finally:
        with test_db.tx(DbRole.OPS) as u:
            u.execute("delete from tenant.partner where partner_id = @id", {"id": pid})


@pytest.mark.integration
def test_outbox_relay_on_postgres(test_db: Db, tmp_path: Path) -> None:
    partner, key = new_id(), f"it-{new_id()}"
    with test_db.tx(DbRole.APP, partner) as u:
        Outbox.enqueue(u, partner, Topics.ORDERS, key, {"orderId": key, "total": D("12.50"), "coupon": None})
    with test_db.tx(DbRole.KB_WRITER) as u:
        Outbox.enqueue(u, None, Topics.KB_PRODUCT_CHANGES, key, {"productId": key})
    rec = Recorder(Topics.ORDERS, Topics.KB_PRODUCT_CHANGES)
    bus = InProcessEventBus([rec, LakeSink(LakeOptions(root=tmp_path))])
    relay = OutboxRelay(test_db, bus)
    bus.start()
    relay.start()
    try:
        deadline = time.monotonic() + 10
        while sum(m.key == key for m in rec.messages) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        assert relay.stop(5) and bus.stop(5)
    mine = [m for m in rec.messages if m.key == key]
    assert [(m.topic, m.partner_id, m.payload) for m in mine] == [
        (Topics.ORDERS, partner, {"orderId": key, "total": D("12.5")}), (Topics.KB_PRODUCT_CHANGES, None, {"productId": key}),
    ]
    assert mine[0].outbox_id < mine[1].outbox_id and mine[0].created_at.tzinfo is not None
    with test_db.tx(DbRole.OPS) as u:
        assert u.scalar("select count(*) from txn.outbox where msg_key = @key and published_at is null", {"key": key}) == 0
        u.execute("delete from txn.outbox where msg_key = @key", {"key": key})
    assert any((tmp_path / "bronze" / Topics.ORDERS).rglob("part-0000.jsonl"))
