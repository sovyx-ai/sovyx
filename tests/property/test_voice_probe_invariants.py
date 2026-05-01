"""Property-based tests for voice probe invariants (Phase 6 / T6.26).

Master mission §Phase 6 T6.26 demands property tests for:

* ``_compute_rms_db`` monotonicity / bounded-output / linearity.
* ``_classify_open_error`` exhaustiveness — total function returning
  a :class:`Diagnosis` for ANY ``BaseException`` input, with the
  unknown-keyword fallback returning ``DRIVER_ERROR``.

Ring-buffer atomicity property is already covered by
``tests/unit/voice/health/test_capture_integrity.py::TestProbeWindowInvariants``
(per ``MISSION-voice-mixer-enterprise-refactor`` D1.2 — that suite
uses Hypothesis already). Cascade-budget enforcement is covered by
``test_cascade.py::TestBudget`` (deterministic clock-injected
tests). Both are intentionally NOT duplicated here per
``feedback_no_speculation`` (don't ship parallel coverage that
drifts).

Each property runs with ``max_examples`` tuned so the suite stays
sub-second while still exploring meaningful corners. The shrinker
concentrates failure cases far more efficiently than random brute
force, so 100–200 examples per property catch the bug classes
example-based tests miss.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from sovyx.voice.health.contract import Diagnosis
from sovyx.voice.health.probe._classifier import _compute_rms_db
from sovyx.voice.health.probe._cold import (
    _DEVICE_BUSY_KEYWORDS,
    _FORMAT_MISMATCH_KEYWORDS,
    _KERNEL_INVALIDATED_KEYWORDS,
    _PERMISSION_KEYWORDS,
    _classify_open_error,
)

# ── _compute_rms_db ───────────────────────────────────────────────────


_INT16_SCALE = float(1 << 15)


def _const_block(amp: int, n: int) -> npt.NDArray[np.int16]:
    """Build an int16 block at constant amplitude ``amp``."""
    return np.full(n, amp, dtype=np.int16)


class TestComputeRmsDbInvariants:
    """Property-based contract for the dBFS RMS computation."""

    def test_empty_block_returns_minus_infinity(self) -> None:
        # Boundary — exhaustive single case (no need for hypothesis).
        result = _compute_rms_db(np.zeros(0, dtype=np.int16), _INT16_SCALE)
        assert result == float("-inf")

    @given(n=st.integers(min_value=1, max_value=1024))
    @settings(max_examples=50)
    def test_zero_block_returns_minus_infinity(self, n: int) -> None:
        block = np.zeros(n, dtype=np.int16)
        assert _compute_rms_db(block, _INT16_SCALE) == float("-inf")

    @given(
        amp=st.integers(min_value=1, max_value=32767),
        n=st.integers(min_value=1, max_value=1024),
    )
    @settings(max_examples=200)
    def test_constant_amplitude_yields_finite_dbfs(
        self,
        amp: int,
        n: int,
    ) -> None:
        # Any non-zero block must produce a FINITE dBFS reading
        # bounded above by 0 dB (full-scale int16 normalised by 2^15
        # gives RMS = 1.0 = 0 dB).
        block = _const_block(amp, n)
        result = _compute_rms_db(block, _INT16_SCALE)
        assert math.isfinite(result)
        assert result <= 0.0
        # Lower bound — RMS of a constant ≠ 0 is at least the int16
        # quantisation floor (1 LSB / 2^15 = ~ -90.3 dB).
        assert result >= -100.0

    @given(
        small=st.integers(min_value=1, max_value=100),
        n=st.integers(min_value=4, max_value=512),
    )
    @settings(max_examples=200)
    def test_doubling_amplitude_adds_six_db(
        self,
        small: int,
        n: int,
    ) -> None:
        # RMS in dB of constant amplitude k * a is exactly 20*log10(k)
        # dB louder than amplitude a. Doubling = +6.020 dB.
        block_small = _const_block(small, n)
        block_double = _const_block(small * 2, n)
        # Skip cases where the doubled amplitude would clip int16
        # (overflow → unrelated wrap-around behaviour, not a property
        # this function promises).
        assume(small * 2 <= 32767)
        rms_small = _compute_rms_db(block_small, _INT16_SCALE)
        rms_double = _compute_rms_db(block_double, _INT16_SCALE)
        # 6.0206 dB ± numerical noise.
        assert math.isclose(rms_double - rms_small, 6.0206, abs_tol=0.01)

    @given(
        amp_a=st.integers(min_value=1, max_value=16383),
        amp_b=st.integers(min_value=1, max_value=16383),
        n=st.integers(min_value=4, max_value=256),
    )
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_monotonicity_in_amplitude(
        self,
        amp_a: int,
        amp_b: int,
        n: int,
    ) -> None:
        # Strict monotonicity: larger constant amplitude → strictly
        # larger RMS dBFS. Equality only when amplitudes are equal.
        block_a = _const_block(amp_a, n)
        block_b = _const_block(amp_b, n)
        rms_a = _compute_rms_db(block_a, _INT16_SCALE)
        rms_b = _compute_rms_db(block_b, _INT16_SCALE)
        if amp_a < amp_b:
            assert rms_a < rms_b
        elif amp_a > amp_b:
            assert rms_a > rms_b
        else:
            assert math.isclose(rms_a, rms_b, abs_tol=1e-9)

    @given(
        scale=st.floats(
            min_value=1.0,
            max_value=1e9,
            allow_nan=False,
            allow_infinity=False,
        ),
        amp=st.integers(min_value=1, max_value=32767),
        n=st.integers(min_value=4, max_value=128),
    )
    @settings(max_examples=100)
    def test_doubling_scale_subtracts_six_db(
        self,
        scale: float,
        amp: int,
        n: int,
    ) -> None:
        # Doubling the scale divisor halves the normalised amplitude,
        # which subtracts 6.020 dB from the RMS. Mirror invariant.
        block = _const_block(amp, n)
        rms_a = _compute_rms_db(block, scale)
        rms_b = _compute_rms_db(block, scale * 2.0)
        assert math.isclose(rms_a - rms_b, 6.0206, abs_tol=0.01)

    @given(n=st.integers(min_value=2, max_value=512))
    @settings(max_examples=50)
    def test_full_scale_amplitude_is_zero_db(self, n: int) -> None:
        # int16 saturated to ±2^15 - 1 with normalised RMS ≈ 1.0
        # registers at ≈ 0 dBFS (the canonical full-scale reference).
        # Tolerance: 1 LSB error per sample → bounded sub-decibel.
        block = np.full(n, 32767, dtype=np.int16)
        rms = _compute_rms_db(block, _INT16_SCALE)
        # Full-scale int16 is 32767/32768 ≈ 0.99997 normalised →
        # 20*log10(0.99997) ≈ -0.000265 dB. Bounded tolerance.
        assert -0.01 <= rms <= 0.01

    # T6.34 — NaN/Inf chaos guard

    @pytest.mark.parametrize(
        ("desc", "values"),
        [
            ("single NaN", [float("nan")]),
            ("NaN mixed with finites", [float("nan"), 0.5, -0.3]),
            ("single +Inf", [float("inf")]),
            ("+Inf mixed", [float("inf"), 0.1, 0.2]),
            ("single -Inf", [float("-inf")]),
            ("-Inf mixed", [float("-inf"), 0.1]),
            ("NaN and Inf together", [float("nan"), float("inf"), 1.0]),
            ("all NaN", [float("nan"), float("nan"), float("nan")]),
            ("all +Inf", [float("inf"), float("inf")]),
        ],
    )
    def test_nan_inf_input_clamps_to_minus_inf(
        self,
        desc: str,
        values: list[float],
    ) -> None:
        # T6.34 — pathological float inputs (NaN / +Inf / -Inf) must
        # produce the canonical no-signal sentinel ``-inf``, NOT a
        # NaN or +Inf return value. Pre-T6.34 the function silently
        # propagated NaN/+Inf, breaking downstream diagnosis logic
        # (``rms < ceiling`` is False for NaN → misclassified as
        # HEALTHY). The ``math.isfinite(mean_sq)`` guard collapses
        # all non-finite cases to ``-inf``.
        block = np.array(values, dtype=np.float32)
        result = _compute_rms_db(block, 1.0)
        assert result == float("-inf"), f"{desc}: got {result}, expected -inf"

    @given(
        finite=st.floats(min_value=-10.0, max_value=10.0, allow_nan=False),
        n=st.integers(min_value=1, max_value=64),
    )
    @settings(max_examples=200)
    def test_no_nan_or_positive_inf_in_output(
        self,
        finite: float,
        n: int,
    ) -> None:
        # Property: for ANY finite input, the output is finite OR
        # exactly -inf (the no-signal sentinel). Never NaN, never +Inf.
        # Combined with the parametrized non-finite test above, this
        # gives us total coverage: the output is always either a real
        # number ≤ 0 dB or the -inf sentinel.
        block = np.full(n, finite, dtype=np.float32)
        result = _compute_rms_db(block, 1.0)
        assert not math.isnan(result)
        assert result != float("inf")
        # Either -inf (zero amplitude) or a finite dBFS value.
        assert result == float("-inf") or math.isfinite(result)

    @given(
        chaos_choices=st.lists(
            st.sampled_from(
                [
                    float("nan"),
                    float("inf"),
                    float("-inf"),
                    0.0,
                    0.5,
                    -0.5,
                    1.0,
                    -1.0,
                ],
            ),
            min_size=1,
            max_size=32,
        ),
    )
    @settings(max_examples=300)
    def test_arbitrary_chaos_input_never_returns_nan_or_positive_inf(
        self,
        chaos_choices: list[float],
    ) -> None:
        # Hypothesis-driven mix of finite + non-finite values in any
        # combination. The output is always either ``-inf`` (no signal)
        # OR a finite dBFS reading. Never NaN, never +Inf, never +Inf
        # disguised as a finite number.
        block = np.array(chaos_choices, dtype=np.float32)
        result = _compute_rms_db(block, 1.0)
        assert not math.isnan(result), (
            f"NaN leaked through for input={chaos_choices!r}, got {result}"
        )
        assert result != float("inf"), (
            f"+Inf leaked through for input={chaos_choices!r}, got {result}"
        )
        # Output is bounded: ≤ 0 dB (sub-unity normalised) OR -inf.
        assert result <= 0.0 or result == float("-inf")


# ── _classify_open_error ──────────────────────────────────────────────


class TestClassifyOpenErrorTotality:
    """Property-based contract for the exception → Diagnosis classifier."""

    @given(text=st.text(max_size=200))
    @settings(max_examples=300)
    def test_total_function_never_raises(self, text: str) -> None:
        # Property: the classifier is TOTAL — every BaseException
        # input produces a Diagnosis return value, no path raises.
        result = _classify_open_error(RuntimeError(text))
        assert isinstance(result, Diagnosis)

    @given(text=st.text(min_size=0, max_size=200))
    @settings(max_examples=300)
    def test_returned_diagnosis_is_in_known_set(self, text: str) -> None:
        # The classifier returns ONLY one of 7 documented values:
        # PERMISSION_DENIED / EXCLUSIVE_MODE_NOT_AVAILABLE (T6.3) /
        # INSUFFICIENT_BUFFER_SIZE (T6.4) / DEVICE_BUSY /
        # FORMAT_MISMATCH / KERNEL_INVALIDATED / DRIVER_ERROR.
        # No other diagnosis leaks out. Guards against future map drift.
        allowed = {
            Diagnosis.PERMISSION_DENIED,
            Diagnosis.EXCLUSIVE_MODE_NOT_AVAILABLE,
            Diagnosis.INSUFFICIENT_BUFFER_SIZE,
            Diagnosis.DEVICE_BUSY,
            Diagnosis.FORMAT_MISMATCH,
            Diagnosis.KERNEL_INVALIDATED,
            Diagnosis.DRIVER_ERROR,
        }
        result = _classify_open_error(RuntimeError(text))
        assert result in allowed

    @given(
        # Random alphanumeric text WITHOUT any keyword from the 4 sets.
        # We deliberately strip a-z to make matching impossible —
        # every keyword in the 4 sets contains lowercase letters.
        text=st.text(
            alphabet=st.characters(
                whitelist_categories=("Nd", "Pc", "Pd", "Pe", "Pi", "Po", "Ps"),
            ),
            min_size=0,
            max_size=200,
        ),
    )
    @settings(max_examples=200)
    def test_no_keyword_match_falls_back_to_driver_error(
        self,
        text: str,
    ) -> None:
        # The unknown-keyword fallback is the cascade's safety net —
        # a transient exception with no known signature still gets
        # routed to the retry-with-different-combo path. Property
        # guards against accidentally collapsing the default to
        # something more specific in a future refactor.
        # Defensive — explicitly verify no keyword leaked through
        # the alphabet restriction (sanity check on the strategy).
        from sovyx.voice.health.probe._cold import (
            _EXCLUSIVE_MODE_NOT_AVAILABLE_KEYWORDS,
            _INSUFFICIENT_BUFFER_SIZE_KEYWORDS,
        )

        msg_lower = text.lower()
        all_keywords = (
            *_PERMISSION_KEYWORDS,
            *_EXCLUSIVE_MODE_NOT_AVAILABLE_KEYWORDS,
            *_INSUFFICIENT_BUFFER_SIZE_KEYWORDS,
            *_DEVICE_BUSY_KEYWORDS,
            *_FORMAT_MISMATCH_KEYWORDS,
            *_KERNEL_INVALIDATED_KEYWORDS,
        )
        assume(not any(kw in msg_lower for kw in all_keywords))
        result = _classify_open_error(RuntimeError(text))
        assert result is Diagnosis.DRIVER_ERROR

    @pytest.mark.parametrize("keyword", _PERMISSION_KEYWORDS)
    def test_permission_keywords_route_to_permission_denied(
        self,
        keyword: str,
    ) -> None:
        # Each keyword in _PERMISSION_KEYWORDS, when the SOLE token
        # in the message, MUST route to PERMISSION_DENIED. Pin the
        # priority order — permission is checked first.
        result = _classify_open_error(RuntimeError(f"prefix {keyword} suffix"))
        assert result is Diagnosis.PERMISSION_DENIED

    @pytest.mark.parametrize("keyword", _DEVICE_BUSY_KEYWORDS)
    def test_device_busy_keywords_route_to_device_busy(
        self,
        keyword: str,
    ) -> None:
        # Property: DEVICE_BUSY keywords route correctly when no
        # PERMISSION or EXCLUSIVE_MODE_NOT_AVAILABLE keyword is also
        # present. We use a neutral prefix to avoid accidental
        # keyword collisions.
        from sovyx.voice.health.probe._cold import (
            _EXCLUSIVE_MODE_NOT_AVAILABLE_KEYWORDS,
        )

        msg = f"audio_engine: {keyword}"
        if any(kw in msg.lower() for kw in _PERMISSION_KEYWORDS):
            pytest.skip(
                f"keyword {keyword!r} overlaps with PERMISSION priority — "
                "covered by test_permission_keywords_route_to_permission_denied"
            )
        if any(kw in msg.lower() for kw in _EXCLUSIVE_MODE_NOT_AVAILABLE_KEYWORDS):
            pytest.skip(
                f"keyword {keyword!r} overlaps with EXCLUSIVE_MODE_NOT_AVAILABLE "
                "priority — covered by the dedicated T6.3 test"
            )
        result = _classify_open_error(RuntimeError(msg))
        assert result is Diagnosis.DEVICE_BUSY

    def test_audclnt_e_exclusive_mode_not_allowed_routes_to_exclusive_mode_not_available(
        self,
    ) -> None:
        # T6.3 — the standalone AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED
        # message (no permission companion) must route to the new
        # EXCLUSIVE_MODE_NOT_AVAILABLE diagnosis, NOT to DEVICE_BUSY
        # (which the bare "exclusive" keyword in _DEVICE_BUSY_KEYWORDS
        # would otherwise capture).
        result = _classify_open_error(
            RuntimeError("AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED"),
        )
        assert result is Diagnosis.EXCLUSIVE_MODE_NOT_AVAILABLE

    def test_exclusive_mode_hex_routes_to_exclusive_mode_not_available(self) -> None:
        # Hex form must route the same way.
        result = _classify_open_error(RuntimeError("PortAudioError 0x88890017"))
        assert result is Diagnosis.EXCLUSIVE_MODE_NOT_AVAILABLE

    def test_exclusive_mode_signed_decimal_routes_correctly(self) -> None:
        result = _classify_open_error(
            RuntimeError("paErrorCode -2004287465 details unavailable"),
        )
        assert result is Diagnosis.EXCLUSIVE_MODE_NOT_AVAILABLE

    def test_gp_blocked_exclusive_mode_routes_to_permission_denied(self) -> None:
        # T6.3 priority pin — when access-denied appears WITH the
        # exclusive-mode-not-allowed substring (the GP-blocked case),
        # PERMISSION_DENIED takes priority. This is the documented
        # Windows GP path: DisallowExclusiveDevice → E_ACCESSDENIED
        # surfaced alongside the AUDCLNT message.
        result = _classify_open_error(
            RuntimeError(
                "Access is denied (AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED via "
                "DisallowExclusiveDevice policy)",
            ),
        )
        assert result is Diagnosis.PERMISSION_DENIED

    def test_audclnt_e_device_in_use_still_routes_to_device_busy(self) -> None:
        # Regression guard — the DEVICE_BUSY path is still reachable.
        # The AUDCLNT_E_DEVICE_IN_USE token has no overlap with the
        # T6.3 keywords, so this must continue to route correctly.
        result = _classify_open_error(
            RuntimeError("AUDCLNT_E_DEVICE_IN_USE"),
        )
        assert result is Diagnosis.DEVICE_BUSY

    # T6.4 — INSUFFICIENT_BUFFER_SIZE classification

    def test_audclnt_buffer_size_not_aligned_routes_to_insufficient_buffer(
        self,
    ) -> None:
        # T6.4 — buffer-size-specific token routes to the new diagnosis,
        # NOT to FORMAT_MISMATCH (which the bare "format" keyword in
        # _FORMAT_MISMATCH_KEYWORDS could otherwise capture if the
        # message contained "format").
        result = _classify_open_error(
            RuntimeError("AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED"),
        )
        assert result is Diagnosis.INSUFFICIENT_BUFFER_SIZE

    def test_audclnt_buffer_too_large_routes_correctly(self) -> None:
        result = _classify_open_error(
            RuntimeError("Stream open failed: AUDCLNT_E_BUFFER_TOO_LARGE"),
        )
        assert result is Diagnosis.INSUFFICIENT_BUFFER_SIZE

    def test_buffer_size_hex_routes_correctly(self) -> None:
        # 0x88890019 (signed-decimal -2004287463) — AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED.
        result = _classify_open_error(RuntimeError("PortAudioError 0x88890019"))
        assert result is Diagnosis.INSUFFICIENT_BUFFER_SIZE

    def test_buffer_size_signed_decimal_routes_correctly(self) -> None:
        result = _classify_open_error(
            RuntimeError("paErrorCode -2004287463 details unavailable"),
        )
        assert result is Diagnosis.INSUFFICIENT_BUFFER_SIZE

    def test_buffer_size_takes_priority_over_format_mismatch(self) -> None:
        # T6.4 priority pin — when "buffer size" appears WITH a
        # FORMAT_MISMATCH keyword, the buffer-size check fires first.
        # Operators see the more-specific diagnosis instead of the
        # broader format-error catch-all.
        result = _classify_open_error(
            RuntimeError(
                "Invalid format: AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED in 48000 Hz",
            ),
        )
        assert result is Diagnosis.INSUFFICIENT_BUFFER_SIZE

    def test_format_mismatch_without_buffer_token_still_routes_correctly(
        self,
    ) -> None:
        # Regression guard — a FORMAT_MISMATCH error without any
        # buffer-size token continues to route to FORMAT_MISMATCH.
        result = _classify_open_error(
            RuntimeError("invalid sample rate 192000"),
        )
        assert result is Diagnosis.FORMAT_MISMATCH

    @pytest.mark.parametrize("keyword", _KERNEL_INVALIDATED_KEYWORDS)
    def test_kernel_invalidated_routes_when_format_keywords_absent(
        self,
        keyword: str,
    ) -> None:
        # KERNEL_INVALIDATED is checked AFTER FORMAT_MISMATCH
        # (documented in classifier docstring) — pin the priority
        # order. When no format keyword overlaps, kernel-invalidated
        # keywords route correctly.
        priority_kw_lower = [
            kw.lower()
            for kw in (
                *_PERMISSION_KEYWORDS,
                *_DEVICE_BUSY_KEYWORDS,
                *_FORMAT_MISMATCH_KEYWORDS,
            )
        ]
        msg = f"runtime: {keyword}"
        if any(kw in msg.lower() for kw in priority_kw_lower):
            pytest.skip(
                f"keyword {keyword!r} overlaps with higher-priority bucket — "
                "covered by the corresponding priority test"
            )
        result = _classify_open_error(RuntimeError(msg))
        assert result is Diagnosis.KERNEL_INVALIDATED

    def test_baseexception_subclasses_classify_too(self) -> None:
        # The signature accepts BaseException, not just Exception.
        # Pin: SystemExit / KeyboardInterrupt / asyncio.CancelledError
        # subclasses still go through the same classification path.
        # (In production this matters because PortAudio errors
        # sometimes wrap into BaseException subclasses on shutdown.)
        result = _classify_open_error(SystemExit("permission denied"))
        assert result is Diagnosis.PERMISSION_DENIED

    def test_empty_message_routes_to_driver_error(self) -> None:
        # Boundary — an exception with no string representation still
        # produces a Diagnosis (the str() fallback gives an empty or
        # default-named string, no keyword matches → DRIVER_ERROR).
        class _BareError(RuntimeError):  # noqa: N818 — test stub; suffix kept terse for narrative
            def __str__(self) -> str:
                return ""

        result = _classify_open_error(_BareError())
        assert result is Diagnosis.DRIVER_ERROR
