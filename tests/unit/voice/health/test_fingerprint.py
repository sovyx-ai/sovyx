"""Tests for §5.13 audio-subsystem fingerprinting helpers."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from sovyx.voice.health._fingerprint import (
    _canonical_bytes,
    _hash_linux_pulse_pipewire_config,
    _hash_macos_coreaudio_plugins,
    compute_audio_subsystem_fingerprint,
    compute_endpoint_fxproperties_sha,
)

# ── Fake winreg module — used by Windows-path tests on every platform ────


class _FakeWinreg:
    """Minimal in-memory stub of stdlib :mod:`winreg`.

    Tree shape mirrors Windows's: a key has values + sub-keys. Values
    are ``(name, data, vtype)`` tuples; sub-keys are nested
    :class:`_FakeKey` instances.
    """

    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, tree: dict[str, _FakeKey]) -> None:
        self._tree = tree

    def OpenKey(self, parent: object, name: str) -> _FakeKey:  # noqa: N802
        if parent == self.HKEY_LOCAL_MACHINE:
            # Walk dotted/backslash path from root.
            node: _FakeKey | None = None
            cur = self._tree
            for part in name.split("\\"):
                if part in cur:
                    node = cur[part]
                    cur = node.subkeys
                else:
                    raise OSError(f"missing key segment: {part}")
            assert node is not None
            return node
        if isinstance(parent, _FakeKey) and name in parent.subkeys:
            return parent.subkeys[name]
        raise OSError(f"missing key: {name}")

    def CloseKey(self, key: object) -> None:  # noqa: N802
        return None

    def EnumKey(self, key: _FakeKey, idx: int) -> str:  # noqa: N802
        names = list(key.subkeys.keys())
        if idx >= len(names):
            raise OSError("no more keys")
        return names[idx]

    def EnumValue(self, key: _FakeKey, idx: int) -> tuple[str, object, int]:  # noqa: N802
        if idx >= len(key.values):
            raise OSError("no more values")
        return key.values[idx]


class _FakeKey:
    def __init__(
        self,
        values: list[tuple[str, object, int]] | None = None,
        subkeys: dict[str, _FakeKey] | None = None,
    ) -> None:
        self.values = values or []
        self.subkeys = subkeys or {}


# ── _canonical_bytes — the value-coercion helper ────────────────────────


class TestCanonicalBytes:
    """Every registry value type we may encounter must be coerced stably."""

    def test_str(self) -> None:
        assert _canonical_bytes("hello") == b"hello"

    def test_bytes(self) -> None:
        assert _canonical_bytes(b"\x01\x02") == b"\x01\x02"

    def test_int(self) -> None:
        assert _canonical_bytes(42) == b"42"

    def test_list_of_strings(self) -> None:
        assert _canonical_bytes(["a", "b", "c"]) == b"a\x00b\x00c"

    def test_list_with_non_strings(self) -> None:
        assert _canonical_bytes([1, "x"]) == b"1\x00x"

    def test_unknown_type_falls_back_to_str(self) -> None:
        assert _canonical_bytes(None) == b"None"


# ── compute_endpoint_fxproperties_sha ───────────────────────────────────


class TestComputeEndpointFxPropertiesSha:
    def test_non_windows_returns_empty(self) -> None:
        with patch("sovyx.voice.health._fingerprint.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert compute_endpoint_fxproperties_sha("{guid}") == ""

    @pytest.mark.skipif(sys.platform != "win32", reason="winreg only on Windows")
    def test_missing_endpoint_returns_empty_on_real_winreg(self) -> None:
        # Real winreg: an obviously-bogus GUID must collapse to "".
        result = compute_endpoint_fxproperties_sha("{not-a-real-guid}")
        assert result == ""

    def test_with_fake_winreg_returns_stable_hash(self) -> None:
        fx = _FakeKey(values=[("0", "{ABC-CLSID}", 1)])
        endpoint = _FakeKey(subkeys={"FxProperties": fx})
        capture_root = _FakeKey(subkeys={"{guid}": endpoint})
        tree = {
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
                                                        subkeys={"Capture": capture_root},
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
            ),
        }
        fake = _FakeWinreg(tree)
        with (
            patch("sovyx.voice.health._fingerprint.sys") as mock_sys,
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            mock_sys.platform = "win32"
            sha1 = compute_endpoint_fxproperties_sha("{guid}")
            sha2 = compute_endpoint_fxproperties_sha("{guid}")
        assert sha1
        assert sha1 == sha2  # determinism

    def test_changing_value_changes_hash(self) -> None:
        def _build(value: str) -> dict[str, _FakeKey]:
            fx = _FakeKey(values=[("0", value, 1)])
            endpoint = _FakeKey(subkeys={"FxProperties": fx})
            capture_root = _FakeKey(subkeys={"{guid}": endpoint})
            return {
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
                                                            subkeys={"Capture": capture_root},
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
                ),
            }

        with patch("sovyx.voice.health._fingerprint.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch.dict("sys.modules", {"winreg": _FakeWinreg(_build("CLSID-A"))}):
                sha_a = compute_endpoint_fxproperties_sha("{guid}")
            with patch.dict("sys.modules", {"winreg": _FakeWinreg(_build("CLSID-B"))}):
                sha_b = compute_endpoint_fxproperties_sha("{guid}")
        assert sha_a != sha_b


# ── compute_audio_subsystem_fingerprint ─────────────────────────────────


class TestComputeAudioSubsystemFingerprint:
    def test_returns_object_with_timestamp(self) -> None:
        fp = compute_audio_subsystem_fingerprint()
        assert fp.computed_at  # ISO-8601, non-empty

    @pytest.mark.skipif(sys.platform != "win32", reason="real-winreg only on Windows")
    def test_windows_populates_endpoints_sha(self) -> None:
        fp = compute_audio_subsystem_fingerprint()
        # On Windows, MMDevices is always present → SHA non-empty.
        assert fp.windows_audio_endpoints_sha
        assert len(fp.windows_audio_endpoints_sha) == 64
        assert fp.linux_pulseaudio_config_sha == ""
        assert fp.macos_coreaudio_plugins_sha == ""

    @pytest.mark.skipif(sys.platform != "win32", reason="real-winreg only on Windows")
    def test_windows_fingerprint_stable(self) -> None:
        a = compute_audio_subsystem_fingerprint()
        b = compute_audio_subsystem_fingerprint()
        assert a.windows_audio_endpoints_sha == b.windows_audio_endpoints_sha
        assert a.windows_fxproperties_global_sha == b.windows_fxproperties_global_sha

    def test_linux_branch_via_platform_patch(self, tmp_path: Path) -> None:
        cfg = tmp_path / "default.pa"
        cfg.write_text("load-module module-suspend-on-idle\n")
        with (
            patch("sovyx.voice.health._fingerprint.sys") as mock_sys,
            patch(
                "sovyx.voice.health._fingerprint._PULSE_CONFIG_PATHS",
                (cfg,),
            ),
            patch("sovyx.voice.health._fingerprint._PIPEWIRE_CONFIG_DIRS", ()),
        ):
            mock_sys.platform = "linux"
            fp = compute_audio_subsystem_fingerprint()
        assert fp.linux_pulseaudio_config_sha
        assert fp.windows_audio_endpoints_sha == ""
        assert fp.macos_coreaudio_plugins_sha == ""

    def test_windows_branch_via_fake_winreg(self) -> None:
        """compute_audio_subsystem_fingerprint covers MMDevices + FxProperties paths."""
        fx_a = _FakeKey(values=[("0", "{CLSID-A}", 1)])
        ep_a = _FakeKey(
            values=[("DeviceState", 1, 4)],
            subkeys={"FxProperties": fx_a, "Properties": _FakeKey()},
        )
        fx_b = _FakeKey(values=[("0", "{CLSID-B}", 1)])
        ep_b = _FakeKey(
            values=[("DeviceState", 1, 4)],
            subkeys={"FxProperties": fx_b},
        )
        capture = _FakeKey(subkeys={"{ep-a}": ep_a})
        render = _FakeKey(subkeys={"{ep-b}": ep_b})
        tree = {
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
                                                        subkeys={
                                                            "Capture": capture,
                                                            "Render": render,
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
            ),
        }
        fake = _FakeWinreg(tree)
        with (
            patch("sovyx.voice.health._fingerprint.sys") as mock_sys,
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            mock_sys.platform = "win32"
            fp = compute_audio_subsystem_fingerprint()
        assert fp.windows_audio_endpoints_sha
        assert fp.windows_fxproperties_global_sha
        # Distinct hashes — different inputs must not collapse.
        assert fp.windows_audio_endpoints_sha != fp.windows_fxproperties_global_sha

    def test_windows_missing_root_keys_returns_empty_inputs(self) -> None:
        """Both Capture and Render absent → SHA over empty input (still stable)."""
        fake = _FakeWinreg({})
        with (
            patch("sovyx.voice.health._fingerprint.sys") as mock_sys,
            patch.dict("sys.modules", {"winreg": fake}),
        ):
            mock_sys.platform = "win32"
            fp = compute_audio_subsystem_fingerprint()
        assert fp.windows_audio_endpoints_sha == hashlib.sha256().hexdigest()
        assert fp.windows_fxproperties_global_sha == hashlib.sha256().hexdigest()

    def test_windows_endpoint_without_fxproperties_skipped(self) -> None:
        """Endpoint with no FxProperties subkey → still walks via OSError catch."""
        ep = _FakeKey()  # no FxProperties at all
        capture = _FakeKey(subkeys={"{ep}": ep})
        tree = {
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
            ),
        }
        with (
            patch("sovyx.voice.health._fingerprint.sys") as mock_sys,
            patch.dict("sys.modules", {"winreg": _FakeWinreg(tree)}),
        ):
            mock_sys.platform = "win32"
            fp = compute_audio_subsystem_fingerprint()
        # No exception, hash still produced.
        assert fp.windows_fxproperties_global_sha

    def test_darwin_branch_via_platform_patch(self, tmp_path: Path) -> None:
        hal_dir = tmp_path / "HAL"
        hal_dir.mkdir()
        plugin = hal_dir / "BlackHole.driver"
        plugin.mkdir()
        (plugin / "Contents").mkdir()
        (plugin / "Contents" / "Info.plist").write_text("<plist/>")
        with (
            patch("sovyx.voice.health._fingerprint.sys") as mock_sys,
            patch(
                "sovyx.voice.health._fingerprint._COREAUDIO_HAL_DIRS",
                (hal_dir,),
            ),
        ):
            mock_sys.platform = "darwin"
            fp = compute_audio_subsystem_fingerprint()
        assert fp.macos_coreaudio_plugins_sha
        assert fp.windows_audio_endpoints_sha == ""
        assert fp.linux_pulseaudio_config_sha == ""


# ── _hash_linux_pulse_pipewire_config (direct) ──────────────────────────


class TestLinuxFingerprint:
    def test_empty_when_no_files_present(self, tmp_path: Path) -> None:
        with (
            patch("sovyx.voice.health._fingerprint._PULSE_CONFIG_PATHS", ()),
            patch("sovyx.voice.health._fingerprint._PIPEWIRE_CONFIG_DIRS", ()),
        ):
            sha = _hash_linux_pulse_pipewire_config()
        # Empty hasher still produces a SHA — that's fine; it's stable.
        assert sha == hashlib.sha256().hexdigest()

    def test_pulse_file_changes_hash(self, tmp_path: Path) -> None:
        cfg = tmp_path / "default.pa"
        cfg.write_text("v1")
        with (
            patch("sovyx.voice.health._fingerprint._PULSE_CONFIG_PATHS", (cfg,)),
            patch("sovyx.voice.health._fingerprint._PIPEWIRE_CONFIG_DIRS", ()),
        ):
            sha_a = _hash_linux_pulse_pipewire_config()
            cfg.write_text("v2")
            sha_b = _hash_linux_pulse_pipewire_config()
        assert sha_a != sha_b

    def test_pipewire_dir_walked_for_conf_files(self, tmp_path: Path) -> None:
        pw = tmp_path / "pipewire"
        pw.mkdir()
        (pw / "client.conf").write_text("ctx = 1")
        (pw / "ignore.txt").write_text("not a conf")
        with (
            patch("sovyx.voice.health._fingerprint._PULSE_CONFIG_PATHS", ()),
            patch("sovyx.voice.health._fingerprint._PIPEWIRE_CONFIG_DIRS", (pw,)),
        ):
            sha = _hash_linux_pulse_pipewire_config()
        assert sha != hashlib.sha256().hexdigest()

    def test_skips_nonexistent_pulse_paths_and_pipewire_dirs(self, tmp_path: Path) -> None:
        """is_file/is_dir False branches: missing entries are skipped silently."""
        with (
            patch(
                "sovyx.voice.health._fingerprint._PULSE_CONFIG_PATHS",
                (tmp_path / "does-not-exist.pa",),
            ),
            patch(
                "sovyx.voice.health._fingerprint._PIPEWIRE_CONFIG_DIRS",
                (tmp_path / "absent-dir",),
            ),
        ):
            sha = _hash_linux_pulse_pipewire_config()
        assert sha == hashlib.sha256().hexdigest()

    def test_pipewire_rglob_oserror_is_swallowed(self, tmp_path: Path) -> None:
        pw = tmp_path / "pipewire"
        pw.mkdir()

        def _boom(_self: Path, _pattern: str) -> object:
            raise OSError("walk denied")

        with (
            patch("sovyx.voice.health._fingerprint._PULSE_CONFIG_PATHS", ()),
            patch("sovyx.voice.health._fingerprint._PIPEWIRE_CONFIG_DIRS", (pw,)),
            patch.object(Path, "rglob", _boom),
        ):
            sha = _hash_linux_pulse_pipewire_config()
        assert sha == hashlib.sha256().hexdigest()

    def test_read_bytes_oserror_skips_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "default.pa"
        cfg.write_text("v1")
        original = Path.read_bytes

        def _boom(self: Path) -> bytes:
            if self == cfg:
                raise OSError("denied")
            return original(self)

        with (
            patch("sovyx.voice.health._fingerprint._PULSE_CONFIG_PATHS", (cfg,)),
            patch("sovyx.voice.health._fingerprint._PIPEWIRE_CONFIG_DIRS", ()),
            patch.object(Path, "read_bytes", _boom),
        ):
            sha = _hash_linux_pulse_pipewire_config()
        # File was found and path hashed, but bytes failed → continues without
        # the trailing record-separator byte. SHA must still be deterministic.
        assert isinstance(sha, str)
        assert len(sha) == 64


# ── _hash_macos_coreaudio_plugins (direct) ──────────────────────────────


class TestMacosFingerprint:
    def test_empty_when_no_dirs(self) -> None:
        with patch("sovyx.voice.health._fingerprint._COREAUDIO_HAL_DIRS", ()):
            sha = _hash_macos_coreaudio_plugins()
        assert sha == hashlib.sha256().hexdigest()

    def test_plugin_path_contributes_to_hash(self, tmp_path: Path) -> None:
        hal = tmp_path / "HAL"
        hal.mkdir()
        with patch("sovyx.voice.health._fingerprint._COREAUDIO_HAL_DIRS", (hal,)):
            sha_a = _hash_macos_coreaudio_plugins()
            (hal / "Loopback.driver").mkdir()
            sha_b = _hash_macos_coreaudio_plugins()
        assert sha_a != sha_b

    def test_info_plist_content_contributes(self, tmp_path: Path) -> None:
        hal = tmp_path / "HAL"
        hal.mkdir()
        plugin = hal / "X.driver"
        (plugin / "Contents").mkdir(parents=True)
        plist = plugin / "Contents" / "Info.plist"
        plist.write_text("<plist v=1/>")
        with patch("sovyx.voice.health._fingerprint._COREAUDIO_HAL_DIRS", (hal,)):
            sha_a = _hash_macos_coreaudio_plugins()
            plist.write_text("<plist v=2/>")
            sha_b = _hash_macos_coreaudio_plugins()
        assert sha_a != sha_b

    def test_iterdir_oserror_swallowed(self, tmp_path: Path) -> None:
        hal = tmp_path / "HAL"
        hal.mkdir()

        def _boom(_self: Path) -> object:
            raise OSError("scandir failed")

        with (
            patch("sovyx.voice.health._fingerprint._COREAUDIO_HAL_DIRS", (hal,)),
            patch.object(Path, "iterdir", _boom),
        ):
            sha = _hash_macos_coreaudio_plugins()
        assert sha == hashlib.sha256().hexdigest()

    def test_info_plist_read_oserror_skipped(self, tmp_path: Path) -> None:
        hal = tmp_path / "HAL"
        hal.mkdir()
        plugin = hal / "X.driver"
        (plugin / "Contents").mkdir(parents=True)
        plist = plugin / "Contents" / "Info.plist"
        plist.write_text("ok")
        original = Path.read_bytes

        def _boom(self: Path) -> bytes:
            if self == plist:
                raise OSError("denied")
            return original(self)

        with (
            patch("sovyx.voice.health._fingerprint._COREAUDIO_HAL_DIRS", (hal,)),
            patch.object(Path, "read_bytes", _boom),
        ):
            sha = _hash_macos_coreaudio_plugins()
        # Plugin path still hashed; plist content silently skipped.
        assert isinstance(sha, str)
        assert len(sha) == 64

    def test_skips_nonexistent_hal_dirs(self, tmp_path: Path) -> None:
        with patch(
            "sovyx.voice.health._fingerprint._COREAUDIO_HAL_DIRS",
            (tmp_path / "no-such-dir",),
        ):
            sha = _hash_macos_coreaudio_plugins()
        assert sha == hashlib.sha256().hexdigest()
