"""Unit tests for :mod:`sovyx.voice.health.wizard` (ADR §4.6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sovyx.voice.health import (
    CaptureOverrides,
    CascadeResult,
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
    VoiceSetupWizard,
    WizardOutcome,
    WizardReport,
)


def _combo(
    *,
    host_api: str = "Windows WASAPI",
    sample_rate: int = 16_000,
    channels: int = 1,
    sample_format: str = "int16",
    exclusive: bool = False,
    auto_convert: bool = False,
    frames_per_buffer: int = 480,
    platform_key: str = "win32",
) -> Combo:
    return Combo(
        host_api=host_api,
        sample_rate=sample_rate,
        channels=channels,
        sample_format=sample_format,
        exclusive=exclusive,
        auto_convert=auto_convert,
        frames_per_buffer=frames_per_buffer,
        platform_key=platform_key,
    )


def _probe_result(
    *,
    diagnosis: Diagnosis,
    combo: Combo | None = None,
    vad_max_prob: float | None = 0.9,
    rms_db: float = -30.0,
) -> ProbeResult:
    return ProbeResult(
        diagnosis=diagnosis,
        mode=ProbeMode.WARM,
        combo=combo if combo is not None else _combo(),
        vad_max_prob=vad_max_prob,
        vad_mean_prob=(vad_max_prob or 0.0) * 0.5,
        rms_db=rms_db,
        callbacks_fired=32,
        duration_ms=3000,
    )


def _cascade_result(
    *,
    endpoint_guid: str = "{GUID}",
    winning_combo: Combo | None = None,
    attempts_count: int = 2,
    source: str = "cascade",
    budget_exhausted: bool = False,
) -> CascadeResult:
    winning_probe = (
        _probe_result(diagnosis=Diagnosis.HEALTHY, combo=winning_combo)
        if winning_combo is not None
        else None
    )
    return CascadeResult(
        endpoint_guid=endpoint_guid,
        winning_combo=winning_combo,
        winning_probe=winning_probe,
        attempts=() if winning_probe is None else (winning_probe,),
        attempts_count=attempts_count,
        budget_exhausted=budget_exhausted,
        source=source if winning_combo is not None else "none",
    )


class _FakeProbe:
    """Test double for ``probe_fn``."""

    def __init__(self, *, result: ProbeResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> ProbeResult:
        self.calls.append(kwargs)
        return self._result


class _FakeCascade:
    """Test double for ``cascade_fn``."""

    def __init__(self, *, result: CascadeResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> CascadeResult:
        self.calls.append(kwargs)
        return self._result


_ENDPOINT = "{0.0.1.00000000}.{GUID-A}"


@pytest.fixture()
def overrides(tmp_path: Path) -> CaptureOverrides:
    return CaptureOverrides(path=tmp_path / "capture_overrides.json")


class TestWizardOutcomeEnum:
    """StrEnum invariants (CLAUDE.md #9)."""

    def test_values_are_strings(self) -> None:
        assert WizardOutcome.PASSED_DIRECT.value == "passed_direct"
        assert WizardOutcome.PASSED_VIA_CASCADE.value == "passed_via_cascade"
        assert WizardOutcome.DEGRADED_NO_COMBO.value == "degraded_no_combo"

    def test_roundtrip_through_str(self) -> None:
        for outcome in WizardOutcome:
            assert WizardOutcome(outcome.value) is outcome


class TestHappyPath:
    """Warm probe returns HEALTHY on first combo."""

    @pytest.mark.asyncio()
    async def test_pins_and_returns_passed_direct(self, overrides: CaptureOverrides) -> None:
        combo = _combo()
        probe = _FakeProbe(result=_probe_result(diagnosis=Diagnosis.HEALTHY, combo=combo))
        cascade = _FakeCascade(result=_cascade_result())
        wizard = VoiceSetupWizard(
            probe_fn=probe,
            cascade_fn=cascade,
            capture_overrides=overrides,
            platform_key="win32",
        )

        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=3,
            preferred_combo=combo,
            device_friendly_name="Mic Array",
        )

        assert isinstance(report, WizardReport)
        assert report.outcome is WizardOutcome.PASSED_DIRECT
        assert report.winning_combo == combo
        assert report.cascade_result is None
        assert report.pinned is True
        assert overrides.get(_ENDPOINT) == combo
        assert len(probe.calls) == 1
        assert probe.calls[0]["mode"] is ProbeMode.WARM
        assert probe.calls[0]["combo"] == combo
        assert len(cascade.calls) == 0

    @pytest.mark.asyncio()
    async def test_pin_suppressed_when_overrides_is_none(self) -> None:
        combo = _combo()
        wizard = VoiceSetupWizard(
            probe_fn=_FakeProbe(result=_probe_result(diagnosis=Diagnosis.HEALTHY, combo=combo)),
            cascade_fn=_FakeCascade(result=_cascade_result()),
            capture_overrides=None,
        )
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=combo,
        )
        assert report.outcome is WizardOutcome.PASSED_DIRECT
        assert report.pinned is False


class TestUserRemediableOutcomes:
    """MUTED / LOW_SIGNAL / PERMISSION_DENIED short-circuit the cascade."""

    @pytest.mark.asyncio()
    async def test_muted_short_circuits_and_attaches_deep_link(
        self, overrides: CaptureOverrides
    ) -> None:
        probe = _FakeProbe(result=_probe_result(diagnosis=Diagnosis.MUTED))
        cascade = _FakeCascade(result=_cascade_result())
        wizard = VoiceSetupWizard(
            probe_fn=probe,
            cascade_fn=cascade,
            capture_overrides=overrides,
            platform_key="win32",
        )
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=_combo(),
        )
        assert report.outcome is WizardOutcome.MUTED
        assert report.deep_link == "ms-settings:sound"
        assert report.winning_combo is None
        assert report.pinned is False
        assert len(cascade.calls) == 0

    @pytest.mark.asyncio()
    async def test_low_signal_short_circuits(self) -> None:
        probe = _FakeProbe(result=_probe_result(diagnosis=Diagnosis.LOW_SIGNAL))
        cascade = _FakeCascade(result=_cascade_result())
        wizard = VoiceSetupWizard(
            probe_fn=probe,
            cascade_fn=cascade,
            platform_key="darwin",
        )
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=_combo(),
        )
        assert report.outcome is WizardOutcome.LOW_SIGNAL
        assert "apple" in report.deep_link
        assert len(cascade.calls) == 0

    @pytest.mark.asyncio()
    async def test_permission_denied_carries_privacy_deep_link(self) -> None:
        probe = _FakeProbe(result=_probe_result(diagnosis=Diagnosis.PERMISSION_DENIED))
        wizard = VoiceSetupWizard(
            probe_fn=probe,
            cascade_fn=_FakeCascade(result=_cascade_result()),
            platform_key="win32",
        )
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=_combo(),
        )
        assert report.outcome is WizardOutcome.PERMISSION_DENIED
        assert report.deep_link == "ms-settings:privacy-microphone"

    @pytest.mark.asyncio()
    async def test_linux_has_empty_deep_link(self) -> None:
        probe = _FakeProbe(result=_probe_result(diagnosis=Diagnosis.MUTED))
        wizard = VoiceSetupWizard(
            probe_fn=probe,
            cascade_fn=_FakeCascade(result=_cascade_result()),
            platform_key="linux",
        )
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=_combo(),
        )
        assert report.outcome is WizardOutcome.MUTED
        assert report.deep_link == ""


class TestDegradedFlow:
    """APO_DEGRADED / VAD_INSENSITIVE / NO_SIGNAL fall through to the cascade."""

    @pytest.mark.asyncio()
    async def test_apo_degraded_triggers_cascade(self, overrides: CaptureOverrides) -> None:
        first_combo = _combo()
        winning = _combo(exclusive=True, sample_rate=48_000)
        probe = _FakeProbe(
            result=_probe_result(
                diagnosis=Diagnosis.APO_DEGRADED,
                combo=first_combo,
                vad_max_prob=0.01,
            )
        )
        cascade = _FakeCascade(
            result=_cascade_result(
                endpoint_guid=_ENDPOINT,
                winning_combo=winning,
                attempts_count=3,
            )
        )
        wizard = VoiceSetupWizard(
            probe_fn=probe,
            cascade_fn=cascade,
            capture_overrides=overrides,
            platform_key="win32",
        )
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=2,
            preferred_combo=first_combo,
            device_friendly_name="Mic",
            detected_apos=("voiceclarityep",),
        )
        assert report.outcome is WizardOutcome.PASSED_VIA_CASCADE
        assert report.winning_combo == winning
        assert report.cascade_result is not None
        assert report.cascade_result.attempts_count == 3
        assert report.pinned is True
        assert overrides.get(_ENDPOINT) == winning
        assert len(cascade.calls) == 1
        cascade_args = cascade.calls[0]
        assert cascade_args["endpoint_guid"] == _ENDPOINT
        assert cascade_args["mode"] is ProbeMode.WARM
        assert cascade_args["detected_apos"] == ("voiceclarityep",)

    @pytest.mark.asyncio()
    async def test_vad_insensitive_triggers_cascade(self) -> None:
        probe = _FakeProbe(
            result=_probe_result(diagnosis=Diagnosis.VAD_INSENSITIVE, vad_max_prob=0.2)
        )
        cascade = _FakeCascade(result=_cascade_result(winning_combo=_combo(exclusive=True)))
        wizard = VoiceSetupWizard(probe_fn=probe, cascade_fn=cascade)
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=_combo(),
        )
        assert report.outcome is WizardOutcome.PASSED_VIA_CASCADE

    @pytest.mark.asyncio()
    async def test_cascade_exhaustion_yields_degraded_no_combo(
        self, overrides: CaptureOverrides
    ) -> None:
        probe = _FakeProbe(result=_probe_result(diagnosis=Diagnosis.APO_DEGRADED))
        cascade = _FakeCascade(result=_cascade_result(winning_combo=None, budget_exhausted=True))
        wizard = VoiceSetupWizard(
            probe_fn=probe,
            cascade_fn=cascade,
            capture_overrides=overrides,
        )
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=_combo(),
        )
        assert report.outcome is WizardOutcome.DEGRADED_NO_COMBO
        assert report.winning_combo is None
        assert report.pinned is False
        assert report.cascade_result is not None
        assert report.cascade_result.budget_exhausted is True
        assert overrides.get(_ENDPOINT) is None

    @pytest.mark.asyncio()
    async def test_no_signal_and_format_mismatch_also_trigger_cascade(self) -> None:
        for diag in (Diagnosis.NO_SIGNAL, Diagnosis.FORMAT_MISMATCH):
            probe = _FakeProbe(result=_probe_result(diagnosis=diag))
            cascade = _FakeCascade(result=_cascade_result(winning_combo=_combo(exclusive=True)))
            wizard = VoiceSetupWizard(probe_fn=probe, cascade_fn=cascade)
            report = await wizard.run(
                endpoint_guid=_ENDPOINT,
                device_index=0,
                preferred_combo=_combo(),
            )
            assert report.outcome is WizardOutcome.PASSED_VIA_CASCADE, f"diag={diag}"
            assert len(cascade.calls) == 1


class TestOtherOutcome:
    """Driver / device / hot-unplug / unknown diagnoses bail out as OTHER."""

    @pytest.mark.asyncio()
    async def test_driver_error_becomes_other(self) -> None:
        probe = _FakeProbe(result=_probe_result(diagnosis=Diagnosis.DRIVER_ERROR))
        cascade = _FakeCascade(result=_cascade_result())
        wizard = VoiceSetupWizard(probe_fn=probe, cascade_fn=cascade)
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=_combo(),
        )
        assert report.outcome is WizardOutcome.OTHER
        assert report.winning_combo is None
        assert report.pinned is False
        assert len(cascade.calls) == 0

    @pytest.mark.asyncio()
    async def test_device_busy_becomes_other(self) -> None:
        probe = _FakeProbe(result=_probe_result(diagnosis=Diagnosis.DEVICE_BUSY))
        wizard = VoiceSetupWizard(
            probe_fn=probe,
            cascade_fn=_FakeCascade(result=_cascade_result()),
        )
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=_combo(),
        )
        assert report.outcome is WizardOutcome.OTHER


class TestValidation:
    """Argument validation at the wizard boundary."""

    @pytest.mark.asyncio()
    async def test_empty_endpoint_guid_raises(self) -> None:
        wizard = VoiceSetupWizard(
            probe_fn=_FakeProbe(result=_probe_result(diagnosis=Diagnosis.HEALTHY)),
            cascade_fn=_FakeCascade(result=_cascade_result()),
        )
        with pytest.raises(ValueError) as exc:
            await wizard.run(
                endpoint_guid="",
                device_index=0,
                preferred_combo=_combo(),
            )
        assert "endpoint_guid" in str(exc.value)


class TestPinBestEffort:
    """Pin IO failure must not degrade the outcome."""

    @pytest.mark.asyncio()
    async def test_pin_exception_is_swallowed_into_pinned_false(self) -> None:
        class _FlakyOverrides:
            def pin(self, *args: Any, **kwargs: Any) -> None:
                raise OSError("disk full")

        combo = _combo()
        wizard = VoiceSetupWizard(
            probe_fn=_FakeProbe(result=_probe_result(diagnosis=Diagnosis.HEALTHY, combo=combo)),
            cascade_fn=_FakeCascade(result=_cascade_result()),
            capture_overrides=_FlakyOverrides(),  # type: ignore[arg-type]
        )
        report = await wizard.run(
            endpoint_guid=_ENDPOINT,
            device_index=0,
            preferred_combo=combo,
        )
        assert report.outcome is WizardOutcome.PASSED_DIRECT
        assert report.pinned is False
        assert report.winning_combo == combo
