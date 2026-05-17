"""Hypothesis property invariants for ``classify_error_code``.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§9.3.

Pure-function invariants the classifier MUST satisfy across every
plausible PortAudio numeric code + HRESULT mnemonic + opener final-
code mnemonic + free-text detail string:

1. **Total** — never raises; always returns a :class:`FailoverErrorClass`
   member.
2. **Deterministic** — calling the classifier twice with the same
   inputs returns the same verdict.
3. **Case-insensitive** — the classifier lowercases inputs before
   token lookup; ``"-9985"`` and ``"-9985"``-with-uppercase-noise
   yield the same verdict.
4. **Whitespace-stripped** — leading/trailing whitespace on the
   ``error_code`` argument MUST NOT change the verdict.
5. **Skip-predicate consistency** — ``is_skip_candidate_class`` returns
   True iff the verdict is in ``{UNOPENABLE_PERMANENT, UNOPENABLE_THIS_BOOT}``.

The property tests use ``@settings(max_examples=200, deadline=None)``
— deadline disabled per anti-pattern #22 (Windows monotonic
granularity makes a fixed deadline flaky for sub-ms pure functions).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.voice.health._failover_error_classifier import (
    FailoverErrorClass,
    classify_error_code,
    is_skip_candidate_class,
)

# Code strategy: numeric PortAudio negative codes + HRESULT mnemonics
# + opener final-codes + arbitrary text. Bounded sizes keep the
# Hypothesis fuzzing tractable.
_code_strategy = st.one_of(
    # Numeric PortAudio range (negative ints 9000-9999).
    st.integers(min_value=-9999, max_value=-9000).map(str),
    # HRESULT mnemonics.
    st.sampled_from(
        [
            "audclnt_e_device_in_use",
            "audclnt_e_unsupported_format",
            "AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED",
            "0x8889000a",
            "0x88890008",
            "paInvalidDevice",
            "paDeviceUnavailable",
            "paInvalidSampleRate",
            "paBadIODeviceCombination",
            "paUnanticipatedHostError",
        ],
    ),
    # Opener final-code mnemonics.
    st.sampled_from(
        [
            "device_not_found",
            "device_in_use",
            "device_unavailable",
            "device_disconnected",
            "device_busy",
            "permission_denied",
            "service_not_running",
            "driver_failure",
            "unsupported_format",
            "buffer_size_error",
            "exclusive_mode_denied",
        ],
    ),
    # Empty + whitespace-only.
    st.sampled_from(["", "   ", "\t\n", " \n "]),
    # Arbitrary text — exercises the UNKNOWN fallback path.
    st.text(min_size=0, max_size=64),
)

_detail_strategy = st.one_of(
    st.text(min_size=0, max_size=128),
    st.sampled_from(
        [
            "invalid device",
            "device unavailable",
            "device disconnected",
            "device is busy",
            "device in use",
            "device or resource busy",
            "invalid sample rate",
            "format not supported",
            "",
        ],
    ),
)


@given(code=_code_strategy, detail=_detail_strategy)
@settings(max_examples=200, deadline=None)
def test_classify_always_returns_failover_error_class_member(
    code: str,
    detail: str,
) -> None:
    """Total function — never raises; verdict is always a member."""
    verdict = classify_error_code(code, detail)
    assert isinstance(verdict, FailoverErrorClass)


@given(code=_code_strategy, detail=_detail_strategy)
@settings(max_examples=200, deadline=None)
def test_classify_is_deterministic(code: str, detail: str) -> None:
    """Same inputs → same verdict across multiple invocations."""
    assert classify_error_code(code, detail) == classify_error_code(code, detail)


@given(code=_code_strategy, detail=_detail_strategy)
@settings(max_examples=200, deadline=None)
def test_classify_is_case_insensitive(code: str, detail: str) -> None:
    """Upper-/lower-case noise on the inputs MUST yield the same verdict."""
    base = classify_error_code(code, detail)
    upper = classify_error_code(code.upper(), detail.upper())
    lower = classify_error_code(code.lower(), detail.lower())
    assert base == upper == lower


@given(code=_code_strategy, detail=_detail_strategy)
@settings(max_examples=200, deadline=None)
def test_classify_strips_whitespace_on_code(code: str, detail: str) -> None:
    """Leading/trailing whitespace on the ``error_code`` arg MUST NOT
    change the verdict.
    """
    padded = f"  {code}  "
    assert classify_error_code(code, detail) == classify_error_code(padded, detail)


@given(code=_code_strategy, detail=_detail_strategy)
@settings(max_examples=200, deadline=None)
def test_is_skip_predicate_aligned_with_unopenable_classes(
    code: str,
    detail: str,
) -> None:
    """``is_skip_candidate_class`` returns True iff the verdict is in
    ``{UNOPENABLE_PERMANENT, UNOPENABLE_THIS_BOOT}`` — encapsulates
    ADR-D4 in one predicate.
    """
    verdict = classify_error_code(code, detail)
    expected = verdict in (
        FailoverErrorClass.UNOPENABLE_PERMANENT,
        FailoverErrorClass.UNOPENABLE_THIS_BOOT,
    )
    assert is_skip_candidate_class(verdict) is expected
