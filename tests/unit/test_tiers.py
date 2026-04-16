"""Tests for sovyx.tiers — service tier definitions."""

from __future__ import annotations

from sovyx.tiers import (
    GRACE_FEATURES,
    TIER_FEATURES,
    TIER_MIND_LIMITS,
    VALID_TIERS,
    ServiceTier,
)


class TestServiceTier:
    """ServiceTier enum shape and values."""

    def test_all_tiers_present(self) -> None:
        assert len(ServiceTier) == 6  # noqa: PLR2004

    def test_values(self) -> None:
        assert ServiceTier.FREE.value == "free"
        assert ServiceTier.SYNC.value == "sync"
        assert ServiceTier.BYOK_PLUS.value == "byok_plus"
        assert ServiceTier.CLOUD.value == "cloud"
        assert ServiceTier.BUSINESS.value == "business"
        assert ServiceTier.ENTERPRISE.value == "enterprise"

    def test_from_string(self) -> None:
        assert ServiceTier("free") == ServiceTier.FREE
        assert ServiceTier("cloud") == ServiceTier.CLOUD

    def test_invalid_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="not a valid"):
            ServiceTier("nonexistent")


class TestTierFeatures:
    """TIER_FEATURES completeness and consistency."""

    def test_every_tier_has_features(self) -> None:
        for tier in ServiceTier:
            assert tier.value in TIER_FEATURES, f"Missing features for {tier.value}"

    def test_free_has_no_features(self) -> None:
        assert TIER_FEATURES["free"] == []

    def test_higher_tiers_have_more_features(self) -> None:
        assert len(TIER_FEATURES["cloud"]) > len(TIER_FEATURES["free"])
        assert len(TIER_FEATURES["enterprise"]) > len(TIER_FEATURES["cloud"])

    def test_enterprise_has_sla(self) -> None:
        assert "sla" in TIER_FEATURES["enterprise"]


class TestTierMindLimits:
    """TIER_MIND_LIMITS values."""

    def test_every_tier_has_limit(self) -> None:
        for tier in ServiceTier:
            assert tier.value in TIER_MIND_LIMITS, f"Missing limit for {tier.value}"

    def test_free_limit(self) -> None:
        assert TIER_MIND_LIMITS["free"] == 2  # noqa: PLR2004

    def test_limits_increase_with_tier(self) -> None:
        order = ["free", "sync", "byok_plus", "cloud", "business", "enterprise"]
        limits = [TIER_MIND_LIMITS[t] for t in order]
        for i in range(1, len(limits)):
            assert limits[i] >= limits[i - 1]

    def test_enterprise_high_limit(self) -> None:
        assert TIER_MIND_LIMITS["enterprise"] >= 999  # noqa: PLR2004


class TestValidTiers:
    """VALID_TIERS consistency with TIER_FEATURES."""

    def test_matches_features_keys(self) -> None:
        assert frozenset(TIER_FEATURES.keys()) == VALID_TIERS

    def test_is_frozenset(self) -> None:
        assert isinstance(VALID_TIERS, frozenset)


class TestGraceFeatures:
    """Grace period features."""

    def test_is_empty_list(self) -> None:
        assert GRACE_FEATURES == []
