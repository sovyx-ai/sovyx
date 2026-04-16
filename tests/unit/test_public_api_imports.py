"""Smoke tests for public API modules (tiers + license).

These modules are consumed by sovyx-cloud but have no in-repo
consumer. This file guarantees they import cleanly and expose
the expected surface.
"""

from __future__ import annotations


class TestTiersImport:
    """sovyx.tiers imports and exposes expected symbols."""

    def test_import(self) -> None:
        from sovyx.tiers import (
            GRACE_FEATURES,
            TIER_FEATURES,
            TIER_MIND_LIMITS,
            VALID_TIERS,
            ServiceTier,
        )

        assert ServiceTier is not None
        assert isinstance(TIER_FEATURES, dict)
        assert isinstance(TIER_MIND_LIMITS, dict)
        assert isinstance(VALID_TIERS, frozenset)
        assert isinstance(GRACE_FEATURES, list)

    def test_tier_count(self) -> None:
        from sovyx.tiers import ServiceTier

        assert len(ServiceTier) == 6  # noqa: PLR2004

    def test_features_and_limits_aligned(self) -> None:
        from sovyx.tiers import TIER_FEATURES, TIER_MIND_LIMITS

        assert set(TIER_FEATURES.keys()) == set(TIER_MIND_LIMITS.keys())


class TestLicenseImport:
    """sovyx.license imports and exposes expected symbols."""

    def test_import(self) -> None:
        from sovyx.license import (
            LicenseClaims,
            LicenseInfo,
            LicenseStatus,
            LicenseValidator,
        )

        assert LicenseValidator is not None
        assert LicenseStatus is not None
        assert LicenseClaims is not None
        assert LicenseInfo is not None

    def test_validator_without_key_returns_invalid(self) -> None:
        from sovyx.license import LicenseStatus, LicenseValidator

        validator = LicenseValidator(public_key=None)
        info = validator.validate("fake.token.here")
        assert info.status == LicenseStatus.INVALID

    def test_info_defaults(self) -> None:
        from sovyx.license import LicenseInfo, LicenseStatus

        info = LicenseInfo(status=LicenseStatus.INVALID)
        assert info.tier == "free"
        assert info.minds_max == 2  # noqa: PLR2004
        assert info.is_valid is False
