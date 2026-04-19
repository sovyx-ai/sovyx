"""Unit tests for :mod:`sovyx.voice._apo_detector`.

The detector reads HKLM via ``winreg`` — a Windows-only stdlib module —
so every test mocks ``winreg`` directly instead of touching the live
registry. Two mock shapes matter:

1. **The happy path** — a fake in-memory registry tree that mirrors the
   real ``MMDevices\\Audio\\Capture\\{endpoint}`` layout and lets us
   assert the detector correctly correlates the PKEY friendly name with
   the FxProperties values.

2. **Failure isolation** — ``OSError`` raised at every depth (root
   missing, endpoint missing, FxProperties missing) must collapse to a
   best-effort empty list. The production code guarantees this because
   the startup path must survive a misconfigured Windows install.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from sovyx.voice._apo_detector import (
    CaptureApoReport,
    detect_capture_apos,
    find_endpoint_report,
)

# ---------------------------------------------------------------------------
# Fake winreg
# ---------------------------------------------------------------------------


class _FakeKey:
    """A single node in the fake registry tree."""

    def __init__(
        self,
        *,
        values: dict[str, Any] | None = None,
        subkeys: dict[str, _FakeKey] | None = None,
    ) -> None:
        self.values: dict[str, Any] = values or {}
        self.subkeys: dict[str, _FakeKey] = subkeys or {}
        self.closed = False


def _make_winreg_mock(root: _FakeKey) -> ModuleType:
    """Return a ``winreg`` stand-in backed by an in-memory tree."""
    mod = ModuleType("winreg")
    mod.HKEY_LOCAL_MACHINE = object()  # type: ignore[attr-defined]

    def _resolve(base: object, path: str) -> _FakeKey:
        key = root if base is mod.HKEY_LOCAL_MACHINE else base
        if not isinstance(key, _FakeKey):  # pragma: no cover — defensive
            raise OSError("bad handle")
        if path:
            for part in path.split("\\"):
                child = key.subkeys.get(part)
                if child is None:
                    raise OSError(f"subkey not found: {part}")
                key = child
        return key

    def open_key(base: object, path: str) -> _FakeKey:
        return _resolve(base, path)

    def close_key(key: object) -> None:
        if isinstance(key, _FakeKey):
            key.closed = True

    def enum_key(key: _FakeKey, idx: int) -> str:
        names = list(key.subkeys.keys())
        if idx >= len(names):
            raise OSError("no more items")
        return names[idx]

    def enum_value(key: _FakeKey, idx: int) -> tuple[str, Any, int]:
        names = list(key.values.keys())
        if idx >= len(names):
            raise OSError("no more items")
        name = names[idx]
        value = key.values[name]
        return (name, value, 1)  # 1 == REG_SZ (irrelevant for the detector)

    def query_value_ex(key: _FakeKey, name: str) -> tuple[Any, int]:
        if name not in key.values:
            raise OSError(f"value not found: {name}")
        return (key.values[name], 1)

    mod.OpenKey = open_key  # type: ignore[attr-defined]
    mod.CloseKey = close_key  # type: ignore[attr-defined]
    mod.EnumKey = enum_key  # type: ignore[attr-defined]
    mod.EnumValue = enum_value  # type: ignore[attr-defined]
    mod.QueryValueEx = query_value_ex  # type: ignore[attr-defined]
    return mod


def _mmdevices_tree(endpoints: dict[str, dict[str, Any]]) -> _FakeKey:
    """Build a fake HKLM tree matching the real capture-endpoints layout.

    ``endpoints`` is a mapping from endpoint GUID (subkey name) to a
    dict with keys:

    - ``state`` (int, required): DeviceState value (1 = active).
    - ``friendly`` (str, optional): PKEY_Device_FriendlyName.
    - ``enumerator`` (str, optional): PKEY_Device_EnumeratorName.
    - ``fx`` (list[Any], optional): ordered FxProperties values.
    """
    capture = _FakeKey(subkeys={})
    for endpoint_id, spec in endpoints.items():
        props_values: dict[str, Any] = {}
        if "friendly" in spec:
            props_values["{a45c254e-df1c-4efd-8020-67d146a850e0},2"] = spec["friendly"]
        if "enumerator" in spec:
            props_values["{b3f8fa53-0004-438e-9003-51a46e139bfc},6"] = spec["enumerator"]
        properties_key = _FakeKey(values=props_values)

        fx_values: dict[str, Any] = {}
        for i, value in enumerate(spec.get("fx", [])):
            fx_values[f"fx_{i}"] = value
        fx_key = _FakeKey(values=fx_values)

        ep_values: dict[str, Any] = {"DeviceState": spec["state"]}
        ep_key = _FakeKey(
            values=ep_values,
            subkeys={"Properties": properties_key, "FxProperties": fx_key},
        )
        capture.subkeys[endpoint_id] = ep_key

    return _FakeKey(
        subkeys={
            "SOFTWARE": _FakeKey(
                subkeys={
                    "Microsoft": _FakeKey(
                        subkeys={
                            "Windows": _FakeKey(
                                subkeys={
                                    "CurrentVersion": _FakeKey(
                                        subkeys={
                                            "MMDevices": _FakeKey(
                                                subkeys={
                                                    "Audio": _FakeKey(
                                                        subkeys={"Capture": capture},
                                                    )
                                                },
                                            ),
                                        },
                                    ),
                                },
                            ),
                        },
                    ),
                },
            ),
        },
    )


def _with_fake_winreg(mock_mod: ModuleType) -> Any:
    """Patch ``sys.platform`` to win32 and inject the fake ``winreg``."""
    return patch.dict(sys.modules, {"winreg": mock_mod})


# ---------------------------------------------------------------------------
# Non-Windows short-circuit
# ---------------------------------------------------------------------------


class TestNonWindowsPlatforms:
    def test_linux_returns_empty(self) -> None:
        with patch.object(sys, "platform", "linux"):
            assert detect_capture_apos() == []

    def test_darwin_returns_empty(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            assert detect_capture_apos() == []


# ---------------------------------------------------------------------------
# Happy-path detection
# ---------------------------------------------------------------------------


class TestVoiceClarityDetection:
    """The motivating use case: flag Windows Voice Clarity on active mic."""

    def test_voca_effect_pack_is_flagged_as_voice_clarity(self) -> None:
        tree = _mmdevices_tree(
            {
                "{ep-razer}": {
                    "state": 1,
                    "friendly": "Microfone (Razer BlackShark V2 Pro)",
                    "enumerator": "USB",
                    "fx": [
                        "SWD\\DRIVERENUM\\{96bedf2c-18cb-4a15-b821-5e95ed0fea61}"
                        "#VocaEffectPack&1&2232a730&0",
                    ],
                },
            }
        )
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        assert len(reports) == 1
        rep = reports[0]
        assert rep.endpoint_name == "Microfone (Razer BlackShark V2 Pro)"
        assert rep.enumerator == "USB"
        assert rep.voice_clarity_active is True
        assert "Windows Voice Clarity" in rep.known_apos
        # The KSCATEGORY_AUDIO_PROCESSING_OBJECT GUID should also surface.
        assert any("96BEDF2C" in c for c in rep.raw_clsids)

    def test_voiceclarityep_substring_also_flagged(self) -> None:
        tree = _mmdevices_tree(
            {
                "{ep-generic}": {
                    "state": 1,
                    "friendly": "Default Mic",
                    "enumerator": "MMDevAPI",
                    "fx": ["voiceclarityep_audio_component.inf_amd64_abc"],
                },
            }
        )
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        assert reports[0].voice_clarity_active is True

    def test_clean_endpoint_has_voice_clarity_false(self) -> None:
        """No APOs bound → voice_clarity_active is False, fx_binding_count 0."""
        tree = _mmdevices_tree(
            {
                "{ep-clean}": {
                    "state": 1,
                    "friendly": "Pristine Mic",
                    "enumerator": "USB",
                    "fx": [],
                },
            }
        )
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        rep = reports[0]
        assert rep.voice_clarity_active is False
        assert rep.known_apos == []
        assert rep.fx_binding_count == 0

    def test_inactive_endpoints_are_skipped(self) -> None:
        """DeviceState != 1 means unplugged / disabled — ignore it."""
        tree = _mmdevices_tree(
            {
                "{ep-unplugged}": {
                    "state": 4,  # not present
                    "friendly": "Old USB Mic",
                    "fx": ["VocaEffectPack"],  # would match if not skipped
                },
                "{ep-active}": {
                    "state": 1,
                    "friendly": "Current Mic",
                    "fx": [],
                },
            }
        )
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        assert len(reports) == 1
        assert reports[0].endpoint_name == "Current Mic"

    def test_multiple_known_apos_are_deduplicated_and_ordered(self) -> None:
        tree = _mmdevices_tree(
            {
                "{ep-stack}": {
                    "state": 1,
                    "friendly": "Stacked Mic",
                    "fx": [
                        # AGC mentioned twice across two values.
                        "{9CF81848-DE9F-4BDF-B177-A9D8B16A7AAB}",
                        "{CF1DDA2C-3B93-4EFE-8AA9-DEB6F8D4FDF1}",  # AEC
                        "{9CF81848-DE9F-4BDF-B177-A9D8B16A7AAB}",  # AGC again
                    ],
                },
            }
        )
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        apos = reports[0].known_apos
        assert apos.count("MS Automatic Gain Control") == 1
        assert apos.count("MS Acoustic Echo Cancellation") == 1
        # Insertion order — AGC observed first.
        assert apos.index("MS Automatic Gain Control") < apos.index(
            "MS Acoustic Echo Cancellation",
        )

    def test_reg_binary_utf16_values_are_decoded(self) -> None:
        """Legacy Windows stores some PKEY values as REG_BINARY wide-strings."""
        blob = "{9CF81848-DE9F-4BDF-B177-A9D8B16A7AAB}".encode("utf-16-le") + b"\x00\x00"
        tree = _mmdevices_tree(
            {
                "{ep-legacy}": {
                    "state": 1,
                    "friendly": "Legacy Mic",
                    "fx": [blob],
                },
            }
        )
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        assert "MS Automatic Gain Control" in reports[0].known_apos


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    """Registry errors at any depth collapse to an empty list."""

    def test_missing_root_returns_empty(self) -> None:
        tree = _FakeKey(subkeys={})  # no SOFTWARE subtree
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            assert detect_capture_apos() == []

    def test_missing_fx_properties_still_yields_report(self) -> None:
        """Clean installs lack FxProperties — must not raise, must return zero-chain report."""
        tree = _mmdevices_tree(
            {
                "{ep-minimal}": {
                    "state": 1,
                    "friendly": "Minimal Mic",
                },
            }
        )
        # Remove FxProperties to simulate a clean install.
        capture = (
            tree.subkeys["SOFTWARE"]
            .subkeys["Microsoft"]
            .subkeys["Windows"]
            .subkeys["CurrentVersion"]
            .subkeys["MMDevices"]
            .subkeys["Audio"]
            .subkeys["Capture"]
        )
        del capture.subkeys["{ep-minimal}"].subkeys["FxProperties"]

        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        assert len(reports) == 1
        assert reports[0].fx_binding_count == 0
        assert reports[0].voice_clarity_active is False


# ---------------------------------------------------------------------------
# Endpoint matching
# ---------------------------------------------------------------------------


class TestFindEndpointReport:
    def _reports(self) -> list[CaptureApoReport]:
        return [
            CaptureApoReport(
                endpoint_id="{ep-1}",
                endpoint_name="Microfone (Razer BlackShark V2 Pro)",
                enumerator="USB",
                fx_binding_count=3,
                known_apos=["Windows Voice Clarity"],
                raw_clsids=[],
                voice_clarity_active=True,
            ),
            CaptureApoReport(
                endpoint_id="{ep-2}",
                endpoint_name="Default Communications Microphone",
                enumerator="MMDevAPI",
                fx_binding_count=0,
                known_apos=[],
                raw_clsids=[],
                voice_clarity_active=False,
            ),
        ]

    def test_exact_match(self) -> None:
        out = find_endpoint_report(
            self._reports(),
            device_name="Microfone (Razer BlackShark V2 Pro)",
        )
        assert out is not None
        assert out.endpoint_id == "{ep-1}"

    def test_truncated_portaudio_mme_name_still_matches(self) -> None:
        """MME device names are capped at 31 chars — matching must be tolerant."""
        out = find_endpoint_report(
            self._reports(),
            device_name="Microfone (Razer BlackShark V2 ",
        )
        assert out is not None
        assert out.endpoint_id == "{ep-1}"

    def test_no_match_returns_none(self) -> None:
        assert find_endpoint_report(self._reports(), device_name="Bluetooth Headset Mic") is None

    def test_none_device_name_returns_none(self) -> None:
        assert find_endpoint_report(self._reports(), device_name=None) is None

    def test_empty_reports_returns_none(self) -> None:
        assert find_endpoint_report([], device_name="whatever") is None


# ---------------------------------------------------------------------------
# Factory integration — voice_apo_detected emitted at pipeline creation
# ---------------------------------------------------------------------------


class TestFactoryEmitsApoDetectionEvent:
    """``create_voice_pipeline`` logs ``voice_apo_detected`` once per boot."""

    def test_emitter_logs_on_voice_clarity_active(self, caplog: Any) -> None:
        import logging

        from sovyx.voice import factory

        fake_reports = [
            CaptureApoReport(
                endpoint_id="{ep-razer}",
                endpoint_name="Microfone (Razer BlackShark V2 Pro)",
                enumerator="USB",
                fx_binding_count=3,
                known_apos=["Windows Voice Clarity"],
                raw_clsids=[],
                voice_clarity_active=True,
            ),
        ]
        with (
            patch("sovyx.voice._apo_detector.detect_capture_apos", return_value=fake_reports),
            caplog.at_level(logging.INFO, logger="sovyx.voice.factory"),
        ):
            factory._emit_capture_apo_detection(
                resolved_name="Microfone (Razer BlackShark V2 Pro)",
            )
        events = [r.message for r in caplog.records]
        assert any("voice_apo_detected" in e for e in events)

    def test_emitter_survives_detector_exception(self, caplog: Any) -> None:
        """A registry blow-up MUST NOT crash pipeline startup."""
        from sovyx.voice import factory

        def _boom() -> list[CaptureApoReport]:
            raise RuntimeError("registry fire")

        with patch("sovyx.voice._apo_detector.detect_capture_apos", side_effect=_boom):
            factory._emit_capture_apo_detection(resolved_name="any")  # must not raise


# ---------------------------------------------------------------------------
# Smoke — catalog is internally consistent
# ---------------------------------------------------------------------------


class TestCatalogInvariants:
    def test_known_clsids_are_uppercase(self) -> None:
        from sovyx.voice._apo_detector import _KNOWN_CLSIDS

        for clsid in _KNOWN_CLSIDS:
            assert clsid == clsid.upper(), f"{clsid} must be uppercase"
            assert clsid.startswith("{") and clsid.endswith("}")

    def test_package_patterns_are_lowercase(self) -> None:
        from sovyx.voice._apo_detector import _PACKAGE_PATTERNS

        for needle, _ in _PACKAGE_PATTERNS:
            assert needle == needle.lower(), f"{needle} must be lowercase"

    def test_report_defaults_construct_cleanly(self) -> None:
        rep = CaptureApoReport(
            endpoint_id="{x}",
            endpoint_name="X",
            enumerator="USB",
            fx_binding_count=0,
        )
        assert rep.known_apos == []
        assert rep.raw_clsids == []
        assert rep.voice_clarity_active is False


# Unused import guard — SimpleNamespace imported for future test utilities.
_ = SimpleNamespace
