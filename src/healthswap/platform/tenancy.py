"""Tenancy (C# Tenancy.cs): the authenticated caller (PartnerContext) and the partner configuration directory.

HsException lives in errors.py as HsError (re-exported here for convenience).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar

from healthswap.domain.commerce.partner_config import PartnerConfig
from healthswap.platform import jsonutil
from healthswap.platform.db import Db, DbRole
from healthswap.platform.errors import HsError

__all__ = ["HsError", "PartnerContext", "PartnerDirectory", "PartnerInfo"]

_EMPTY = uuid.UUID(int=0)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartnerContext:
    """Authenticated caller: partner, OAuth client or SDK session (bound to userRef, cartRef and storeId)."""

    partner_id: uuid.UUID = _EMPTY
    client_id: str = ""
    scopes: frozenset[str] = frozenset()
    is_sdk_session: bool = False
    sdk_user_ref: str | None = None
    sdk_cart_ref: str | None = None
    sdk_store_id: str | None = None
    sdk_market: str | None = None

    @property
    def is_server(self) -> bool:
        return not self.is_sdk_session


@dataclass(frozen=True, slots=True)
class PartnerInfo:
    partner_id: uuid.UUID
    display_name: str
    geography: str
    markets: tuple[str, ...]
    status: str
    config: PartnerConfig
    # tenant.partner.config as stored (jsonb text).
    config_json: str


_COLUMNS = "partner_id, display_name, geography, markets, status, config::text as config"


class PartnerDirectory:
    """Tenant configuration directory: in-process cache with explicit invalidation (HLD 18: kill switch effective in seconds)."""

    ttl: ClassVar[timedelta] = timedelta(seconds=15)
    TTL: ClassVar[timedelta] = ttl

    def __init__(self, db: Db) -> None:
        self.db = db
        self._cache: dict[uuid.UUID, tuple[PartnerInfo, float]] = {}

    def get(self, partner_id: uuid.UUID) -> PartnerInfo:
        """The partner's info (cached for `ttl`). Raises HsError 401 HS-AUTH-INVALID for an unknown partner."""
        partner_id = partner_id if isinstance(partner_id, uuid.UUID) else uuid.UUID(str(partner_id))
        cached = self._cache.get(partner_id)
        if cached is not None and time.monotonic() - cached[1] < self.ttl.total_seconds():
            return cached[0]
        with self.db.tx(DbRole.APP, partner_id) as u:
            row = u.one(f"select {_COLUMNS} from tenant.partner where partner_id = @partnerId", {"partnerId": partner_id})
        if row is None:
            raise HsError(401, "HS-AUTH-INVALID", "Unknown partner", "The token's partner is not registered")
        info = self._to_info(row)
        self._cache[partner_id] = (info, time.monotonic())
        return info

    def list_all(self) -> list[PartnerInfo]:
        """Every partner ordered by display name (hs_ops; not cached)."""
        with self.db.tx(DbRole.OPS) as u:
            rows = u.query(f"select {_COLUMNS} from tenant.partner order by display_name")
        return [self._to_info(r) for r in rows]

    def invalidate(self, partner_id: uuid.UUID) -> None:
        self._cache.pop(partner_id if isinstance(partner_id, uuid.UUID) else uuid.UUID(str(partner_id)), None)

    @staticmethod
    def parse_config(config: str | bytes | Mapping[str, Any] | None) -> PartnerConfig:
        """A PartnerConfig from the jsonb text (or an already parsed document); JSON null gives the defaults.

        Replaces C# `JsonSerializer.Deserialize<PartnerConfig>(json, PartnerDirectory.ConfigJson)`; raises ValueError.
        """
        data = jsonutil.loads(config) if isinstance(config, (str, bytes)) else config
        return PartnerConfig() if data is None else PartnerConfig.from_dict(data)

    @classmethod
    def _to_info(cls, r: Mapping[str, Any]) -> PartnerInfo:
        config_json = r["config"]
        return PartnerInfo(
            r["partner_id"], r["display_name"], r["geography"], tuple(r["markets"] or ()), r["status"], cls.parse_config(config_json), config_json
        )
