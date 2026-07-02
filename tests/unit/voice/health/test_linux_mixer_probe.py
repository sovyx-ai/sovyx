"""Unit tests for :mod:`sovyx.voice.health._linux_mixer_probe` log levels.

v0.38.0 / W3.F1 — F2-M09 (audit) closure. The
``/proc/asound/cards`` exists()-then-OSError TOCTOU race used to log
at DEBUG level, so operators investigating "no mixer detected" on a
system that should have mixer cards missed the signal entirely. This
file pins the WARNING-level promotion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

from sovyx.voice.health import _linux_mixer_probe as mod

if TYPE_CHECKING:
    import pytest


class _StubProcCards:
    """Minimal ``Path``-shaped stub: exists()=True + read_text raises OSError."""

    def exists(self) -> bool:
        return True

    def read_text(self, *_args: object, **_kwargs: object) -> str:
        msg = "transient FS race"
        raise OSError(msg)


class TestProcCardsReadFailureLogLevel:
    """When /proc/asound/cards exists() succeeds but read fails, log at WARNING."""

    def test_oserror_during_read_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Promoted from DEBUG → WARNING per audit F2-M09."""
        caplog.set_level(logging.DEBUG, logger="sovyx.voice.health._linux_mixer_probe")
        with (
            patch.object(mod, "sys") as sys_mock,
            patch("shutil.which", return_value="/usr/bin/amixer"),
            patch.object(mod, "_PROC_CARDS", _StubProcCards()),
        ):
            sys_mock.platform = "linux"
            result = mod.enumerate_alsa_mixer_snapshots()

        assert result == []
        # Find the structured log record + assert it was emitted at
        # WARNING (not DEBUG, the pre-fix level).
        warn_records = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "linux_mixer_proc_cards_read_failed" in r.getMessage()
        ]
        assert warn_records, (
            "expected a WARNING-level linux_mixer_proc_cards_read_failed event; "
            f"got: {[r.getMessage() for r in caplog.records]!r}"
        )
        # And conversely — there must NOT be a DEBUG-level record for
        # the same event (would mean the level got reverted).
        debug_records = [
            r
            for r in caplog.records
            if r.levelname == "DEBUG" and "linux_mixer_proc_cards_read_failed" in r.getMessage()
        ]
        assert not debug_records, (
            "linux_mixer_proc_cards_read_failed must NOT log at DEBUG (regression)"
        )


class TestTuningThreading:
    """LINUX-18 — a caller-supplied tuning must reach the classifier.

    Pre-fix ``enumerate_alsa_mixer_snapshots`` built its own fresh
    ``VoiceTuningConfig``, so a programmatic override classified with
    env defaults while the caller reported the override values."""

    _SCONTENTS = (
        "Simple mixer control 'Internal Mic Boost',0\n"
        "  Capabilities: volume\n"
        "  Limits: 0 - 3\n"
        "  Front Left: 2 [66%] [24.00dB]\n"
        "  Front Right: 2 [66%] [24.00dB]\n"
    )

    _PROC_CARDS_TEXT = (
        " 0 [Generic        ]: HDA-Intel - HD-Audio Generic\n"
        "                      HD-Audio Generic at 0x10b8000 irq 88\n"
    )

    def _snapshots(self, tuning: object) -> list[object]:
        from unittest.mock import MagicMock

        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = self._SCONTENTS
        completed.stderr = ""
        proc_cards = MagicMock()
        proc_cards.exists.return_value = True
        proc_cards.read_text.return_value = self._PROC_CARDS_TEXT
        with (
            patch.object(mod, "sys") as sys_mock,
            patch("shutil.which", return_value="/usr/bin/amixer"),
            patch.object(mod, "_PROC_CARDS", proc_cards),
            patch.object(mod.subprocess, "run", return_value=completed),
        ):
            sys_mock.platform = "linux"
            return mod.enumerate_alsa_mixer_snapshots(tuning=tuning)  # type: ignore[arg-type]

    def test_caller_tuning_drives_saturation_classification(self) -> None:
        from sovyx.engine.config import VoiceTuningConfig

        # Boost at 2/3 (ratio ~0.66). A strict override ceiling of 0.5
        # must flag saturation_risk; a lax 0.9 must not.
        strict = VoiceTuningConfig(linux_mixer_saturation_ratio_ceiling=0.5)
        lax = VoiceTuningConfig(linux_mixer_saturation_ratio_ceiling=0.9)

        strict_snaps = self._snapshots(strict)
        lax_snaps = self._snapshots(lax)
        assert strict_snaps and lax_snaps
        assert strict_snaps[0].controls[0].saturation_risk is True  # type: ignore[attr-defined]
        assert lax_snaps[0].controls[0].saturation_risk is False  # type: ignore[attr-defined]

    def test_default_none_builds_fresh_tuning(self) -> None:
        snaps = self._snapshots(None)
        assert snaps  # legacy callers keep working with tuning=None
