"""Tests for sovyx.observability.privacy.short_hash.

Phase 0 (mission MISSION-voice-calibration-extreme-audit-2026-05-06.md §4.2 P0.T1)
makes ``short_hash`` the single source of truth for hashing operator-set
identifiers (``mind_id``, ``job_id``, ``profile_id``) before they reach
telemetry. The contract is small but load-bearing: determinism, fixed
length, and acceptable collision behaviour for our scale.
"""

from __future__ import annotations

import secrets
import string

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.observability.privacy import short_hash


class TestShortHash:
    """Contract: 16 hex chars, deterministic, low-collision at our scale."""

    def test_returns_16_hex_chars(self) -> None:
        result = short_hash("mind-jonny")
        assert len(result) == 16
        assert all(c in string.hexdigits for c in result)

    def test_deterministic_same_input_same_hash(self) -> None:
        a = short_hash("mind-jonny")
        b = short_hash("mind-jonny")
        assert a == b

    def test_different_inputs_different_hashes(self) -> None:
        a = short_hash("mind-alpha")
        b = short_hash("mind-beta")
        assert a != b

    def test_handles_unicode(self) -> None:
        result = short_hash("usuário-jonny")
        assert len(result) == 16
        # Roundtrip determinism
        assert result == short_hash("usuário-jonny")

    def test_handles_empty_string(self) -> None:
        result = short_hash("")
        assert len(result) == 16
        # SHA256("") = e3b0c44298fc1c14… — first 16 hex chars are stable
        assert result == "e3b0c44298fc1c14"

    def test_known_value_jonny(self) -> None:
        # Locked golden so a future refactor can't silently change the algorithm.
        assert short_hash("jonny") == "0fae56d5786cade8"

    def test_collision_smoke_10k_random(self) -> None:
        """10k random 32-byte strings → zero collisions at 64-bit prefix."""
        seen: set[str] = set()
        for _ in range(10_000):
            value = secrets.token_hex(16)
            digest = short_hash(value)
            assert digest not in seen, f"collision at {value}"
            seen.add(digest)
        assert len(seen) == 10_000

    @given(value=st.text(min_size=0, max_size=256))
    @settings(max_examples=100)
    def test_property_idempotent(self, value: str) -> None:
        assert short_hash(value) == short_hash(value)

    @given(value=st.text(min_size=1, max_size=256))
    @settings(max_examples=100)
    def test_property_length_invariant(self, value: str) -> None:
        result = short_hash(value)
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)
