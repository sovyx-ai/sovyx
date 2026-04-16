"""Tests for sovyx.license — offline JWT license validation."""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sovyx.license import (
    GRACE_PERIOD_DAYS,
    JWT_ALGORITHM,
    TOKEN_VALIDITY_DAYS,
    LicenseClaims,
    LicenseInfo,
    LicenseStatus,
    LicenseValidator,
)


@pytest.fixture
def _keys() -> tuple[Ed25519PrivateKey, object]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return private, public


@pytest.fixture
def validator(_keys: tuple[Ed25519PrivateKey, object]) -> LicenseValidator:
    _, public = _keys
    return LicenseValidator(public_key=public)


def _issue_token(
    private_key: Ed25519PrivateKey,
    *,
    tier: str = "cloud",
    features: list[str] | None = None,
    minds_max: int = 10,
    exp_offset: int = TOKEN_VALIDITY_DAYS * 86400,
) -> str:
    now = int(time.time())
    claims = {
        "sub": "user-123",
        "tier": tier,
        "features": features or ["backup_hourly", "relay", "llm_proxy"],
        "minds_max": minds_max,
        "iat": now,
        "exp": now + exp_offset,
        "refresh_before": now + 2 * 86400,
    }
    return pyjwt.encode(claims, private_key, algorithm=JWT_ALGORITHM)


class TestLicenseValidator:
    """JWT license validation — happy path + error paths."""

    def test_valid_token(
        self, _keys: tuple[Ed25519PrivateKey, object], validator: LicenseValidator
    ) -> None:
        private, _ = _keys
        token = _issue_token(private)
        info = validator.validate(token)
        assert info.status == LicenseStatus.VALID
        assert info.tier == "cloud"
        assert info.minds_max == 10  # noqa: PLR2004
        assert info.is_valid is True

    def test_valid_token_preserves_claims(
        self, _keys: tuple[Ed25519PrivateKey, object], validator: LicenseValidator
    ) -> None:
        private, _ = _keys
        token = _issue_token(private, tier="business", minds_max=25)
        info = validator.validate(token)
        assert info.claims is not None
        assert info.claims.tier == "business"
        assert info.claims.minds_max == 25  # noqa: PLR2004
        assert info.claims.account_id == "user-123"

    def test_expired_within_grace(
        self, _keys: tuple[Ed25519PrivateKey, object], validator: LicenseValidator
    ) -> None:
        private, _ = _keys
        token = _issue_token(private, exp_offset=-3600)
        info = validator.validate(token)
        assert info.status == LicenseStatus.GRACE
        assert info.is_valid is True
        assert info.grace_days_remaining > 0

    def test_expired_beyond_grace(
        self, _keys: tuple[Ed25519PrivateKey, object], validator: LicenseValidator
    ) -> None:
        private, _ = _keys
        token = _issue_token(private, exp_offset=-(GRACE_PERIOD_DAYS + 1) * 86400)
        info = validator.validate(token)
        assert info.status == LicenseStatus.EXPIRED
        assert info.is_valid is False

    def test_invalid_signature(self, validator: LicenseValidator) -> None:
        wrong_key = Ed25519PrivateKey.generate()
        token = _issue_token(wrong_key)
        info = validator.validate(token)
        assert info.status == LicenseStatus.INVALID
        assert info.is_valid is False

    def test_malformed_token(self, validator: LicenseValidator) -> None:
        info = validator.validate("not.a.jwt")
        assert info.status == LicenseStatus.INVALID

    def test_no_public_key(self) -> None:
        validator = LicenseValidator(public_key=None)
        info = validator.validate("any.token.here")
        assert info.status == LicenseStatus.INVALID

    def test_free_tier_defaults(self, validator: LicenseValidator) -> None:
        info = LicenseInfo(status=LicenseStatus.INVALID)
        assert info.tier == "free"
        assert info.minds_max == 2  # noqa: PLR2004
        assert info.features == []


class TestLicenseClaims:
    """LicenseClaims dataclass properties."""

    def test_account_id(self) -> None:
        claims = LicenseClaims(
            sub="u-456",
            tier="sync",
            features=[],
            minds_max=2,
            iat=0,
            exp=0,
            refresh_before=0,
        )
        assert claims.account_id == "u-456"

    def test_seconds_until_expiry(self) -> None:
        now = int(time.time())
        claims = LicenseClaims(
            sub="u",
            tier="free",
            features=[],
            minds_max=2,
            iat=now,
            exp=now + 3600,
            refresh_before=now + 1800,
        )
        assert 3590 <= claims.seconds_until_expiry <= 3600  # noqa: PLR2004

    def test_is_refresh_due(self) -> None:
        now = int(time.time())
        claims = LicenseClaims(
            sub="u",
            tier="free",
            features=[],
            minds_max=2,
            iat=now - 86400,
            exp=now + 86400,
            refresh_before=now - 100,
        )
        assert claims.is_refresh_due is True


class TestLicenseStatus:
    """LicenseStatus enum values."""

    def test_values(self) -> None:
        assert LicenseStatus.VALID.value == "valid"
        assert LicenseStatus.GRACE.value == "grace"
        assert LicenseStatus.EXPIRED.value == "expired"
        assert LicenseStatus.INVALID.value == "invalid"
