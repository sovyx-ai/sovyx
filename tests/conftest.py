"""Shared test fixtures for Sovyx test suite."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings

if TYPE_CHECKING:
    from collections.abc import Iterator


# ── Click/Typer Rich-rendering normalization (CI cross-platform) ───────────
# Rationale (replaces the rc.16 per-test ANSI/box-drawing strip
# band-aids): every test that runs ``CliRunner().invoke(...)`` and asserts
# on ``result.output`` is sensitive to Rich's TTY-detected colour codes
# (``--full-diag`` rendered as ``-`` + ANSI + ``-full`` + ANSI + ``-diag``)
# AND to Rich's terminal-width wrapping (long paths split by box-drawing
# ``│`` U+2502 chars). On Linux/macOS CI runners those features are
# active; on local Windows shells they aren't. The diff broke
# ``test_calibrate_flag_help`` + ``test_signing_key_missing_path`` for
# 5 RCs.
#
# rc.16 fixed it via post-strip in 2 individual tests — but per
# ``feedback_enterprise_only`` that's symptom-fix, not cause-fix.
# This session-level setdefault is the upstream fix: sourced ONCE,
# every ``CliRunner.invoke`` call inherits it, future tests get the
# discipline for free. ``setdefault`` so an individual test that
# WANTS the colour/wrap behaviour (testing TTY rendering itself)
# can override locally via ``CliRunner.invoke(..., env={...})``.
#
# * ``NO_COLOR=1`` — disables Rich/Click colour output (POSIX
#   ``no-color`` standard, https://no-color.org).
# * ``COLUMNS=240`` — wide terminal so Rich doesn't wrap error
#   panels at 80 cols + insert box-drawing chars that split words
#   like filenames across lines.
os.environ.setdefault("NO_COLOR", "1")
os.environ.setdefault("COLUMNS", "240")

# ── Hypothesis global profile ──────────────────────────────────────────────
# deadline=None prevents flaky failures when the host is under load
# (e.g., CI with parallel workers, low-RAM environments).  The main
# offender is tiktoken's lazy encoding load (>200 ms on first call),
# but any I/O-touching property test can hit the default 200 ms limit.
# suppress_health_check includes too_slow so Hypothesis doesn't
# abort on legitimately expensive strategies (large unicode texts).
settings.register_profile(
    "sovyx",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("sovyx")


@pytest.fixture(autouse=True)
def _clear_rate_limiter() -> None:
    """Reset module-level rate limiter between tests.

    The RateLimitMiddleware uses module-level ``_buckets`` shared across
    all TestClient instances.  Without clearing, cumulative requests
    across tests hit the limit and return 429 unexpectedly.
    """
    try:
        from sovyx.dashboard.rate_limit import _buckets

        _buckets.clear()
    except ImportError:
        pass


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Temporary data directory for test isolation."""
    d = tmp_path / "sovyx-data"
    d.mkdir()
    return d


@pytest.fixture
def mind_dir(data_dir: Path) -> Path:
    """Temporary mind directory."""
    d = data_dir / "minds" / "test-mind"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def short_socket_path() -> Iterator[Path]:
    """Path for an ``AF_UNIX`` socket that survives the macOS 104-char limit.

    macOS caps ``sockaddr_un.sun_path`` at **104 characters** (including
    the null terminator). GitHub's macOS runner expands pytest's
    ``tmp_path`` under ``/private/var/folders/XX/…/T/pytest-of-runner/
    pytest-N/test-name0/`` — easily 100+ chars before the socket filename
    is even appended. Binding there fails with::

        OSError: AF_UNIX path too long

    Using this fixture pins the parent directory to ``/tmp`` on POSIX
    (via ``tempfile.mkdtemp(dir="/tmp")``), keeping the full path under
    ~25 characters regardless of runner.

    The yielded ``Path`` is the socket FILE — its parent directory
    already exists (mkdtemp created it), but the file itself does not,
    so callers that bind a server OR test for absence both work.

    On Windows the tests that use this fixture run over TCP (not
    ``AF_UNIX``), so the constraint doesn't apply; we still create a
    temp dir under the default ``$TEMP`` so the fixture's teardown
    behaviour is uniform across OSes.
    """
    # POSIX: pin to /tmp (short). Windows: default temp dir (RPC tests
    # on Windows use TCP — the path here is only a carrier for the
    # ``.port`` sidecar file, so any writable location works).
    parent_dir = None if sys.platform == "win32" else "/tmp"
    d = Path(tempfile.mkdtemp(prefix="sx-rpc-", dir=parent_dir))
    try:
        yield d / "s"
    finally:
        shutil.rmtree(d, ignore_errors=True)
