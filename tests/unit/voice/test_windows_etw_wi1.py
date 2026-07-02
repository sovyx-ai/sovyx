"""Tests for the WI1 Windows audio ETW event-log probe.

Coverage:
* Cross-platform safety (non-win32 returns structured UNKNOWN per
  channel without raising).
* wevtutil binary missing → NOT-found note per channel.
* wevtutil timeout / OSError isolated per channel.
* Channel-not-found RETURN CODE (15007 / 0x80073A9F) → "channel not
  present" note — locale-neutral; the pre-WINDOWS-2 English-stderr
  sniff misclassified absent channels on pt-BR Windows.
* Successful parse of REAL wevtutil ``/f:XML`` output captured on a
  pt-BR Windows 11 host (WINDOWS-9 convention: parsers of OS tool
  output carry at least one real localized-host fixture).
* Numeric ``<Level>`` mapping (locale-neutral; localized level VALUES
  like ``Informações`` broke the old ``/f:Text`` token table).
* Malformed XML appends a note (probe-failure isolation — the probe
  must never be silently blind).
* Level filter integer mapping (XPath construction).
* Bounds clamping on lookback_seconds + max_events_per_channel.
* Per-event raw_text truncation at 4 KB.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._windows_etw import (
    EtwEvent,
    EtwEventLevel,
    EtwQueryResult,
    _is_channel_not_found_returncode,
    _level_to_microsoft_int,
    _parse_wevtutil_xml_output,
    query_audio_etw_events,
)


def _fake_run(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raise_exc: type[BaseException] | None = None,
) -> Any:
    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        if raise_exc is not None:
            raise raise_exc("simulated failure")
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    return _factory


def _parse(stdout: str, *, channel: str = "C") -> tuple[tuple[EtwEvent, ...], list[str]]:
    """Run the XML parser, returning (events, notes)."""
    notes: list[str] = []
    events = _parse_wevtutil_xml_output(stdout, channel=channel, notes=notes)
    return events, notes


# ── Cross-platform safety ─────────────────────────────────────────


class TestCrossPlatformSafety:
    def test_linux_returns_unknown_per_channel(self) -> None:
        with patch.object(sys, "platform", "linux"):
            results = query_audio_etw_events()
        assert len(results) >= 1
        for r in results:
            assert isinstance(r, EtwQueryResult)
            assert r.events == ()
            assert any("non-windows" in n for n in r.notes)

    def test_darwin_returns_unknown_per_channel(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            results = query_audio_etw_events()
        for r in results:
            assert r.events == ()
            assert any("non-windows" in n for n in r.notes)

    def test_returned_tuple_matches_default_channels(self) -> None:
        with patch.object(sys, "platform", "linux"):
            results = query_audio_etw_events()
        # Defaults to the three audio channels.
        assert len(results) == 3  # noqa: PLR2004
        names = {r.channel for r in results}
        assert "Microsoft-Windows-Audio/Operational" in names

    def test_custom_channel_set_respected(self) -> None:
        with patch.object(sys, "platform", "linux"):
            results = query_audio_etw_events(channels=("CustomChannel",))
        assert len(results) == 1
        assert results[0].channel == "CustomChannel"


# ── Probe failure isolation ───────────────────────────────────────


class TestProbeFailures:
    def test_wevtutil_missing_returns_per_channel_note(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value=None),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert r.events == ()
            assert any("not found" in n for n in r.notes)

    def test_wevtutil_timeout_isolated_per_channel(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired("wevtutil", 5),
            ),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert r.events == ()
            assert any("timed out" in n for n in r.notes)

    def test_wevtutil_oserror_isolated(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch("subprocess.run", side_effect=OSError("spawn boom")),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert any("spawn failed" in n for n in r.notes)

    def test_channel_not_found_rc_with_ptbr_stderr_classified(self) -> None:
        # REAL pt-BR Windows 11 behaviour (captured 2026-07-02):
        # rc=15007 (ERROR_EVT_CHANNEL_NOT_FOUND) with fully localized
        # stderr. The pre-WINDOWS-2 code grepped stderr for English
        # "not found" and misclassified the absent channel as a
        # generic probe failure. Classification MUST key on the
        # return code, never the localized text.
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    returncode=15007,
                    stderr=(
                        "Não foi possível encontrar o canal especificado.\n\n"
                        "Falha ao abrir a consulta de evento.\n"
                        "Não foi possível encontrar o canal especificado.\n"
                    ),
                ),
            ),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert r.events == ()
            assert any("not present" in n for n in r.notes)

    def test_channel_not_found_rc_english_stderr_classified(self) -> None:
        # Same rc on an English host — identical classification.
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(
                    returncode=15007,
                    stderr="The specified channel could not be found.",
                ),
            ),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert any("not present" in n for n in r.notes)

    def test_unknown_nonzero_exit_collapses_to_note(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(returncode=42, stderr="some weird error"),
            ),
        ):
            results = query_audio_etw_events()
        for r in results:
            assert any("exited 42" in n for n in r.notes)


class TestChannelNotFoundReturncode:
    def test_win32_error_code_matches(self) -> None:
        assert _is_channel_not_found_returncode(15007) is True

    def test_hresult_form_matches(self) -> None:
        assert _is_channel_not_found_returncode(0x80073A9F) is True

    def test_negative_hresult_form_matches(self) -> None:
        # An HRESULT surfaced as a negative CPython exit code.
        assert _is_channel_not_found_returncode(0x80073A9F - (1 << 32)) is True

    @pytest.mark.parametrize("rc", [0, 1, 42, 5, 15008, 0x3A9E])
    def test_other_codes_do_not_match(self, rc: int) -> None:
        assert _is_channel_not_found_returncode(rc) is False


# ── Successful parse — REAL captured fixture ─────────────────────

# Verbatim `wevtutil qe Microsoft-Windows-Audio/Operational /c:3 /f:XML`
# output captured on the operator's pt-BR Windows 11 host (2026-07-02):
# a CONCATENATED sequence of <Event> roots on one line, no enclosing
# document element, numeric <Level>, attribute-form provider/timestamp.
_REAL_WEVTUTIL_XML = (
    "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
    "<System><Provider Name='Microsoft-Windows-Audio'"
    " Guid='{ae4bd3be-f36f-45b6-8d21-bdd6fb832853}'/><EventID>65</EventID>"
    "<Version>0</Version><Level>4</Level><Task>129</Task><Opcode>56</Opcode>"
    "<Keywords>0x4000000000000000</Keywords>"
    "<TimeCreated SystemTime='2026-02-22T15:46:34.3677596Z'/>"
    "<EventRecordID>364</EventRecordID><Correlation/>"
    "<Execution ProcessID='2828' ThreadID='2996'/>"
    "<Channel>Microsoft-Windows-Audio/Operational</Channel>"
    "<Computer>G-DESKTOP</Computer><Security UserID='S-1-5-18'/></System>"
    "<EventData><Data Name='DeviceName'>High Definition Audio Device</Data>"
    "<Data Name='DeviceId'>{0.0.1.00000000}.{7529a723-242e-4d4c-94d4-feb0fdcc58d1}</Data>"
    "<Data Name='flow'>1</Data><Data Name='NewState'>8</Data></EventData></Event>"
    "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
    "<System><Provider Name='Microsoft-Windows-Audio'"
    " Guid='{ae4bd3be-f36f-45b6-8d21-bdd6fb832853}'/><EventID>65</EventID>"
    "<Version>0</Version><Level>4</Level><Task>129</Task><Opcode>56</Opcode>"
    "<Keywords>0x4000000000000000</Keywords>"
    "<TimeCreated SystemTime='2026-02-22T15:46:34.3688824Z'/>"
    "<EventRecordID>365</EventRecordID><Correlation/>"
    "<Execution ProcessID='2828' ThreadID='2996'/>"
    "<Channel>Microsoft-Windows-Audio/Operational</Channel>"
    "<Computer>G-DESKTOP</Computer><Security UserID='S-1-5-18'/></System>"
    "<EventData><Data Name='DeviceName'>High Definition Audio Device</Data>"
    "<Data Name='DeviceId'>{0.0.1.00000000}.{7529a723-242e-4d4c-94d4-feb0fdcc58d1}</Data>"
    "<Data Name='flow'>1</Data><Data Name='NewState'>4</Data></EventData></Event>"
    "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
    "<System><Provider Name='Microsoft-Windows-Audio'"
    " Guid='{ae4bd3be-f36f-45b6-8d21-bdd6fb832853}'/><EventID>65</EventID>"
    "<Version>0</Version><Level>4</Level><Task>129</Task><Opcode>56</Opcode>"
    "<Keywords>0x4000000000000000</Keywords>"
    "<TimeCreated SystemTime='2026-02-22T15:46:34.3738215Z'/>"
    "<EventRecordID>366</EventRecordID><Correlation/>"
    "<Execution ProcessID='2828' ThreadID='2996'/>"
    "<Channel>Microsoft-Windows-Audio/Operational</Channel>"
    "<Computer>G-DESKTOP</Computer><Security UserID='S-1-5-18'/></System>"
    "<EventData><Data Name='DeviceName'>Razer BlackShark V2 Pro</Data>"
    "<Data Name='DeviceId'>{0.0.1.00000000}.{8981deb5-1e0d-4121-9d31-cc1e400f098d}</Data>"
    "<Data Name='flow'>1</Data><Data Name='NewState'>1</Data></EventData></Event>"
)


def _synthetic_event(
    *,
    level: str = "<Level>2</Level>",
    event_id: str = "<EventID>12</EventID>",
    data: str = "<Data Name='DeviceName'>Mic</Data>",
) -> str:
    return (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
        "<System><Provider Name='Microsoft-Windows-Audio'/>"
        f"{event_id}{level}"
        "<TimeCreated SystemTime='2026-04-25T12:30:00.000Z'/>"
        "<Channel>Microsoft-Windows-Audio/Operational</Channel></System>"
        f"<EventData>{data}</EventData></Event>"
    )


class TestSuccessfulParseRealFixture:
    def test_parses_three_events_from_real_output(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout=_REAL_WEVTUTIL_XML, returncode=0),
            ),
        ):
            results = query_audio_etw_events(channels=("OneChannel",))
        assert len(results) == 1
        r = results[0]
        assert r.notes == ()
        assert len(r.events) == 3  # noqa: PLR2004

    def test_first_event_fields_parsed(self) -> None:
        events, notes = _parse(
            _REAL_WEVTUTIL_XML,
            channel="Microsoft-Windows-Audio/Operational",
        )
        assert notes == []
        assert events[0].event_id == 65  # noqa: PLR2004
        # Numeric <Level>4</Level> → INFO. On this pt-BR host the
        # /f:Text render emitted the LOCALIZED value "Informações",
        # which the retired English token table mapped to UNKNOWN.
        assert events[0].level is EtwEventLevel.INFO
        assert events[0].timestamp_iso == "2026-02-22T15:46:34.3677596Z"
        assert events[0].provider == "Microsoft-Windows-Audio"

    def test_event_data_synthesized_into_description(self) -> None:
        events, _ = _parse(_REAL_WEVTUTIL_XML)
        assert "DeviceName=Razer BlackShark V2 Pro" in events[2].description
        assert "NewState=1" in events[2].description

    def test_raw_text_carries_event_xml(self) -> None:
        # ElementTree re-serialization may prefix tags (ns0:) — assert
        # on the load-bearing content, not the exact serialization.
        events, _ = _parse(_REAL_WEVTUTIL_XML)
        assert "EventID" in events[0].raw_text
        assert ">65<" in events[0].raw_text
        assert "Razer BlackShark V2 Pro" in events[2].raw_text
        # Each event carries ONLY its own XML.
        assert "Razer" not in events[0].raw_text


class TestSuccessfulParseSynthetic:
    def test_error_level_classified(self) -> None:
        events, _ = _parse(_synthetic_event(level="<Level>2</Level>"))
        assert len(events) == 1
        assert events[0].level is EtwEventLevel.ERROR

    @pytest.mark.parametrize(
        ("level_int", "expected"),
        [
            (0, EtwEventLevel.INFO),  # LogAlways
            (1, EtwEventLevel.CRITICAL),
            (2, EtwEventLevel.ERROR),
            (3, EtwEventLevel.WARNING),
            (4, EtwEventLevel.INFO),
            (5, EtwEventLevel.VERBOSE),
        ],
    )
    def test_numeric_level_mapping(self, level_int: int, expected: EtwEventLevel) -> None:
        events, _ = _parse(_synthetic_event(level=f"<Level>{level_int}</Level>"))
        assert events[0].level is expected

    def test_out_of_range_level_is_unknown(self) -> None:
        events, _ = _parse(_synthetic_event(level="<Level>99</Level>"))
        assert events[0].level is EtwEventLevel.UNKNOWN

    def test_missing_level_element_is_unknown(self) -> None:
        events, _ = _parse(_synthetic_event(level=""))
        assert events[0].level is EtwEventLevel.UNKNOWN

    def test_garbage_level_text_is_unknown(self) -> None:
        events, _ = _parse(_synthetic_event(level="<Level>Aviso</Level>"))
        assert events[0].level is EtwEventLevel.UNKNOWN

    def test_garbage_event_id_parses_as_zero(self) -> None:
        events, _ = _parse(_synthetic_event(event_id="<EventID>nope</EventID>"))
        assert events[0].event_id == 0

    def test_raw_text_truncated_to_4kb(self) -> None:
        big = _synthetic_event(data=f"<Data Name='blob'>{'x' * 10_000}</Data>")
        events, _ = _parse(big)
        assert len(events) == 1
        assert len(events[0].raw_text) == 4096  # noqa: PLR2004

    def test_description_truncated_to_512(self) -> None:
        big = _synthetic_event(data=f"<Data Name='blob'>{'y' * 1000}</Data>")
        events, _ = _parse(big)
        assert len(events[0].description) == 512  # noqa: PLR2004

    def test_unnamed_data_element_kept_as_bare_value(self) -> None:
        events, _ = _parse(_synthetic_event(data="<Data>bare-value</Data>"))
        assert "bare-value" in events[0].description


# ── Empty / malformed input ──────────────────────────────────────


class TestEdgeCases:
    def test_empty_stdout_yields_no_events_and_no_notes(self) -> None:
        events, notes = _parse("")
        assert events == ()
        assert notes == []

    def test_whitespace_stdout_yields_no_events_and_no_notes(self) -> None:
        events, notes = _parse("   \n  ")
        assert events == ()
        assert notes == []

    def test_malformed_xml_appends_note_never_silently_blind(self) -> None:
        # WINDOWS-2 regression guard: an unparseable payload MUST be
        # distinguishable from a healthy quiet channel. The retired
        # text splitter returned 0 events + 0 notes for real Windows 11
        # output (header format drift), leaving the probe silently blind.
        events, notes = _parse("<Event><System>truncated garbage")
        assert events == ()
        assert len(notes) == 1
        assert "parse failed" in notes[0]

    def test_non_event_roots_are_skipped(self) -> None:
        events, notes = _parse("<Unrelated/>" + _synthetic_event())
        assert len(events) == 1
        assert notes == []


# ── Level → Microsoft integer mapping ────────────────────────────


class TestLevelMapping:
    def test_critical_maps_to_1(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.CRITICAL) == 1

    def test_error_maps_to_2(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.ERROR) == 2  # noqa: PLR2004

    def test_warning_maps_to_3(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.WARNING) == 3  # noqa: PLR2004

    def test_info_maps_to_4(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.INFO) == 4  # noqa: PLR2004

    def test_verbose_maps_to_5(self) -> None:
        assert _level_to_microsoft_int(EtwEventLevel.VERBOSE) == 5  # noqa: PLR2004

    def test_unknown_falls_back_to_4(self) -> None:
        # UNKNOWN should default to "everything except verbose" so we
        # don't lose useful events when a contrived caller passes it.
        assert _level_to_microsoft_int(EtwEventLevel.UNKNOWN) == 4  # noqa: PLR2004


# ── Bounds clamping + argv contract ──────────────────────────────


class TestBounds:
    def test_lookback_clamped_to_one_minute_min(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout="", returncode=0),
            ) as run_mock,
        ):
            query_audio_etw_events(lookback_seconds=0, channels=("X",))
        # The XPath argument should reflect the clamped 60_000 ms.
        call_argv = run_mock.call_args.args[0]
        xpath_arg = next(a for a in call_argv if a.startswith("/q:"))
        assert "60000" in xpath_arg

    def test_lookback_clamped_to_one_day_max(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout="", returncode=0),
            ) as run_mock,
        ):
            query_audio_etw_events(lookback_seconds=10_000_000, channels=("X",))
        call_argv = run_mock.call_args.args[0]
        xpath_arg = next(a for a in call_argv if a.startswith("/q:"))
        # 86400 s × 1000 = 86_400_000 ms.
        assert "86400000" in xpath_arg

    def test_max_events_clamped_to_one_min(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout="", returncode=0),
            ) as run_mock,
        ):
            query_audio_etw_events(max_events_per_channel=0, channels=("X",))
        call_argv = run_mock.call_args.args[0]
        c_arg = next(a for a in call_argv if a.startswith("/c:"))
        assert c_arg == "/c:1"

    def test_max_events_clamped_to_500(self) -> None:
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout="", returncode=0),
            ) as run_mock,
        ):
            query_audio_etw_events(max_events_per_channel=10_000, channels=("X",))
        call_argv = run_mock.call_args.args[0]
        c_arg = next(a for a in call_argv if a.startswith("/c:"))
        assert c_arg == "/c:500"

    def test_query_uses_locale_neutral_xml_render(self) -> None:
        # WINDOWS-2 contract: /f:Text is locale/format-fragile
        # (localized Level values + Event[N]-header drift) — the argv
        # MUST request the XML render.
        with (
            patch.object(sys, "platform", "win32"),
            patch("shutil.which", return_value="C:\\Windows\\System32\\wevtutil.exe"),
            patch(
                "subprocess.run",
                side_effect=_fake_run(stdout="", returncode=0),
            ) as run_mock,
        ):
            query_audio_etw_events(channels=("X",))
        call_argv = run_mock.call_args.args[0]
        assert "/f:XML" in call_argv
        assert "/f:Text" not in call_argv


# ── Report contract ──────────────────────────────────────────────


class TestEventContract:
    def test_event_is_frozen_dataclass(self) -> None:
        ev = EtwEvent(
            channel="C",
            level=EtwEventLevel.WARNING,
            event_id=1,
        )
        with pytest.raises(Exception) as exc:  # noqa: PT011 — FrozenInstanceError
            ev.event_id = 2  # type: ignore[misc]
        assert (
            "frozen" in str(exc.value).lower()
            or "FrozenInstanceError"
            in type(
                exc.value,
            ).__name__
        )

    def test_level_enum_value_stable(self) -> None:
        # StrEnum values are part of the public contract — dashboard
        # consumes the string token.
        assert EtwEventLevel.CRITICAL.value == "critical"
        assert EtwEventLevel.ERROR.value == "error"
        assert EtwEventLevel.WARNING.value == "warning"
        assert EtwEventLevel.INFO.value == "information"
        assert EtwEventLevel.VERBOSE.value == "verbose"
        assert EtwEventLevel.UNKNOWN.value == "unknown"


pytestmark = pytest.mark.timeout(10)
