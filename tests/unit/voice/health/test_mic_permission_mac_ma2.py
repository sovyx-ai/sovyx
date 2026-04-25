"""Tests for the MA2 macOS TCC probe.

Mocks sqlite3 + sys.platform so the suite stays cross-platform and
deterministic. Verifies:

* The TCC reader produces correct (auth_value, notes) for each of
  the 4 canonical TCCAccessAuthValue codes (denied/unknown/allowed/
  limited).
* sqlite3 OperationalError (no Full Disk Access) collapses to
  (None, notes) — not a raise.
* Missing TCC.db file collapses to (None, notes).
* The status-token mapping translates auth_value correctly.
* The MA2 wire-up in :func:`check_microphone_permission` returns the
  right MicPermissionStatus on darwin.
* The remediation_hint adapts to macOS Settings paths when on darwin.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._mic_permission import (
    MicPermissionReport,
    MicPermissionStatus,
    check_microphone_permission,
)
from sovyx.voice.health._mic_permission_mac import (
    auth_value_to_status_token,
    query_macos_microphone_permission,
)

# ── Cross-platform branches in MA2 module ─────────────────────────


class TestNonDarwinShortCircuit:
    def test_linux_returns_none_with_note(self) -> None:
        with patch.object(sys, "platform", "linux"):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("non-darwin" in n for n in notes)

    def test_windows_returns_none_with_note(self) -> None:
        with patch.object(sys, "platform", "win32"):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("non-darwin" in n for n in notes)


# ── auth_value_to_status_token mapping ────────────────────────────


class TestAuthValueMapping:
    def test_none_maps_to_unknown(self) -> None:
        assert auth_value_to_status_token(None) == "unknown"

    def test_denied_zero_maps_to_denied(self) -> None:
        assert auth_value_to_status_token(0) == "denied"

    def test_unknown_one_maps_to_unknown(self) -> None:
        # AUTH_UNKNOWN — Sovyx hasn't been asked. UNKNOWN, not DENIED.
        # Bug-bait: a future refactor that "simplifies" 0 + 1 → both
        # denied would FALSELY block first-run setups.
        assert auth_value_to_status_token(1) == "unknown"

    def test_allowed_two_maps_to_granted(self) -> None:
        assert auth_value_to_status_token(2) == "granted"

    def test_limited_three_maps_to_granted(self) -> None:
        # LIMITED isn't applicable to mic per Apple docs but if it
        # ever appeared we treat as granted (better than false DENY).
        assert auth_value_to_status_token(3) == "granted"

    def test_unrecognised_value_maps_to_unknown(self) -> None:
        # Defensive: a future Apple value (4+) maps to UNKNOWN, NOT
        # to denied. Better to fall through to the cascade's
        # post-open silence detector than to false-block.
        assert auth_value_to_status_token(99) == "unknown"


# ── TCC.db reader (mocked sqlite3) ────────────────────────────────


def _build_fake_sqlite3(
    *,
    rows: list[tuple[Any, ...]] | None = None,
    open_error: type[BaseException] | None = None,
    query_error: type[BaseException] | None = None,
) -> Any:
    """Build a sqlite3.connect replacement that returns canned rows."""

    def _connect(*_args: Any, **_kwargs: Any) -> Any:
        if open_error is not None:
            raise open_error("simulated open failure")
        conn = MagicMock()
        cursor = MagicMock()
        if query_error is not None:
            conn.execute.side_effect = query_error("simulated query failure")
        else:
            cursor.fetchone.return_value = rows[0] if rows else None
            conn.execute.return_value = cursor
        conn.close = MagicMock()
        return conn

    return _connect


class TestTCCReader:
    def _patch_darwin_with_existing_db(self) -> Any:
        """Pretend the user TCC.db exists (avoid touching real FS)."""
        return patch(
            "sovyx.voice.health._mic_permission_mac._user_tcc_path",
            return_value=MagicMock(exists=lambda: True),
        )

    def test_no_tcc_db_file_returns_none_with_note(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._mic_permission_mac._user_tcc_path",
                return_value=MagicMock(exists=lambda: False),
            ),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("not found" in n for n in notes)

    def test_open_operationalerror_returns_none_with_fda_note(self) -> None:
        # sqlite3.OperationalError on connect → likely needs Full
        # Disk Access. The note must mention FDA so the dashboard
        # can recommend granting it (vs. misclassifying mic state).
        with (
            patch.object(sys, "platform", "darwin"),
            self._patch_darwin_with_existing_db(),
            patch(
                "sqlite3.connect",
                side_effect=_build_fake_sqlite3(open_error=sqlite3.OperationalError),
            ),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("Full Disk Access" in n for n in notes)

    def test_unexpected_open_error_returns_none(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            self._patch_darwin_with_existing_db(),
            patch(
                "sqlite3.connect",
                side_effect=_build_fake_sqlite3(open_error=RuntimeError),
            ),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("unexpected sqlite open" in n for n in notes)

    def test_allowed_row_returns_2(self) -> None:
        # MAX(auth_value)=2, COUNT=1 → allowed.
        with (
            patch.object(sys, "platform", "darwin"),
            self._patch_darwin_with_existing_db(),
            patch(
                "sqlite3.connect",
                side_effect=_build_fake_sqlite3(rows=[(2, 1)]),
            ),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value == 2  # noqa: PLR2004
        assert notes == []

    def test_denied_row_returns_0(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            self._patch_darwin_with_existing_db(),
            patch(
                "sqlite3.connect",
                side_effect=_build_fake_sqlite3(rows=[(0, 1)]),
            ),
        ):
            auth_value, _ = query_macos_microphone_permission()
        assert auth_value == 0

    def test_max_aggregation_picks_highest_privilege(self) -> None:
        """If TWO clients exist (one DENIED=0, one ALLOWED=2), the
        MAX picks ALLOWED. The rationale: at least one client (e.g.
        Terminal) has been granted; the inheriting Sovyx process is
        likely fine. This is the documented anti-false-DENY bias."""
        with (
            patch.object(sys, "platform", "darwin"),
            self._patch_darwin_with_existing_db(),
            patch(
                "sqlite3.connect",
                side_effect=_build_fake_sqlite3(rows=[(2, 2)]),
            ),
        ):
            auth_value, _ = query_macos_microphone_permission()
        assert auth_value == 2  # noqa: PLR2004

    def test_no_microphone_rows_returns_none(self) -> None:
        # COUNT=0 → Sovyx never asked; macOS will prompt on first
        # capture. UNKNOWN, not DENIED.
        with (
            patch.object(sys, "platform", "darwin"),
            self._patch_darwin_with_existing_db(),
            patch(
                "sqlite3.connect",
                side_effect=_build_fake_sqlite3(rows=[(None, 0)]),
            ),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("never asked" in n for n in notes)

    def test_query_operationalerror_returns_none(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            self._patch_darwin_with_existing_db(),
            patch(
                "sqlite3.connect",
                side_effect=_build_fake_sqlite3(query_error=sqlite3.OperationalError),
            ),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("query failed" in n for n in notes)


# ── End-to-end via check_microphone_permission ────────────────────


class TestEndToEndDarwinIntegration:
    def test_darwin_allowed_returns_granted(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._mic_permission_mac.query_macos_microphone_permission",
                return_value=(2, []),
            ),
        ):
            report = check_microphone_permission()
        assert report.status is MicPermissionStatus.GRANTED
        assert report.remediation_hint == ""

    def test_darwin_denied_returns_denied_with_macos_hint(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._mic_permission_mac.query_macos_microphone_permission",
                return_value=(0, []),
            ),
        ):
            report = check_microphone_permission()
            # remediation_hint reads sys.platform live; access INSIDE
            # the with block so the patched darwin value applies.
            hint = report.remediation_hint
        assert report.status is MicPermissionStatus.DENIED
        # MA2: hint must mention the macOS Settings path, NOT Windows.
        assert "System Settings" in hint
        assert "Privacy & Security" in hint
        assert "Windows" not in hint

    def test_darwin_no_db_returns_unknown_with_fda_hint(self) -> None:
        # No TCC.db → query returns (None, ["not found"]) → UNKNOWN.
        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._mic_permission_mac.query_macos_microphone_permission",
                return_value=(None, ["user TCC.db not found"]),
            ),
        ):
            report = check_microphone_permission()
            hint = report.remediation_hint
        assert report.status is MicPermissionStatus.UNKNOWN
        assert "Full Disk Access" in hint

    def test_darwin_probe_crash_returns_unknown_no_raise(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The TCC reader itself crashes — must NOT propagate.
        import logging

        with (
            patch.object(sys, "platform", "darwin"),
            patch(
                "sovyx.voice.health._mic_permission_mac.query_macos_microphone_permission",
                side_effect=RuntimeError("probe boom"),
            ),
        ):
            caplog.set_level(logging.WARNING, logger="sovyx.voice.health._mic_permission")
            report = check_microphone_permission()
        assert report.status is MicPermissionStatus.UNKNOWN
        # WARN logged for the crash.
        crash_events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.mic_permission.tcc_probe_failed"
        ]
        assert len(crash_events) == 1


# ── Remediation hint OS-awareness ─────────────────────────────────


class TestRemediationHintOSAware:
    def test_macos_denied_hint_omits_windows_phrasing(self) -> None:
        report = MicPermissionReport(status=MicPermissionStatus.DENIED)
        with patch.object(sys, "platform", "darwin"):
            hint = report.remediation_hint
        assert "Windows" not in hint
        assert "macOS" in hint

    def test_windows_denied_hint_omits_macos_phrasing(self) -> None:
        report = MicPermissionReport(status=MicPermissionStatus.DENIED)
        with patch.object(sys, "platform", "win32"):
            hint = report.remediation_hint
        assert "macOS" not in hint
        assert "System Settings" not in hint

    def test_macos_unknown_hint_mentions_fda(self) -> None:
        report = MicPermissionReport(status=MicPermissionStatus.UNKNOWN)
        with patch.object(sys, "platform", "darwin"):
            hint = report.remediation_hint
        assert "Full Disk Access" in hint

    def test_granted_hint_empty_on_any_platform(self) -> None:
        report = MicPermissionReport(status=MicPermissionStatus.GRANTED)
        for plat in ("darwin", "win32", "linux"):
            with patch.object(sys, "platform", plat):
                assert report.remediation_hint == ""


pytestmark = pytest.mark.timeout(10)
