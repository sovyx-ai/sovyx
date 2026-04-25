"""Tests for :mod:`sovyx.voice._observability_pii`.

Covers M1's three reusable surfaces:

* ``hash_pii`` — deterministic + salt-isolated truncated SHA-256
* ``BoundedCardinalityBucket`` — top-N preservation + overflow to "other"
* ``mint_utterance_id`` — UUID4 distinctness invariant

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.10 M1.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice._observability_pii import (
    _DEFAULT_CARDINALITY_OTHER_LABEL,
    _DEFAULT_PII_HASH_LEN,
    BoundedCardinalityBucket,
    hash_pii,
    mint_utterance_id,
)

# ── hash_pii ────────────────────────────────────────────────────────


class TestHashPII:
    def test_empty_input_returns_empty(self) -> None:
        assert hash_pii("") == ""

    def test_default_length_is_12_hex(self) -> None:
        out = hash_pii("device-guid-A")
        assert len(out) == _DEFAULT_PII_HASH_LEN
        assert re.fullmatch(r"[0-9a-f]+", out), "output must be lowercase hex"

    def test_deterministic_within_release(self) -> None:
        a = hash_pii("device-guid-A")
        b = hash_pii("device-guid-A")
        assert a == b

    def test_distinct_values_produce_distinct_fingerprints(self) -> None:
        a = hash_pii("device-guid-A")
        b = hash_pii("device-guid-B")
        assert a != b

    def test_salt_changes_fingerprint(self) -> None:
        """Two namespaces with different salts produce different
        fingerprints for the same value — cross-namespace correlation
        attack mitigated."""
        bare = hash_pii("device-guid-A")
        salted = hash_pii("device-guid-A", salt="voice.endpoint")
        assert bare != salted

    def test_same_salt_produces_same_fingerprint(self) -> None:
        a = hash_pii("device-guid-A", salt="voice.endpoint")
        b = hash_pii("device-guid-A", salt="voice.endpoint")
        assert a == b

    def test_explicit_length_respected(self) -> None:
        out_8 = hash_pii("x", length=8)
        assert len(out_8) == 8
        out_64 = hash_pii("x", length=64)
        assert len(out_64) == 64

    def test_length_floor_enforced(self) -> None:
        with pytest.raises(ValueError, match=r"length must be in \[8, 64\]"):
            hash_pii("x", length=4)

    def test_length_ceiling_enforced(self) -> None:
        with pytest.raises(ValueError, match=r"length must be in \[8, 64\]"):
            hash_pii("x", length=128)

    def test_truncation_is_prefix_of_full_sha256(self) -> None:
        """Sanity — the truncated output is the hex prefix of the
        canonical salted SHA-256, not some other transform."""
        full = hashlib.sha256(b"::device-guid-A").hexdigest()
        assert hash_pii("device-guid-A") == full[:_DEFAULT_PII_HASH_LEN]

    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(value=st.text(min_size=1, max_size=200))
    def test_property_always_hex_at_default_length(self, value: str) -> None:
        out = hash_pii(value)
        assert len(out) == _DEFAULT_PII_HASH_LEN
        assert re.fullmatch(r"[0-9a-f]+", out)

    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        value=st.text(min_size=1, max_size=100),
        salt_a=st.text(max_size=50),
        salt_b=st.text(max_size=50),
    )
    def test_property_different_salts_likely_different_hash(
        self,
        value: str,
        salt_a: str,
        salt_b: str,
    ) -> None:
        """If salts differ, fingerprints almost-always differ
        (collision probability at 12 hex = 48 bits is negligible
        for a 50-example Hypothesis run)."""
        if salt_a == salt_b:
            return
        assert hash_pii(value, salt=salt_a) != hash_pii(value, salt=salt_b)


# ── BoundedCardinalityBucket ────────────────────────────────────────


class TestBoundedCardinalityBucket:
    def test_first_n_distinct_preserved(self) -> None:
        bucket = BoundedCardinalityBucket(maxsize=3)
        assert bucket.bucket("a") == "a"
        assert bucket.bucket("b") == "b"
        assert bucket.bucket("c") == "c"
        assert bucket.preserved_count == 3  # noqa: PLR2004

    def test_overflow_buckets_to_other(self) -> None:
        bucket = BoundedCardinalityBucket(maxsize=2)
        assert bucket.bucket("a") == "a"
        assert bucket.bucket("b") == "b"
        # Third distinct → overflow.
        assert bucket.bucket("c") == _DEFAULT_CARDINALITY_OTHER_LABEL
        assert bucket.bucket("d") == _DEFAULT_CARDINALITY_OTHER_LABEL

    def test_repeated_preserved_value_returns_verbatim(self) -> None:
        bucket = BoundedCardinalityBucket(maxsize=2)
        bucket.bucket("a")
        bucket.bucket("a")
        assert bucket.bucket("a") == "a"
        # Hits accounted for — top_n picks it up.
        top = bucket.top_n(n=10)
        assert ("a", 3) in top

    def test_other_count_increments(self) -> None:
        bucket = BoundedCardinalityBucket(maxsize=1)
        bucket.bucket("a")
        bucket.bucket("b")  # overflow
        bucket.bucket("c")  # overflow
        bucket.bucket("d")  # overflow
        assert bucket.other_count == 3  # noqa: PLR2004

    def test_empty_string_passes_through_without_consuming_slot(self) -> None:
        bucket = BoundedCardinalityBucket(maxsize=2)
        # Empty values are operationally common (missing field) — they
        # MUST not consume a preserved slot.
        for _ in range(100):
            assert bucket.bucket("") == ""
        assert bucket.preserved_count == 0
        # The bucket still has full capacity for real values.
        assert bucket.bucket("a") == "a"
        assert bucket.bucket("b") == "b"
        assert bucket.bucket("c") == _DEFAULT_CARDINALITY_OTHER_LABEL

    def test_custom_other_label(self) -> None:
        bucket = BoundedCardinalityBucket(maxsize=1, other_label="<overflow>")
        bucket.bucket("a")
        assert bucket.bucket("b") == "<overflow>"

    def test_is_full_property(self) -> None:
        bucket = BoundedCardinalityBucket(maxsize=2)
        assert bucket.is_full is False
        bucket.bucket("a")
        assert bucket.is_full is False
        bucket.bucket("b")
        assert bucket.is_full is True
        # Overflow does not change is_full state.
        bucket.bucket("c")
        assert bucket.is_full is True

    def test_top_n_respects_n_argument(self) -> None:
        bucket = BoundedCardinalityBucket(maxsize=10)
        for i in range(5):
            for _ in range(i + 1):
                bucket.bucket(f"v{i}")
        top3 = bucket.top_n(n=3)
        assert len(top3) == 3
        # Most-frequent first: v4 (5 hits), v3 (4), v2 (3).
        assert top3[0][0] == "v4"
        assert top3[0][1] == 5  # noqa: PLR2004

    def test_top_n_excludes_overflow_bucket(self) -> None:
        bucket = BoundedCardinalityBucket(maxsize=2)
        bucket.bucket("a")
        bucket.bucket("b")
        for _ in range(100):
            bucket.bucket("c")  # all overflow
        top = bucket.top_n(n=10)
        # other_label MUST NOT appear in top_n — it's reported via
        # other_count only.
        assert all(v != _DEFAULT_CARDINALITY_OTHER_LABEL for v, _ in top)
        assert bucket.other_count == 100  # noqa: PLR2004

    def test_maxsize_floor_enforced(self) -> None:
        with pytest.raises(ValueError, match=r"maxsize must be in \[1, 100000\]"):
            BoundedCardinalityBucket(maxsize=0)

    def test_maxsize_ceiling_enforced(self) -> None:
        with pytest.raises(ValueError, match=r"maxsize must be in \[1, 100000\]"):
            BoundedCardinalityBucket(maxsize=200_000)

    def test_empty_other_label_rejected(self) -> None:
        with pytest.raises(ValueError, match="other_label must be"):
            BoundedCardinalityBucket(maxsize=10, other_label="")

    def test_thread_safety_under_contention(self) -> None:
        """Concurrent threads must not corrupt the preserved set or
        the hit counter. Drive a stress workload + assert invariants."""
        bucket = BoundedCardinalityBucket(maxsize=50)

        def _worker() -> None:
            for i in range(100):
                bucket.bucket(f"value-{i % 60}")

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Invariants:
        # * preserved_count never exceeds maxsize.
        assert bucket.preserved_count <= 50  # noqa: PLR2004
        # * Total hits + overflow == total bucket() calls (8 threads
        #   * 100 iterations = 800).
        total_hits = sum(count for _, count in bucket.top_n(n=100))
        assert total_hits + bucket.other_count == 800  # noqa: PLR2004


# ── mint_utterance_id ───────────────────────────────────────────────


class TestMintUtteranceId:
    def test_returns_canonical_uuid4_string(self) -> None:
        out = mint_utterance_id()
        # Round-trip via UUID parser — proves it's a valid UUID4
        # string in canonical 36-char form.
        parsed = uuid.UUID(out)
        assert str(parsed) == out
        assert parsed.version == 4

    def test_collision_resistant_at_realistic_scale(self) -> None:
        """Generate 10 000 IDs — none should collide. Birthday-bound
        probability at UUID4 (122 bits) for 10 000 samples is
        ~9 × 10**-30, so a single collision in this test is a defect."""
        ids = {mint_utterance_id() for _ in range(10_000)}
        assert len(ids) == 10_000


# ── Module __all__ surface ──────────────────────────────────────────


class TestPublicSurface:
    def test_all_exports(self) -> None:
        from sovyx.voice import _observability_pii as mod

        assert set(mod.__all__) == {
            "BoundedCardinalityBucket",
            "hash_pii",
            "mint_utterance_id",
        }
