"""Tests for :mod:`sovyx.voice.health._alsa_ucm` (F4 layer 2).

Mocks ``shutil.which`` + ``subprocess.run`` so the suite stays
cross-platform and deterministic.

Fixture truthfulness (Debugging Rule #13 / audit finding LINUX-3):
the fake alsaucm output mirrors the REAL binary formats from upstream
alsa-utils ``alsaucm/usecase.c``:

* ``list _verbs`` → ``  0: HiFi`` numbered items, verb comment on a
  following more-indented line, ``  list is empty`` for an empty list
  (rc=0), non-zero exit + stderr when the card has no UCM profile.
* ``get _verb`` → ``  _verb=HiFi``.

The previously-fabricated ``Available verbs:\n  HiFi: Description``
shape is emitted by NO alsaucm version and is deliberately absent.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._alsa_ucm import (
    UcmReport,
    UcmRoutingError,
    UcmStatus,
    detect_ucm,
    enumerate_verbs,
    get_active_verb,
    set_verb,
)


def _fake_run(
    *,
    list_stdout: str = (
        "  0: HiFi\n"
        "    Default high-fidelity playback / capture\n"
        "  1: VoiceCall\n"
        "    Hands-free / mobile telephony\n"
    ),
    list_returncode: int = 0,
    list_raise: type[BaseException] | None = None,
    get_stdout: str = "  _verb=HiFi\n",
    get_returncode: int = 0,
    get_raise: type[BaseException] | None = None,
    set_stdout: str = "",
    set_returncode: int = 0,
    set_raise: type[BaseException] | None = None,
) -> Any:
    def _run(args: tuple[str, ...], **_kwargs: Any) -> Any:
        verb = args[3] if len(args) >= 4 else ""  # noqa: PLR2004
        if verb == "list":
            if list_raise is not None:
                raise list_raise(args, _kwargs.get("timeout", 0))
            return MagicMock(returncode=list_returncode, stdout=list_stdout, stderr="")
        if verb == "get":
            if get_raise is not None:
                raise get_raise(args, _kwargs.get("timeout", 0))
            return MagicMock(returncode=get_returncode, stdout=get_stdout, stderr="")
        if verb == "set":
            if set_raise is not None:
                raise set_raise(args, _kwargs.get("timeout", 0))
            return MagicMock(
                returncode=set_returncode,
                stdout=set_stdout,
                stderr="set failure context" if set_returncode else "",
            )
        return MagicMock(returncode=1, stdout="", stderr="unknown verb")

    return _run


# ── Cross-platform branches ────────────────────────────────────────


class TestNonLinuxBranches:
    def test_windows_returns_unavailable(self) -> None:
        with patch.object(sys, "platform", "win32"):
            r = detect_ucm("0")
        assert r.status is UcmStatus.UNAVAILABLE
        assert any("non-linux" in n for n in r.notes)

    def test_darwin_returns_unavailable(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            r = detect_ucm("0")
        assert r.status is UcmStatus.UNAVAILABLE


# ── Linux detection ────────────────────────────────────────────────


class TestLinuxDetection:
    def test_alsaucm_missing_returns_unavailable(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value=None),
        ):
            r = detect_ucm("PCH")
        assert r.status is UcmStatus.UNAVAILABLE
        assert r.alsaucm_available is False
        assert r.card_id == "PCH"

    def test_empty_verb_list_returns_no_profile(self) -> None:
        # Real alsaucm prints "  list is empty" with rc=0 when the
        # profile exists but lists zero verbs. Pre-fix the parser
        # accepted that literal line as a phantom verb name → false
        # AVAILABLE (audit finding LINUX-3).
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    list_stdout="  list is empty\n",
                    get_stdout="",
                    get_returncode=1,
                ),
            ),
        ):
            r = detect_ucm("PCH")
        assert r.status is UcmStatus.NO_PROFILE
        assert r.verbs == ()

    def test_card_without_profile_nonzero_exit_returns_no_profile(self) -> None:
        # A card WITHOUT a UCM profile makes real alsaucm exit non-zero
        # ("error failed to import hw:N use case configuration").
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    list_returncode=99,
                    list_stdout="",
                    get_stdout="",
                    get_returncode=99,
                ),
            ),
        ):
            r = detect_ucm("PCH")
        assert r.status is UcmStatus.NO_PROFILE
        assert r.verbs == ()

    def test_verbs_with_no_active_returns_available(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(get_stdout="", get_returncode=1),
            ),
        ):
            r = detect_ucm("PCH")
        assert r.status is UcmStatus.AVAILABLE
        assert r.verbs == ("HiFi", "VoiceCall")
        assert r.active_verb is None

    def test_active_verb_in_list_returns_active(self) -> None:
        # Real ``get _verb`` output is "  _verb=HiFi" — the parser must
        # strip the "_verb=" prefix or ACTIVE is unreachable (LINUX-3).
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch("subprocess.run", side_effect=_fake_run(get_stdout="  _verb=HiFi\n")),
        ):
            r = detect_ucm("PCH")
        assert r.status is UcmStatus.ACTIVE
        assert r.active_verb == "HiFi"

    def test_active_verb_not_in_list_returns_available(self) -> None:
        # Stale active_verb (verb removed from profile) — defensive
        # downgrade to AVAILABLE so the cascade can pick a real one.
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(get_stdout="  _verb=GhostVerb\n"),
            ),
        ):
            r = detect_ucm("PCH")
        assert r.status is UcmStatus.AVAILABLE
        assert r.active_verb == "GhostVerb"

    def test_quoted_active_verb_is_unquoted(self) -> None:
        # Defensive legacy tolerance — quoting + bare values are not
        # what real alsaucm emits but must not break the parser.
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch("subprocess.run", side_effect=_fake_run(get_stdout='"HiFi"\n')),
        ):
            r = detect_ucm("PCH")
        assert r.active_verb == "HiFi"

    def test_list_timeout_returns_unknown(self) -> None:
        # Genuine probe failure (timeout) — we could not ask the card,
        # so NO_PROFILE would be a truthfulness inversion. UNKNOWN was
        # previously unreachable on every path (LINUX-3).
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(list_raise=subprocess.TimeoutExpired),
            ),
        ):
            r = detect_ucm("PCH")
        assert r.status is UcmStatus.UNKNOWN
        assert any("timed out" in n for n in r.notes)

    def test_list_spawn_failure_returns_unknown(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(list_raise=OSError),
            ),
        ):
            r = detect_ucm("PCH")
        assert r.status is UcmStatus.UNKNOWN

    def test_list_nonzero_returns_no_profile_with_note(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(list_returncode=1, list_stdout=""),
            ),
        ):
            r = detect_ucm("PCH")
        assert r.status is UcmStatus.NO_PROFILE
        assert any("exited 1" in n for n in r.notes)

    def test_comment_lines_and_colons_do_not_pollute_verbs(self) -> None:
        # Verb comments follow on their own more-indented line and may
        # contain colons; verbs with spaces in the name must survive.
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    list_stdout=(
                        "  0: HiFi\n    Play HiFi: quality music\n  1: Voice Call\n  2: HDMI\n"
                    ),
                    get_stdout="",
                    get_returncode=1,
                ),
            ),
        ):
            r = detect_ucm("PCH")
        assert r.verbs == ("HiFi", "Voice Call", "HDMI")


# ── Standalone helpers ─────────────────────────────────────────────


class TestStandaloneHelpers:
    def test_enumerate_verbs_returns_empty_when_alsaucm_missing(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value=None),
        ):
            assert enumerate_verbs("0") == ()

    def test_enumerate_verbs_returns_empty_on_non_linux(self) -> None:
        with patch.object(sys, "platform", "win32"):
            assert enumerate_verbs("0") == ()

    def test_get_active_verb_returns_none_on_non_linux(self) -> None:
        with patch.object(sys, "platform", "win32"):
            assert get_active_verb("0") is None

    def test_get_active_verb_returns_none_when_alsaucm_missing(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value=None),
        ):
            assert get_active_verb("0") is None

    def test_get_active_verb_strips_quotes(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch("subprocess.run", side_effect=_fake_run(get_stdout="'HiFi'\n")),
        ):
            assert get_active_verb("0") == "HiFi"

    def test_get_active_verb_strips_real_key_value_prefix(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch("subprocess.run", side_effect=_fake_run(get_stdout="  _verb=HiFi\n")),
        ):
            assert get_active_verb("0") == "HiFi"

    def test_enumerate_verbs_returns_empty_on_probe_failure(self) -> None:
        # Standalone helper keeps its ()-on-any-failure contract even
        # though detect_ucm now distinguishes UNKNOWN.
        with (
            patch.object(sys, "platform", "linux"),
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(list_raise=subprocess.TimeoutExpired),
            ),
        ):
            assert enumerate_verbs("0") == ()


# ── set_verb routing ───────────────────────────────────────────────


class TestSetVerb:
    @pytest.mark.asyncio
    async def test_success_returns_none(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch("subprocess.run", side_effect=_fake_run()),
        ):
            await set_verb("PCH", "HiFi")  # No exception = success.

    @pytest.mark.asyncio
    async def test_raises_when_alsaucm_missing(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(UcmRoutingError, match="alsaucm binary not found"),
        ):
            await set_verb("PCH", "HiFi")

    @pytest.mark.asyncio
    async def test_raises_on_subprocess_timeout(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(set_raise=subprocess.TimeoutExpired),
            ),
            pytest.raises(UcmRoutingError, match="exceeded"),
        ):
            await set_verb("PCH", "HiFi")

    @pytest.mark.asyncio
    async def test_raises_on_nonzero_with_structured_detail(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/alsaucm"),
            patch("subprocess.run", side_effect=_fake_run(set_returncode=1)),
            pytest.raises(UcmRoutingError) as exc_info,
        ):
            await set_verb("PCH", "HiFi")
        assert exc_info.value.returncode == 1
        assert "set failure context" in exc_info.value.stderr
        assert "HiFi" in exc_info.value.command
        assert "PCH" in exc_info.value.command


# ── Report contract ────────────────────────────────────────────────


class TestReportContract:
    def test_status_enum_values_stable(self) -> None:
        assert UcmStatus.UNAVAILABLE.value == "unavailable"
        assert UcmStatus.NO_PROFILE.value == "no_profile"
        assert UcmStatus.AVAILABLE.value == "available"
        assert UcmStatus.ACTIVE.value == "active"
        assert UcmStatus.UNKNOWN.value == "unknown"

    def test_default_report_carries_card_id(self) -> None:
        r = UcmReport(status=UcmStatus.UNAVAILABLE, card_id="0")
        assert r.card_id == "0"
        assert r.verbs == ()
        assert r.active_verb is None
