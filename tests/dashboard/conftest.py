"""Shared fixtures for dashboard tests.

The CORE problem: monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", ...)
does NOT work reliably in CI with pytest-xdist. Forked workers may resolve
the string path to a DIFFERENT module object than the one create_app() uses.

The FIX: import the module directly and use patch.object() on the actual
module object. This guarantees the same object identity.

This conftest provides an autouse fixture that patches _ensure_token()
via patch.object on the directly-imported module — not via string path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import sovyx.dashboard.server as _server_mod

_DASHBOARD_TEST_TOKEN = "dashboard-test-token-fixed"


@pytest.fixture(autouse=True)
def _pin_ensure_token() -> None:
    """Patch _ensure_token so create_app() always uses our known token.

    Uses patch.object on the directly-imported module to avoid
    string-path resolution issues in xdist workers.

    Any local _clean_token / token fixture that also patches TOKEN_FILE
    is harmless — _ensure_token is never called because we replace it.
    """
    with patch.object(_server_mod, "_ensure_token", return_value=_DASHBOARD_TEST_TOKEN):
        yield


@pytest.fixture()
def token() -> str:
    """Return the fixed dashboard test token.

    Tests that define their own ``token`` fixture override this.
    Tests that don't get a deterministic, known token.
    """
    return _DASHBOARD_TEST_TOKEN


@pytest.fixture()
def auth_headers(token: str) -> dict[str, str]:
    """Authorization headers using the current token fixture.

    Uses whatever ``token`` fixture is active (local or conftest).
    """
    return {"Authorization": f"Bearer {token}"}
