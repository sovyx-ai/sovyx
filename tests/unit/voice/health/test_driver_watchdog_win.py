"""Unit tests for :mod:`sovyx.voice.health._driver_watchdog_win`.

Covers the Windows Kernel-PnP Driver Watchdog pre-flight scan used by
:mod:`sovyx.voice.health._factory_integration` to decide whether to
skip exclusive-mode attempts on known-fragile hardware (v0.20.3 Razer
BlackShark V2 Pro post-mortem).

The module shells out to ``powershell.exe``; every test patches
:func:`asyncio.create_subprocess_exec` so no real process is spawned and
the suite stays cross-platform (the production module short-circuits on
non-Windows before ever calling subprocess).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice.health import _driver_watchdog_win as module
from sovyx.voice.health._driver_watchdog_win import (
    DriverWatchdogEvent,
    DriverWatchdogScan,
    _parse_events,
    scan_recent_driver_watchdog_events,
)

# Verbatim Kernel-PnP Driver Watchdog 900 event message captured on the
# operator's pt-BR Windows 11 host (2026-04-19, read-only Get-WinEvent) —
# the EXACT hardware of the v0.20.3 Kernel-Power-41 post-mortem
# (VID_1532 = Razer, PID_0528 = BlackShark V2 Pro). Note: the message
# carries ONLY the PnP device instance path + localized prose; the
# vendor friendly name ("Razer BlackShark V2 Pro") appears NOWHERE.
_REAL_RAZER_900_MESSAGE = (
    "Um thread de execução longa para a fila de eventos do dispositivo "
    "foi detectado. O thread está sendo executado por 3004 milissegundos.\n"
    "ID do Thread: 0x25DC\n"
    "Dispositivo: USB\\VID_1532&PID_0528&MI_00\\6&191a269&0&0000\n"
    "Serviço: usbaudio\n"
    "Categoria do Evento: 1\n"
    "GUID do Evento:argumento do evento  {cb3a400e-46f0-11d0-b08f-00609713053f}\n"
    ":status do argumento  0x15\n"
    ":dados específicos da categoria  0x0\n"
    ":\n"
    "{00000000-0000-0000-0000-000000000000}\n"
    "USB\\VID_1532&PID_0528&MI_00\\6&191a269&0&0000"
)

_REAL_RAZER_900_EVENT = DriverWatchdogEvent(
    event_id=900,
    time_created_iso="2026-04-19T23:34:28.3999662Z",
    message_excerpt=_REAL_RAZER_900_MESSAGE[:512],
)

# Storage-class watchdog event from the same host — carries the
# '#'-separated symbolic-link form of the PnP path (no VID/PID; a
# USB mass-storage volume). Used to pin the unrelated-device negative.
_REAL_STORAGE_900_MESSAGE = (
    "Um thread de execução longa para a fila de eventos do dispositivo "
    "foi detectado. O thread está sendo executado por 3014 milissegundos.\n"
    "ID do Thread: 0x3D28\n"
    "Dispositivo: \n"
    "Serviço: \n"
    "Categoria do Evento: 2\n"
    "\\??\\STORAGE#Volume#_??_USBSTOR#Disk&Ven_&Prod_USB_DISK_2.0&Rev_PMAP"
    "#2700431667F32C96&0#{53f56307-b6bf-11d0-94f2-00a0c91efb8b}"
    "#{53f5630d-b6bf-11d0-94f2-00a0c91efb8b}"
)

# ---------------------------------------------------------------------------
# DriverWatchdogScan.matches_device
# ---------------------------------------------------------------------------


class TestMatchesDevice:
    """Pin the case-insensitive substring match used by the pre-flight."""

    def test_empty_needle_returns_false(self) -> None:
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(
                DriverWatchdogEvent(
                    event_id=900,
                    time_created_iso="2026-04-20T00:00:00Z",
                    message_excerpt="USB\\VID_1532&PID_0528\\...",
                ),
            ),
        )
        assert scan.matches_device("") is False
        assert scan.matches_device("   ") is False

    def test_exact_substring_case_insensitive(self) -> None:
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(
                DriverWatchdogEvent(
                    event_id=900,
                    time_created_iso="2026-04-20T00:00:00Z",
                    message_excerpt="Device USB\\VID_1532&PID_0528\\6&1A2B wedged.",
                ),
            ),
        )
        assert scan.matches_device("vid_1532&pid_0528") is True
        assert scan.matches_device("VID_1532&PID_0528") is True

    def test_no_match_returns_false(self) -> None:
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(
                DriverWatchdogEvent(
                    event_id=900,
                    time_created_iso="2026-04-20T00:00:00Z",
                    message_excerpt="USB\\VID_046D&PID_0892\\ABC",
                ),
            ),
        )
        assert scan.matches_device("VID_1532&PID_0528") is False

    def test_empty_events_never_matches(self) -> None:
        scan = DriverWatchdogScan(scan_attempted=True, events=())
        assert scan.matches_device("anything") is False

    def test_friendly_name_never_matches_real_event_message(self) -> None:
        # WINDOWS-5 defect documentation: real watchdog messages carry
        # ONLY the PnP instance path — a vendor friendly name (what
        # both production callers used to pass as the sole needle)
        # can never match, which is why the Kernel-Power-41 downgrade
        # safety net never fired. Hardware-identity matching
        # (matches_hardware_id below) is the load-bearing correlator.
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(_REAL_RAZER_900_EVENT,),
        )
        assert scan.matches_device("Razer BlackShark V2 Pro") is False

    def test_any_events_property(self) -> None:
        empty = DriverWatchdogScan(scan_attempted=True, events=())
        assert empty.any_events is False
        populated = DriverWatchdogScan(
            scan_attempted=True,
            events=(
                DriverWatchdogEvent(
                    event_id=901,
                    time_created_iso="2026-04-20T00:00:00Z",
                    message_excerpt="",
                ),
            ),
        )
        assert populated.any_events is True


# ---------------------------------------------------------------------------
# DriverWatchdogScan.matches_hardware_id (WINDOWS-5)
# ---------------------------------------------------------------------------


class TestMatchesHardwareId:
    """WINDOWS-5 regression: hardware-identity correlation against the
    REAL captured 900-event message shape."""

    def test_real_razer_event_targets_razer_vid_pid(self) -> None:
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(_REAL_RAZER_900_EVENT,),
        )
        assert scan.matches_hardware_id(usb_vid="1532", usb_pid="0528") is True

    def test_real_razer_event_case_insensitive(self) -> None:
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(_REAL_RAZER_900_EVENT,),
        )
        assert scan.matches_hardware_id(usb_vid="1532", usb_pid="0528") is True
        # Uppercase / padded input normalised.
        assert scan.matches_hardware_id(usb_vid=" 1532 ", usb_pid="0528") is True

    def test_unrelated_device_does_not_match_real_event(self) -> None:
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(_REAL_RAZER_900_EVENT,),
        )
        # Logitech C922 — present on the host but NOT in the event.
        assert scan.matches_hardware_id(usb_vid="046d", usb_pid="0892") is False

    def test_storage_event_does_not_match_audio_device(self) -> None:
        # A drift storage-volume watchdog event must not downgrade the
        # USB headset.
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(
                DriverWatchdogEvent(
                    event_id=900,
                    time_created_iso="2026-04-21T20:26:59.1320254Z",
                    message_excerpt=_REAL_STORAGE_900_MESSAGE[:512],
                ),
            ),
        )
        assert scan.matches_hardware_id(usb_vid="1532", usb_pid="0528") is False

    def test_hash_separated_form_matches(self) -> None:
        # Defensive: PnP symbolic links replace path separators with
        # '#' (see the real STORAGE event above) — accept a rendering
        # that also '#'-separates the VID/PID pair itself.
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(
                DriverWatchdogEvent(
                    event_id=900,
                    time_created_iso="2026-04-20T00:00:00Z",
                    message_excerpt=(
                        "\\??\\USB#VID_1532#PID_0528#6&191a269&0&0000"
                        "#{6994ad04-93ef-11d0-a3cc-00a0c9223196}"
                    ),
                ),
            ),
        )
        assert scan.matches_hardware_id(usb_vid="1532", usb_pid="0528") is True

    def test_empty_vid_or_pid_returns_false(self) -> None:
        scan = DriverWatchdogScan(
            scan_attempted=True,
            events=(_REAL_RAZER_900_EVENT,),
        )
        assert scan.matches_hardware_id(usb_vid="", usb_pid="0528") is False
        assert scan.matches_hardware_id(usb_vid="1532", usb_pid="  ") is False

    def test_empty_events_never_match(self) -> None:
        scan = DriverWatchdogScan(scan_attempted=True, events=())
        assert scan.matches_hardware_id(usb_vid="1532", usb_pid="0528") is False


# ---------------------------------------------------------------------------
# _parse_events
# ---------------------------------------------------------------------------


class TestParseEvents:
    """Pin JSON payload normalisation and filtering."""

    def test_malformed_json_returns_none(self) -> None:
        assert _parse_events("not json at all") is None

    def test_empty_list_returns_empty_tuple(self) -> None:
        assert _parse_events("[]") == ()

    def test_single_dict_normalised_to_list(self) -> None:
        # ConvertTo-Json emits a bare object for a single item.
        payload = json.dumps(
            {
                "EventId": 900,
                "TimeCreated": "2026-04-20T12:00:00Z",
                "Message": "driver wedged",
            }
        )
        events = _parse_events(payload)
        assert events is not None
        assert len(events) == 1
        assert events[0].event_id == 900
        assert events[0].time_created_iso == "2026-04-20T12:00:00Z"
        assert events[0].message_excerpt == "driver wedged"

    def test_multi_item_list(self) -> None:
        payload = json.dumps(
            [
                {
                    "EventId": 900,
                    "TimeCreated": "2026-04-20T12:00:00Z",
                    "Message": "a",
                },
                {
                    "EventId": 901,
                    "TimeCreated": "2026-04-20T12:01:00Z",
                    "Message": "b",
                },
            ]
        )
        events = _parse_events(payload)
        assert events is not None
        assert len(events) == 2
        assert events[0].event_id == 900
        assert events[1].event_id == 901

    def test_filters_out_non_900_901_ids(self) -> None:
        payload = json.dumps(
            [
                {"EventId": 42, "TimeCreated": "x", "Message": "ignored"},
                {"EventId": 900, "TimeCreated": "y", "Message": "kept"},
                {"EventId": 0, "TimeCreated": "z", "Message": "ignored"},
            ]
        )
        events = _parse_events(payload)
        assert events is not None
        assert len(events) == 1
        assert events[0].event_id == 900

    def test_skips_non_dict_entries(self) -> None:
        payload = json.dumps(
            ["string-item", 123, None, {"EventId": 901, "TimeCreated": "t", "Message": "m"}]
        )
        events = _parse_events(payload)
        assert events is not None
        assert len(events) == 1
        assert events[0].event_id == 901

    def test_truncates_long_message(self) -> None:
        long_msg = "X" * 2000
        payload = json.dumps({"EventId": 900, "TimeCreated": "t", "Message": long_msg})
        events = _parse_events(payload)
        assert events is not None
        assert len(events[0].message_excerpt) == module._MESSAGE_TRUNCATE_CHARS

    def test_missing_fields_default_empty(self) -> None:
        payload = json.dumps({"EventId": 900})
        events = _parse_events(payload)
        assert events is not None
        assert events[0].time_created_iso == ""
        assert events[0].message_excerpt == ""

    def test_non_int_event_id_skipped(self) -> None:
        payload = json.dumps(
            [
                {"EventId": "not-a-number", "TimeCreated": "t", "Message": "m"},
                {"EventId": 900, "TimeCreated": "t", "Message": "m"},
            ]
        )
        events = _parse_events(payload)
        assert events is not None
        # "not-a-number" triggers ValueError in int() → skip
        assert len(events) == 1
        assert events[0].event_id == 900


# ---------------------------------------------------------------------------
# scan_recent_driver_watchdog_events
# ---------------------------------------------------------------------------


class TestScanRecentDriverWatchdogEvents:
    """Pin the subprocess-lifecycle handling of the scan."""

    @pytest.mark.asyncio()
    async def test_non_windows_short_circuits(self) -> None:
        with patch.object(module.sys, "platform", "linux"):
            scan = await scan_recent_driver_watchdog_events()
        assert scan.scan_attempted is False
        assert scan.scan_failed is False
        assert scan.events == ()

    @pytest.mark.asyncio()
    async def test_spawn_failure_returns_non_attempted(self) -> None:
        async def _raise(*_a: object, **_kw: object) -> None:
            raise FileNotFoundError("powershell missing")

        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(module.asyncio, "create_subprocess_exec", _raise),
        ):
            scan = await scan_recent_driver_watchdog_events()
        assert scan.scan_attempted is False
        assert scan.scan_failed is False
        assert scan.events == ()

    @pytest.mark.asyncio()
    async def test_timeout_kills_process_and_flags_failure(self) -> None:
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        # Process.kill() is sync in asyncio — plain MagicMock avoids the
        # "coroutine never awaited" warning that AsyncMock would trigger.
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        async def _spawn(*_a: object, **_kw: object) -> object:
            return proc

        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(module.asyncio, "create_subprocess_exec", _spawn),
        ):
            scan = await scan_recent_driver_watchdog_events(timeout_s=0.01)

        assert scan.scan_attempted is True
        assert scan.scan_failed is True
        proc.kill.assert_called_once()

    @pytest.mark.asyncio()
    async def test_nonzero_exit_flags_failure(self) -> None:
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"boom"))

        async def _spawn(*_a: object, **_kw: object) -> object:
            return proc

        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(module.asyncio, "create_subprocess_exec", _spawn),
        ):
            scan = await scan_recent_driver_watchdog_events()

        assert scan.scan_attempted is True
        assert scan.scan_failed is True

    @pytest.mark.asyncio()
    async def test_empty_stdout_is_clean_bill_of_health(self) -> None:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"   \n", b""))

        async def _spawn(*_a: object, **_kw: object) -> object:
            return proc

        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(module.asyncio, "create_subprocess_exec", _spawn),
        ):
            scan = await scan_recent_driver_watchdog_events()

        assert scan.scan_attempted is True
        assert scan.scan_failed is False
        assert scan.events == ()

    @pytest.mark.asyncio()
    async def test_malformed_json_flags_failure(self) -> None:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"{not json", b""))

        async def _spawn(*_a: object, **_kw: object) -> object:
            return proc

        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(module.asyncio, "create_subprocess_exec", _spawn),
        ):
            scan = await scan_recent_driver_watchdog_events()

        assert scan.scan_attempted is True
        assert scan.scan_failed is True

    @pytest.mark.asyncio()
    async def test_happy_path_parses_events(self) -> None:
        payload = json.dumps(
            [
                {
                    "EventId": 900,
                    "TimeCreated": "2026-04-20T10:00:00Z",
                    "Message": "USB\\VID_1532&PID_0528\\6&1A wedged",
                }
            ]
        ).encode("utf-8")

        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(payload, b""))

        async def _spawn(*_a: object, **_kw: object) -> object:
            return proc

        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(module.asyncio, "create_subprocess_exec", _spawn),
        ):
            scan = await scan_recent_driver_watchdog_events()

        assert scan.scan_attempted is True
        assert scan.scan_failed is False
        assert len(scan.events) == 1
        assert scan.events[0].event_id == 900
        assert scan.matches_device("VID_1532&PID_0528") is True

    @pytest.mark.asyncio()
    async def test_script_substitutes_lookback_and_truncate(self) -> None:
        captured_args: list[tuple[object, ...]] = []

        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))

        async def _spawn(*args: object, **_kw: object) -> object:
            captured_args.append(args)
            return proc

        with (
            patch.object(module.sys, "platform", "win32"),
            patch.object(module.asyncio, "create_subprocess_exec", _spawn),
        ):
            await scan_recent_driver_watchdog_events(lookback_hours=48)

        assert captured_args, "subprocess was never spawned"
        script = captured_args[0][-1]
        assert isinstance(script, str)
        assert "__LOOKBACK__" not in script
        assert "__TRUNCATE__" not in script
        assert "AddHours(-48)" in script
        assert str(module._MESSAGE_TRUNCATE_CHARS) in script
