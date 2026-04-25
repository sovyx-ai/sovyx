"""Hypothesis property tests for the Linux mixer apply layer (F8).

Targets the pure helpers in :mod:`sovyx.voice.health._linux_mixer_apply`
that derive raw integer targets from preset values + live control
snapshots. These functions are the load-bearing arithmetic of the
band-aid #5 mitigation cascade — a wrong clamp or off-by-one in the
fraction → raw conversion silently sets the codec to a broken value
that's expensive to debug post-mortem.

Invariants exercised:

1. ``_clamp_raw`` always returns a value in ``[min_raw, max_raw]``.
2. ``_clamp_raw`` is idempotent (``clamp(clamp(x)) == clamp(x)``).
3. ``_clamp_raw`` is identity for in-range values.
4. ``_compute_target_raw`` output is always in ``[min_raw, max_raw]``,
   even for out-of-range fractions (``-1.0``, ``2.5``, etc.).
5. ``_compute_target_raw`` is monotonic in the active fraction
   (capture vs boost branch held constant).
6. ``_compute_target_raw`` at ``fraction=0.0`` returns ``min_raw``;
   at ``fraction=1.0`` returns ``max_raw``.
7. ``_is_capture_name`` is case-insensitive.
8. ``_translate_preset_value`` of a Raw value matches ``_clamp_raw``.
9. ``_translate_preset_value`` of a Fraction always lands in
   ``[min_raw, max_raw]``.
10. ``_translate_preset_value`` of ``Fraction(0.5)`` is approximately
    the midpoint (within 1 LSB rounding).

Reference: F1 inventory band-aid #5 (mixer reset semantics) +
F8 task in mission tracker.
"""

from __future__ import annotations

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from sovyx.voice.health._linux_mixer_apply import (
    _clamp_raw,
    _compute_target_raw,
    _is_capture_name,
    _translate_preset_value,
)
from sovyx.voice.health.contract import (
    MixerControlSnapshot,
    MixerPresetValueFraction,
    MixerPresetValueRaw,
)

# ── Hypothesis strategies ──────────────────────────────────────────


@st.composite
def control_snapshots(
    draw: st.DrawFn,
    *,
    name: str | None = None,
    is_boost_control: bool = True,
    capture: bool | None = None,
) -> MixerControlSnapshot:
    """Build a valid :class:`MixerControlSnapshot`.

    ``min_raw`` is drawn ``[0, 32]`` (real codecs use 0 floors with
    the rare negative-floor outlier; we exercise small positive
    floors as a representative span). ``max_raw`` strictly greater
    than ``min_raw`` so the span is always positive.
    """
    min_raw = draw(st.integers(min_value=0, max_value=32))
    span = draw(st.integers(min_value=1, max_value=255))
    max_raw = min_raw + span
    current_raw = draw(st.integers(min_value=min_raw, max_value=max_raw))
    if name is None:
        if capture is True:
            name = draw(st.sampled_from(["Capture", "Digital Capture Volume", "ADC Capture"]))
        elif capture is False:
            name = draw(
                st.sampled_from(["Internal Mic Boost", "Line Boost", "Mic Boost", "Headphone"]),
            )
        else:
            name = draw(
                st.sampled_from(
                    [
                        "Capture",
                        "Internal Mic Boost",
                        "Digital Capture Volume",
                        "Line Boost",
                        "Mic Boost",
                    ],
                ),
            )
    return MixerControlSnapshot(
        name=name,
        min_raw=min_raw,
        max_raw=max_raw,
        current_raw=current_raw,
        current_db=None,
        max_db=None,
        is_boost_control=is_boost_control,
        saturation_risk=False,
    )


# ── Property 1-3: _clamp_raw invariants ────────────────────────────


class TestClampRawProperties:
    """``_clamp_raw`` is the load-bearing bounds enforcer for every
    preset application. A bug here would silently push the codec
    out of its valid range — exactly the failure mode the spec is
    trying to prevent."""

    @given(target=st.integers(min_value=-10_000, max_value=10_000), control=control_snapshots())
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_output_always_within_bounds(
        self,
        target: int,
        control: MixerControlSnapshot,
    ) -> None:
        result = _clamp_raw(target, control)
        assert control.min_raw <= result <= control.max_raw

    @given(target=st.integers(min_value=-10_000, max_value=10_000), control=control_snapshots())
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_idempotent(
        self,
        target: int,
        control: MixerControlSnapshot,
    ) -> None:
        once = _clamp_raw(target, control)
        twice = _clamp_raw(once, control)
        assert once == twice

    @given(control=control_snapshots())
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_identity_for_in_range_values(self, control: MixerControlSnapshot) -> None:
        # Every value within [min, max] must round-trip unchanged.
        for value in (control.min_raw, control.current_raw, control.max_raw):
            assert _clamp_raw(value, control) == value


# ── Property 4-7: _compute_target_raw invariants ───────────────────


class TestComputeTargetRawProperties:
    """``_compute_target_raw`` is the band-aid #5 path — the function
    that derives a codec raw value from a fraction. A wrong clamp
    here sets the codec to a value the user didn't ask for, then
    the entire bypass cascade decides the device is broken."""

    @given(
        boost_fraction=st.floats(min_value=-2.0, max_value=2.0, allow_nan=False),
        capture_fraction=st.floats(min_value=-2.0, max_value=2.0, allow_nan=False),
        control=control_snapshots(),
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_output_always_within_bounds(
        self,
        boost_fraction: float,
        capture_fraction: float,
        control: MixerControlSnapshot,
    ) -> None:
        # Even for absurd fractions (1.5, -0.3) the clamp must hold.
        result = _compute_target_raw(
            control,
            boost_fraction=boost_fraction,
            capture_fraction=capture_fraction,
        )
        assert control.min_raw <= result <= control.max_raw

    @given(
        f1=st.floats(min_value=0.0, max_value=1.0),
        f2=st.floats(min_value=0.0, max_value=1.0),
        control=control_snapshots(capture=False),
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_monotonic_in_boost_fraction(
        self,
        f1: float,
        f2: float,
        control: MixerControlSnapshot,
    ) -> None:
        # Boost branch — capture_fraction inert.
        if f1 > f2:
            f1, f2 = f2, f1
        r1 = _compute_target_raw(control, boost_fraction=f1, capture_fraction=0.5)
        r2 = _compute_target_raw(control, boost_fraction=f2, capture_fraction=0.5)
        assert r1 <= r2

    @given(
        f1=st.floats(min_value=0.0, max_value=1.0),
        f2=st.floats(min_value=0.0, max_value=1.0),
        control=control_snapshots(capture=True),
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_monotonic_in_capture_fraction(
        self,
        f1: float,
        f2: float,
        control: MixerControlSnapshot,
    ) -> None:
        # Capture branch — boost_fraction inert.
        if f1 > f2:
            f1, f2 = f2, f1
        r1 = _compute_target_raw(control, boost_fraction=0.5, capture_fraction=f1)
        r2 = _compute_target_raw(control, boost_fraction=0.5, capture_fraction=f2)
        assert r1 <= r2

    @given(control=control_snapshots())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_fraction_zero_returns_min_raw(self, control: MixerControlSnapshot) -> None:
        # Whichever branch fires (capture or boost), fraction 0.0
        # collapses to min_raw via the linear interpolation.
        result = _compute_target_raw(control, boost_fraction=0.0, capture_fraction=0.0)
        assert result == control.min_raw

    @given(control=control_snapshots())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_fraction_one_returns_max_raw(self, control: MixerControlSnapshot) -> None:
        result = _compute_target_raw(control, boost_fraction=1.0, capture_fraction=1.0)
        assert result == control.max_raw


# ── Property 8: _is_capture_name case-insensitivity ────────────────


class TestIsCaptureNameProperties:
    @given(suffix=st.text(min_size=0, max_size=20))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_capture_substring_detected_regardless_of_case(self, suffix: str) -> None:
        # Build a name with "capture" in mixed case + arbitrary suffix.
        for token in ("capture", "Capture", "CAPTURE", "CaPtUrE"):
            assert _is_capture_name(f"{token} {suffix}") is True

    @given(name=st.text(alphabet="abdefghijklmnoprstuvwxyz", min_size=1, max_size=20))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_non_capture_names_not_flagged(self, name: str) -> None:
        # Alphabet excludes 'c' so "capture" can never appear.
        assume("capture" not in name.lower())
        assert _is_capture_name(name) is False


# ── Property 9-11: _translate_preset_value invariants ──────────────


class TestTranslatePresetValueProperties:
    """``_translate_preset_value`` is the public entry point from KB
    profile YAML to the codec. A bug here means the KB-spec'd value
    is silently misapplied — the entire mission-#5 cascade depends
    on this being exact."""

    @given(raw=st.integers(min_value=-10_000, max_value=10_000), control=control_snapshots())
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_raw_value_matches_clamp_raw(
        self,
        raw: int,
        control: MixerControlSnapshot,
    ) -> None:
        # MixerPresetValueRaw is a thin wrapper over _clamp_raw; the
        # property locks that contract so a future refactor can't
        # silently change semantics.
        translated = _translate_preset_value(MixerPresetValueRaw(raw=raw), control)
        clamped = _clamp_raw(raw, control)
        assert translated == clamped

    @given(
        fraction=st.floats(min_value=0.0, max_value=1.0),
        control=control_snapshots(),
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_fraction_value_within_bounds(
        self,
        fraction: float,
        control: MixerControlSnapshot,
    ) -> None:
        translated = _translate_preset_value(
            MixerPresetValueFraction(fraction=fraction),
            control,
        )
        assert control.min_raw <= translated <= control.max_raw

    @given(control=control_snapshots())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_fraction_half_is_midpoint(self, control: MixerControlSnapshot) -> None:
        # Within 1 LSB rounding the halfway fraction maps to the
        # control's midpoint. A wrong arithmetic ordering (e.g.
        # ``min + (max - min) // 2`` vs ``min + round((max-min)*0.5)``)
        # would surface as a 1+ LSB drift on odd-span controls.
        translated = _translate_preset_value(
            MixerPresetValueFraction(fraction=0.5),
            control,
        )
        expected = control.min_raw + round((control.max_raw - control.min_raw) * 0.5)
        assert abs(translated - expected) <= 1
