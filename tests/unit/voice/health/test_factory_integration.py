"""Unit tests for :mod:`sovyx.voice.health._factory_integration`.

Pins ADR §5.11 semantics:

* Endpoint GUID derivation — Windows MMDevice GUID when an APO report
  is available, surrogate hash otherwise.
* :func:`run_boot_cascade` persists a HEALTHY winner to the store.
* :func:`run_boot_cascade` swallows store / cascade exceptions so the
  factory never aborts voice-enable on a migration side-effect.
* Path helpers resolve to ``<data_dir>/voice/*.json`` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.device_enum import DeviceEntry
from sovyx.voice.health._factory_integration import (
    CascadeBootOutcome,
    CascadeBootVerdict,
    classify_cascade_boot_result,
    derive_endpoint_guid,
    resolve_capture_overrides_path,
    resolve_combo_store_path,
    run_boot_cascade,
    select_alternative_endpoint,
)
from sovyx.voice.health._quarantine import EndpointQuarantine
from sovyx.voice.health.contract import (
    CascadeResult,
    Combo,
    Diagnosis,
    ProbeMode,
    ProbeResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    *,
    index: int = 3,
    name: str = "Microfone (Razer BlackShark V2 Pro)",
    host_api_name: str = "Windows WASAPI",
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=0,
        host_api_name=host_api_name,
        max_input_channels=1,
        max_output_channels=0,
        default_samplerate=48_000,
        is_os_default=True,
    )


@dataclass
class _FakeApoReport:
    """Minimal shape compatible with :func:`find_endpoint_report`."""

    endpoint_id: str
    endpoint_name: str
    device_interface_name: str = ""
    enumerator: str = "USB"
    fx_binding_count: int = 0
    known_apos: list[str] = field(default_factory=list)
    raw_clsids: list[str] = field(default_factory=list)
    voice_clarity_active: bool = False


@dataclass
class _FakeStore:
    """Stand-in for :class:`ComboStore`."""

    entries: dict[str, Combo] = field(default_factory=dict)
    record_calls: list[tuple[str, Combo]] = field(default_factory=list)
    get_raises: bool = False
    record_raises: bool = False

    def get(self, endpoint_guid: str) -> None:
        if self.get_raises:
            msg = "fake store explodes"
            raise RuntimeError(msg)
        return None

    def needs_revalidation(self, endpoint_guid: str) -> bool:  # noqa: ARG002
        return False

    def invalidate(self, endpoint_guid: str, reason: str) -> None:
        self.entries.pop(endpoint_guid, None)
        # no record of invalidations required by these tests

    def record_winning(
        self,
        endpoint_guid: str,
        *,
        device_friendly_name: str,  # noqa: ARG002
        device_interface_name: str,  # noqa: ARG002
        device_class: str,  # noqa: ARG002
        endpoint_fxproperties_sha: str,  # noqa: ARG002
        combo: Combo,
        probe: ProbeResult,  # noqa: ARG002
        detected_apos: Sequence[str],  # noqa: ARG002
        cascade_attempts_before_success: int,  # noqa: ARG002
    ) -> None:
        if self.record_raises:
            msg = "fake store record explodes"
            raise RuntimeError(msg)
        self.entries[endpoint_guid] = combo
        self.record_calls.append((endpoint_guid, combo))


@dataclass
class _FakeOverrides:
    """Stand-in for :class:`CaptureOverrides` — no pinned combos."""

    def get(self, endpoint_guid: str) -> None:  # noqa: ARG002
        return None


def _healthy_probe(combo: Combo, mode: ProbeMode) -> ProbeResult:
    return ProbeResult(
        diagnosis=Diagnosis.HEALTHY,
        mode=mode,
        combo=combo,
        vad_max_prob=0.9,
        vad_mean_prob=0.6,
        rms_db=-25.0,
        callbacks_fired=50,
        duration_ms=1_500,
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestPathHelpers:
    """Paths live under ``data_dir/voice/`` exactly — no hidden subdirs."""

    def test_combo_store_path(self, tmp_path: Path) -> None:
        assert resolve_combo_store_path(tmp_path) == tmp_path / "voice" / "capture_combos.json"

    def test_capture_overrides_path(self, tmp_path: Path) -> None:
        assert (
            resolve_capture_overrides_path(tmp_path)
            == tmp_path / "voice" / "capture_overrides.json"
        )


# ---------------------------------------------------------------------------
# Endpoint GUID derivation
# ---------------------------------------------------------------------------


class TestDeriveEndpointGuid:
    """GUID resolution order: MMDevice match → surrogate hash."""

    def test_mmdevice_match_when_apo_report_available(self) -> None:
        entry = _make_entry(name="Microfone (Razer BlackShark V2 Pro)")
        report = _FakeApoReport(
            endpoint_id="{0.0.1.00000000}.{abc-123}",
            endpoint_name="Microfone (Razer BlackShark V2 Pro)",
            device_interface_name="Razer BlackShark V2 Pro",
        )

        guid = derive_endpoint_guid(
            entry,
            apo_reports=[report],  # type: ignore[list-item]
            platform_key="win32",
        )

        assert guid == "{0.0.1.00000000}.{abc-123}"

    def test_surrogate_when_no_apo_match(self) -> None:
        entry = _make_entry(name="Some USB Headset (Acme)")
        # APO report exists but won't match this device.
        report = _FakeApoReport(
            endpoint_id="{unrelated}",
            endpoint_name="Different Mic",
            device_interface_name="Different Mic",
        )

        guid = derive_endpoint_guid(
            entry,
            apo_reports=[report],  # type: ignore[list-item]
            platform_key="win32",
        )

        assert guid.startswith("{surrogate-")
        assert guid.endswith("}")

    def test_surrogate_when_no_apo_reports(self) -> None:
        entry = _make_entry()
        guid = derive_endpoint_guid(entry, apo_reports=None, platform_key="win32")
        assert guid.startswith("{surrogate-")

    def test_surrogate_stable_across_calls(self) -> None:
        entry = _make_entry()
        g1 = derive_endpoint_guid(entry, apo_reports=None, platform_key="linux")
        g2 = derive_endpoint_guid(entry, apo_reports=None, platform_key="linux")
        assert g1 == g2

    def test_surrogate_differs_across_host_apis(self) -> None:
        a = _make_entry(host_api_name="Windows WASAPI")
        b = _make_entry(host_api_name="MME")
        guid_a = derive_endpoint_guid(a, apo_reports=None, platform_key="win32")
        guid_b = derive_endpoint_guid(b, apo_reports=None, platform_key="win32")
        assert guid_a != guid_b

    def test_surrogate_differs_across_platforms(self) -> None:
        entry = _make_entry()
        guid_win = derive_endpoint_guid(entry, apo_reports=None, platform_key="win32")
        guid_linux = derive_endpoint_guid(entry, apo_reports=None, platform_key="linux")
        assert guid_win != guid_linux

    def test_non_windows_ignores_apo_reports(self) -> None:
        entry = _make_entry()
        report = _FakeApoReport(
            endpoint_id="{would-win-on-windows}",
            endpoint_name=entry.name,
            device_interface_name=entry.name,
        )

        guid = derive_endpoint_guid(
            entry,
            apo_reports=[report],  # type: ignore[list-item]
            platform_key="linux",
        )

        assert guid.startswith("{surrogate-")


# ---------------------------------------------------------------------------
# run_boot_cascade
# ---------------------------------------------------------------------------


class TestRunBootCascade:
    """§5.11: cascade populates store, never blocks boot on failure."""

    @pytest.mark.asyncio()
    async def test_records_winner_to_store(self, tmp_path: Path) -> None:
        entry = _make_entry()
        store = _FakeStore()
        overrides = _FakeOverrides()

        async def probe(
            *,
            combo: Combo,
            mode: ProbeMode,
            device_index: int,  # noqa: ARG001
            hard_timeout_s: float,  # noqa: ARG001
        ) -> ProbeResult:
            return _healthy_probe(combo, mode)

        # Monkeypatch run_cascade inside the integration module to route
        # through our fake probe. Equivalent to injecting probe_fn all
        # the way down; the integration module only exposes the tuning
        # knobs so we wire via the public cascade entry point.
        from sovyx.voice.health import _factory_integration
        from sovyx.voice.health import cascade as cascade_mod

        original = cascade_mod.run_cascade

        async def cascade_wrapper(**kwargs: object) -> object:
            return await original(**{**kwargs, "probe_fn": probe})  # type: ignore[arg-type]

        monkey = cascade_wrapper
        _factory_integration.run_cascade = monkey  # type: ignore[attr-defined]
        try:
            result = await run_boot_cascade(
                resolved=entry,
                data_dir=tmp_path,
                tuning=VoiceTuningConfig(),
                apo_reports=None,
                platform_key="win32",
                combo_store=store,  # type: ignore[arg-type]
                capture_overrides=overrides,  # type: ignore[arg-type]
            )
        finally:
            _factory_integration.run_cascade = original  # type: ignore[attr-defined]

        assert isinstance(result, CascadeResult)
        assert result.winning_combo is not None
        assert result.source == "cascade"
        assert len(store.record_calls) == 1
        recorded_guid, recorded_combo = store.record_calls[0]
        assert recorded_combo == result.winning_combo
        assert recorded_guid.startswith("{surrogate-")

    @pytest.mark.asyncio()
    async def test_returns_none_when_cascade_raises(self, tmp_path: Path) -> None:
        entry = _make_entry()
        store = _FakeStore()
        overrides = _FakeOverrides()

        from sovyx.voice.health import _factory_integration
        from sovyx.voice.health import cascade as cascade_mod

        original = cascade_mod.run_cascade

        async def exploding_cascade(**_kwargs: object) -> object:
            msg = "cascade-side detonation"
            raise RuntimeError(msg)

        _factory_integration.run_cascade = exploding_cascade  # type: ignore[attr-defined]
        try:
            result = await run_boot_cascade(
                resolved=entry,
                data_dir=tmp_path,
                tuning=VoiceTuningConfig(),
                platform_key="win32",
                combo_store=store,  # type: ignore[arg-type]
                capture_overrides=overrides,  # type: ignore[arg-type]
            )
        finally:
            _factory_integration.run_cascade = original  # type: ignore[attr-defined]

        assert result is None
        assert store.record_calls == []

    @pytest.mark.asyncio()
    async def test_passes_detected_apos_when_available(self, tmp_path: Path) -> None:
        entry = _make_entry(name="Microfone (Razer BlackShark V2 Pro)")
        report = _FakeApoReport(
            endpoint_id="{0.0.1.0000}",
            endpoint_name=entry.name,
            device_interface_name="Razer BlackShark V2 Pro",
            enumerator="USB",
            known_apos=["Windows Voice Clarity"],
        )
        store = _FakeStore()

        captured_kwargs: dict[str, object] = {}

        from sovyx.voice.health import _factory_integration

        original = _factory_integration.run_cascade

        async def capturing_cascade(**kwargs: object) -> CascadeResult:
            captured_kwargs.update(kwargs)
            return CascadeResult(
                endpoint_guid=str(kwargs["endpoint_guid"]),
                winning_combo=None,
                winning_probe=None,
                attempts=(),
                attempts_count=0,
                budget_exhausted=False,
                source="none",
            )

        _factory_integration.run_cascade = capturing_cascade  # type: ignore[attr-defined]
        try:
            await run_boot_cascade(
                resolved=entry,
                data_dir=tmp_path,
                tuning=VoiceTuningConfig(),
                apo_reports=[report],  # type: ignore[list-item]
                platform_key="win32",
                combo_store=store,  # type: ignore[arg-type]
                capture_overrides=_FakeOverrides(),  # type: ignore[arg-type]
            )
        finally:
            _factory_integration.run_cascade = original  # type: ignore[attr-defined]

        assert captured_kwargs["endpoint_guid"] == "{0.0.1.0000}"
        assert captured_kwargs["detected_apos"] == ("Windows Voice Clarity",)
        assert captured_kwargs["device_interface_name"] == "Razer BlackShark V2 Pro"
        assert captured_kwargs["device_class"] == "USB"
        assert captured_kwargs["mode"] is ProbeMode.COLD

    @pytest.mark.asyncio()
    async def test_respects_voice_clarity_autofix_flag(self, tmp_path: Path) -> None:
        entry = _make_entry()
        captured_kwargs: dict[str, object] = {}

        from sovyx.voice.health import _factory_integration

        original = _factory_integration.run_cascade

        async def capturing_cascade(**kwargs: object) -> CascadeResult:
            captured_kwargs.update(kwargs)
            return CascadeResult(
                endpoint_guid="x",
                winning_combo=None,
                winning_probe=None,
                attempts=(),
                attempts_count=0,
                budget_exhausted=False,
                source="none",
            )

        _factory_integration.run_cascade = capturing_cascade  # type: ignore[attr-defined]
        try:
            # The `voice_clarity_autofix` setting on VoiceTuningConfig
            # must propagate to run_cascade's matching kwarg unchanged.
            tuning = VoiceTuningConfig(voice_clarity_autofix=False)
            await run_boot_cascade(
                resolved=entry,
                data_dir=tmp_path,
                tuning=tuning,
                platform_key="win32",
                combo_store=_FakeStore(),  # type: ignore[arg-type]
                capture_overrides=_FakeOverrides(),  # type: ignore[arg-type]
            )
        finally:
            _factory_integration.run_cascade = original  # type: ignore[attr-defined]

        assert captured_kwargs["voice_clarity_autofix"] is False

    @pytest.mark.asyncio()
    async def test_propagates_cascade_budget_from_tuning(self, tmp_path: Path) -> None:
        captured_kwargs: dict[str, object] = {}

        from sovyx.voice.health import _factory_integration

        original = _factory_integration.run_cascade

        async def capturing_cascade(**kwargs: object) -> CascadeResult:
            captured_kwargs.update(kwargs)
            return CascadeResult(
                endpoint_guid="x",
                winning_combo=None,
                winning_probe=None,
                attempts=(),
                attempts_count=0,
                budget_exhausted=False,
                source="none",
            )

        _factory_integration.run_cascade = capturing_cascade  # type: ignore[attr-defined]
        try:
            tuning = VoiceTuningConfig(
                cascade_total_budget_s=12.5,
                cascade_attempt_budget_s=2.5,
            )
            await run_boot_cascade(
                resolved=_make_entry(),
                data_dir=tmp_path,
                tuning=tuning,
                platform_key="win32",
                combo_store=_FakeStore(),  # type: ignore[arg-type]
                capture_overrides=_FakeOverrides(),  # type: ignore[arg-type]
            )
        finally:
            _factory_integration.run_cascade = original  # type: ignore[attr-defined]

        assert captured_kwargs["total_budget_s"] == pytest.approx(12.5)
        assert captured_kwargs["attempt_budget_s"] == pytest.approx(2.5)

    @pytest.mark.asyncio()
    async def test_forwards_quarantine_kwarg_to_run_cascade(self, tmp_path: Path) -> None:
        captured_kwargs: dict[str, object] = {}

        from sovyx.voice.health import _factory_integration

        original = _factory_integration.run_cascade

        async def capturing_cascade(**kwargs: object) -> CascadeResult:
            captured_kwargs.update(kwargs)
            return CascadeResult(
                endpoint_guid="x",
                winning_combo=None,
                winning_probe=None,
                attempts=(),
                attempts_count=0,
                budget_exhausted=False,
                source="none",
            )

        _factory_integration.run_cascade = capturing_cascade  # type: ignore[attr-defined]
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        try:
            await run_boot_cascade(
                resolved=_make_entry(),
                data_dir=tmp_path,
                tuning=VoiceTuningConfig(),
                platform_key="win32",
                combo_store=_FakeStore(),  # type: ignore[arg-type]
                capture_overrides=_FakeOverrides(),  # type: ignore[arg-type]
                quarantine=q,
            )
        finally:
            _factory_integration.run_cascade = original  # type: ignore[attr-defined]

        assert captured_kwargs["quarantine"] is q
        # kill-switch follows the tuning config.
        assert captured_kwargs["kernel_invalidated_failover_enabled"] is True


# ---------------------------------------------------------------------------
# §4.4.7 select_alternative_endpoint — fail-over picker
# ---------------------------------------------------------------------------


def _input_entry(
    *,
    index: int,
    name: str,
    host_api_name: str = "Windows WASAPI",
    is_default: bool = False,
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower()[:30],
        host_api_index=0,
        host_api_name=host_api_name,
        max_input_channels=1,
        max_output_channels=0,
        default_samplerate=48_000,
        is_os_default=is_default,
    )


class TestSelectAlternativeEndpoint:
    """§4.4.7 fail-over: filter quarantined + excluded devices."""

    def test_returns_none_when_no_devices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovyx.voice import device_enum

        monkeypatch.setattr(device_enum, "enumerate_devices", lambda: [])
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        assert select_alternative_endpoint(quarantine=q, platform_key="win32") is None

    def test_excludes_quarantined_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovyx.voice import device_enum

        bad = _input_entry(index=0, name="Bad Mic")
        good = _input_entry(index=1, name="Good Mic")
        monkeypatch.setattr(device_enum, "enumerate_devices", lambda: [bad, good])

        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        bad_guid = derive_endpoint_guid(bad, apo_reports=None, platform_key="win32")
        q.add(endpoint_guid=bad_guid)

        result = select_alternative_endpoint(quarantine=q, platform_key="win32")
        assert result is not None
        assert result.name == "Good Mic"

    def test_excludes_excluded_guids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovyx.voice import device_enum

        skip = _input_entry(index=0, name="Skip Me")
        keep = _input_entry(index=1, name="Keep Me")
        monkeypatch.setattr(device_enum, "enumerate_devices", lambda: [skip, keep])

        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        skip_guid = derive_endpoint_guid(skip, apo_reports=None, platform_key="win32")

        result = select_alternative_endpoint(
            quarantine=q,
            platform_key="win32",
            exclude_endpoint_guids=(skip_guid,),
        )
        assert result is not None
        assert result.name == "Keep Me"

    def test_prefers_os_default_among_candidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovyx.voice import device_enum

        non_default = _input_entry(index=0, name="Other Mic", is_default=False)
        default = _input_entry(index=1, name="Default Mic", is_default=True)
        # pick_preferred de-dups by canonical_name; give distinct names.
        monkeypatch.setattr(
            device_enum,
            "enumerate_devices",
            lambda: [non_default, default],
        )
        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        result = select_alternative_endpoint(quarantine=q, platform_key="win32")
        assert result is not None
        assert result.name == "Default Mic"

    def test_returns_none_when_all_skippable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovyx.voice import device_enum

        a = _input_entry(index=0, name="Mic A")
        b = _input_entry(index=1, name="Mic B")
        monkeypatch.setattr(device_enum, "enumerate_devices", lambda: [a, b])

        q = EndpointQuarantine(quarantine_s=300.0, maxsize=8)
        for dev in (a, b):
            q.add(
                endpoint_guid=derive_endpoint_guid(
                    dev,
                    apo_reports=None,
                    platform_key="win32",
                ),
            )

        assert select_alternative_endpoint(quarantine=q, platform_key="win32") is None

    def test_default_quarantine_used_when_not_supplied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sovyx.voice import device_enum
        from sovyx.voice.health import _quarantine

        good = _input_entry(index=0, name="Only Mic")
        monkeypatch.setattr(device_enum, "enumerate_devices", lambda: [good])

        _quarantine.reset_default_quarantine()
        try:
            result = select_alternative_endpoint(platform_key="win32")
            assert result is not None
            assert result.name == "Only Mic"
        finally:
            _quarantine.reset_default_quarantine()


# ---------------------------------------------------------------------------
# CascadeBootVerdict / classify_cascade_boot_result (v0.20.2 §4.4.7 / Bug D)
# ---------------------------------------------------------------------------


def _make_combo() -> Combo:
    return Combo(
        host_api="WASAPI",
        sample_rate=16_000,
        channels=1,
        sample_format="int16",
        exclusive=False,
        auto_convert=False,
        frames_per_buffer=512,
        platform_key="win32",
    )


def _make_cascade_result(
    *,
    source: str,
    has_winner: bool,
    attempts_count: int = 0,
    endpoint_guid: str = "endpoint-guid-1",
) -> CascadeResult:
    winning_combo = _make_combo() if has_winner else None
    return CascadeResult(
        endpoint_guid=endpoint_guid,
        winning_combo=winning_combo,
        winning_probe=None,
        attempts=(),
        attempts_count=attempts_count,
        budget_exhausted=False,
        source=source,
    )


class TestClassifyCascadeBootResult:
    """Decision matrix for :func:`classify_cascade_boot_result`.

    Each branch must map deterministically to a stable ``(verdict,
    reason)`` pair — the dashboard 503 handler + UI keys depend on it.
    """

    def test_none_result_is_degraded(self) -> None:
        outcome = classify_cascade_boot_result(None)
        assert outcome.verdict is CascadeBootVerdict.DEGRADED
        assert outcome.reason == "cascade_declined"
        assert outcome.attempts == 0
        assert outcome.result is None

    def test_winner_is_healthy(self) -> None:
        result = _make_cascade_result(source="cascade", has_winner=True, attempts_count=2)
        outcome = classify_cascade_boot_result(result)
        assert outcome.verdict is CascadeBootVerdict.HEALTHY
        assert outcome.reason == "winner"
        assert outcome.attempts == 2
        assert outcome.result is result

    def test_store_hit_is_healthy(self) -> None:
        result = _make_cascade_result(source="store", has_winner=True)
        outcome = classify_cascade_boot_result(result)
        assert outcome.verdict is CascadeBootVerdict.HEALTHY
        assert outcome.reason == "winner"

    def test_pinned_hit_is_healthy(self) -> None:
        result = _make_cascade_result(source="pinned", has_winner=True)
        outcome = classify_cascade_boot_result(result)
        assert outcome.verdict is CascadeBootVerdict.HEALTHY

    def test_quarantined_no_winner_is_inoperative(self) -> None:
        result = _make_cascade_result(source="quarantined", has_winner=False, attempts_count=0)
        outcome = classify_cascade_boot_result(result)
        assert outcome.verdict is CascadeBootVerdict.INOPERATIVE
        assert outcome.reason == "no_alternative_endpoint"
        assert outcome.result is result

    def test_exhausted_is_inoperative(self) -> None:
        result = _make_cascade_result(source="none", has_winner=False, attempts_count=7)
        outcome = classify_cascade_boot_result(result)
        assert outcome.verdict is CascadeBootVerdict.INOPERATIVE
        assert outcome.reason == "no_winner"
        assert outcome.attempts == 7

    def test_outcome_is_frozen(self) -> None:
        outcome = classify_cascade_boot_result(None)
        with pytest.raises(Exception) as exc_info:
            outcome.reason = "mutated"  # type: ignore[misc]
        # FrozenInstanceError is a subclass of AttributeError.
        assert type(exc_info.value).__name__ in {"FrozenInstanceError", "AttributeError"}

    def test_verdict_str_value_is_stable(self) -> None:
        # StrEnum values are the stable dashboard-facing payload.
        assert CascadeBootVerdict.HEALTHY.value == "healthy"
        assert CascadeBootVerdict.DEGRADED.value == "degraded"
        assert CascadeBootVerdict.INOPERATIVE.value == "inoperative"

    def test_outcome_result_reference_preserved(self) -> None:
        # Dashboard triage reads result.attempts / result.source for logs.
        result = _make_cascade_result(source="none", has_winner=False, attempts_count=4)
        outcome: CascadeBootOutcome = classify_cascade_boot_result(result)
        assert outcome.result is result
        assert outcome.result is not None
        assert outcome.result.source == "none"
