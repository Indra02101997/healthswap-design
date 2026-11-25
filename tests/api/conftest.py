"""Fixtures of the API integration tests: one seeded in-process API per test module on its own fresh dev database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from harness import Api, running_api


@pytest.fixture(scope="module")
def api(fresh_database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Api]:
    with running_api(fresh_database_url, tmp_path_factory.mktemp("api")) as a:
        yield a


@pytest.fixture(autouse=True)
def _responses_follow_the_contract(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fails a test when any /v1 response it received violated api/openapi.yaml (checked by the TestClient hook)."""
    if "api" not in request.fixturenames:
        yield
        return
    a: Api = request.getfixturevalue("api")
    a.hook.violations.clear()
    yield
    violations, a.hook.violations[:] = list(a.hook.violations), []
    assert not violations, "responses violating api/openapi.yaml:\n" + "\n".join(violations)
