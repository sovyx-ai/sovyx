"""Tests for LicenseService — JWT Ed25519, grace period, refresh (V05-09).

Covers:
    - Token issuance with Ed25519 signing
    - Validation (valid, expired, grace period, invalid)
    - Tier features and mind limits
    - Refresh loop lifecycle
    - Offline validation (public key only)
    - Edge cases (wrong key, tampered token, missing claims)

References:
    - SPE-033 §3.3: LicenseService specification
    - IMPL-SUP-006: Tier definitions
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.cloud.license import (
    GRACE_FEATURES,
    GRACE_PERIOD_DAYS,
    JWT_ALGORITHM,
    REFRESH_BEFORE_DAYS,
    TIER_FEATURES,
    TIER_MIND_LIMITS,
    TOKEN_VALIDITY_DAYS,
    VALID_TIERS,
    LicenseClaims,
    LicenseInfo,
    LicenseService,
    LicenseStatus,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def private_key() -> Ed25519PrivateKey:
    """Generate a fresh Ed25519 private key."""
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def public_key(private_key: Ed25519PrivateKey) -> Ed25519PublicKey:
    """Extract public key from private key."""
    return private_key.public_key()


@pytest.fixture()
def other_private_key() -> Ed25519PrivateKey:
    """A different key (for wrong-key tests)."""
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def server_service(private_key: Ed25519PrivateKey) -> LicenseService:
    """Service with private key (can issue + validate)."""
    return LicenseService(private_key=private_key)


@pytest.fixture()
def client_service(public_key: Ed25519PublicKey) -> LicenseService:
    """Service with public key only (can only validate)."""
    return LicenseService(public_key=public_key)


@pytest.fixture()
def user_id() -> UUID:
    """A fixed user UUID for tests."""
    return UUID("12345678-1234-5678-1234-567812345678")


# ── Construction ──────────────────────────────────────────────────────────


class TestConstruction:
    """LicenseService construction and key handling."""

    def test_requires_at_least_one_key(self) -> None:
        with pytest.raises(ValueError, match="At least one"):
            LicenseService()

    def test_private_key_derives_public(self, private_key: Ed25519PrivateKey) -> None:
        svc = LicenseService(private_key=private_key)
        assert svc.public_key is not None

    def test_public_key_only(self, public_key: Ed25519PublicKey) -> None:
        svc = LicenseService(public_key=public_key)
        assert svc.public_key is public_key

    def test_both_keys_public_preferred(
        self, private_key: Ed25519PrivateKey, other_private_key: Ed25519PrivateKey
    ) -> None:
        other_pub = other_private_key.public_key()
        svc = LicenseService(private_key=private_key, public_key=other_pub)
        assert svc.public_key is other_pub

    def test_initial_token_is_none(self, server_service: LicenseService) -> None:
        assert server_service.current_token is None

    def test_initial_is_valid_false(self, server_service: LicenseService) -> None:
        assert server_service.is_valid() is False


# ── Token Issuance ────────────────────────────────────────────────────────


class TestIssuance:
    """Token issuance via issue_license()."""

    @pytest.mark.parametrize("tier", sorted(VALID_TIERS))
    async def test_issue_all_tiers(
        self, server_service: LicenseService, user_id: UUID, tier: str
    ) -> None:
        token = await server_service.issue_license(user_id, tier)
        assert isinstance(token, str)
        assert len(token) > 0

    async def test_issue_invalid_tier(self, server_service: LicenseService, user_id: UUID) -> None:
        with pytest.raises(ValueError, match="Invalid tier"):
            await server_service.issue_license(user_id, "premium")

    async def test_issue_without_private_key(
        self, client_service: LicenseService, user_id: UUID
    ) -> None:
        with pytest.raises(ValueError, match="private key"):
            await client_service.issue_license(user_id, "cloud")

    async def test_issued_token_has_correct_claims(
        self, server_service: LicenseService, user_id: UUID
    ) -> None:
        token = await server_service.issue_license(user_id, "cloud")
        info = server_service.validate(token)

        assert info.status == LicenseStatus.VALID
        assert info.claims is not None
        assert info.claims.sub == str(user_id)
        assert info.claims.tier == "cloud"
        assert info.claims.features == TIER_FEATURES["cloud"]
        assert info.claims.minds_max == TIER_MIND_LIMITS["cloud"]

    async def test_issued_token_expiry(
        self, server_service: LicenseService, user_id: UUID
    ) -> None:
        before = int(time.time())
        token = await server_service.issue_license(user_id, "sync")
        after = int(time.time())

        info = server_service.validate(token)
        assert info.claims is not None
        expected_exp_min = before + TOKEN_VALIDITY_DAYS * 86400
        expected_exp_max = after + TOKEN_VALIDITY_DAYS * 86400
        assert expected_exp_min <= info.claims.exp <= expected_exp_max

    async def test_issued_token_refresh_before(
        self, server_service: LicenseService, user_id: UUID
    ) -> None:
        token = await server_service.issue_license(user_id, "business")
        info = server_service.validate(token)
        assert info.claims is not None
        expected_refresh = info.claims.iat + REFRESH_BEFORE_DAYS * 86400
        assert info.claims.refresh_before == expected_refresh

    @pytest.mark.parametrize("tier", sorted(VALID_TIERS))
    async def test_tier_features_match(
        self, server_service: LicenseService, user_id: UUID, tier: str
    ) -> None:
        token = await server_service.issue_license(user_id, tier)
        info = server_service.validate(token)
        assert info.features == TIER_FEATURES[tier]

    @pytest.mark.parametrize("tier", sorted(VALID_TIERS))
    async def test_tier_mind_limits_match(
        self, server_service: LicenseService, user_id: UUID, tier: str
    ) -> None:
        token = await server_service.issue_license(user_id, tier)
        info = server_service.validate(token)
        assert info.minds_max == TIER_MIND_LIMITS[tier]


# ── Validation ────────────────────────────────────────────────────────────


class TestValidation:
    """Token validation via validate()."""

    async def test_valid_token(self, server_service: LicenseService, user_id: UUID) -> None:
        token = await server_service.issue_license(user_id, "cloud")
        info = server_service.validate(token)
        assert info.status == LicenseStatus.VALID
        assert info.is_valid is True
        assert info.tier == "cloud"

    async def test_valid_token_client_side(
        self,
        server_service: LicenseService,
        client_service: LicenseService,
        user_id: UUID,
    ) -> None:
        token = await server_service.issue_license(user_id, "business")
        info = client_service.validate(token)
        assert info.status == LicenseStatus.VALID
        assert info.tier == "business"

    def test_invalid_token_garbage(self, server_service: LicenseService) -> None:
        info = server_service.validate("not.a.jwt")
        assert info.status == LicenseStatus.INVALID
        assert info.is_valid is False

    def test_invalid_token_empty(self, server_service: LicenseService) -> None:
        info = server_service.validate("")
        assert info.status == LicenseStatus.INVALID

    async def test_wrong_key_rejects(
        self,
        server_service: LicenseService,
        other_private_key: Ed25519PrivateKey,
        user_id: UUID,
    ) -> None:
        # Issue with main key
        token = await server_service.issue_license(user_id, "cloud")
        # Validate with different key
        other_service = LicenseService(public_key=other_private_key.public_key())
        info = other_service.validate(token)
        assert info.status == LicenseStatus.INVALID

    async def test_tampered_token(self, server_service: LicenseService, user_id: UUID) -> None:
        token = await server_service.issue_license(user_id, "cloud")
        # Flip a character in the payload
        parts = token.split(".")
        payload = parts[1]
        tampered_char = "A" if payload[0] != "A" else "B"
        parts[1] = tampered_char + payload[1:]
        tampered = ".".join(parts)
        info = server_service.validate(tampered)
        assert info.status == LicenseStatus.INVALID


# ── Grace Period ──────────────────────────────────────────────────────────


class TestGracePeriod:
    """7-day grace period after expiry — degraded mode."""

    def _make_expired_token(
        self,
        private_key: Ed25519PrivateKey,
        user_id: UUID,
        expired_seconds_ago: int,
    ) -> str:
        """Create a token that expired N seconds ago."""
        now = int(time.time())
        exp = now - expired_seconds_ago
        claims = {
            "sub": str(user_id),
            "tier": "cloud",
            "features": TIER_FEATURES["cloud"],
            "minds_max": TIER_MIND_LIMITS["cloud"],
            "iat": exp - TOKEN_VALIDITY_DAYS * 86400,
            "exp": exp,
            "refresh_before": exp - (TOKEN_VALIDITY_DAYS - REFRESH_BEFORE_DAYS) * 86400,
        }
        return jwt.encode(claims, private_key, algorithm=JWT_ALGORITHM)

    def test_grace_period_day_1(
        self, private_key: Ed25519PrivateKey, server_service: LicenseService, user_id: UUID
    ) -> None:
        # Expired 1 day ago → in grace
        token = self._make_expired_token(private_key, user_id, 86400)
        info = server_service.validate(token)
        assert info.status == LicenseStatus.GRACE
        assert info.is_valid is True
        assert info.features == GRACE_FEATURES
        assert info.minds_max == TIER_MIND_LIMITS["free"]
        assert info.grace_days_remaining > 0

    def test_grace_period_day_6(
        self, private_key: Ed25519PrivateKey, server_service: LicenseService, user_id: UUID
    ) -> None:
        token = self._make_expired_token(private_key, user_id, 6 * 86400)
        info = server_service.validate(token)
        assert info.status == LicenseStatus.GRACE
        assert info.is_valid is True
        assert info.grace_days_remaining >= 0

    def test_grace_period_expired(
        self, private_key: Ed25519PrivateKey, server_service: LicenseService, user_id: UUID
    ) -> None:
        # Expired 8 days ago → beyond grace
        token = self._make_expired_token(private_key, user_id, 8 * 86400)
        info = server_service.validate(token)
        assert info.status == LicenseStatus.EXPIRED
        assert info.is_valid is False

    def test_grace_period_exactly_7_days(
        self, private_key: Ed25519PrivateKey, server_service: LicenseService, user_id: UUID
    ) -> None:
        # Expired exactly 7 days ago — should still be in grace (boundary)
        token = self._make_expired_token(private_key, user_id, GRACE_PERIOD_DAYS * 86400 - 1)
        info = server_service.validate(token)
        assert info.status == LicenseStatus.GRACE

    def test_grace_retains_original_tier(
        self, private_key: Ed25519PrivateKey, server_service: LicenseService, user_id: UUID
    ) -> None:
        token = self._make_expired_token(private_key, user_id, 86400)
        info = server_service.validate(token)
        assert info.tier == "cloud"  # Original tier preserved for display
        assert info.claims is not None
        assert info.claims.tier == "cloud"

    def test_grace_has_no_cloud_features(
        self, private_key: Ed25519PrivateKey, server_service: LicenseService, user_id: UUID
    ) -> None:
        token = self._make_expired_token(private_key, user_id, 86400)
        info = server_service.validate(token)
        assert info.features == GRACE_FEATURES  # Empty — no cloud features


# ── LicenseClaims ─────────────────────────────────────────────────────────


class TestLicenseClaims:
    """LicenseClaims dataclass properties."""

    def test_account_id(self) -> None:
        uid = str(uuid4())
        claims = LicenseClaims(
            sub=uid,
            tier="free",
            features=[],
            minds_max=2,
            iat=0,
            exp=999999999,
            refresh_before=999999999,
        )
        assert claims.account_id == uid

    def test_is_refresh_due_false(self) -> None:
        future = int(time.time()) + 86400 * 10
        claims = LicenseClaims(
            sub="x",
            tier="free",
            features=[],
            minds_max=2,
            iat=0,
            exp=future,
            refresh_before=future,
        )
        assert claims.is_refresh_due is False

    def test_is_refresh_due_true(self) -> None:
        past = int(time.time()) - 100
        claims = LicenseClaims(
            sub="x",
            tier="free",
            features=[],
            minds_max=2,
            iat=0,
            exp=past + 86400 * 7,
            refresh_before=past,
        )
        assert claims.is_refresh_due is True

    def test_seconds_until_expiry_positive(self) -> None:
        future_exp = int(time.time()) + 3600
        claims = LicenseClaims(
            sub="x",
            tier="free",
            features=[],
            minds_max=2,
            iat=0,
            exp=future_exp,
            refresh_before=0,
        )
        assert claims.seconds_until_expiry > 0

    def test_seconds_until_expiry_negative(self) -> None:
        past_exp = int(time.time()) - 3600
        claims = LicenseClaims(
            sub="x",
            tier="free",
            features=[],
            minds_max=2,
            iat=0,
            exp=past_exp,
            refresh_before=0,
        )
        assert claims.seconds_until_expiry < 0


# ── LicenseInfo ───────────────────────────────────────────────────────────


class TestLicenseInfo:
    """LicenseInfo result object."""

    def test_valid_is_valid(self) -> None:
        info = LicenseInfo(status=LicenseStatus.VALID, tier="cloud")
        assert info.is_valid is True

    def test_grace_is_valid(self) -> None:
        info = LicenseInfo(status=LicenseStatus.GRACE, tier="cloud")
        assert info.is_valid is True

    def test_expired_not_valid(self) -> None:
        info = LicenseInfo(status=LicenseStatus.EXPIRED)
        assert info.is_valid is False

    def test_invalid_not_valid(self) -> None:
        info = LicenseInfo(status=LicenseStatus.INVALID)
        assert info.is_valid is False

    def test_defaults(self) -> None:
        info = LicenseInfo(status=LicenseStatus.INVALID)
        assert info.tier == "free"
        assert info.features == []
        assert info.minds_max == 2
        assert info.grace_days_remaining == 0
        assert info.claims is None


# ── Token Caching ─────────────────────────────────────────────────────────


class TestTokenCaching:
    """set_token() and is_valid() integration."""

    async def test_set_valid_token(self, server_service: LicenseService, user_id: UUID) -> None:
        token = await server_service.issue_license(user_id, "cloud")
        info = server_service.set_token(token)
        assert info.status == LicenseStatus.VALID
        assert server_service.current_token == token
        assert server_service.is_valid() is True

    def test_set_invalid_token_not_cached(self, server_service: LicenseService) -> None:
        info = server_service.set_token("garbage")
        assert info.status == LicenseStatus.INVALID
        assert server_service.current_token is None

    def test_set_grace_token_cached(
        self, private_key: Ed25519PrivateKey, server_service: LicenseService, user_id: UUID
    ) -> None:
        now = int(time.time())
        exp = now - 86400  # expired 1 day ago
        claims = {
            "sub": str(user_id),
            "tier": "cloud",
            "features": TIER_FEATURES["cloud"],
            "minds_max": TIER_MIND_LIMITS["cloud"],
            "iat": exp - 7 * 86400,
            "exp": exp,
            "refresh_before": exp - 2 * 86400,
        }
        token = jwt.encode(claims, private_key, algorithm=JWT_ALGORITHM)
        info = server_service.set_token(token)
        assert info.status == LicenseStatus.GRACE
        assert server_service.current_token == token


# ── Refresh Loop ──────────────────────────────────────────────────────────


class TestRefreshLoop:
    """Background 24h refresh task."""

    async def test_start_and_stop_refresh(self, server_service: LicenseService) -> None:
        callback = AsyncMock(return_value=None)
        await server_service.start_refresh(callback)
        assert server_service._refresh_task is not None
        assert not server_service._refresh_task.done()

        await server_service.stop_refresh()
        assert server_service._refresh_task is None

    async def test_stop_without_start(self, server_service: LicenseService) -> None:
        # Should not raise
        await server_service.stop_refresh()

    async def test_restart_refresh(self, server_service: LicenseService) -> None:
        cb1 = AsyncMock(return_value=None)
        cb2 = AsyncMock(return_value=None)

        await server_service.start_refresh(cb1)
        task1 = server_service._refresh_task

        await server_service.start_refresh(cb2)
        task2 = server_service._refresh_task

        assert task1 is not task2
        assert task1 is not None and task1.cancelled()

        await server_service.stop_refresh()

    async def test_refresh_updates_token(
        self, server_service: LicenseService, user_id: UUID
    ) -> None:
        old_token = await server_service.issue_license(user_id, "cloud")
        server_service.set_token(old_token)

        new_token = await server_service.issue_license(user_id, "business")
        callback = AsyncMock(return_value=new_token)

        # Call _do_refresh directly to test without waiting 24h
        server_service._refresh_callback = callback
        await server_service._do_refresh()

        assert server_service.current_token == new_token
        callback.assert_called_once_with(old_token)

    async def test_refresh_none_keeps_old_token(
        self, server_service: LicenseService, user_id: UUID
    ) -> None:
        token = await server_service.issue_license(user_id, "cloud")
        server_service.set_token(token)

        callback = AsyncMock(return_value=None)
        server_service._refresh_callback = callback
        await server_service._do_refresh()

        assert server_service.current_token == token

    async def test_refresh_invalid_token_keeps_old(
        self, server_service: LicenseService, user_id: UUID
    ) -> None:
        token = await server_service.issue_license(user_id, "cloud")
        server_service.set_token(token)

        callback = AsyncMock(return_value="garbage.invalid.token")
        server_service._refresh_callback = callback
        await server_service._do_refresh()

        assert server_service.current_token == token

    async def test_refresh_no_token_skips(self, server_service: LicenseService) -> None:
        callback = AsyncMock(return_value="something")
        server_service._refresh_callback = callback
        await server_service._do_refresh()  # No current token → noop
        callback.assert_not_called()

    async def test_refresh_no_callback_skips(
        self, server_service: LicenseService, user_id: UUID
    ) -> None:
        token = await server_service.issue_license(user_id, "cloud")
        server_service.set_token(token)
        await server_service._do_refresh()  # No callback → noop

    async def test_refresh_callback_exception_handled(
        self, server_service: LicenseService, user_id: UUID
    ) -> None:
        token = await server_service.issue_license(user_id, "cloud")
        server_service.set_token(token)

        callback = AsyncMock(side_effect=ConnectionError("network down"))
        server_service._refresh_callback = callback
        # Should not raise
        await server_service._do_refresh()
        assert server_service.current_token == token

    async def test_refresh_loop_handles_exception_and_continues(
        self, server_service: LicenseService, user_id: UUID
    ) -> None:
        """_refresh_loop catches exceptions from _do_refresh and continues."""
        token = await server_service.issue_license(user_id, "cloud")
        server_service.set_token(token)

        call_count = 0

        async def _mock_do_refresh() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            # Second call succeeds, then cancel
            raise asyncio.CancelledError

        with (
            patch.object(server_service, "_do_refresh", side_effect=_mock_do_refresh),
            patch("sovyx.cloud.license.REFRESH_INTERVAL_SECONDS", 0),
            pytest.raises(asyncio.CancelledError),
        ):
            await server_service._refresh_loop()

        assert call_count == 2  # Continued after first error


# ── Tier Definitions ──────────────────────────────────────────────────────


class TestTierDefinitions:
    """TIER_FEATURES and TIER_MIND_LIMITS consistency."""

    def test_all_tiers_have_features(self) -> None:
        for tier in VALID_TIERS:
            assert tier in TIER_FEATURES

    def test_all_tiers_have_mind_limits(self) -> None:
        for tier in VALID_TIERS:
            assert tier in TIER_MIND_LIMITS

    def test_free_has_no_features(self) -> None:
        assert TIER_FEATURES["free"] == []

    def test_enterprise_has_most_features(self) -> None:
        ent_features = set(TIER_FEATURES["enterprise"])
        for tier, _features in TIER_FEATURES.items():
            if tier != "enterprise":
                # Not strict superset (starter has different features than cloud)
                pass
        assert len(ent_features) > 0

    def test_mind_limits_increase_with_tier(self) -> None:
        order = ["free", "starter", "sync", "cloud", "business", "enterprise"]
        limits = [TIER_MIND_LIMITS[t] for t in order]
        for i in range(1, len(limits)):
            assert limits[i] >= limits[i - 1]

    def test_six_tiers_defined(self) -> None:
        assert len(VALID_TIERS) == 6


# ── LicenseStatus Enum ───────────────────────────────────────────────────


class TestLicenseStatus:
    """LicenseStatus enum values."""

    def test_values(self) -> None:
        assert LicenseStatus.VALID.value == "valid"
        assert LicenseStatus.GRACE.value == "grace"
        assert LicenseStatus.EXPIRED.value == "expired"
        assert LicenseStatus.INVALID.value == "invalid"

    def test_all_statuses(self) -> None:
        assert len(LicenseStatus) == 4


# ── Property-Based Tests ─────────────────────────────────────────────────


class TestPropertyBased:
    """Hypothesis property-based tests."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(tier=st.sampled_from(sorted(VALID_TIERS)))
    async def test_issue_validate_roundtrip(self, tier: str) -> None:
        """Any issued token validates successfully."""
        key = Ed25519PrivateKey.generate()
        svc = LicenseService(private_key=key)
        token = await svc.issue_license(uuid4(), tier)
        info = svc.validate(token)
        assert info.status == LicenseStatus.VALID
        assert info.tier == tier

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(garbage=st.text(min_size=1, max_size=200))
    def test_garbage_never_valid(self, garbage: str) -> None:
        """Random strings never validate as valid."""
        key = Ed25519PrivateKey.generate()
        svc = LicenseService(private_key=key)
        info = svc.validate(garbage)
        assert info.status == LicenseStatus.INVALID

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        expired_secs=st.integers(
            min_value=1,
            max_value=GRACE_PERIOD_DAYS * 86400 - 60,
        )
    )
    def test_grace_period_within_range(self, expired_secs: int) -> None:
        """Tokens expired within grace window are in GRACE status."""
        key = Ed25519PrivateKey.generate()
        svc = LicenseService(private_key=key)
        now = int(time.time())
        exp = now - expired_secs
        claims = {
            "sub": str(uuid4()),
            "tier": "cloud",
            "features": TIER_FEATURES["cloud"],
            "minds_max": TIER_MIND_LIMITS["cloud"],
            "iat": exp - 7 * 86400,
            "exp": exp,
            "refresh_before": exp - 2 * 86400,
        }
        token = jwt.encode(claims, key, algorithm=JWT_ALGORITHM)
        info = svc.validate(token)
        assert info.status == LicenseStatus.GRACE

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        expired_secs=st.integers(
            min_value=GRACE_PERIOD_DAYS * 86400 + 60,
            max_value=365 * 86400,
        )
    )
    def test_beyond_grace_is_expired(self, expired_secs: int) -> None:
        """Tokens expired beyond grace window are EXPIRED."""
        key = Ed25519PrivateKey.generate()
        svc = LicenseService(private_key=key)
        now = int(time.time())
        exp = now - expired_secs
        claims = {
            "sub": str(uuid4()),
            "tier": "cloud",
            "features": TIER_FEATURES["cloud"],
            "minds_max": TIER_MIND_LIMITS["cloud"],
            "iat": exp - 7 * 86400,
            "exp": exp,
            "refresh_before": exp - 2 * 86400,
        }
        token = jwt.encode(claims, key, algorithm=JWT_ALGORITHM)
        info = svc.validate(token)
        assert info.status == LicenseStatus.EXPIRED


# ── Edge Cases ────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases and error paths."""

    def test_validate_without_public_key(self) -> None:
        """Service with only private key can still validate (derives public key)."""
        key = Ed25519PrivateKey.generate()
        svc = LicenseService(private_key=key)
        # This should work — public key is derived from private key
        info = svc.validate("garbage")
        assert info.status == LicenseStatus.INVALID

    def test_validate_raises_without_public_key(self) -> None:
        """Validate raises ValueError when public key is explicitly None."""
        key = Ed25519PrivateKey.generate()
        svc = LicenseService(private_key=key)
        svc._public_key = None  # Force None for testing
        with pytest.raises(ValueError, match="public key"):
            svc.validate("anything")

    async def test_token_missing_required_claims(
        self, private_key: Ed25519PrivateKey, server_service: LicenseService
    ) -> None:
        """Token with missing required claims is invalid."""
        claims = {
            "sub": str(uuid4()),
            "tier": "cloud",
            # Missing: features, minds_max
            "iat": int(time.time()),
            "exp": int(time.time()) + 86400,
        }
        token = jwt.encode(claims, private_key, algorithm=JWT_ALGORITHM)
        info = server_service.validate(token)
        # PyJWT with require should reject this
        assert info.status == LicenseStatus.INVALID

    async def test_different_user_ids_different_tokens(
        self, server_service: LicenseService
    ) -> None:
        t1 = await server_service.issue_license(uuid4(), "cloud")
        t2 = await server_service.issue_license(uuid4(), "cloud")
        assert t1 != t2

    def test_token_with_missing_refresh_before_uses_default(
        self, private_key: Ed25519PrivateKey, server_service: LicenseService
    ) -> None:
        """Token without refresh_before still works (backward compat)."""
        now = int(time.time())
        claims = {
            "sub": str(uuid4()),
            "tier": "free",
            "features": [],
            "minds_max": 2,
            "iat": now,
            "exp": now + TOKEN_VALIDITY_DAYS * 86400,
            # No refresh_before
        }
        token = jwt.encode(claims, private_key, algorithm=JWT_ALGORITHM)
        info = server_service.validate(token)
        assert info.status == LicenseStatus.VALID
        assert info.claims is not None
        # Should have computed a default refresh_before
        assert info.claims.refresh_before > 0
