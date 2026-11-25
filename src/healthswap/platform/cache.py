"""Minimal Redis-shaped key-value store used by the hot path (HLD 10.2; C# Cache/CacheStore.cs).

`ICacheStore` is the abstraction, `MemoryCacheStore` the in-process implementation with Redis semantics and TTLs (local
development, tests, single-node demos) and `RedisCacheStore` the production store on Redis 6+ (Azure Managed Redis or any
Redis) using the synchronous `redis` package. `create_cache_store(CacheOptions)` picks one like C# PlatformRegistration.

Method names are the snake_case of the C# methods (GetAsync -> get, MGetAsync -> m_get, HSetAsync -> h_set,
HMGetAsync -> hm_get, HGetAllAsync -> h_get_all, HDelAsync -> h_del, SAddAsync -> s_add, SMembersAsync -> s_members,
SRemAsync -> s_rem, CountAsync -> count, DeleteByPrefixAsync -> delete_by_prefix); the Redis command names (mget, hset,
hmget, hgetall, hdel, sadd, smembers, srem) are accepted as aliases. TTLs are `timedelta` or seconds.
"""

from __future__ import annotations

import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from healthswap.platform.settings import CacheOptions

Ttl = timedelta | float | int | None
Fields = Mapping[str, str] | Iterable[tuple[str, str]]


def _ttl_seconds(ttl: Ttl) -> float | None:
    if ttl is None:
        return None
    return ttl.total_seconds() if isinstance(ttl, timedelta) else float(ttl)


def _pairs(fields: Fields) -> list[tuple[str, str]]:
    return list(fields.items()) if isinstance(fields, Mapping) else [(f, v) for f, v in fields]


class ICacheStore(ABC):
    """Redis-shaped key-value API used by HotCache. Missing keys and fields read as None."""

    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def m_get(self, keys: Sequence[str]) -> list[str | None]: ...

    @abstractmethod
    def set(self, key: str, value: str, ttl: Ttl = None) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> bool: ...

    @abstractmethod
    def h_set(self, key: str, fields: Fields, ttl: Ttl = None) -> None: ...

    @abstractmethod
    def hm_get(self, key: str, fields: Sequence[str]) -> list[str | None]: ...

    @abstractmethod
    def h_get_all(self, key: str) -> dict[str, str]: ...

    @abstractmethod
    def h_del(self, key: str, fields: Sequence[str]) -> None: ...

    @abstractmethod
    def s_add(self, key: str, members: Sequence[str], ttl: Ttl = None) -> None: ...

    @abstractmethod
    def s_members(self, key: str) -> list[str]: ...

    @abstractmethod
    def s_rem(self, key: str, members: Sequence[str]) -> None: ...

    @abstractmethod
    def count(self, prefix: str) -> int:
        """Number of live keys starting with `prefix`."""

    @abstractmethod
    def delete_by_prefix(self, prefix: str) -> int:
        """Deletes every key starting with `prefix` and returns how many were deleted."""

    # Redis command spellings.
    def mget(self, keys: Sequence[str]) -> list[str | None]:
        return self.m_get(keys)

    def hset(self, key: str, fields: Fields, ttl: Ttl = None) -> None:
        self.h_set(key, fields, ttl)

    def hmget(self, key: str, fields: Sequence[str]) -> list[str | None]:
        return self.hm_get(key, fields)

    def h_m_get(self, key: str, fields: Sequence[str]) -> list[str | None]:
        return self.hm_get(key, fields)

    def hgetall(self, key: str) -> dict[str, str]:
        return self.h_get_all(key)

    def hdel(self, key: str, fields: Sequence[str]) -> None:
        self.h_del(key, fields)

    def sadd(self, key: str, members: Sequence[str], ttl: Ttl = None) -> None:
        self.s_add(key, members, ttl)

    def smembers(self, key: str) -> list[str]:
        return self.s_members(key)

    def srem(self, key: str, members: Sequence[str]) -> None:
        self.s_rem(key, members)


CacheStore = ICacheStore

# ---------------------------------------------------------------------------------------------------------------------
# In-process store
# ---------------------------------------------------------------------------------------------------------------------


class _Hash(dict[str, str]):
    pass


class _Set(dict[str, None]):
    """Set with insertion order."""


class _Entry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: str | _Hash | _Set, expires_at: float | None) -> None:
        self.value = value
        self.expires_at = expires_at  # None = no expiry

    def expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


class MemoryCacheStore(ICacheStore):
    """In-process store with Redis semantics and TTLs (thread-safe). `time_source` returns seconds (monotonic)."""

    def __init__(self, time_source: Callable[[], float] = time.monotonic) -> None:
        self._data: dict[str, _Entry] = {}
        self._lock = threading.RLock()
        self._now = time_source

    def _expiry(self, ttl: Ttl) -> float | None:
        seconds = _ttl_seconds(ttl)
        return None if seconds is None else self._now() + seconds

    def _live(self, key: str) -> _Entry | None:
        e = self._data.get(key)
        if e is None:
            return None
        if not e.expired(self._now()):
            return e
        del self._data[key]
        return None

    def _value(self, key: str, kind: type) -> Any:
        e = self._live(key)
        return e.value if e is not None and isinstance(e.value, kind) else None

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._value(key, str)

    def m_get(self, keys: Sequence[str]) -> list[str | None]:
        with self._lock:
            return [self._value(k, str) for k in keys]

    def set(self, key: str, value: str, ttl: Ttl = None) -> None:
        with self._lock:
            self._data[key] = _Entry(value, self._expiry(ttl))

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    def h_set(self, key: str, fields: Fields, ttl: Ttl = None) -> None:
        with self._lock:
            e = self._data.get(key)
            if e is None or e.expired(self._now()) or not isinstance(e.value, _Hash):
                e = self._data[key] = _Entry(_Hash(), self._expiry(ttl))
            elif ttl is not None:
                e.expires_at = self._expiry(ttl)
            e.value.update(_pairs(fields))

    def hm_get(self, key: str, fields: Sequence[str]) -> list[str | None]:
        with self._lock:
            h = self._value(key, _Hash)
            return [None if h is None else h.get(f) for f in fields]

    def h_get_all(self, key: str) -> dict[str, str]:
        with self._lock:
            h = self._value(key, _Hash)
            return {} if h is None else dict(h)

    def h_del(self, key: str, fields: Sequence[str]) -> None:
        with self._lock:
            h = self._value(key, _Hash)
            if h is not None:
                for f in fields:
                    h.pop(f, None)

    def s_add(self, key: str, members: Sequence[str], ttl: Ttl = None) -> None:
        with self._lock:
            e = self._data.get(key)
            if e is None or e.expired(self._now()) or not isinstance(e.value, _Set):
                e = self._data[key] = _Entry(_Set(), self._expiry(ttl))
            e.value.update(dict.fromkeys(members))

    def s_members(self, key: str) -> list[str]:
        with self._lock:
            s = self._value(key, _Set)
            return [] if s is None else list(s)

    def s_rem(self, key: str, members: Sequence[str]) -> None:
        with self._lock:
            s = self._value(key, _Set)
            if s is not None:
                for m in members:
                    s.pop(m, None)

    def count(self, prefix: str) -> int:
        with self._lock:
            now = self._now()
            return sum(1 for k, e in self._data.items() if k.startswith(prefix) and not e.expired(now))

    def delete_by_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                del self._data[k]
            return len(keys)


# ---------------------------------------------------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------------------------------------------------

_GLOB_SPECIAL = re.compile(r"([*?\[\]\\])")


def redis_glob_escape(text: str) -> str:
    """Escapes Redis glob metacharacters so a key prefix matches literally in SCAN MATCH."""
    return _GLOB_SPECIAL.sub(r"\\\1", text)


def redis_url(configuration: str) -> str:
    """A redis:// URL from a URL or a StackExchange.Redis configuration string ("host:port,password=...,ssl=True")."""
    text = configuration.strip()
    if "://" in text:
        return text
    parts = [p.strip() for p in text.split(",") if p.strip()]
    options = {k.strip().lower(): v.strip() for k, _, v in (p.partition("=") for p in parts if "=" in p)}
    endpoint = next((p for p in parts if "=" not in p), "localhost:6379")
    if endpoint.startswith("["):  # [IPv6]:port
        host, _, rest = endpoint.partition("]")
        host, port = host + "]", rest[1:] if rest.startswith(":") and len(rest) > 1 else "6379"
    elif endpoint.count(":") == 1:
        host, port = endpoint.split(":")
    else:
        host, port = endpoint, "6379"
    user, password = options.get("user"), options.get("password")
    auth = ""
    if user or password:
        auth = quote(user or "", safe="") + (":" + quote(password, safe="") if password else "") + "@"
    scheme = "rediss" if options.get("ssl", "false").lower() == "true" else "redis"
    return f"{scheme}://{auth}{host}:{port}/{options.get('defaultdatabase', '0')}"


def _text(value: Any) -> str | None:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _ms(ttl: Ttl) -> int | None:
    seconds = _ttl_seconds(ttl)
    return None if seconds is None else int(round(seconds * 1000))


class RedisCacheStore(ICacheStore):
    """Production store on Redis (one-to-one mapping of the cache API). `client` is a redis.Redis (or RedisCluster)."""

    _DELETE_BATCH = 500

    def __init__(self, client: Any) -> None:
        self.client = client

    @classmethod
    def connect(cls, configuration: str) -> RedisCacheStore:
        """C# ConnectionMultiplexer.Connect: accepts a redis:// URL or a StackExchange.Redis configuration string."""
        import redis  # optional dependency (pip install healthswap[redis])

        return cls(redis.Redis.from_url(redis_url(configuration), decode_responses=True))

    def get(self, key: str) -> str | None:
        return _text(self.client.get(key))

    def m_get(self, keys: Sequence[str]) -> list[str | None]:
        keys = list(keys)
        return [_text(v) for v in self.client.mget(keys)] if keys else []

    def set(self, key: str, value: str, ttl: Ttl = None) -> None:
        ms = _ms(ttl)
        if ms is None:
            self.client.set(key, value)
        else:
            self.client.set(key, value, px=max(1, ms))

    def delete(self, key: str) -> bool:
        return self.client.delete(key) > 0

    def h_set(self, key: str, fields: Fields, ttl: Ttl = None) -> None:
        mapping = dict(_pairs(fields))
        ms = _ms(ttl)
        if ms is None:
            if mapping:
                self.client.hset(key, mapping=mapping)
            return
        pipe = self.client.pipeline(transaction=False)
        if mapping:
            pipe.hset(key, mapping=mapping)
        pipe.pexpire(key, ms)
        pipe.execute()

    def hm_get(self, key: str, fields: Sequence[str]) -> list[str | None]:
        fields = list(fields)
        return [_text(v) for v in self.client.hmget(key, fields)] if fields else []

    def h_get_all(self, key: str) -> dict[str, str]:
        return {_text(k): _text(v) for k, v in self.client.hgetall(key).items()}

    def h_del(self, key: str, fields: Sequence[str]) -> None:
        if fields:
            self.client.hdel(key, *fields)

    def s_add(self, key: str, members: Sequence[str], ttl: Ttl = None) -> None:
        ms = _ms(ttl)
        if ms is None:
            if members:
                self.client.sadd(key, *members)
            return
        pipe = self.client.pipeline(transaction=False)
        if members:
            pipe.sadd(key, *members)
        pipe.pexpire(key, ms)
        pipe.execute()

    def s_members(self, key: str) -> list[str]:
        return [_text(v) for v in self.client.smembers(key)]

    def s_rem(self, key: str, members: Sequence[str]) -> None:
        if members:
            self.client.srem(key, *members)

    def count(self, prefix: str) -> int:
        return sum(1 for _ in self.client.scan_iter(match=redis_glob_escape(prefix) + "*", count=1000))

    def delete_by_prefix(self, prefix: str) -> int:
        n = 0
        batch: list[Any] = []
        for key in self.client.scan_iter(match=redis_glob_escape(prefix) + "*", count=1000):
            batch.append(key)
            if len(batch) >= self._DELETE_BATCH:
                n += self.client.delete(*batch)
                batch.clear()
        if batch:
            n += self.client.delete(*batch)
        return n


def create_cache_store(options: CacheOptions | None = None) -> ICacheStore:
    """RedisCacheStore when cache.redis is configured, else MemoryCacheStore (C# PlatformRegistration)."""
    if options is not None and options.redis and options.redis.strip():
        return RedisCacheStore.connect(options.redis)
    return MemoryCacheStore()
