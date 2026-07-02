"""HEALTH-2 (2026-07-02) — cascade-layer quarantine-reason wiring.

AP #70 closure: :func:`resolve_reason_from_diagnosis` was shipped as
Mission H3 foundation with a docstring claiming the cascade layer
consumed it, but had ZERO production callers — so cascade quarantines
carried empty ``resolved_reason`` and ``STREAM_OPEN_TIMEOUT`` endpoints
(designed recheck-INELIGIBLE as ``capture_dead``) were cold-probe-
rechecked, burning the rechecker budget.

Pins the wired chain:

* ``cascade/_budget.py`` ``_quarantine_endpoint(diagnosis=...)`` resolves
  the terminal :class:`Diagnosis` via the H3 SSoT resolver and stamps
  ``resolved_reason`` / ``derived_reason`` on the quarantine entry.
* ``STREAM_OPEN_TIMEOUT`` → ``capture_dead`` → recheck-INELIGIBLE (the
  :class:`KernelInvalidatedRechecker` round skips the entry).
* ``KERNEL_INVALIDATED`` → ``kernel_invalidated`` → recheck-ELIGIBLE.
* Resolver contract violations (non-terminal diagnosis) are guarded —
  the quarantine write path never breaks.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sovyx.voice.health import (
    Combo,
    Diagnosis,
    EndpointQuarantine,
    KernelInvalidatedRechecker,
    ProbeMode,
    ProbeResult,
    QuarantineEntry,
    is_recheck_eligible,
    reset_default_quarantine,
)
from sovyx.voice.health.cascade import _budget as budget_mod
from sovyx.voice.health.cascade._budget import _quarantine_endpoint


@pytest.fixture(autouse=True)
def _reset_singleton():  # type: ignore[no-untyped-def]
    reset_default_quarantine()
    yield
    reset_default_quarantine()


@pytest.fixture()
def store() -> EndpointQuarantine:
    return EndpointQuarantine(quarantine_s=60.0)


def _quarantine(
    store: EndpointQuarantine,
    *,
    diagnosis: Diagnosis | None,
    guid: str = "{surrogate-razer-wasapi}",
) -> bool:
    return _quarantine_endpoint(
        quarantine=store,
        endpoint_guid=guid,
        device_friendly_name="Razer BlackShark V2 Pro",
        device_interface_name="",
        host_api="WASAPI",
        platform_key="win32",
        reason="probe_cascade",  # h3-allowlist: lifecycle-tag (test replay)
        physical_device_id="razer blackshark v2 pro",
        diagnosis=diagnosis,
    )


def _win_combo() -> Combo:
    return Combo(
        host_api="Windows WASAPI",
        sample_rate=16_000,
        channels=1,
        sample_format="int16",
        exclusive=True,
        auto_convert=False,
        frames_per_buffer=480,
        platform_key="win32",
    )


def _probe_result(diagnosis: Diagnosis) -> ProbeResult:
    return ProbeResult(
        diagnosis=diagnosis,
        mode=ProbeMode.COLD,
        combo=_win_combo(),
        vad_max_prob=None,
        vad_mean_prob=None,
        rms_db=-30.0,
        callbacks_fired=5,
        duration_ms=200,
        error=None,
    )


class _SpyProbe:
    """Records recheck probe calls; always answers HEALTHY."""

    def __init__(self) -> None:
        self.calls: list[QuarantineEntry] = []

    async def __call__(self, entry: QuarantineEntry) -> ProbeResult:
        self.calls.append(entry)
        return _probe_result(Diagnosis.HEALTHY)


class TestCascadeResolvedReason:
    """The centraliser stamps the SSoT-resolved reason (AP #70 wire-up)."""

    def test_stream_open_timeout_resolves_capture_dead(
        self,
        store: EndpointQuarantine,
    ) -> None:
        assert _quarantine(store, diagnosis=Diagnosis.STREAM_OPEN_TIMEOUT) is True
        entry = store.get("{surrogate-razer-wasapi}")
        assert entry is not None
        assert entry.resolved_reason == "capture_dead"
        assert entry.derived_reason == "capture_dead"
        # Lifecycle tag preserved on the legacy ``reason`` field.
        assert entry.reason == "probe_cascade"

    def test_kernel_invalidated_resolves_kernel_invalidated(
        self,
        store: EndpointQuarantine,
    ) -> None:
        assert _quarantine(store, diagnosis=Diagnosis.KERNEL_INVALIDATED) is True
        entry = store.get("{surrogate-razer-wasapi}")
        assert entry is not None
        assert entry.resolved_reason == "kernel_invalidated"

    def test_no_diagnosis_preserves_inherit_semantics(
        self,
        store: EndpointQuarantine,
    ) -> None:
        """``diagnosis=None`` keeps the pre-wiring inherit-from-prior
        behaviour (fresh entry → empty resolved_reason)."""
        assert _quarantine(store, diagnosis=None) is True
        entry = store.get("{surrogate-razer-wasapi}")
        assert entry is not None
        assert entry.resolved_reason == ""
        assert entry.derived_reason == ""

    def test_non_terminal_diagnosis_never_blocks_quarantine(
        self,
        store: EndpointQuarantine,
    ) -> None:
        """The resolver raises ValueError on non-terminal diagnoses
        (its documented contract); the centraliser guards it — the
        quarantine write path is the failover safety net and MUST
        NOT break."""
        assert _quarantine(store, diagnosis=Diagnosis.HEALTHY) is True
        entry = store.get("{surrogate-razer-wasapi}")
        assert entry is not None
        assert entry.resolved_reason == ""

    def test_resolution_metric_emitted_on_resolve(
        self,
        store: EndpointQuarantine,
    ) -> None:
        with patch.object(
            budget_mod,
            "record_quarantine_resolution_outcome",
        ) as metric:
            _quarantine(store, diagnosis=Diagnosis.STREAM_OPEN_TIMEOUT)
        metric.assert_called_once_with(
            diagnosis=Diagnosis.STREAM_OPEN_TIMEOUT.value,
            resolved_reason="capture_dead",
            platform="win32",
        )

    def test_no_store_returns_false(self) -> None:
        assert (
            _quarantine_endpoint(
                quarantine=None,
                endpoint_guid="{x}",
                device_friendly_name="",
                device_interface_name="",
                host_api="WASAPI",
                platform_key="win32",
                reason="probe_cascade",  # h3-allowlist: lifecycle-tag (test replay)
                diagnosis=Diagnosis.STREAM_OPEN_TIMEOUT,
            )
            is False
        )


class TestRecheckEligibilityChain:
    """The rechecker consumes the resolved reason (HEALTH-2 chain)."""

    def test_stream_open_timeout_quarantine_is_recheck_ineligible(
        self,
        store: EndpointQuarantine,
    ) -> None:
        _quarantine(store, diagnosis=Diagnosis.STREAM_OPEN_TIMEOUT)
        entry = store.get("{surrogate-razer-wasapi}")
        assert entry is not None
        assert (
            is_recheck_eligible(
                entry.resolved_reason or entry.derived_reason or entry.reason,
            )
            is False
        )

    async def test_rechecker_round_skips_capture_dead_entry(
        self,
        store: EndpointQuarantine,
    ) -> None:
        """A cold re-probe of a substrate-dead endpoint burns the
        rechecker budget for nothing — the round must skip it."""
        _quarantine(store, diagnosis=Diagnosis.STREAM_OPEN_TIMEOUT)
        probe = _SpyProbe()
        rechecker = KernelInvalidatedRechecker(
            probe_entry=probe,
            quarantine=store,
            interval_s=0.01,
        )

        rechecker._started = True  # drive _round directly, no loop race
        await rechecker._round()

        assert probe.calls == []
        # Entry stays quarantined — its own TTL is the exit.
        assert store.is_quarantined("{surrogate-razer-wasapi}") is True

    async def test_rechecker_round_probes_kernel_invalidated_entry(
        self,
        store: EndpointQuarantine,
    ) -> None:
        _quarantine(store, diagnosis=Diagnosis.KERNEL_INVALIDATED)
        probe = _SpyProbe()
        rechecker = KernelInvalidatedRechecker(
            probe_entry=probe,
            quarantine=store,
            interval_s=0.01,
        )

        rechecker._started = True  # drive _round directly, no loop race
        await rechecker._round()

        assert len(probe.calls) == 1
        # HEALTHY probe answer → cleared (recheck_recovered path).
        assert store.is_quarantined("{surrogate-razer-wasapi}") is False
