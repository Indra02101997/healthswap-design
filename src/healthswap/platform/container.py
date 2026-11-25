"""PlatformRegistration.cs ported: the shared composition root for the API, the CLI and the integration tests.

`Container(settings)` builds every singleton lazily on first access (one instance per container, thread-safe) and wires it
exactly like the C# service collection. Properties are the snake_case of the C# registered type (`catalog_service`,
`in_process_event_bus`, ...) with the short aliases `refs`, `models`, `partners`, `bus`, `data_protection` and `clock`.

    with Container(load_settings()) as c:
        c.start_workers()                       # C# AddHealthSwapWorkers
        c.catalog_service.upsert(...)
    # close(): stops the workers and closes the database pool, HTTP clients, Redis and the ONNX sessions

`start_workers` runs the C# hosted services in registration order: the in-process bus, the outbox relay, the evaluation
recorder, the webhook dispatcher (include_webhooks), the finalizers (include_scheduler and scheduler.enabled) and the cache
warmer (include_cache_warmer); `stop_workers` stops them in reverse order like IHost.
"""

from __future__ import annotations

import functools
import logging
import threading
from pathlib import Path
from typing import Any

import httpx

from healthswap.domain.primitives import Clock, OffsetClock
from healthswap.platform.bus import IBusConsumer, InProcessEventBus, LakeSink, OutboxRelay
from healthswap.platform.cache import ICacheStore, RedisCacheStore, create_cache_store
from healthswap.platform.db import Db
from healthswap.platform.hot_cache import HotCache
from healthswap.platform.knowledge.ingestor import ProductIngestor
from healthswap.platform.knowledge.jobs import ExplanationPregen, ScoringJob, SwapGraphBuilder
from healthswap.platform.knowledge.kb_reader import KbReader, RecallIngestor
from healthswap.platform.knowledge.reference_data import ReferenceDataLoader, ReferenceDataRepository
from healthswap.platform.llm.gateway import AzureOpenAiProvider, LlmGateway, LlmProvider
from healthswap.platform.llm.mock import MockLlmProvider
from healthswap.platform.ml.registry import ModelRegistry
from healthswap.platform.seeding.demo_seeder import DemoSeeder
from healthswap.platform.seeding.ml_data import MlData
from healthswap.platform.services.billing import BillingService
from healthswap.platform.services.catalog import (
    CatalogEnrichment,
    CatalogService,
    FeedService,
    InventoryService,
    PreferenceService,
    PrivacyService,
)
from healthswap.platform.services.evaluation import EvaluationRecorder, EvaluationService
from healthswap.platform.services.events import EventService, FinalizerWorker, RecoChargeService, SuggestionStore
from healthswap.platform.services.infrastructure import (
    DataProtectionProvider,
    IdempotencyService,
    LedgerService,
    SecretProtector,
    WebhookDispatcher,
    WebhookFanoutConsumer,
)
from healthswap.platform.services.orders import AttributionEngine, OrderService
from healthswap.platform.services.read import CacheWarmer, ProductService, TransparencyService
from healthswap.platform.settings import Settings
from healthswap.platform.tenancy import PartnerDirectory
from healthswap.platform.workers import BackgroundWorker

log = logging.getLogger(__name__)

_MISSING = object()


class service(functools.cached_property):  # noqa: N801 - used like the decorator it extends
    """functools.cached_property that builds each service once per container under the container's re-entrant lock
    (Python 3.12+ cached_property has no lock; services resolve their dependencies recursively on the same thread)."""

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        cache = instance.__dict__
        value = cache.get(self.attrname, _MISSING)
        if value is _MISSING:
            with instance._lock:
                value = cache.get(self.attrname, _MISSING)
                if value is _MISSING:
                    value = self.func(instance)
                    cache[self.attrname] = value
        return value


class Container:
    """Composition root (C# AddHealthSwapPlatform + AddHealthSwapWorkers)."""

    # C# named HttpClient timeouts: "azure-openai" 60 s, "webhooks" 15 s (each delivery is also cut at webhooks.timeout_seconds).
    LLM_HTTP_TIMEOUT_SECONDS = 60.0
    WEBHOOK_HTTP_TIMEOUT_SECONDS = 15.0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._workers: list[BackgroundWorker] = []
        self._closed = False

    def override(self, name: str, value: Any) -> None:
        """Supplies a service instead of building it (tests, the API's demo webhook transport). Call it before the
        service (or anything depending on it) is first resolved; `close()` releases an overridden HTTP client too."""
        if not isinstance(getattr(type(self), name, None), service):
            raise AttributeError(f"Container has no service named {name!r}")
        with self._lock:
            if name in self.__dict__:
                raise RuntimeError(f"The {name} service has already been built")
            self.__dict__[name] = value

    # ---- configuration and infrastructure ---------------------------------------------------------------------------

    @property
    def repo_root(self) -> Path:
        return self.settings.repo_root

    @service
    def offset_clock(self) -> OffsetClock:
        return OffsetClock()

    @property
    def clock(self) -> Clock:
        """IClock: the same OffsetClock instance (C# registers it as both OffsetClock and IClock)."""
        return self.offset_clock

    @service
    def db(self) -> Db:
        return Db(self.settings.database)

    @service
    def cache_store(self) -> ICacheStore:
        """RedisCacheStore when cache.redis is configured, else the in-process MemoryCacheStore."""
        return create_cache_store(self.settings.cache)

    @service
    def hot_cache(self) -> HotCache:
        return HotCache(self.cache_store)

    @service
    def data_protection_provider(self) -> DataProtectionProvider:
        """IDataProtectionProvider (application name HealthSwap, keys in data_protection.keys_directory)."""
        return DataProtectionProvider(self.settings.data_protection.keys_directory)

    @property
    def data_protection(self) -> DataProtectionProvider:
        return self.data_protection_provider

    # ---- knowledge plane --------------------------------------------------------------------------------------------

    @service
    def partner_directory(self) -> PartnerDirectory:
        return PartnerDirectory(self.db)

    @property
    def partners(self) -> PartnerDirectory:
        return self.partner_directory

    @service
    def reference_data_repository(self) -> ReferenceDataRepository:
        return ReferenceDataRepository(self.db)

    @property
    def refs(self) -> ReferenceDataRepository:
        return self.reference_data_repository

    @service
    def reference_data_loader(self) -> ReferenceDataLoader:
        return ReferenceDataLoader(self.db)

    @service
    def kb_reader(self) -> KbReader:
        return KbReader(self.db, self.refs)

    @service
    def recall_ingestor(self) -> RecallIngestor:
        return RecallIngestor(self.db)

    @service
    def model_registry(self) -> ModelRegistry:
        return ModelRegistry(self.settings.models)

    @property
    def models(self) -> ModelRegistry:
        return self.model_registry

    @service
    def llm_http_client(self) -> httpx.Client:
        """The "azure-openai" named HttpClient (60 s timeout)."""
        return httpx.Client(timeout=self.LLM_HTTP_TIMEOUT_SECONDS)

    @service
    def llm_provider(self) -> LlmProvider:
        """ILlmProvider: AzureOpenAI (case-insensitive) or the deterministic offline mock."""
        if self.settings.llm.provider.upper() == "AZUREOPENAI":
            return AzureOpenAiProvider(self.llm_http_client, self.settings.llm)
        return MockLlmProvider(self.settings.llm)

    @service
    def llm_gateway(self) -> LlmGateway:
        return LlmGateway(self.llm_provider, self.settings.llm, self.db)

    @service
    def product_ingestor(self) -> ProductIngestor:
        return ProductIngestor(self.db, self.refs, self.models, self.llm_gateway, self.clock, self.settings.lake)

    @service
    def scoring_job(self) -> ScoringJob:
        return ScoringJob(self.db, self.kb_reader, self.refs, self.hot_cache, self.clock)

    @service
    def swap_graph_builder(self) -> SwapGraphBuilder:
        return SwapGraphBuilder(self.db, self.refs, self.models, self.llm_gateway, self.hot_cache, self.settings.graph, self.clock)

    @service
    def explanation_pregen(self) -> ExplanationPregen:
        return ExplanationPregen(self.db, self.refs, self.llm_gateway, self.hot_cache, self.clock)

    # ---- services ---------------------------------------------------------------------------------------------------

    @service
    def secret_protector(self) -> SecretProtector:
        return SecretProtector(self.data_protection_provider)

    @service
    def ledger_service(self) -> LedgerService:
        return LedgerService(self.clock)

    @service
    def idempotency_service(self) -> IdempotencyService:
        return IdempotencyService(self.db, self.clock)

    @service
    def catalog_service(self) -> CatalogService:
        return CatalogService(self.db, self.hot_cache, self.partners, self.models, self.clock)

    @service
    def catalog_enrichment(self) -> CatalogEnrichment:
        return CatalogEnrichment(self.db, self.product_ingestor, self.scoring_job, self.catalog_service, self.clock)

    @service
    def feed_service(self) -> FeedService:
        return FeedService(self.db, self.catalog_service, self.catalog_enrichment, self.data_protection_provider, self.settings.feeds, self.clock)

    @service
    def inventory_service(self) -> InventoryService:
        return InventoryService(self.db, self.hot_cache, self.partners)

    @service
    def preference_service(self) -> PreferenceService:
        return PreferenceService(self.db, self.hot_cache, self.clock)

    @service
    def privacy_service(self) -> PrivacyService:
        return PrivacyService(self.db, self.hot_cache, self.clock)

    @service
    def product_service(self) -> ProductService:
        return ProductService(self.hot_cache, self.partners)

    @service
    def transparency_service(self) -> TransparencyService:
        return TransparencyService(self.db, self.refs, self.hot_cache, self.clock)

    @service
    def cache_warmer(self) -> CacheWarmer:
        return CacheWarmer(self.db, self.hot_cache, self.refs, self.partners)

    @service
    def demo_seeder(self) -> DemoSeeder:
        return DemoSeeder(
            self.db, self.reference_data_loader, self.refs, self.product_ingestor, self.recall_ingestor, self.scoring_job, self.swap_graph_builder,
            self.explanation_pregen, self.catalog_service, self.catalog_enrichment, self.inventory_service, self.secret_protector, self.partners, self.clock,
        )

    @service
    def evaluation_recorder(self) -> EvaluationRecorder:
        return EvaluationRecorder(self.db)

    @service
    def evaluation_service(self) -> EvaluationService:
        return EvaluationService(
            self.hot_cache, self.partners, self.refs, self.preference_service, self.models, self.evaluation_recorder, self.settings.evaluation, self.clock,
        )

    @service
    def suggestion_store(self) -> SuggestionStore:
        return SuggestionStore(self.db, self.hot_cache)

    @service
    def reco_charge_service(self) -> RecoChargeService:
        return RecoChargeService(self.db, self.ledger_service, self.clock)

    @service
    def event_service(self) -> EventService:
        return EventService(self.db, self.hot_cache, self.suggestion_store, self.reco_charge_service, self.partners, self.refs, self.clock)

    @service
    def finalizer_worker(self) -> FinalizerWorker:
        """RA-1 charge finalizer + FP-1 return-window finalizer (attribution_engine)."""
        return FinalizerWorker(self.reco_charge_service, self.attribution_engine, self.settings.scheduler)

    # ---- orders, attribution, billing, ML data (OrderServices.cs, BillingService.cs, Seeding/MlData.cs) -------------

    @service
    def attribution_engine(self) -> AttributionEngine:
        return AttributionEngine(self.db, self.ledger_service, self.clock)

    @service
    def order_service(self) -> OrderService:
        return OrderService(self.db, self.hot_cache, self.partners, self.suggestion_store, self.attribution_engine, self.clock)

    @service
    def billing_service(self) -> BillingService:
        return BillingService(self.db, self.ledger_service, self.partners, self.clock)

    @service
    def ml_data(self) -> MlData:
        return MlData(self.db, self.refs, self.hot_cache, self.evaluation_service, self.explanation_pregen, self.llm_gateway, self.partners)

    # ---- bus, webhooks ----------------------------------------------------------------------------------------------

    @service
    def webhook_fanout_consumer(self) -> WebhookFanoutConsumer:
        return WebhookFanoutConsumer(self.db)

    @service
    def lake_sink(self) -> LakeSink:
        return LakeSink(self.settings.lake)

    @service
    def bus_consumers(self) -> tuple[IBusConsumer, ...]:
        """IEnumerable<IBusConsumer> in registration order: webhook fan-out, then the lake sink."""
        return (self.webhook_fanout_consumer, self.lake_sink)

    @service
    def in_process_event_bus(self) -> InProcessEventBus:
        return InProcessEventBus(self.bus_consumers)

    @property
    def bus(self) -> InProcessEventBus:
        return self.in_process_event_bus

    @service
    def outbox_relay(self) -> OutboxRelay:
        return OutboxRelay(self.db, self.bus)

    @service
    def webhook_http_client(self) -> httpx.Client:
        """The "webhooks" named HttpClient: no automatic redirects (SSRF), 15 s timeout."""
        return httpx.Client(follow_redirects=False, timeout=self.WEBHOOK_HTTP_TIMEOUT_SECONDS)

    @service
    def webhook_dispatcher(self) -> WebhookDispatcher:
        return WebhookDispatcher(self.db, self.webhook_http_client, self.secret_protector, self.settings.webhooks)

    # ---- workers and lifetime ---------------------------------------------------------------------------------------

    @property
    def workers(self) -> tuple[BackgroundWorker, ...]:
        """The workers started by start_workers, in start order."""
        return tuple(self._workers)

    def start_workers(self, include_scheduler: bool = True, include_cache_warmer: bool = True, include_webhooks: bool = True) -> None:
        """C# AddHealthSwapWorkers: starts the hosted services in registration order (idempotent)."""
        if self._closed:
            raise RuntimeError("The container is closed")
        workers: list[BackgroundWorker] = [self.bus, self.outbox_relay, self.evaluation_recorder]
        if include_webhooks:
            workers.append(self.webhook_dispatcher)
        if include_scheduler and self.settings.scheduler.enabled:
            workers.append(self.finalizer_worker)
        if include_cache_warmer:
            workers.append(self.cache_warmer)
        with self._lock:
            for w in workers:
                if w not in self._workers:
                    w.start()
                    self._workers.append(w)

    def stop_workers(self, timeout: float = 10.0) -> None:
        """Stops the started workers in reverse order (IHost.StopAsync)."""
        with self._lock:
            workers, self._workers = self._workers[::-1], []
        for w in workers:
            if not w.stop(timeout):
                log.warning("%s did not stop within %.0f s", w.name, timeout)

    def close(self) -> None:
        """Stops the workers and releases what was created (database pool, HTTP clients, Redis, ONNX sessions). Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.stop_workers()
        built = self.__dict__
        for name in ("webhook_http_client", "llm_http_client"):
            if name in built:
                built[name].close()
        provider = built.get("llm_provider")
        if provider is not None and hasattr(provider, "close"):
            provider.close()
        if "model_registry" in built:
            built["model_registry"].close()
        store = built.get("cache_store")
        if isinstance(store, RedisCacheStore):
            store.client.close()
        if "db" in built:
            built["db"].close()

    def __enter__(self) -> Container:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
