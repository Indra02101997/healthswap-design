"""Event bus (C# Bus/EventBus.cs): topics, the transactional outbox, the outbox relay, the in-process bus and the lake sink.

Producers write events in the same transaction as the state change:

    Outbox.enqueue(u, partner_id, Topics.ORDERS, order_id, {"orderId": order_id, "status": "PLACED"})

`OutboxRelay` (a BackgroundWorker) publishes unpublished rows to `InProcessEventBus` (a BackgroundWorker and the
Kafka-API stand-in for local development), which hands every message, in order and at least once, to each consumer whose
`topics` contain the message topic or "*". A consumer is any object with a `topics` collection and a
`handle(message: BusMessage)` method (IBusConsumer); register it with `InProcessEventBus(consumers)` or `bus.subscribe(c)`.
Consumers dedupe by key (at-least-once delivery) and must not raise for messages they ignore.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from healthswap.domain.primitives import as_utc
from healthswap.platform import jsonutil
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.settings import LakeOptions
from healthswap.platform.workers import BackgroundWorker


class Topics:
    CART_EVALUATIONS = "hs.cart-evaluations.v1"
    SUGGESTION_EVENTS = "hs.suggestion-events.v1"
    ORDERS = "hs.orders.v1"
    ATTRIBUTION = "hs.attribution.v1"
    RECO_CHARGES = "hs.reco-charges.v1"
    CATALOG_CHANGES = "hs.catalog-changes.v1"
    INVENTORY_UPDATES = "hs.inventory-updates.v1"
    KB_PRODUCT_CHANGES = "hs.kb-product-changes.v1"
    BILLING = "hs.billing.v1"


@dataclass(frozen=True, slots=True)
class BusMessage:
    outbox_id: int
    topic: str
    key: str
    # Parsed JSON payload (C# JsonElement): the producer's camelCase keys; numbers are int or Decimal (jsonutil.loads).
    payload: Any
    partner_id: UUID | None
    created_at: datetime


class IBusConsumer(Protocol):
    """Bus consumer: `topics` holds topic names or "*" (every topic)."""

    topics: Collection[str]

    def handle(self, message: BusMessage) -> None: ...


BusConsumer = IBusConsumer


class Outbox:
    """Writes domain events in the same transaction as the state change (transactional outbox)."""

    @staticmethod
    def enqueue(u: Uow, partner_id: UUID | None, topic: str, key: str, payload: Any) -> None:
        """Inserts into txn.outbox; `payload` is serialized like the API (jsonutil.dumps: camelCase dataclasses, nulls dropped)."""
        u.execute(
            "insert into txn.outbox (partner_id, topic, msg_key, payload) values (@partnerId, @topic, @key, @payload::jsonb)",
            {"partnerId": partner_id, "topic": topic, "key": key, "payload": jsonutil.dumps(payload)},
        )


class InProcessEventBus(BackgroundWorker):
    """
    Kafka-API stand-in for local development: an in-process bus fed by the outbox relay. Production replaces the relay's
    publish step with a Kafka/Event Hubs producer; consumers keep the same contract (at-least-once, dedupe by key).
    """

    def __init__(self, consumers: Iterable[IBusConsumer] = (), capacity: int = 10_000) -> None:
        super().__init__()
        self._queue: queue.Queue[BusMessage] = queue.Queue(maxsize=capacity)
        self._consumers: tuple[IBusConsumer, ...] = tuple(consumers)
        self._consumers_lock = threading.Lock()

    @property
    def consumers(self) -> tuple[IBusConsumer, ...]:
        return self._consumers

    def subscribe(self, consumer: IBusConsumer) -> None:
        with self._consumers_lock:
            self._consumers = (*self._consumers, consumer)

    def unsubscribe(self, consumer: IBusConsumer) -> None:
        with self._consumers_lock:
            self._consumers = tuple(c for c in self._consumers if c is not consumer)

    def publish(self, message: BusMessage) -> None:
        """Queues a message (C# PublishAsync). Blocks while the bounded queue is full; raises once the bus is stopping."""
        while True:
            try:
                self._queue.put(message, timeout=0.25)
                return
            except queue.Full:
                if self.stopping.is_set():
                    raise RuntimeError("The event bus is stopping") from None

    def execute(self) -> None:
        for message in self.read_all(self._queue):
            self.dispatch(message)

    def dispatch(self, message: BusMessage) -> None:
        """Hands one message to every consumer of its topic (or "*"); consumer failures are logged, never raised."""
        for c in self._consumers:
            topics = c.topics
            if message.topic in topics or "*" in topics:
                try:
                    c.handle(message)
                except Exception:
                    self.log.exception("Consumer %s failed for %s/%s", type(c).__name__, message.topic, message.key)

    @property
    def pending(self) -> int:
        """Messages published but not yet handled."""
        return self._queue.unfinished_tasks

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Waits until every published message has been handled (tests, CLI). False when the timeout elapses first."""
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True


class OutboxRelay(BackgroundWorker):
    """Relays unpublished outbox rows to the bus (hs_ops role, FOR UPDATE SKIP LOCKED so replicas can run in parallel)."""

    def __init__(self, db: Db, bus: InProcessEventBus) -> None:
        super().__init__()
        self.db = db
        self.bus = bus

    def pump(self) -> int:
        """Publishes up to 500 unpublished rows and marks them published; returns how many were relayed."""
        with self.db.tx(DbRole.OPS) as u:
            rows = u.query(
                "select outbox_id, partner_id, topic, msg_key, payload::text as payload, created_at from txn.outbox "
                "where published_at is null order by outbox_id limit 500 for update skip locked"
            )
            for r in rows:
                self.bus.publish(BusMessage(r["outbox_id"], r["topic"], r["msg_key"], jsonutil.loads(r["payload"]), r["partner_id"], r["created_at"]))
            if rows:
                u.execute("update txn.outbox set published_at = now() where outbox_id = any(@ids)", {"ids": [r["outbox_id"] for r in rows]})
        return len(rows)

    def execute(self) -> None:
        while not self.stopping.is_set():
            try:
                n = self.pump()
                if not self.sleep(0.05 if n > 0 else 0.5):
                    break
            except Exception:
                self.log.warning("Outbox relay iteration failed", exc_info=True)
                if not self.sleep(2.0):
                    break


class LakeSink:
    """Bronze layer of the lakehouse: immutable JSONL files per topic and day (training data source for ml/)."""

    topics: Collection[str] = ("*",)

    def __init__(self, options: LakeOptions) -> None:
        self.options = options
        self._gate = threading.Lock()

    def handle(self, message: BusMessage) -> None:
        if not self.options.enabled:
            return
        directory = Path(self.options.root) / "bronze" / message.topic / f"dt={as_utc(message.created_at):%Y-%m-%d}"
        directory.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "outboxId": message.outbox_id, "topic": message.topic, "key": message.key, "partnerId": message.partner_id,
            "createdAt": message.created_at, "payload": message.payload,
        }
        if message.partner_id is None:  # HsJson omits null properties; nulls inside the payload are kept
            del record["partnerId"]
        line = jsonutil.dumps(record, drop_none=False)
        with self._gate, (directory / "part-0000.jsonl").open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
