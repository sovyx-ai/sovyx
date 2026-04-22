"""T12.1 — unit tests for ``build_capture_candidates``.

Covers the ordering contract of the candidate-set builder introduced
by voice-linux-cascade-root-fix T3. Exhaustive over Linux / Windows /
macOS scenarios; property-style invariants live in
``tests/property/voice/test_candidate_builder_invariants.py`` (T12.3).
"""

from __future__ import annotations

import pytest

from sovyx.voice.device_enum import DeviceEntry, DeviceKind
from sovyx.voice.health._candidate_builder import build_capture_candidates
from sovyx.voice.health.contract import CandidateSource


def _entry(
    *,
    index: int,
    name: str,
    kind: DeviceKind,
    host_api: str = "ALSA",
    in_ch: int = 1,
    is_default: bool = False,
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=0,
        host_api_name=host_api,
        max_input_channels=in_ch,
        max_output_channels=0,
        default_samplerate=48000,
        is_os_default=is_default,
        kind=kind,
    )


class TestLinuxHardwareResolved:
    """VAIO-shape scenarios — user picked a hw:X,Y device on Linux."""

    def test_includes_session_manager_and_default(self) -> None:
        hw = _entry(
            index=4,
            name="HD-Audio Generic: SN6180 Analog (hw:1,0)",
            kind=DeviceKind.HARDWARE,
        )
        pipewire = _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL)
        default = _entry(
            index=7,
            name="default",
            kind=DeviceKind.OS_DEFAULT,
            is_default=True,
        )
        all_devs = [hw, pipewire, default]

        candidates = build_capture_candidates(
            resolved=hw,
            all_devices=all_devs,
            platform_key="linux",
        )

        sources = [c.source for c in candidates]
        kinds = [c.kind for c in candidates]
        device_indices = [c.device_index for c in candidates]
        assert sources[0] == CandidateSource.USER_PREFERRED
        assert device_indices[0] == 4
        assert CandidateSource.SESSION_MANAGER_VIRTUAL in sources
        assert CandidateSource.OS_DEFAULT in sources
        # pipewire virtual comes before default os-default alias.
        assert sources.index(CandidateSource.SESSION_MANAGER_VIRTUAL) < sources.index(
            CandidateSource.OS_DEFAULT,
        )
        assert DeviceKind.HARDWARE in kinds
        assert DeviceKind.SESSION_MANAGER_VIRTUAL in kinds
        assert DeviceKind.OS_DEFAULT in kinds

    def test_dedup_by_device_index_and_host_api(self) -> None:
        # pipewire appears twice in the enumeration with the same
        # (index, host_api) key — builder must not duplicate.
        hw = _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE)
        pipewire_a = _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL)
        pipewire_b = _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL)
        candidates = build_capture_candidates(
            resolved=hw,
            all_devices=[hw, pipewire_a, pipewire_b],
            platform_key="linux",
        )
        keys = [(c.device_index, c.host_api_name) for c in candidates]
        assert len(keys) == len(set(keys))

    def test_all_candidates_have_unique_endpoint_guid_per_device(self) -> None:
        hw = _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE)
        pipewire = _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL)
        default = _entry(index=7, name="default", kind=DeviceKind.OS_DEFAULT)
        candidates = build_capture_candidates(
            resolved=hw,
            all_devices=[hw, pipewire, default],
            platform_key="linux",
        )
        guids = {c.endpoint_guid for c in candidates}
        assert len(guids) == len(candidates)

    def test_preference_rank_is_zero_based_contiguous(self) -> None:
        hw = _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE)
        pipewire = _entry(index=6, name="pipewire", kind=DeviceKind.SESSION_MANAGER_VIRTUAL)
        candidates = build_capture_candidates(
            resolved=hw,
            all_devices=[hw, pipewire],
            platform_key="linux",
        )
        assert [c.preference_rank for c in candidates] == list(range(len(candidates)))


class TestLinuxVirtualResolved:
    """User already picked a session-manager virtual — don't duplicate."""

    def test_does_not_re_append_pipewire_virtual(self) -> None:
        pipewire = _entry(
            index=6,
            name="pipewire",
            kind=DeviceKind.SESSION_MANAGER_VIRTUAL,
        )
        default = _entry(index=7, name="default", kind=DeviceKind.OS_DEFAULT)
        candidates = build_capture_candidates(
            resolved=pipewire,
            all_devices=[pipewire, default],
            platform_key="linux",
        )
        session_count = sum(
            1 for c in candidates if c.source == CandidateSource.SESSION_MANAGER_VIRTUAL
        )
        # Zero SESSION_MANAGER_VIRTUAL-source entries — the resolved one
        # lands as USER_PREFERRED.
        assert session_count == 0
        assert candidates[0].device_index == pipewire.index
        assert candidates[0].source == CandidateSource.USER_PREFERRED

    def test_os_default_resolved_skips_default_append(self) -> None:
        default = _entry(
            index=7,
            name="default",
            kind=DeviceKind.OS_DEFAULT,
            is_default=True,
        )
        hw = _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE)
        candidates = build_capture_candidates(
            resolved=default,
            all_devices=[default, hw],
            platform_key="linux",
        )
        os_default_count = sum(1 for c in candidates if c.source == CandidateSource.OS_DEFAULT)
        assert os_default_count == 0


class TestNonLinuxBehaviour:
    """Windows / macOS — returns [resolved, *canonical_siblings]."""

    def test_windows_wasapi_only(self) -> None:
        wasapi = _entry(
            index=18,
            name="Microfone (Razer BlackShark V2 Pro)",
            kind=DeviceKind.UNKNOWN,
            host_api="Windows WASAPI",
            is_default=True,
        )
        candidates = build_capture_candidates(
            resolved=wasapi,
            all_devices=[wasapi],
            platform_key="win32",
        )
        assert len(candidates) == 1
        assert candidates[0].source == CandidateSource.USER_PREFERRED
        assert candidates[0].host_api_name == "Windows WASAPI"

    def test_windows_includes_canonical_siblings(self) -> None:
        wasapi = _entry(
            index=18,
            name="Microfone (Razer BlackShark V2 Pro)",
            kind=DeviceKind.UNKNOWN,
            host_api="Windows WASAPI",
            is_default=True,
        )
        # Canonical_name is 30-char truncated — build siblings that
        # share the same canonical prefix via the _entry factory.
        directsound = _entry(
            index=8,
            name="Microfone (Razer BlackShark V2 Pro)",
            kind=DeviceKind.UNKNOWN,
            host_api="Windows DirectSound",
        )
        mme = _entry(
            index=1,
            name="Microfone (Razer BlackShark V2 Pro)",
            kind=DeviceKind.UNKNOWN,
            host_api="MME",
        )
        candidates = build_capture_candidates(
            resolved=wasapi,
            all_devices=[wasapi, directsound, mme],
            platform_key="win32",
        )
        assert len(candidates) == 3
        assert candidates[0].source == CandidateSource.USER_PREFERRED
        sibling_sources = [c.source for c in candidates[1:]]
        assert all(s == CandidateSource.CANONICAL_SIBLING for s in sibling_sources)

    def test_macos_behaves_like_windows(self) -> None:
        core = _entry(
            index=0,
            name="Built-in Microphone",
            kind=DeviceKind.UNKNOWN,
            host_api="Core Audio",
            is_default=True,
        )
        candidates = build_capture_candidates(
            resolved=core,
            all_devices=[core],
            platform_key="darwin",
        )
        assert len(candidates) == 1
        # macOS never appends SESSION_MANAGER_VIRTUAL / OS_DEFAULT even
        # when the DeviceKind is HARDWARE (guard is platform-specific).


class TestErrorSurface:
    def test_refuses_output_only_device(self) -> None:
        out_only = _entry(
            index=0,
            name="HDMI Output",
            kind=DeviceKind.UNKNOWN,
            in_ch=0,
        )
        with pytest.raises(ValueError) as exc:
            build_capture_candidates(
                resolved=out_only,
                all_devices=[out_only],
                platform_key="linux",
            )
        assert "no input channels" in str(exc.value)


class TestFallbackTailRemainsBounded:
    def test_unlisted_remaining_inputs_appended(self) -> None:
        hw = _entry(index=4, name="hw:1,0", kind=DeviceKind.HARDWARE)
        other_hw = _entry(
            index=5,
            name="hw:2,0",
            kind=DeviceKind.HARDWARE,
        )
        candidates = build_capture_candidates(
            resolved=hw,
            all_devices=[hw, other_hw],
            platform_key="linux",
        )
        sources = [c.source for c in candidates]
        assert sources[0] == CandidateSource.USER_PREFERRED
        # hw:2,0 has a different canonical_name from hw:1,0 and no
        # virtual/default kind, so it falls into the FALLBACK tail.
        assert CandidateSource.FALLBACK in sources
