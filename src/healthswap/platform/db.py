"""PostgreSQL access (C# Data/Db.cs).

Every unit of work runs as exactly one database role (SET LOCAL ROLE) and, for tenant work, with the transaction-local
setting app.partner_id that row-level security policies read. Settings are transaction-local, so connections can be
pooled safely and the PGlite development server (tools/devdb) can multiplex them.

SQL is written in the Dapper style of the original implementation: `@name` parameters plus PostgreSQL casts, for example
`where partner_id = @partnerId::uuid`. Queries are rewritten to psycopg placeholders (`%(name)s`) and literal `%` signs
are escaped. `@name` inside string literals, quoted identifiers and comments is left untouched, and so are the operators
`@>`, `<@` and `@@`.

Usage:
    with db.tx(DbRole.APP, partner_id) as u:
        rows = u.query("select * from txn.order_header where order_id = @orderId", {"orderId": order_id})
        u.set_role(DbRole.LEDGER_WRITER)   # least privilege inside one transaction
        u.execute("insert into billing.journal ...", {...})

Concurrency rules (also required by PGlite, which serializes transactions):
    never open a second transaction while one is open in the same thread, and never make HTTP calls inside one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Any, TypeVar
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:55432/postgres?sslmode=disable&connect_timeout=30"


class DbRole(StrEnum):
    """Database roles from db/schema.sql."""

    APP = "hs_app"
    LEDGER_WRITER = "hs_ledger_writer"
    KB_WRITER = "hs_kb_writer"
    OPS = "hs_ops"
    READ_ONLY = "hs_readonly"


@dataclass(slots=True)
class DbOptions:
    url: str = DEFAULT_DATABASE_URL
    # Switch roles per transaction (recommended; required for row-level security when the login is privileged).
    use_roles: bool = True
    min_pool_size: int = 1
    max_pool_size: int = 16
    pool_timeout_seconds: float = 30.0


# ---------------------------------------------------------------------------------------------------------------------
# Dapper-style SQL -> psycopg
# ---------------------------------------------------------------------------------------------------------------------


def _ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def _ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


@lru_cache(maxsize=4096)
def convert_sql(sql: str) -> str:
    """Rewrites @name parameters to %(name)s and escapes literal % (only used when parameters are passed)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "'":  # string literal ('' escapes)
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1].replace("%", "%%"))
            i = j + 1
        elif ch == '"':  # quoted identifier
            j = sql.find('"', i + 1)
            j = n - 1 if j < 0 else j
            out.append(sql[i:j + 1].replace("%", "%%"))
            i = j + 1
        elif ch == "-" and nxt == "-":  # line comment
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(sql[i:j].replace("%", "%%"))
            i = j
        elif ch == "/" and nxt == "*":  # block comment
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(sql[i:j].replace("%", "%%"))
            i = j
        elif ch == "$" and (nxt == "$" or _ident_start(nxt)) and (i == 0 or not _ident_char(sql[i - 1])):  # dollar quoting
            k = sql.find("$", i + 1)
            tag = sql[i:k + 1] if k > 0 else ""
            if tag and all(_ident_char(c) for c in tag[1:-1]):
                end = sql.find(tag, k + 1)
                end = n if end < 0 else end + len(tag)
                out.append(sql[i:end].replace("%", "%%"))
                i = end
            else:
                out.append(ch)
                i += 1
        elif ch == "%":
            out.append("%%")
            i += 1
        elif ch == "@" and _ident_start(nxt) and (i == 0 or sql[i - 1] not in "@<"):
            j = i + 1
            while j < n and _ident_char(sql[j]):
                j += 1
            out.append(f"%({sql[i + 1:j]})s")
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _params(params: Mapping[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    if params is None and not kwargs:
        return None
    merged = dict(params or {})
    merged.update(kwargs)
    return merged


# ---------------------------------------------------------------------------------------------------------------------
# Unit of work
# ---------------------------------------------------------------------------------------------------------------------


class Uow:
    """One transaction on one connection (C# UnitOfWork)."""

    def __init__(self, conn: psycopg.Connection, partner_id: UUID | None, use_roles: bool) -> None:
        self.conn = conn
        self.partner_id = partner_id
        self.role: DbRole | None = None
        self._use_roles = use_roles

    def set_role(self, role: DbRole) -> None:
        """Least privilege inside one transaction, for example hs_app for the order and hs_ledger_writer for postings."""
        self.role = DbRole(role)
        if self._use_roles:
            self.conn.execute(f"set local role {self.role.value}")

    def _run(self, sql: str, params: Mapping[str, Any] | None, kwargs: dict[str, Any]) -> psycopg.Cursor:
        p = _params(params, kwargs)
        return self.conn.execute(convert_sql(sql) if p is not None else sql, p, prepare=False)

    def query(self, sql: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        cur = self._run(sql, params, kwargs)
        return cur.fetchall() if cur.description is not None else []

    def one(self, sql: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
        """First row or None (C# QueryFirstOrDefault)."""
        cur = self._run(sql, params, kwargs)
        return cur.fetchone() if cur.description is not None else None

    def single(self, sql: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Exactly one row (C# QuerySingle)."""
        rows = self.query(sql, params, **kwargs)
        if len(rows) != 1:
            raise LookupError(f"Expected exactly one row, got {len(rows)}")
        return rows[0]

    def scalar(self, sql: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        """First column of the first row, or None (C# ExecuteScalar / QueryFirstOrDefault<scalar>)."""
        row = self.one(sql, params, **kwargs)
        return None if row is None else next(iter(row.values()), None)

    def execute(self, sql: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> int:
        cur = self._run(sql, params, kwargs)
        return cur.rowcount

    def execute_many(self, sql: str, rows: list[Mapping[str, Any]]) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(convert_sql(sql), rows)
            return cur.rowcount


# ---------------------------------------------------------------------------------------------------------------------
# Db
# ---------------------------------------------------------------------------------------------------------------------


def _configure(conn: psycopg.Connection) -> None:
    conn.execute("set time zone 'UTC'")


class Db:
    """Entry point for PostgreSQL access (connection pool + role-scoped transactions)."""

    def __init__(self, options: DbOptions | None = None) -> None:
        self.options = options or DbOptions()
        self.use_roles = self.options.use_roles
        self.pool = ConnectionPool(
            self.options.url,
            min_size=self.options.min_pool_size,
            max_size=self.options.max_pool_size,
            timeout=self.options.pool_timeout_seconds,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
            configure=_configure,
            open=False,
            name="healthswap",
        )
        self._opened = False

    def open(self, wait: bool = False, timeout: float = 30.0) -> None:
        if not self._opened:
            self.pool.open(wait=wait, timeout=timeout)
            self._opened = True

    @staticmethod
    def role_name(role: DbRole) -> str:
        return DbRole(role).value

    @contextmanager
    def tx(self, role: DbRole, partner_id: UUID | None = None) -> Iterator[Uow]:
        """Read-committed transaction as `role`; commits on success and rolls back on exceptions."""
        self.open()
        with self.pool.connection() as conn, conn.transaction():
            uow = Uow(conn, partner_id, self.use_roles)
            uow.set_role(role)
            if partner_id is not None:
                conn.execute("select set_config('app.partner_id', %s, true)", [str(partner_id)])
            yield uow

    def in_tx(self, role: DbRole, partner_id: UUID | None, work: Callable[[Uow], T]) -> T:
        """Runs `work` in a unit of work and commits (C# InTxAsync)."""
        with self.tx(role, partner_id) as u:
            return work(u)

    @contextmanager
    def raw(self) -> Iterator[psycopg.Connection]:
        """Raw autocommit connection for schema bootstrap only (no role switching)."""
        self.open()
        with self.pool.connection() as conn:
            yield conn

    def wait_ready(self, timeout_seconds: float = 60.0) -> None:
        """Waits until the database accepts queries (used by `hs db wait` and the test fixture)."""
        deadline = time.monotonic() + timeout_seconds
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.options.url, autocommit=True, connect_timeout=5) as conn:
                    conn.execute("select 1")
                return
            except psycopg.Error as ex:  # not up yet
                last = ex
                time.sleep(0.5)
        raise TimeoutError(f"Database not reachable at {self.options.url.split('@')[-1]}: {last}")

    def close(self) -> None:
        if self._opened:
            self.pool.close()
            self._opened = False
