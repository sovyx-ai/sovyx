"""Windows driver-watchdog pre-flight correlation tests (WINDOWS-5).

Covers :func:`_autofix_after_driver_watchdog_scan`'s hardware-identity
matching and :func:`_derive_windows_usb_vid_pid` — the WINDOWS-5 fix
that makes the Kernel-Power-41 downgrade safety net actually fire:
real Driver Watchdog 900/901 messages carry ONLY the PnP device
instance path (empirically ``USB\\VID_1532&PID_0528&MI_00\\…`` for the
v0.20.3 Razer BlackShark V2 Pro post-mortem hardware), never the
vendor friendly name the callers used to pass, so the pre-fix
substring match returned ``targeted=False`` on every real incident.

Peer of ``test_factory_integration_linux_watchdog.py`` /
``test_factory_integration_macos_watchdog.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sovyx.voice.health import _driver_watchdog_observability as obs_module
from sovyx.voice.health import _driver_watchdog_win as win_module
from sovyx.voice.health import _endpoint_fingerprint_win as fp_module
from sovyx.voice.health._driver_watchdog_observability import (
    _autofix_after_driver_watchdog_scan,
    _derive_windows_usb_vid_pid,
)
from sovyx.voice.health._driver_watchdog_win import (
    DriverWatchdogEvent,
    DriverWatchdogScan,
)

# Verbatim message shape captured on the operator's pt-BR host
# (2026-04-19 Kernel-PnP Driver Watchdog 900 for the Razer headset).
_REAL_RAZER_900_EVENT = DriverWatchdogEvent(
    event_id=900,
    time_created_iso="2026-04-19T23:34:28.3999662Z",
    message_excerpt=(
        "Um thread de execução longa para a fila de eventos do "
        "dispositivo foi detectado. O thread está sendo executado por "
        "3004 milissegundos.\n"
        "ID do Thread: 0x25DC\n"
        "Dispositivo: USB\\VID_1532&PID_0528&MI_00\\6&191a269&0&0000\n"
        "Serviço: usbaudio"
    ),
)

_RAZER_SCAN = DriverWatchdogScan(
    scan_attempted=True,
    events=(_REAL_RAZER_900_EVENT,),
)


async def _run_autofix(
    scan: DriverWatchdogScan,
    *,
    device_interface_name: str = "Razer BlackShark V2 Pro",
    endpoint_id: str = "{8981deb5-1e0d-4121-9d31-cc1e400f098d}",
    vid_pid: tuple[str, str] | None = ("1532", "0528"),
) -> bool:
    """Drive the pre-flight with a canned scan + canned VID/PID."""

    async def _fake_scan(**_kw: object) -> DriverWatchdogScan:
        return scan

    with (
        patch.object(
            win_module,
            "scan_recent_driver_watchdog_events",
            _fake_scan,
        ),
        patch.object(
            obs_module,
            "_derive_windows_usb_vid_pid",
            return_value=vid_pid,
        ),
    ):
        return await _autofix_after_driver_watchdog_scan(
            resolved_name="Microfone (Razer BlackShark V2 Pro)",
            device_interface_name=device_interface_name,
            lookback_hours=24,
            timeout_s=3.0,
            endpoint_id=endpoint_id,
        )


class TestAutofixHardwareIdentityCorrelation:
    @pytest.mark.asyncio()
    async def test_real_razer_event_downgrades_via_hardware_id(self) -> None:
        # THE WINDOWS-5 regression: friendly name "Razer BlackShark V2
        # Pro" never substring-matches the real message; the USB
        # VID/PID does. targeted=True → autofix downgraded (False).
        result = await _run_autofix(_RAZER_SCAN, vid_pid=("1532", "0528"))
        assert result is False

    @pytest.mark.asyncio()
    async def test_friendly_name_alone_pre_fix_behaviour_was_inert(self) -> None:
        # With no VID/PID derivable, only the friendly-name heuristic
        # runs — and it cannot match a real message. This pins WHY the
        # hardware-identity path is load-bearing.
        result = await _run_autofix(_RAZER_SCAN, vid_pid=None)
        assert result is True

    @pytest.mark.asyncio()
    async def test_unrelated_device_stays_enabled(self) -> None:
        # C922 webcam probing while the Razer event exists — no
        # hardware match, no name match → autofix untouched.
        result = await _run_autofix(_RAZER_SCAN, vid_pid=("046d", "0892"))
        assert result is True

    @pytest.mark.asyncio()
    async def test_friendly_name_secondary_heuristic_still_works(self) -> None:
        # A needle that already IS hardware-ID-shaped keeps matching
        # through the secondary matches_device path even without a
        # derived VID/PID.
        result = await _run_autofix(
            _RAZER_SCAN,
            device_interface_name="VID_1532&PID_0528",
            vid_pid=None,
        )
        assert result is False

    @pytest.mark.asyncio()
    async def test_clean_scan_stays_enabled(self) -> None:
        clean = DriverWatchdogScan(scan_attempted=True, events=())
        result = await _run_autofix(clean)
        assert result is True

    @pytest.mark.asyncio()
    async def test_failed_scan_trusts_tuning_default(self) -> None:
        failed = DriverWatchdogScan(scan_attempted=True, scan_failed=True)
        result = await _run_autofix(failed)
        assert result is True


class TestDeriveWindowsUsbVidPid:
    def test_non_guid_input_returns_none(self) -> None:
        assert _derive_windows_usb_vid_pid("") is None
        assert _derive_windows_usb_vid_pid("hw:1,0") is None

    @pytest.mark.parametrize(
        "endpoint_id",
        [
            "{surrogate-abcd1234-ab12-ab12-ab12-abcdef123456}",
            "{linux-usb-1532:0543-0-capture}",
            "{macos-usb-uid-input}",
        ],
    )
    def test_non_windows_fingerprints_short_circuit(self, endpoint_id: str) -> None:
        # Must return None WITHOUT touching the COM resolver.
        with patch.object(
            fp_module,
            "resolve_endpoint_to_usb_fingerprint",
        ) as resolver:
            assert _derive_windows_usb_vid_pid(endpoint_id) is None
        resolver.assert_not_called()

    def test_full_immdevice_id_resolves(self) -> None:
        with patch.object(
            fp_module,
            "resolve_endpoint_to_usb_fingerprint",
            return_value="usb-1532:0528-SERIAL9",
        ) as resolver:
            result = _derive_windows_usb_vid_pid(
                "{0.0.1.00000000}.{8981deb5-1e0d-4121-9d31-cc1e400f098d}",
            )
        assert result == ("1532", "0528")
        assert resolver.call_count == 1

    def test_bare_registry_guid_retried_with_capture_prefix(self) -> None:
        # MMDevices registry subkeys (what CaptureApoReport.endpoint_id
        # / the win32 derive_endpoint_guid carry) are the bare {guid};
        # IMMDeviceEnumerator::GetDevice needs the full capture-flow
        # form — the helper retries with the prefix.
        calls: list[str] = []

        def _resolver(endpoint_id: str) -> str | None:
            calls.append(endpoint_id)
            if endpoint_id.startswith("{0.0.1.00000000}."):
                return "usb-1532:0528"
            return None

        with patch.object(
            fp_module,
            "resolve_endpoint_to_usb_fingerprint",
            side_effect=_resolver,
        ):
            result = _derive_windows_usb_vid_pid(
                "{8981deb5-1e0d-4121-9d31-cc1e400f098d}",
            )
        assert result == ("1532", "0528")
        assert calls == [
            "{8981deb5-1e0d-4121-9d31-cc1e400f098d}",
            "{0.0.1.00000000}.{8981deb5-1e0d-4121-9d31-cc1e400f098d}",
        ]

    def test_fingerprint_without_serial_resolves(self) -> None:
        with patch.object(
            fp_module,
            "resolve_endpoint_to_usb_fingerprint",
            return_value="usb-046d:0892",
        ):
            assert _derive_windows_usb_vid_pid("{guid}") == ("046d", "0892")

    def test_non_usb_endpoint_returns_none(self) -> None:
        with patch.object(
            fp_module,
            "resolve_endpoint_to_usb_fingerprint",
            return_value=None,
        ):
            assert _derive_windows_usb_vid_pid("{0.0.1.00000000}.{guid}") is None

    def test_malformed_fingerprint_returns_none(self) -> None:
        with patch.object(
            fp_module,
            "resolve_endpoint_to_usb_fingerprint",
            return_value="pci-0000:00:1f.3",
        ):
            assert _derive_windows_usb_vid_pid("{0.0.1.00000000}.{guid}") is None


pytestmark = pytest.mark.timeout(10)
