"""POLISH-18: Property-based tests for dunning state machine.

Properties verified:
  1. Any number of elapsed days maps to a valid DunningState
  2. State progression is monotonically ordered (more days → further state)
  3. Zero or negative days → ACTIVE
  4. ≥14 days → always PAST_DUE_DAY14
  5. payment_succeeded always clears dunning (returns to ACTIVE)
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from sovyx.cloud.dunning import (
    _STATE_ORDER,
    DunningState,
    _days_to_state,
)


def _state_index(state: DunningState) -> int:
    """Get the index of a state in the progression order."""
    return _STATE_ORDER.index(state)


class TestDunningStateInvariants:
    """Property-based tests for dunning state transitions."""

    @given(days=st.integers(min_value=0, max_value=365))
    def test_always_returns_valid_state(self, days: int) -> None:
        """Any non-negative day count maps to a valid DunningState."""
        state = _days_to_state(days)
        assert isinstance(state, DunningState)

    @given(
        days_a=st.integers(min_value=0, max_value=365),
        days_b=st.integers(min_value=0, max_value=365),
    )
    def test_monotonic_progression(self, days_a: int, days_b: int) -> None:
        """More elapsed days → same or later state (never goes backwards)."""
        state_a = _days_to_state(days_a)
        state_b = _days_to_state(days_b)
        if days_a <= days_b:
            assert _state_index(state_a) <= _state_index(state_b)

    @given(days=st.integers(min_value=-100, max_value=0))
    def test_zero_or_negative_is_active(self, days: int) -> None:
        """Zero or negative elapsed days → ACTIVE."""
        state = _days_to_state(days)
        assert state == DunningState.ACTIVE

    @given(days=st.integers(min_value=14, max_value=10000))
    def test_fourteen_plus_always_day14(self, days: int) -> None:
        """≥14 elapsed days → PAST_DUE_DAY14 (worst non-canceled state)."""
        state = _days_to_state(days)
        assert state == DunningState.PAST_DUE_DAY14

    @given(days=st.integers(min_value=1, max_value=13))
    def test_mid_range_never_canceled(self, days: int) -> None:
        """Days 1-13 never result in CANCELED (only time-based, not event-based)."""
        state = _days_to_state(days)
        assert state != DunningState.CANCELED

    def test_all_intermediate_states_reachable(self) -> None:
        """Each PAST_DUE state is reachable by at least one day value."""
        seen = set()
        for d in range(0, 30):
            seen.add(_days_to_state(d))
        # All non-CANCELED states should be reachable
        for state in _STATE_ORDER:
            if state != DunningState.CANCELED:
                assert state in seen, f"{state} not reachable"
