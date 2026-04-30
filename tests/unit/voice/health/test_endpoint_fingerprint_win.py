"""Tests for Windows IMMDevice endpoint → USB fingerprint resolver.

Phase 5 / T5.51 — Windows-side consumer of the cross-platform
:mod:`sovyx.voice.health._usb_fingerprint` foundation shipped in
T5.43.

Coverage:

* :func:`resolve_endpoint_to_usb_fingerprint` short-circuits on
  empty endpoint ID.
* :func:`resolve_endpoint_to_usb_fingerprint` short-circuits on
  non-Windows platforms.
* :func:`resolve_endpoint_to_usb_fingerprint` returns None when
  comtypes is unavailable + emits the WARN exactly once per process.
* :func:`resolve_endpoint_to_usb_fingerprint` happy paths: USB with
  serial, USB without serial, non-USB device (PCI codec, BTHENUM,
  HDAUDIO).
* :func:`_resolve_endpoint_to_pnp_id` failure modes: CreateObject
  raises, GetDevice raises, GetDevice returns None, OpenPropertyStore
  raises, OpenPropertyStore returns None.
* :func:`_read_pnp_id_from_property_store` PROPVARIANT branch
  coverage: vt != VT_LPWSTR, pwszVal null, GetValue raises,
  propvariant None, vt attribute access fails.
* Once-per-process WARN latch: emitted on first failure, silent on
  second.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health import _endpoint_fingerprint_win as module_under_test
from sovyx.voice.health._endpoint_fingerprint_win import (
    _read_pnp_id_from_property_store,
    _resolve_endpoint_to_pnp_id,
    resolve_endpoint_to_usb_fingerprint,
)


@pytest.fixture(autouse=True)
def _reset_comtypes_warning_latch() -> None:
    """The module-level ``_comtypes_warning_emitted`` latch persists
    across test instances when run in the same process. Reset it
    before every test so latch-related assertions stay deterministic.
    """
    module_under_test._comtypes_warning_emitted = False


@pytest.fixture()
def _force_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we're on Windows so the resolver doesn't short-circuit
    at the platform gate. Tests that need to verify the gate itself
    must NOT use this fixture and instead set sys.platform directly.
    """
    monkeypatch.setattr(sys, "platform", "win32")


# ── Public API: resolve_endpoint_to_usb_fingerprint ─────────────────


class TestResolveEndpointPublicSurface:
    """High-level contract: empty input + platform gate."""

    def test_empty_endpoint_id_returns_none(self) -> None:
        assert resolve_endpoint_to_usb_fingerprint("") is None

    def test_none_like_endpoint_id_returns_none(self) -> None:
        # Type ignore because the signature says str — but
        # defensively the function falsy-checks the input.
        assert resolve_endpoint_to_usb_fingerprint(None) is None  # type: ignore[arg-type]

    def test_non_windows_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        # _resolve_endpoint_to_pnp_id MUST NOT be called on non-
        # Windows — the gate is at the public-API layer.
        with patch.object(
            module_under_test,
            "_resolve_endpoint_to_pnp_id",
        ) as mock_resolve:
            result = resolve_endpoint_to_usb_fingerprint("{0.0.1.00000000}.{guid}")
        assert result is None
        mock_resolve.assert_not_called()

    def test_darwin_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch.object(
            module_under_test,
            "_resolve_endpoint_to_pnp_id",
        ) as mock_resolve:
            result = resolve_endpoint_to_usb_fingerprint("{0.0.1.00000000}.{guid}")
        assert result is None
        mock_resolve.assert_not_called()


class TestResolveEndpointHappyPath:
    """End-to-end happy paths — patch the COM resolver to deliver a
    PnP ID, verify the T5.43 fingerprint formatter is called."""

    def test_usb_endpoint_with_serial(
        self,
        _force_windows: None,
    ) -> None:
        with patch.object(
            module_under_test,
            "_resolve_endpoint_to_pnp_id",
            return_value=r"USB\VID_046D&PID_0A45\AB12CD34",
        ):
            fp = resolve_endpoint_to_usb_fingerprint("{0.0.1.00000000}.{guid}")
        assert fp == "usb-046d:0a45-AB12CD34"

    def test_usb_endpoint_without_serial(
        self,
        _force_windows: None,
    ) -> None:
        with patch.object(
            module_under_test,
            "_resolve_endpoint_to_pnp_id",
            return_value=r"USB\VID_1532&PID_0528",
        ):
            fp = resolve_endpoint_to_usb_fingerprint("{0.0.1.00000000}.{guid}")
        assert fp == "usb-1532:0528"

    def test_non_usb_pci_endpoint_returns_none(
        self,
        _force_windows: None,
    ) -> None:
        # PCI codec — fingerprint_usb_device rejects this prefix.
        with patch.object(
            module_under_test,
            "_resolve_endpoint_to_pnp_id",
            return_value=r"PCI\VEN_10EC&DEV_8168&SUBSYS_85AA1043",
        ):
            fp = resolve_endpoint_to_usb_fingerprint("{0.0.1.00000000}.{guid}")
        assert fp is None

    def test_non_usb_bluetooth_endpoint_returns_none(
        self,
        _force_windows: None,
    ) -> None:
        # Bluetooth A2DP / HFP — different PnP prefix; fingerprint
        # function returns None.
        with patch.object(
            module_under_test,
            "_resolve_endpoint_to_pnp_id",
            return_value=r"BTHENUM\Dev_VID&02xxxx_PID&xxxx",
        ):
            fp = resolve_endpoint_to_usb_fingerprint("{0.0.1.00000000}.{guid}")
        assert fp is None

    def test_non_usb_hdaudio_endpoint_returns_none(
        self,
        _force_windows: None,
    ) -> None:
        # Onboard HD Audio codec.
        with patch.object(
            module_under_test,
            "_resolve_endpoint_to_pnp_id",
            return_value=r"HDAUDIO\FUNC_01&VEN_10EC&DEV_0299&SUBSYS_xxxx",
        ):
            fp = resolve_endpoint_to_usb_fingerprint("{0.0.1.00000000}.{guid}")
        assert fp is None

    def test_resolver_returns_none_propagates(
        self,
        _force_windows: None,
    ) -> None:
        with patch.object(
            module_under_test,
            "_resolve_endpoint_to_pnp_id",
            return_value=None,
        ):
            fp = resolve_endpoint_to_usb_fingerprint("{0.0.1.00000000}.{guid}")
        assert fp is None


# ── COM chain: _resolve_endpoint_to_pnp_id ──────────────────────────


def _make_fake_bindings() -> tuple[Any, Any, Any, Any]:
    """Build a 4-tuple of mock interface classes matching the shape
    of :func:`_build_property_store_bindings`'s return."""
    return (
        MagicMock(name="IMMDeviceEnumerator"),
        MagicMock(name="IMMDevice"),
        MagicMock(name="IPropertyStore"),
        MagicMock(name="PROPVARIANT"),
    )


class TestResolveEndpointToPnpIdComtypesUnavailable:
    """When comtypes isn't installed, the bindings builder returns
    None, the WARN fires once per process, subsequent calls are
    silent."""

    def test_comtypes_unavailable_returns_none(self) -> None:
        with patch.object(
            module_under_test,
            "_build_property_store_bindings",
            return_value=None,
        ):
            result = _resolve_endpoint_to_pnp_id("{guid}")
        assert result is None

    def test_comtypes_unavailable_emits_warn_once(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # First call emits the WARN.
        with patch.object(
            module_under_test,
            "_build_property_store_bindings",
            return_value=None,
        ):
            _resolve_endpoint_to_pnp_id("{guid}")
            first_call_logs = [r for r in caplog.records if "comtypes_unavailable" in r.message]
            assert len(first_call_logs) == 1

            # Second + third calls do NOT emit again — the latch
            # suppresses repeated WARNs.
            caplog.clear()
            _resolve_endpoint_to_pnp_id("{guid}")
            _resolve_endpoint_to_pnp_id("{guid}")
            subsequent_logs = [r for r in caplog.records if "comtypes_unavailable" in r.message]
            assert subsequent_logs == []


class TestResolveEndpointToPnpIdComCallChain:
    """Patch ``_build_property_store_bindings`` + the comtypes
    ``CreateObject`` to verify the COM chain's failure modes degrade
    gracefully."""

    def _patch_create_object(
        self,
        return_value: Any = None,
        side_effect: Any = None,
    ) -> Any:
        """Patch ``comtypes.client.CreateObject`` via sys.modules.

        The function imports ``comtypes.client`` lazily inside
        :func:`_resolve_endpoint_to_pnp_id`; sys.modules is the
        canonical patch target for that lazy import.
        """
        fake_client_module = MagicMock(name="comtypes.client")
        if side_effect is not None:
            fake_client_module.CreateObject = MagicMock(side_effect=side_effect)
        else:
            fake_client_module.CreateObject = MagicMock(return_value=return_value)
        return patch.dict(
            sys.modules,
            {"comtypes.client": fake_client_module},
        )

    def test_create_enumerator_raises_returns_none(
        self,
    ) -> None:
        bindings = _make_fake_bindings()
        with (
            patch.object(
                module_under_test,
                "_build_property_store_bindings",
                return_value=bindings,
            ),
            self._patch_create_object(side_effect=OSError("CoCreateInstance E_FAIL")),
        ):
            result = _resolve_endpoint_to_pnp_id("{0.0.1.00000000}.{guid}")
        assert result is None

    def test_get_device_raises_returns_none(
        self,
    ) -> None:
        bindings = _make_fake_bindings()
        mock_enumerator = MagicMock(name="enumerator")
        mock_enumerator.GetDevice = MagicMock(side_effect=OSError("E_NOTFOUND"))
        with (
            patch.object(
                module_under_test,
                "_build_property_store_bindings",
                return_value=bindings,
            ),
            self._patch_create_object(return_value=mock_enumerator),
        ):
            result = _resolve_endpoint_to_pnp_id("{stale-endpoint-id}")
        assert result is None
        # Verify the call was attempted with the endpoint ID.
        mock_enumerator.GetDevice.assert_called_once_with("{stale-endpoint-id}")

    def test_get_device_returns_none_returns_none(
        self,
    ) -> None:
        bindings = _make_fake_bindings()
        mock_enumerator = MagicMock(name="enumerator")
        mock_enumerator.GetDevice = MagicMock(return_value=None)
        with (
            patch.object(
                module_under_test,
                "_build_property_store_bindings",
                return_value=bindings,
            ),
            self._patch_create_object(return_value=mock_enumerator),
        ):
            result = _resolve_endpoint_to_pnp_id("{guid}")
        assert result is None

    def test_open_property_store_raises_returns_none(
        self,
    ) -> None:
        bindings = _make_fake_bindings()
        mock_device = MagicMock(name="device")
        mock_device.OpenPropertyStore = MagicMock(side_effect=OSError("E_ACCESSDENIED"))
        mock_enumerator = MagicMock(name="enumerator")
        mock_enumerator.GetDevice = MagicMock(return_value=mock_device)
        with (
            patch.object(
                module_under_test,
                "_build_property_store_bindings",
                return_value=bindings,
            ),
            self._patch_create_object(return_value=mock_enumerator),
        ):
            result = _resolve_endpoint_to_pnp_id("{guid}")
        assert result is None

    def test_open_property_store_returns_none_returns_none(
        self,
    ) -> None:
        bindings = _make_fake_bindings()
        mock_device = MagicMock(name="device")
        mock_device.OpenPropertyStore = MagicMock(return_value=None)
        mock_enumerator = MagicMock(name="enumerator")
        mock_enumerator.GetDevice = MagicMock(return_value=mock_device)
        with (
            patch.object(
                module_under_test,
                "_build_property_store_bindings",
                return_value=bindings,
            ),
            self._patch_create_object(return_value=mock_enumerator),
        ):
            result = _resolve_endpoint_to_pnp_id("{guid}")
        assert result is None


# ── PROPVARIANT extraction: _read_pnp_id_from_property_store ────────


class TestReadPnpIdFromPropertyStore:
    """Branch coverage for the PROPVARIANT inspection path."""

    def test_get_value_raises_returns_none(self) -> None:
        mock_propstore = MagicMock(name="propstore")
        mock_propstore.GetValue = MagicMock(side_effect=OSError("E_INVALIDARG"))
        result = _read_pnp_id_from_property_store(mock_propstore)
        assert result is None

    def test_get_value_returns_none_returns_none(self) -> None:
        mock_propstore = MagicMock(name="propstore")
        mock_propstore.GetValue = MagicMock(return_value=None)
        result = _read_pnp_id_from_property_store(mock_propstore)
        assert result is None

    def test_propvariant_vt_not_lpwstr_returns_none(self) -> None:
        # Variant type 0 is VT_EMPTY; anything other than 31
        # (VT_LPWSTR) means the property store returned an
        # unexpected type — fail safe.
        mock_propvariant = MagicMock(name="propvariant")
        mock_propvariant.vt = 0  # VT_EMPTY
        mock_propvariant.pwszVal = 0  # ignored when vt != 31

        mock_propstore = MagicMock(name="propstore")
        mock_propstore.GetValue = MagicMock(return_value=mock_propvariant)
        result = _read_pnp_id_from_property_store(mock_propstore)
        assert result is None

    def test_propvariant_vt_attribute_error_returns_none(self) -> None:
        # Some malformed PROPVARIANT instances surface as objects
        # without a ``vt`` attribute at all (driver-side memory
        # corruption). Fail safe.
        mock_propvariant = object()  # no .vt, no .pwszVal

        mock_propstore = MagicMock(name="propstore")
        mock_propstore.GetValue = MagicMock(return_value=mock_propvariant)
        result = _read_pnp_id_from_property_store(mock_propstore)
        assert result is None

    def test_propvariant_pwszval_zero_returns_none(self) -> None:
        # vt=31 but pwszVal is a null pointer (0) — happens when
        # the property exists but the LPWSTR allocation failed.
        mock_propvariant = MagicMock(name="propvariant")
        mock_propvariant.vt = 31  # VT_LPWSTR
        mock_propvariant.pwszVal = 0

        mock_propstore = MagicMock(name="propstore")
        mock_propstore.GetValue = MagicMock(return_value=mock_propvariant)
        result = _read_pnp_id_from_property_store(mock_propstore)
        assert result is None

    def test_propvariant_pwszval_attribute_error_returns_none(self) -> None:
        # vt=31 but no pwszVal attribute — defensive against
        # struct-layout regressions.
        class _BrokenVariant:
            vt = 31

        mock_propstore = MagicMock(name="propstore")
        mock_propstore.GetValue = MagicMock(return_value=_BrokenVariant())
        result = _read_pnp_id_from_property_store(mock_propstore)
        assert result is None

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="ctypes.cast(c_void_p → c_wchar_p) round-trip needs Windows ABI",
    )
    def test_real_lpwstr_round_trip(self) -> None:
        """Pin the happy-path LPWSTR extraction with a real ctypes
        wide-string buffer. Skipped on non-Windows because the
        comtypes-marshalled layout is platform-specific; the test's
        purpose is to verify our cast logic when the propvariant
        carries a genuine wide-string pointer.
        """
        import ctypes

        # Allocate a wide-string buffer, get its pointer as a
        # c_void_p (mimicking what comtypes' PROPVARIANT.pwszVal
        # carries).
        pnp_string = r"USB\VID_1532&PID_0528\REAL12345"
        buf = ctypes.create_unicode_buffer(pnp_string)
        ptr = ctypes.cast(buf, ctypes.c_void_p).value

        mock_propvariant = MagicMock(name="propvariant")
        mock_propvariant.vt = 31  # VT_LPWSTR
        mock_propvariant.pwszVal = ptr

        mock_propstore = MagicMock(name="propstore")
        mock_propstore.GetValue = MagicMock(return_value=mock_propvariant)
        result = _read_pnp_id_from_property_store(mock_propstore)
        assert result == pnp_string


# ── Latch reset across test instances ────────────────────────────────


class TestComtypesWarningLatchAcrossCalls:
    """Pin the autouse fixture's latch reset so the once-per-process
    contract doesn't leak state across tests."""

    def test_latch_reset_between_test_instances_part_a(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # First test instance: latch starts False (autouse fixture);
        # call emits WARN.
        with patch.object(
            module_under_test,
            "_build_property_store_bindings",
            return_value=None,
        ):
            _resolve_endpoint_to_pnp_id("{guid}")
        assert any("comtypes_unavailable" in r.message for r in caplog.records)

    def test_latch_reset_between_test_instances_part_b(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Second test instance: autouse fixture reset the latch, so
        # this call ALSO emits the WARN. If the autouse fixture
        # didn't run, this test would observe an empty log and fail.
        with patch.object(
            module_under_test,
            "_build_property_store_bindings",
            return_value=None,
        ):
            _resolve_endpoint_to_pnp_id("{guid}")
        assert any("comtypes_unavailable" in r.message for r in caplog.records)
