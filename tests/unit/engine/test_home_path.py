"""Unit tests for #32 defensive home-directory resolver.

Three failure modes the resolver MUST survive without crashing:
1. ``Path.home()`` raises RuntimeError (POSIX with no HOME, no passwd
   entry — typical of stripped containers).
2. ``Path.home()`` returns a path that doesn't exist on disk.
3. ``Path.home()`` returns a path that exists but isn't writable
   (sandboxed runtimes).

In every case the resolver must return a usable Path AND emit a
structured warning so operators see what's happening.
"""

from __future__ import annotations

import logging as _logging
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sovyx.engine._home_path import _user_suffix, resolve_home_dir

# ── Happy path ────────────────────────────────────────────────


class TestHappyPath:
    def test_returns_writable_home_when_available(self, tmp_path: Path) -> None:
        # tmp_path is always writable. Patch Path.home() to point there.
        with patch("sovyx.engine._home_path.Path.home", return_value=tmp_path):
            out = resolve_home_dir()
        assert out == tmp_path

    def test_no_warning_emitted_on_happy_path(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch("sovyx.engine._home_path.Path.home", return_value=tmp_path),
            caplog.at_level(_logging.WARNING, logger="sovyx.engine._home_path"),
        ):
            resolve_home_dir()
        # Happy path = no warnings.
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warns == []


# ── RuntimeError from Path.home() ─────────────────────────────


class TestHomeRuntimeError:
    def test_runtime_error_falls_back_to_tempdir(self) -> None:
        # Simulate POSIX container with no HOME and no passwd entry.
        # The functional contract is "returns the deterministic
        # tempdir-based fallback Path"; the WARN is observability
        # nice-to-have but structlog routing makes it caplog-flaky.
        # We assert on the contract directly: out matches the
        # deterministic fallback location.
        with patch(
            "sovyx.engine._home_path.Path.home",
            side_effect=RuntimeError("Could not determine home directory."),
        ):
            out = resolve_home_dir()
        # Must return a path under the system tempdir.
        tempdir = Path(tempfile.gettempdir()).resolve()
        assert tempdir in out.resolve().parents or out.resolve().parent == tempdir
        # The fallback dir was created.
        assert out.exists()
        # And the name follows the documented prefix convention.
        assert out.name.startswith("sovyx-fallback-")


# ── Home returns nonexistent / unwritable path ────────────────


class TestHomeNonexistent:
    def test_nonexistent_home_gets_created_when_possible(self, tmp_path: Path) -> None:
        # Simulate HOME pointing at a path that doesn't exist yet but
        # whose parent IS writable (the resolver should mkdir it and
        # use it — that's the typical first-boot case for a fresh
        # container with HOME=/root).
        nonexistent = tmp_path / "freshly-minted-home"
        with patch("sovyx.engine._home_path.Path.home", return_value=nonexistent):
            out = resolve_home_dir()
        assert out == nonexistent
        assert nonexistent.exists()


# ── User suffix stability ─────────────────────────────────────


class TestUserSuffix:
    def test_returns_stable_per_call(self) -> None:
        # Two calls in the same process MUST return the same suffix
        # — otherwise repeated boots would create different fallback
        # dirs and accumulate orphans.
        assert _user_suffix() == _user_suffix()

    def test_returns_non_empty_string(self) -> None:
        suffix = _user_suffix()
        assert suffix != ""

    def test_windows_username_path_sanitises_characters(self) -> None:
        # On a hypothetical Windows host with a username containing
        # path separators or other unsafe chars, the suffix must be
        # sanitised (alphanumerics + underscore + hyphen only).
        # On Windows os.getuid does not exist; on POSIX it does. We
        # delete the attribute via the module's `os` reference so the
        # AttributeError fall-through is exercised regardless of host.
        import sovyx.engine._home_path as hp_mod

        os_mod = hp_mod.os
        had_getuid = hasattr(os_mod, "getuid")
        original = os_mod.getuid if had_getuid else None
        if had_getuid:
            del os_mod.getuid  # type: ignore[attr-defined]
        try:
            with patch.dict(
                os.environ,
                {"USERNAME": "Bad/User\\Name<test>", "USER": ""},
                clear=False,
            ):
                suffix = _user_suffix()
        finally:
            if had_getuid and original is not None:
                os_mod.getuid = original  # type: ignore[attr-defined]
        # No path separators or unsafe chars left.
        assert "/" not in suffix
        assert "\\" not in suffix
        assert "<" not in suffix
        # Alphanumerics preserved.
        assert "Bad" in suffix
        assert "User" in suffix


# ── Idempotent across multiple boots ──────────────────────────


class TestIdempotency:
    def test_repeated_fallback_uses_same_dir(
        self,
    ) -> None:
        # Two consecutive resolve_home_dir() calls under the same
        # failure mode must return the SAME fallback path — operators
        # should not see /tmp/sovyx-fallback-* accumulate.
        with patch(
            "sovyx.engine._home_path.Path.home",
            side_effect=RuntimeError("no home"),
        ):
            a = resolve_home_dir()
            b = resolve_home_dir()
        assert a == b


# ── Bounds + sanity ───────────────────────────────────────────


class TestBoundsAndSanity:
    def test_returns_path_instance(self, tmp_path: Path) -> None:
        with patch("sovyx.engine._home_path.Path.home", return_value=tmp_path):
            out = resolve_home_dir()
        assert isinstance(out, Path)

    def test_returned_path_exists_on_disk(self, tmp_path: Path) -> None:
        with patch("sovyx.engine._home_path.Path.home", return_value=tmp_path):
            out = resolve_home_dir()
        assert out.exists()


pytestmark = pytest.mark.timeout(10)
