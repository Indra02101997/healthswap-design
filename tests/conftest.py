"""Shared fixtures.

Integration tests ask for `fresh_database_url`: every test module gets its own throwaway PGlite dev database (tools/devdb started
in memory on a free port; see healthswap.devdb) with db/schema.sql applied and no data. Tests using it are marked `integration`
automatically, so `pytest -m "not integration"` runs only the fast tests. Without Node.js or `npm ci` in tools/devdb they are
skipped with the reason.

HS_TEST_DATABASE_URL (optional) points at an externally managed fresh database instead; because tests write to it, it serves
a single test module per run (select one, for example `pytest tests/platform/test_seed.py`), the other modules are skipped.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from healthswap.devdb import DevDatabase, unavailable_reason
from healthswap.paths import repo_root

_EXTERNAL_DATABASE_OWNER = pytest.StashKey[str]()


@pytest.fixture(scope="session")
def root() -> Path:
    return repo_root()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Runs before the `-m` deselection, so tests that use the dev database are always selected (or excluded) as integration tests.
    for item in items:
        if "fresh_database_url" in getattr(item, "fixturenames", ()) and item.get_closest_marker("integration") is None:
            item.add_marker(pytest.mark.integration)


def pytest_report_header(config: pytest.Config) -> str:
    if os.environ.get("HS_TEST_DATABASE_URL", "").strip():
        return "integration database: HS_TEST_DATABASE_URL (one test module)"
    reason = unavailable_reason(repo_root())
    return f"integration database: unavailable ({reason})" if reason else "integration database: fresh in-memory tools/devdb per test module"


def _external_database_problem(url: str) -> str | None:
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=15) as conn:
            row = conn.execute("select to_regclass('kb.product') is not null and to_regclass('tenant.partner') is not null").fetchone()
            if row is None or not row[0]:
                return "HS_TEST_DATABASE_URL has no HealthSwap schema (apply db/schema.sql or start tools/devdb/server.mjs --memory)"
            used = conn.execute("select exists (select 1 from kb.product) or exists (select 1 from tenant.partner)").fetchone()
    except psycopg.Error as ex:
        return f"HS_TEST_DATABASE_URL is not reachable: {ex}"
    if used is not None and used[0]:
        return "HS_TEST_DATABASE_URL must point at a fresh database (schema only, no data); restart tools/devdb/server.mjs --memory"
    return None


@pytest.fixture(scope="module")
def fresh_database_url(request: pytest.FixtureRequest, root: Path) -> Iterator[str]:
    """libpq URL of a database with db/schema.sql applied and no data, private to the requesting test module."""
    external = os.environ.get("HS_TEST_DATABASE_URL", "").strip()
    if external:
        module = request.node.nodeid
        owner = request.config.stash.setdefault(_EXTERNAL_DATABASE_OWNER, module)
        if owner != module:
            pytest.skip(f"HS_TEST_DATABASE_URL serves one test module per run and {owner} used it; select a single module, "
                        "or unset it to give every module its own fresh dev database")
        problem = _external_database_problem(external)
        if problem is not None:
            pytest.skip(problem)
        yield external
        return
    reason = unavailable_reason(root)
    if reason is not None:
        pytest.skip(reason)
    with DevDatabase(root) as devdb:
        assert devdb.url is not None
        yield devdb.url
