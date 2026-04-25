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

Regression context
==================

Before the 2026-04 fix this module read the wrong PKEY slots:
``{a45c254e-...},2`` (DeviceDesc) was being treated as the friendly
name, and ``{b3f8fa53-...},6`` (DeviceInterface_FriendlyName) was being
treated as the enumerator. On a system with multiple USB mics every
endpoint reported ``endpoint_name="Microfone"``, the substring matcher
in :func:`find_endpoint_report` then collided across endpoints, and the
detector returned the wrong report for the active device. Tests below
exercise the corrected slots and the strict matcher.
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


_PKEY_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_PKEY_FRIENDLY = "{a45c254e-df1c-4efd-8020-67d146a850e0},14"
_PKEY_ENUMERATOR = "{a45c254e-df1c-4efd-8020-67d146a850e0},24"
_PKEY_DEVICE_INTERFACE = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"


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
    - ``friendly`` (str, optional): PKEY_Device_FriendlyName (PID 14).
    - ``desc`` (str, optional): PKEY_DeviceDesc (PID 2) — fallback only.
    - ``enumerator`` (str, optional): PKEY_Device_EnumeratorName (PID 24).
    - ``device_interface`` (str, optional): PKEY_DeviceInterface_FriendlyName.
    - ``fx`` (list[Any], optional): ordered FxProperties values.
    """
    capture = _FakeKey(subkeys={})
    for endpoint_id, spec in endpoints.items():
        props_values: dict[str, Any] = {}
        if "friendly" in spec:
            props_values[_PKEY_FRIENDLY] = spec["friendly"]
        if "desc" in spec:
            props_values[_PKEY_DESC] = spec["desc"]
        if "enumerator" in spec:
            props_values[_PKEY_ENUMERATOR] = spec["enumerator"]
        if "device_interface" in spec:
            props_values[_PKEY_DEVICE_INTERFACE] = spec["device_interface"]
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
                    "device_interface": "Razer BlackShark V2 Pro",
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
        assert rep.device_interface_name == "Razer BlackShark V2 Pro"
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
# PKEY slot correctness — regression for the 2026-04 mismatch bug
# ---------------------------------------------------------------------------


class TestPkeySlotCorrectness:
    """The detector must read the *documented* PKEY slots, not adjacent ones.

    Before the fix, ``endpoint_name`` was sourced from PKEY_DeviceDesc
    (PID 2) which collapses to ``"Microfone"`` on every USB mic in
    pt-BR Windows, and ``enumerator`` was sourced from
    PKEY_DeviceInterface_FriendlyName (PID 6 of the device-interface
    GUID), which is not an enumerator at all. These tests pin the
    correct slots so the bug cannot silently regress.
    """

    def test_endpoint_name_comes_from_pid_14_not_pid_2(self) -> None:
        """PKEY_Device_FriendlyName lives at PID 14; PID 2 is DeviceDesc."""
        tree = _mmdevices_tree(
            {
                "{ep}": {
                    "state": 1,
                    "friendly": "Microfone (Razer BlackShark V2 Pro)",
                    "desc": "Microfone",  # PID 2 — must be ignored when 14 present
                    "enumerator": "USB",
                },
            }
        )
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        assert reports[0].endpoint_name == "Microfone (Razer BlackShark V2 Pro)"

    def test_enumerator_is_pid_24_not_device_interface_name(self) -> None:
        """PKEY_Device_EnumeratorName is PID 24, not PID 6 of the iface GUID."""
        tree = _mmdevices_tree(
            {
                "{ep}": {
                    "state": 1,
                    "friendly": "Mic",
                    "enumerator": "USB",
                    "device_interface": "Razer BlackShark V2 Pro",
                },
            }
        )
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        rep = reports[0]
        assert rep.enumerator == "USB"
        assert rep.device_interface_name == "Razer BlackShark V2 Pro"

    def test_endpoint_name_falls_back_to_desc_when_friendly_missing(self) -> None:
        """OEM installs sometimes only populate PKEY_DeviceDesc."""
        tree = _mmdevices_tree(
            {
                "{ep}": {
                    "state": 1,
                    "desc": "Microfone",  # only PID 2 present
                    "enumerator": "USB",
                },
            }
        )
        with patch.object(sys, "platform", "win32"), _with_fake_winreg(_make_winreg_mock(tree)):
            reports = detect_capture_apos()
        assert reports[0].endpoint_name == "Microfone"


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
                endpoint_id="{ep-razer}",
                endpoint_name="Microfone (Razer BlackShark V2 Pro)",
                enumerator="USB",
                fx_binding_count=3,
                device_interface_name="Razer BlackShark V2 Pro",
                known_apos=["Windows Voice Clarity"],
                raw_clsids=[],
                voice_clarity_active=True,
            ),
            CaptureApoReport(
                endpoint_id="{ep-c922}",
                endpoint_name="Microfone (C922 Pro Stream Webcam)",
                enumerator="USB",
                fx_binding_count=3,
                device_interface_name="C922 Pro Stream Webcam",
                known_apos=["Windows Voice Clarity"],
                raw_clsids=[],
                voice_clarity_active=True,
            ),
            CaptureApoReport(
                endpoint_id="{ep-defcomms}",
                endpoint_name="Default Communications Microphone",
                enumerator="MMDevAPI",
                fx_binding_count=0,
                device_interface_name="",
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
        assert out.endpoint_id == "{ep-razer}"

    def test_match_by_device_interface_name(self) -> None:
        """When PortAudio name carries the vendor string, the iface field wins."""
        out = find_endpoint_report(
            self._reports(),
            device_name="Razer BlackShark V2 Pro (Microfone)",
        )
        assert out is not None
        assert out.endpoint_id == "{ep-razer}"

    def test_truncated_portaudio_mme_name_still_matches(self) -> None:
        """MME device names are capped at 31 chars — matching must be tolerant."""
        out = find_endpoint_report(
            self._reports(),
            device_name="Microfone (Razer BlackShark V2 ",
        )
        assert out is not None
        assert out.endpoint_id == "{ep-razer}"

    def test_endpoint_id_exact_match_overrides_name(self) -> None:
        """Caller-supplied endpoint_id is the strongest signal."""
        out = find_endpoint_report(
            self._reports(),
            device_name="Microfone (Razer BlackShark V2 Pro)",
            endpoint_id="{ep-c922}",
        )
        assert out is not None
        assert out.endpoint_id == "{ep-c922}"

    def test_bare_device_class_word_does_not_collide(self) -> None:
        """Regression: bare "Microfone" must NOT match every "Microfone (X)" mic.

        This is the bug that masked Voice Clarity on the active headset:
        the old substring matcher returned the first endpoint whose
        friendly name started with "Microfone", regardless of which mic
        was actually in use. The strict matcher requires a distinctive
        token past the device-class prefix.
        """
        out = find_endpoint_report(self._reports(), device_name="Microfone")
        assert out is None

    def test_no_match_returns_none(self) -> None:
        assert find_endpoint_report(self._reports(), device_name="Bluetooth Headset Mic") is None

    def test_none_device_name_with_endpoint_id_still_works(self) -> None:
        out = find_endpoint_report(
            self._reports(),
            device_name=None,
            endpoint_id="{ep-razer}",
        )
        assert out is not None
        assert out.endpoint_id == "{ep-razer}"

    def test_none_device_name_no_endpoint_id_returns_none(self) -> None:
        assert find_endpoint_report(self._reports(), device_name=None) is None

    def test_empty_reports_returns_none(self) -> None:
        assert find_endpoint_report([], device_name="whatever") is None

    def test_too_short_device_name_returns_none(self) -> None:
        """A 3-char needle is too generic to disambiguate anything."""
        assert find_endpoint_report(self._reports(), device_name="Mic") is None


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
                device_interface_name="Razer BlackShark V2 Pro",
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
        assert rep.device_interface_name == ""


# Unused import guard — SimpleNamespace imported for future test utilities.
_ = SimpleNamespace


# ---------------------------------------------------------------------------
# W4 — Registry permission failure surfaces a structured WARN
# ---------------------------------------------------------------------------


class TestW4RegistryPermissionWarning:
    """Pre-W4 the OpenKey OSError swallowed at DEBUG and was invisible
    in operator logs. Now it emits a structured WARN with
    ``voice.action_required`` so the operator knows APO detection is
    disabled and what to do about it."""

    def test_open_failure_logs_structured_warn_event(self, caplog: Any) -> None:
        import logging as _logging

        # Build a winreg mock whose OpenKey raises OSError at every call
        # — simulates "Sovyx token cannot read MMDevices subtree".
        winreg_mod = ModuleType("winreg")
        winreg_mod.HKEY_LOCAL_MACHINE = "HKLM"  # type: ignore[attr-defined]

        def _raise_oserror(*_args: Any, **_kwargs: Any) -> None:
            msg = "Access is denied"
            raise OSError(msg)

        winreg_mod.OpenKey = _raise_oserror  # type: ignore[attr-defined]
        winreg_mod.EnumKey = lambda *_a, **_k: ""  # type: ignore[attr-defined]
        winreg_mod.QueryValueEx = lambda *_a, **_k: ("", 0)  # type: ignore[attr-defined]
        winreg_mod.CloseKey = lambda *_a, **_k: None  # type: ignore[attr-defined]
        winreg_mod.REG_SZ = 1  # type: ignore[attr-defined]
        winreg_mod.REG_DWORD = 4  # type: ignore[attr-defined]
        winreg_mod.REG_BINARY = 3  # type: ignore[attr-defined]

        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": winreg_mod}),
            caplog.at_level(_logging.WARNING, logger="sovyx.voice._apo_detector"),
        ):
            reports = detect_capture_apos()

        # Returned an empty list (no APOs detectable) — gracefully
        # degraded, no exception propagated.
        assert reports == []
        # WARN was emitted with the expected event name + action_required.
        warn_events = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "apo_registry_access_denied" in r.getMessage()
        ]
        assert len(warn_events) == 1, (
            f"expected one WARN event, got {[r.getMessage() for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# W12 — Unicode-aware device-name matching (NFKC + casefold)
# ---------------------------------------------------------------------------


class TestW12UnicodeAwareMatching:
    """Pre-W12 ``str.lower()`` was the only normalisation, which:

    * Failed on full-width ASCII (Asian-locale paste).
    * Mishandled German ß (should fold to ``ss``, not stay as ß).

    These are real failure modes for vendor names on non-en-US Windows
    installs. The normaliser now uses NFKC + casefold uniformly.
    """

    def _reports(self) -> list[CaptureApoReport]:
        return [
            CaptureApoReport(
                endpoint_id="{ep-fullwidth}",
                # Full-width ASCII as the friendly name (sometimes
                # observed when device descriptors are pasted from
                # Asian-locale Windows installs).
                endpoint_name="Microfone (Razer BlackShark V2 Pro)",  # noqa: RUF001
                enumerator="USB",
                fx_binding_count=3,
                device_interface_name="Razer BlackShark V2 Pro",  # noqa: RUF001
                known_apos=[],
                raw_clsids=[],
                voice_clarity_active=False,
            ),
        ]

    def test_fullwidth_query_matches_halfwidth_target(self) -> None:
        # Caller passes a half-width name; target endpoint_name carries
        # the full-width characters. NFKC normalisation must collapse
        # them so the matcher still resolves.
        rep = find_endpoint_report(
            self._reports(),
            device_name="Razer BlackShark V2 Pro",
        )
        assert rep is not None
        assert rep.endpoint_id == "{ep-fullwidth}"

    def test_eszett_casefolded_correctly(self) -> None:
        # Build a report whose vendor name contains German ß. Caller
        # passes the casefolded form ("ss"). casefold() must equate
        # them; .lower() would not.
        reports = [
            CaptureApoReport(
                endpoint_id="{ep-de}",
                endpoint_name="Mikrofon (Großmeister Audio)",
                enumerator="USB",
                fx_binding_count=3,
                device_interface_name="Großmeister Audio",
                known_apos=[],
                raw_clsids=[],
                voice_clarity_active=False,
            ),
        ]
        # The casefolded form of "Groß" is "gross". A caller that read
        # the device name from a stack that already lower-cased it via
        # .lower() (preserving ß) won't match — but a caller that
        # passes the *original* string with ß WILL match because both
        # sides go through _norm.
        rep = find_endpoint_report(
            reports,
            device_name="Großmeister Audio",
        )
        assert rep is not None
        assert rep.endpoint_id == "{ep-de}"

    def test_norm_helper_handles_empty_string(self) -> None:
        from sovyx.voice._apo_detector import _norm

        assert _norm("") == ""
        assert _norm("   ") == ""

    def test_norm_helper_collapses_fullwidth(self) -> None:
        from sovyx.voice._apo_detector import _norm

        # Full-width "ABC" → half-width "abc".
        assert _norm("ＡＢＣ") == "abc"

    def test_norm_helper_casefolds_eszett(self) -> None:
        from sovyx.voice._apo_detector import _norm

        # casefold() folds ß → ss; lower() would not.
        assert _norm("Großmeister") == "grossmeister"
        # Sanity guard: this property is what str.lower() would NOT give us.
        assert "Großmeister".lower() != "grossmeister"
