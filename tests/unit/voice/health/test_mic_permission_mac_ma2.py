"""Tests for the MA2 macOS TCC probe.

Mocks sys.platform + the candidate-client environment so the suite
stays cross-platform and deterministic; the TCC.db itself is a REAL
sqlite database built per-test with the canonical ``access`` schema
(MACOS-2 — the probe now reads per-row ``client``/``client_type``/
``auth_value``, so canned-cursor mocks would no longer exercise the
matching logic). Verifies:

* Client matching (MACOS-2): an unrelated app's grant (Zoom) never
  reports GRANTED; a matched-client deny wins (Terminal denied while
  hosted under Terminal → DENIED); no matched row → UNKNOWN with a
  "may belong to another app" note.
* Candidate derivation: ``__CFBundleIdentifier`` env, psutil ancestry
  heuristic, and sys.executable path-form matching.
* Deny-precedence: a matched LIMITED/ALLOWED row never outvotes a
  matched deny.
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
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
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
        # LIMITED isn't applicable to mic per Apple docs; the enum has
        # no LIMITED member so it maps to granted. Since MACOS-2 the
        # row-level verdict lets a matched deny win BEFORE limited is
        # considered, so this mapping can no longer mask a deny.
        assert auth_value_to_status_token(3) == "granted"

    def test_unrecognised_value_maps_to_unknown(self) -> None:
        # Defensive: a future Apple value (4+) maps to UNKNOWN, NOT
        # to denied. Better to fall through to the cascade's
        # post-open silence detector than to false-block.
        assert auth_value_to_status_token(99) == "unknown"


# ── Real-shaped TCC.db fixtures ───────────────────────────────────


def _make_tcc_db(tmp_path: Path, rows: list[tuple[str, str, int, int]]) -> Path:
    """Build a real sqlite TCC.db with the canonical ``access`` schema.

    ``rows`` = (service, client, client_type, auth_value)."""
    db_path = tmp_path / "TCC.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE access ("
            "service TEXT, client TEXT, client_type INTEGER, "
            "auth_value INTEGER, auth_reason INTEGER)"
        )
        conn.executemany(
            "INSERT INTO access (service, client, client_type, auth_value, auth_reason) "
            "VALUES (?, ?, ?, ?, 0)",
            [(*row,) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _fake_process_with_parents(*parent_names: str) -> Any:
    """psutil.Process replacement whose .parents() yields procs with
    the given names."""

    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        parents = []
        for name in parent_names:
            proc = MagicMock()
            proc.name.return_value = name
            parents.append(proc)
        me = MagicMock()
        me.parents.return_value = parents
        return me

    return _factory


@pytest.fixture()
def _no_ambient_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip environment/ancestry candidates so each test controls
    exactly which clients match (deterministic on any host/CI OS)."""
    monkeypatch.delenv("__CFBundleIdentifier", raising=False)


def _patch_tcc_path(db_path: Path) -> Any:
    return patch(
        "sovyx.voice.health._mic_permission_mac._user_tcc_path",
        return_value=db_path,
    )


def _patch_no_parents() -> Any:
    return patch.object(psutil, "Process", _fake_process_with_parents())


_MIC = "kTCCServiceMicrophone"


@pytest.mark.usefixtures("_no_ambient_candidates")
class TestTCCClientMatching:
    """MACOS-2 — verdicts are scoped to the client hosting the process."""

    def test_unrelated_grant_with_matched_deny_returns_denied(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Zoom GRANTED + Terminal DENIED, process hosted under
        # Terminal (env __CFBundleIdentifier) → DENIED. The pre-fix
        # MAX(auth_value) reported GRANTED here.
        db = _make_tcc_db(
            tmp_path,
            [
                (_MIC, "us.zoom.xos", 0, 2),
                (_MIC, "com.apple.Terminal", 0, 0),
            ],
        )
        monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
        ):
            auth_value, _notes = query_macos_microphone_permission()
        assert auth_value == 0

    def test_unrelated_grant_only_returns_unknown_with_hint(
        self,
        tmp_path: Path,
    ) -> None:
        # Zoom + Chrome granted, but nothing matches the hosting app →
        # UNKNOWN (None), never GRANTED, with a note explaining the
        # grants may belong to other apps.
        db = _make_tcc_db(
            tmp_path,
            [
                (_MIC, "us.zoom.xos", 0, 2),
                (_MIC, "com.google.Chrome", 0, 2),
            ],
        )
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
            patch.object(sys, "executable", "/opt/sovyx/bin/python-sovyx"),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("belong to other apps" in n for n in notes)
        assert any("Privacy & Security" in n for n in notes)

    def test_matched_client_granted_returns_allowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _make_tcc_db(tmp_path, [(_MIC, "com.apple.Terminal", 0, 2)])
        monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value == 2  # noqa: PLR2004
        assert not any("belong to other apps" in n for n in notes)

    def test_ancestry_heuristic_matches_iterm(self, tmp_path: Path) -> None:
        # No env bundle id; a psutil ancestor named "iTerm2" maps to
        # com.googlecode.iterm2 via the heuristic constant.
        db = _make_tcc_db(tmp_path, [(_MIC, "com.googlecode.iterm2", 0, 2)])
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            patch.object(psutil, "Process", _fake_process_with_parents("zsh", "iTerm2")),
        ):
            auth_value, _notes = query_macos_microphone_permission()
        assert auth_value == 2  # noqa: PLR2004

    def test_path_form_client_matches_sys_executable(self, tmp_path: Path) -> None:
        db = _make_tcc_db(
            tmp_path,
            [(_MIC, "/usr/local/bin/python3.12", 1, 2)],
        )
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
            patch.object(sys, "executable", "/usr/local/bin/python3.12"),
        ):
            auth_value, _notes = query_macos_microphone_permission()
        assert auth_value == 2  # noqa: PLR2004

    def test_path_form_client_matches_by_basename_suffix(self, tmp_path: Path) -> None:
        # Venv interpreter vs the TCC row for the base interpreter —
        # basename-suffix heuristic bridges the two path forms.
        db = _make_tcc_db(
            tmp_path,
            [(_MIC, "/usr/local/bin/python3.12", 1, 2)],
        )
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
            patch.object(sys, "executable", "/Users/op/venv/bin/python3.12"),
        ):
            auth_value, _notes = query_macos_microphone_permission()
        assert auth_value == 2  # noqa: PLR2004

    def test_matched_deny_beats_matched_limited_and_allowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # LIMITED=3 > ALLOWED=2 > DENIED=0 numerically — the old MAX
        # let a limited row outvote an explicit deny. Deny must win.
        db = _make_tcc_db(
            tmp_path,
            [
                (_MIC, "com.apple.Terminal", 0, 3),
                (_MIC, "com.apple.Terminal", 0, 2),
                (_MIC, "com.apple.Terminal", 0, 0),
            ],
        )
        monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
        ):
            auth_value, _notes = query_macos_microphone_permission()
        assert auth_value == 0

    def test_matched_limited_only_returns_limited(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _make_tcc_db(tmp_path, [(_MIC, "com.apple.Terminal", 0, 3)])
        monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
        ):
            auth_value, _notes = query_macos_microphone_permission()
        assert auth_value == 3  # noqa: PLR2004
        assert auth_value_to_status_token(auth_value) == "granted"

    def test_matched_never_asked_returns_unknown_value(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _make_tcc_db(tmp_path, [(_MIC, "com.apple.Terminal", 0, 1)])
        monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
        ):
            auth_value, _notes = query_macos_microphone_permission()
        assert auth_value == 1
        assert auth_value_to_status_token(auth_value) == "unknown"

    def test_other_service_rows_ignored(self, tmp_path: Path) -> None:
        # Camera rows must never contribute to the microphone verdict.
        db = _make_tcc_db(
            tmp_path,
            [("kTCCServiceCamera", "com.apple.Terminal", 0, 2)],
        )
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("never asked" in n for n in notes)


@pytest.mark.usefixtures("_no_ambient_candidates")
class TestTCCReaderFailures:
    def test_no_tcc_db_file_returns_none_with_note(self, tmp_path: Path) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(tmp_path / "missing" / "TCC.db"),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("not found" in n for n in notes)

    def test_open_operationalerror_returns_none_with_fda_note(
        self,
        tmp_path: Path,
    ) -> None:
        # sqlite3.OperationalError on connect → likely needs Full
        # Disk Access. The note must mention FDA so the dashboard
        # can recommend granting it (vs. misclassifying mic state).
        db = _make_tcc_db(tmp_path, [])
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            patch(
                "sqlite3.connect",
                side_effect=sqlite3.OperationalError("unable to open database file"),
            ),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("Full Disk Access" in n for n in notes)

    def test_unexpected_open_error_returns_none(self, tmp_path: Path) -> None:
        db = _make_tcc_db(tmp_path, [])
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            patch("sqlite3.connect", side_effect=RuntimeError("boom")),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("unexpected sqlite open" in n for n in notes)

    def test_no_microphone_rows_returns_none(self, tmp_path: Path) -> None:
        # Empty access table → Sovyx never asked; macOS will prompt on
        # first capture. UNKNOWN, not DENIED.
        db = _make_tcc_db(tmp_path, [])
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("never asked" in n for n in notes)

    def test_query_operationalerror_returns_none(self, tmp_path: Path) -> None:
        # A TCC.db without the ``access`` table (schema drift) → the
        # SELECT raises OperationalError → (None, notes).
        db_path = tmp_path / "TCC.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE unrelated (x TEXT)")
        conn.commit()
        conn.close()
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db_path),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("query failed" in n for n in notes)

    def test_psutil_walk_failure_degrades_to_note(
        self,
        tmp_path: Path,
    ) -> None:
        # Ancestry walk crashing must not fail the probe — it just
        # loses one candidate source (env + sys.executable remain).
        db = _make_tcc_db(tmp_path, [(_MIC, "us.zoom.xos", 0, 2)])
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            patch.object(psutil, "Process", side_effect=RuntimeError("no proc")),
            patch.object(sys, "executable", "/opt/sovyx/bin/python-sovyx"),
        ):
            auth_value, notes = query_macos_microphone_permission()
        assert auth_value is None
        assert any("process-ancestry walk unavailable" in n for n in notes)


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

    def test_darwin_unmatched_grants_end_to_end_unknown(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # MACOS-2 regression, full chain: Zoom-only grant → probe
        # returns None → check_microphone_permission → UNKNOWN (the
        # pre-fix chain reported GRANTED here) and the notes carry the
        # other-app explanation for the dashboard.
        monkeypatch.delenv("__CFBundleIdentifier", raising=False)
        db = _make_tcc_db(tmp_path, [(_MIC, "us.zoom.xos", 0, 2)])
        with (
            patch.object(sys, "platform", "darwin"),
            _patch_tcc_path(db),
            _patch_no_parents(),
            patch.object(sys, "executable", "/opt/sovyx/bin/python-sovyx"),
        ):
            report = check_microphone_permission()
        assert report.status is MicPermissionStatus.UNKNOWN
        assert any("belong to other apps" in n for n in report.notes)

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
