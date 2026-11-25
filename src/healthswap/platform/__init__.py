"""Platform layer: PostgreSQL, cache, event bus, LLM gateway, ONNX models, knowledge jobs and partner services.

Porting conventions (the Python code mirrors the original C# reference implementation module by module):

Naming
    * C# method `FooBarAsync` / `FooBar` -> `foo_bar` (drop "Async"); property `FooBar` -> `foo_bar`; classes keep names.
    * `CancellationToken` parameters are dropped; `ILogger<T>` -> `logging.getLogger(__name__)`.
    * Constructor injection: `sealed class X(Db db, HotCache cache, IClock clock)` -> `class X` with
      `__init__(self, db: Db, cache: HotCache, clock: Clock)` storing `self.db`, `self.cache`, `self.clock`.
      `container.py` wires every service (C# PlatformRegistration).
    * Options classes (`FooOptions`) -> dataclasses in `settings.py` with snake_case fields and the same defaults.
    * Clock: C# `clock.UtcNow` -> `clock.utc_now()` (healthswap.domain.primitives.Clock).

Concurrency
    * Synchronous code. Web handlers run in FastAPI's thread pool; background workers are daemon threads with a stop
      event. PGlite serializes transactions: never open a second transaction while one is open in the same thread and
      never make HTTP calls inside a transaction.

Database (db.py)
    * `with db.tx(DbRole.APP, partner_id) as u:` or `db.in_tx(role, partner_id, fn)`; `u.query/one/single/scalar/execute`,
      `u.set_role(...)`. SQL is copied verbatim from the C# (Dapper `@name` parameters); pass a dict of parameters.
      Rows are dicts keyed by column name; timestamptz -> aware UTC datetime, numeric -> Decimal, uuid -> UUID,
      json/jsonb -> parsed objects (queries that cast to ::text return str). Lists bind to arrays; add an explicit cast
      (for example `@ids::uuid[]`) when a list may be empty.

Data shapes
    * API requests and responses are plain dicts with the camelCase keys of api/openapi.yaml (what C# System.Text.Json
      produced with HsJson.Options); None values are dropped on output (jsonutil). Money is `Money.to_api()`
      ({"amount": "1.00", "currency": "INR"}); timestamps use `primitives.iso()`.
    * Internal records are frozen dataclasses; domain types come from `healthswap.domain`.
    * Errors: raise `HsError` (errors.py); `HsError.invalid/not_found/forbidden/conflict/unprocessable` mirror the C#
      `HsException` factories and keep the exact HS-* codes.
"""
