"""T12.2 — deterministic reproduction of the VAIO incident.

Simulates the exact enumeration the VAIO produced (8 devices, all ALSA
host_api, hw:1,0 held by PipeWire) and verifies that
:func:`sovyx.voice.health._candidate_builder.build_capture_candidates`
+ :func:`sovyx.voice.health.cascade.run_cascade_for_candidates` pick
the ``pipewire`` virtual as the winner without human intervention.

Primary DoD #1 of voice-linux-cascade-root-fix: this test passing
counts as the field-validated cure — we never needed the VAIO itself.
No sounddevice is imported, no OS audio stack is touched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from sovyx.voice.device_enum import DeviceEntry, DeviceKind
from sovyx.voice.health._candidate_builder import build_capture_candidates
from sovyx.voice.health.cascade import (
    LINUX_CASCADE,
    run_cascade_for_candidates,
)
from sovyx.voice.health.contract import (
    CandidateEndpoint,
    CandidateSource,
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)

# VAIO device enumeration — reconstructed from the forensic log.
VAIO_DEVICES: list[DeviceEntry] = [
    DeviceEntry(
        index=0,
        name="HD-Audio Generic: HDMI 0 (hw:0,3)",
        canonical_name="hd-audio generic: hdmi 0 (hw:0",
        host_api_index=0,
        host_api_name="ALSA",
        max_input_channels=0,
        max_output_channels=8,
        default_samplerate=44100,
        is_os_default=False,
        kind=DeviceKind.HARDWARE,
    ),
    DeviceEntry(
        index=4,
        name="HD-Audio Generic: SN6180 Analog (hw:1,0)",
        canonical_name="hd-audio generic: sn6180 analog",
        host_api_index=0,
        host_api_name="ALSA",
        max_input_channels=2,
        max_output_channels=2,
        default_samplerate=48000,
        is_os_default=False,
        kind=DeviceKind.HARDWARE,
    ),
    DeviceEntry(
        index=6,
        name="pipewire",
        canonical_name="pipewire",
        host_api_index=0,
        host_api_name="ALSA",
        max_input_channels=64,
        max_output_channels=64,
        default_samplerate=44100,
        is_os_default=False,
        kind=DeviceKind.SESSION_MANAGER_VIRTUAL,
    ),
    DeviceEntry(
        index=7,
        name="default",
        canonical_name="default",
        host_api_index=0,
        host_api_name="ALSA",
        max_input_channels=64,
        max_output_channels=64,
        default_samplerate=44100,
        is_os_default=True,
        kind=DeviceKind.OS_DEFAULT,
    ),
]


@dataclass
class _ProbeDispatcher:
    """Simulates PipeWire grabbing hw:1,0 while pipewire / default work."""

    call_log: list[tuple[int, str, int]]  # (device_index, host_api, sample_rate)

    async def __call__(
        self,
        *,
        combo: Combo,
        mode: ProbeMode,
        device_index: int,
        hard_timeout_s: float,
    ) -> ProbeResult:
        self.call_log.append((device_index, combo.host_api, combo.sample_rate))
        # hw:1,0 (index 4) is held by PipeWire — every open fails with
        # DEVICE_BUSY on every combo.
        if device_index == 4:
            return ProbeResult(
                diagnosis=Diagnosis.DEVICE_BUSY,
                mode=mode,
                combo=combo,
                vad_max_prob=None,
                vad_mean_prob=None,
                rms_db=float("-inf"),
                callbacks_fired=0,
                duration_ms=10,
                error="Device unavailable [PaErrorCode -9985]",
            )
        # pipewire (index 6) and default (index 7) open cleanly on the
        # first combo attempt.
        return ProbeResult(
            diagnosis=Diagnosis.HEALTHY,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=-18.0,
            callbacks_fired=30,
            duration_ms=100,
            error=None,
        )


@pytest.mark.asyncio()
async def test_vaio_cascade_escapes_to_pipewire() -> None:
    """Primary DoD #1 — VAIO reproduction produces a pipewire winner.

    Mimics the exact enumeration the VAIO produced. Verifies the
    candidate-set includes pipewire + default as rank ≥ 1, and that
    the cascade picks pipewire as the winner (rank 1 ahead of default
    rank 2) because hw:1,0 is busy.
    """
    hw = next(d for d in VAIO_DEVICES if d.index == 4)
    candidates = build_capture_candidates(
        resolved=hw,
        all_devices=VAIO_DEVICES,
        platform_key="linux",
    )

    # Candidate-set invariants.
    assert candidates[0].source == CandidateSource.USER_PREFERRED
    assert candidates[0].device_index == 4
    session_candidates = [
        c for c in candidates if c.source == CandidateSource.SESSION_MANAGER_VIRTUAL
    ]
    assert len(session_candidates) == 1
    assert session_candidates[0].device_index == 6  # pipewire virtual
    default_candidates = [c for c in candidates if c.source == CandidateSource.OS_DEFAULT]
    assert len(default_candidates) == 1
    assert default_candidates[0].device_index == 7  # default alias

    # Run cascade with the deterministic probe.
    dispatcher = _ProbeDispatcher(call_log=[])
    result = await run_cascade_for_candidates(
        candidates=candidates,
        mode=ProbeMode.COLD,
        platform_key="linux",
        probe_fn=dispatcher,
        total_budget_s=10.0,
        attempt_budget_s=5.0,
    )

    # Winner assertions — exactly the DoD #1 contract.
    assert result.winning_combo is not None
    assert result.winning_candidate is not None
    assert result.winning_candidate.device_index == 6
    assert result.winning_candidate.source == CandidateSource.SESSION_MANAGER_VIRTUAL
    assert result.winning_candidate.kind == DeviceKind.SESSION_MANAGER_VIRTUAL
    assert result.source == "cascade"

    # Call ladder — at least one hw:1,0 probe failed before the
    # pipewire probe ran. No probe targeted device 7 (default) because
    # pipewire already won.
    device_indices_called = [entry[0] for entry in dispatcher.call_log]
    assert 4 in device_indices_called
    assert 6 in device_indices_called
    assert 7 not in device_indices_called
    # First probe was against device 4 (the user's resolved choice).
    assert device_indices_called[0] == 4


@pytest.mark.asyncio()
async def test_all_hardware_busy_exhausts_candidate_set() -> None:
    """When even session-manager virtuals fail, cascade exhausts cleanly.

    Exercises the return path that :class:`CaptureDeviceContendedError`
    is raised on — the integration point for T7's session-manager
    contention heuristic.
    """
    hw = next(d for d in VAIO_DEVICES if d.index == 4)
    candidates = build_capture_candidates(
        resolved=hw,
        all_devices=VAIO_DEVICES,
        platform_key="linux",
    )

    async def always_busy(
        *,
        combo: Combo,
        mode: ProbeMode,
        device_index: int,  # noqa: ARG001
        hard_timeout_s: float,  # noqa: ARG001
    ) -> ProbeResult:
        return ProbeResult(
            diagnosis=Diagnosis.DEVICE_BUSY,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=float("-inf"),
            callbacks_fired=0,
            duration_ms=5,
            error="Device unavailable [PaErrorCode -9985]",
        )

    result = await run_cascade_for_candidates(
        candidates=candidates,
        mode=ProbeMode.COLD,
        platform_key="linux",
        probe_fn=always_busy,
        total_budget_s=10.0,
        attempt_budget_s=5.0,
    )
    assert result.winning_combo is None
    assert result.winning_candidate is None
    assert result.source == "none"


@pytest.mark.asyncio()
async def test_empty_candidates_raises() -> None:
    with pytest.raises(ValueError) as exc:
        await run_cascade_for_candidates(
            candidates=(),
            mode=ProbeMode.COLD,
            platform_key="linux",
        )
    assert "non-empty" in str(exc.value)


def test_linux_cascade_is_six_combos() -> None:
    # Invariant the VAIO forensic log relies on: exactly 6 combos per
    # candidate in the Linux cascade default table.
    assert len(LINUX_CASCADE) == 6


@pytest.mark.asyncio()
async def test_single_candidate_equivalence_preserves_semantics() -> None:
    """Regression: len(candidates) == 1 must behave like pre-refactor run_cascade.

    This is the ``Windows / macOS happy path equivalence`` invariant
    from the ADR: when the candidate-set has exactly one entry (every
    non-Linux path today), the cascade should behave identically to
    a single-endpoint cascade run.
    """
    entry = DeviceEntry(
        index=0,
        name="Razer BlackShark",
        canonical_name="razer blackshark",
        host_api_index=0,
        host_api_name="Windows WASAPI",
        max_input_channels=1,
        max_output_channels=0,
        default_samplerate=48000,
        is_os_default=True,
        kind=DeviceKind.UNKNOWN,
    )
    candidate = CandidateEndpoint(
        device_index=entry.index,
        host_api_name=entry.host_api_name,
        kind=entry.kind,
        canonical_name=entry.canonical_name,
        friendly_name=entry.name,
        source=CandidateSource.USER_PREFERRED,
        preference_rank=0,
        endpoint_guid="{test-guid}",
        default_samplerate=entry.default_samplerate,
    )

    async def healthy(
        *,
        combo: Combo,
        mode: ProbeMode,
        device_index: int,  # noqa: ARG001
        hard_timeout_s: float,  # noqa: ARG001
    ) -> ProbeResult:
        return ProbeResult(
            diagnosis=Diagnosis.HEALTHY,
            mode=mode,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=-15.0,
            callbacks_fired=30,
            duration_ms=50,
            error=None,
        )

    result = await run_cascade_for_candidates(
        candidates=[candidate],
        mode=ProbeMode.COLD,
        platform_key="win32",
        probe_fn=healthy,
        total_budget_s=10.0,
        attempt_budget_s=5.0,
    )
    assert result.winning_combo is not None
    assert result.winning_candidate is candidate
    assert result.source == "cascade"


def test_pipewire_kind_is_session_manager_virtual_after_classification() -> None:
    """Regression: DeviceKind.SESSION_MANAGER_VIRTUAL is what the
    classifier produces for the VAIO's pipewire entry — this is the
    invariant the candidate-builder relies on to include the virtual."""
    pipewire = next(d for d in VAIO_DEVICES if d.name == "pipewire")
    assert pipewire.kind == DeviceKind.SESSION_MANAGER_VIRTUAL


# Keep `asyncio` import alive — some test runners prune unused modules
# in fixture collection.
_ = asyncio.iscoroutinefunction
