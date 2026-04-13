"""Shared fixtures for dashboard tests.

Provides a deterministic auth token that works reliably in CI
with pytest-xdist, where module-level globals can diverge from
TOKEN_FILE reads due to forking and module re-imports.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

_DASHBOARD_TEST_TOKEN = "dashboard-test-token-fixed"


@pytest.fixture(autouse=True)
def _pin_ensure_token() -> None:
    """Patch _ensure_token so create_app() always uses our known token.

    This is autouse for ALL dashboard tests.  Any test that calls
    create_app() will get _server_token == _DASHBOARD_TEST_TOKEN.

    Uses unittest.mock.patch (not monkeypatch) because some fixtures
    call create_app() before monkeypatch is available.
    """
    with patch(
        "sovyx.dashboard.server._ensure_token",
        return_value=_DASHBOARD_TEST_TOKEN,
    ):
        yield


@pytest.fixture()
def token() -> str:
    """Return the fixed dashboard test token."""
    return _DASHBOARD_TEST_TOKEN


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    """Authorization headers with the fixed test token."""
    return {"Authorization": f"Bearer {_DASHBOARD_TEST_TOKEN}"}
