"""Tests for ``sovyx.voice.health._alsa_input_cards.enumerate_input_card_ids``.

Mission anchor:
``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 1 T1.3.

The helper drives the multi-card UCM fan-out in
:func:`sovyx.voice.factory._diagnostics._maybe_log_alsa_ucm_status`.
These tests cover its three failure modes (non-Linux, missing
``/proc/asound/cards``, unreadable proc file) plus the three success
modes (capture-only card, playback-only card excluded, mixed card).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from sovyx.voice.health import _alsa_input_cards as mod

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_PROC_CARDS_FIXTURE = """\
 0 [PCH            ]: HDA-Intel - HDA Intel PCH
                      HDA Intel PCH at 0xfccc8000 irq 75
 1 [Generic        ]: HDA-Intel - HD-Audio Generic
                      HD-Audio Generic at 0xfcc7c000 irq 76
"""


def _seed_proc_cards(tmp_path: Path, content: str = _PROC_CARDS_FIXTURE) -> tuple[Path, Path]:
    """Seed a fake /proc/asound layout under ``tmp_path``.

    Returns ``(proc_cards_path, proc_card_root)`` so the tests can
    monkeypatch the module-level constants atomically.
    """
    proc_cards = tmp_path / "cards"
    proc_cards.write_text(content, encoding="utf-8")
    return proc_cards, tmp_path


def _make_card_dir(root: Path, card_index: int, *pcm_names: str) -> Path:
    """Create ``<root>/card<N>/<pcm0c|pcm0p|...>`` empty directories."""
    card_dir = root / f"card{card_index}"
    card_dir.mkdir(parents=True, exist_ok=True)
    for pcm in pcm_names:
        (card_dir / pcm).mkdir()
    return card_dir


class TestEnumerateInputCardIds:
    def test_non_linux_returns_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        result = mod.enumerate_input_card_ids()
        assert result == []

    def test_missing_proc_cards_returns_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(mod, "_PROC_CARDS", tmp_path / "missing")
        monkeypatch.setattr(mod, "_PROC_CARD_ROOT", tmp_path)
        assert mod.enumerate_input_card_ids() == []

    def test_unreadable_proc_cards_returns_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")

        class _FakeProcCards:
            """Stand-in for /proc/asound/cards that raises OSError on read.

            We can't ``patch.object`` ``read_text`` on a real
            :class:`pathlib.WindowsPath` (slot-based, read-only). A
            small fake exposing the two attributes the production code
            consults (``exists``, ``read_text``) is the cleanest way to
            simulate the unreadable-proc case cross-platform.
            """

            def exists(self) -> bool:
                return True

            def read_text(self, *_args: object, **_kwargs: object) -> str:
                raise OSError("permission denied")

        monkeypatch.setattr(mod, "_PROC_CARDS", _FakeProcCards())
        monkeypatch.setattr(mod, "_PROC_CARD_ROOT", tmp_path)
        assert mod.enumerate_input_card_ids() == []

    def test_card_with_capture_pcm_is_included(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        proc_cards, proc_root = _seed_proc_cards(tmp_path)
        monkeypatch.setattr(mod, "_PROC_CARDS", proc_cards)
        monkeypatch.setattr(mod, "_PROC_CARD_ROOT", proc_root)
        # Card 1 = SN6180 mic codec — exposes pcm0c (capture).
        _make_card_dir(proc_root, 0, "pcm3p", "pcm7p")  # HDMI playback only
        _make_card_dir(proc_root, 1, "pcm0c", "pcm0p")  # mic + speakers

        result = mod.enumerate_input_card_ids()
        assert result == [(1, "Generic")]

    def test_playback_only_card_is_excluded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Forensic case: the user's host has card 0 = HDA Intel PCH
        # with HDMI 0/1/2/3 — all playback (pcm3p/7p/8p/9p), zero
        # capture. Card 0 MUST be excluded from the UCM probe loop
        # because alsaucm cannot help with a card that has no input.
        monkeypatch.setattr(sys, "platform", "linux")
        proc_cards, proc_root = _seed_proc_cards(tmp_path)
        monkeypatch.setattr(mod, "_PROC_CARDS", proc_cards)
        monkeypatch.setattr(mod, "_PROC_CARD_ROOT", proc_root)
        _make_card_dir(proc_root, 0, "pcm3p", "pcm7p", "pcm8p", "pcm9p")
        _make_card_dir(proc_root, 1, "pcm0c", "pcm0p")

        result = mod.enumerate_input_card_ids()
        # Only card 1 should be returned — card 0's HDMI-only PCMs
        # have no capture suffix.
        assert (0, "PCH") not in result
        assert (1, "Generic") in result

    def test_card_dir_missing_is_excluded_silently(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # /proc/asound/cards lists card 1 but the per-card directory
        # is absent (kernel module half-loaded, transient state during
        # boot). Function returns [] for that card, no exception.
        monkeypatch.setattr(sys, "platform", "linux")
        proc_cards, proc_root = _seed_proc_cards(tmp_path)
        monkeypatch.setattr(mod, "_PROC_CARDS", proc_cards)
        monkeypatch.setattr(mod, "_PROC_CARD_ROOT", proc_root)
        # Don't create any card<N>/ subdirectories.
        result = mod.enumerate_input_card_ids()
        assert result == []

    def test_mixed_card_with_both_capture_and_playback_is_included(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        proc_cards, proc_root = _seed_proc_cards(tmp_path)
        monkeypatch.setattr(mod, "_PROC_CARDS", proc_cards)
        monkeypatch.setattr(mod, "_PROC_CARD_ROOT", proc_root)
        # Both cards have capture — typical for a USB mic + on-board codec.
        _make_card_dir(proc_root, 0, "pcm0c", "pcm0p")
        _make_card_dir(proc_root, 1, "pcm0c")

        result = mod.enumerate_input_card_ids()
        # Order follows kernel index (card 0 first).
        assert result == [(0, "PCH"), (1, "Generic")]
