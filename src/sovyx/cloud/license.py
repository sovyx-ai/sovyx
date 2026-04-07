"""License service — JWT Ed25519 token issuance, validation, and refresh.

Provides offline-capable license validation using Ed25519-signed JWT tokens.
Tokens are validated locally with an embedded public key — no network needed.
A 7-day grace period after expiry allows degraded local-only operation.
Background refresh every 24h keeps tokens fresh.

References:
    - SPE-033 §3.3: LicenseService specification
    - IMPL-SUP-006: Tier definitions and pricing
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import jwt

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from uuid import UUID

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

logger = get_logger(__name__)

# ── Tier definitions (IMPL-SUP-006 / SPE-033 §3.3) ──────────────────────

TIER_FEATURES: dict[str, list[str]] = {
    "free": [],
    "starter": ["backup_daily", "relay"],
    "sync": ["backup_daily", "relay", "byok_routing", "byok_caching", "byok_analytics"],
    "cloud": ["backup_hourly", "relay", "llm_proxy"],
    "business": ["backup_hourly", "relay", "llm_proxy", "sso", "team"],
    "enterprise": [
        "backup_hourly",
        "relay",
        "llm_proxy",
        "sso",
        "team",
        "ldap",
        "dedicated_relay",
        "sla",
    ],
}

TIER_MIND_LIMITS: dict[str, int] = {
    "free": 2,
    "starter": 2,
    "sync": 5,
    "cloud": 10,
    "business": 25,
    "enterprise": 999,
}

VALID_TIERS = frozenset(TIER_FEATURES.keys())

# ── Constants ─────────────────────────────────────────────────────────────

TOKEN_VALIDITY_DAYS = 7
REFRESH_BEFORE_DAYS = 5
GRACE_PERIOD_DAYS = 7
REFRESH_INTERVAL_SECONDS = 86400  # 24 hours
JWT_ALGORITHM = "EdDSA"

# ── Grace period features (local-only, no cloud) ─────────────────────────

GRACE_FEATURES: list[str] = []


class LicenseStatus(enum.Enum):
    """License validation status."""

    VALID = "valid"
    GRACE = "grace"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class LicenseClaims:
    """Decoded JWT license claims."""

    sub: str
    tier: str
    features: list[str]
    minds_max: int
    iat: int
    exp: int
    refresh_before: int

    @property
    def account_id(self) -> str:
        """Return the account UUID string."""
        return self.sub

    @property
    def is_refresh_due(self) -> bool:
        """Check if the token should be refreshed (past refresh_before)."""
        return int(time.time()) >= self.refresh_before

    @property
    def seconds_until_expiry(self) -> int:
        """Seconds remaining until token expires (can be negative)."""
        return self.exp - int(time.time())


@dataclass(frozen=True, slots=True)
class LicenseInfo:
    """Result of license validation with status and claims."""

    status: LicenseStatus
    claims: LicenseClaims | None = None
    tier: str = "free"
    features: list[str] = field(default_factory=list)
    minds_max: int = 2
    grace_days_remaining: int = 0

    @property
    def is_valid(self) -> bool:
        """Whether the license allows operation (valid or grace)."""
        return self.status in {LicenseStatus.VALID, LicenseStatus.GRACE}


# Type alias for the refresh callback
RefreshCallback = Callable[[str], Coroutine[Any, Any, str | None]]


class LicenseService:
    """Issue and validate JWT license tokens (Ed25519 signed).

    Server-side usage (has private key)::

        service = LicenseService(private_key=key)
        token = await service.issue_license(user_id, "cloud")

    Client-side usage (public key only)::

        service = LicenseService(public_key=key)
        info = service.validate(token)
        if info.is_valid:
            print(f"Tier: {info.tier}")

    The public key is extracted from the private key when only a private
    key is provided. For daemon-side validation, only the public key is
    needed.
    """

    def __init__(
        self,
        *,
        private_key: Ed25519PrivateKey | None = None,
        public_key: Ed25519PublicKey | None = None,
    ) -> None:
        if private_key is None and public_key is None:
            msg = "At least one of private_key or public_key must be provided"
            raise ValueError(msg)

        self._private_key = private_key
        self._public_key = public_key or (private_key.public_key() if private_key else None)
        self._current_token: str | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_callback: RefreshCallback | None = None

    @property
    def public_key(self) -> Ed25519PublicKey | None:
        """Return the public key used for validation."""
        return self._public_key

    @property
    def current_token(self) -> str | None:
        """Return the currently cached token."""
        return self._current_token

    async def issue_license(self, user_id: UUID, tier: str) -> str:
        """Issue a JWT license token.

        Args:
            user_id: Account UUID.
            tier: Subscription tier (free/starter/sync/cloud/business/enterprise).

        Returns:
            Signed JWT string.

        Raises:
            ValueError: If tier is invalid or no private key is available.
        """
        if self._private_key is None:
            msg = "Cannot issue licenses without a private key"
            raise ValueError(msg)

        if tier not in VALID_TIERS:
            msg = f"Invalid tier: {tier!r}. Must be one of {sorted(VALID_TIERS)}"
            raise ValueError(msg)

        now = int(time.time())
        claims = {
            "sub": str(user_id),
            "tier": tier,
            "features": TIER_FEATURES[tier],
            "minds_max": TIER_MIND_LIMITS[tier],
            "iat": now,
            "exp": now + TOKEN_VALIDITY_DAYS * 86400,
            "refresh_before": now + REFRESH_BEFORE_DAYS * 86400,
        }

        token: str = jwt.encode(claims, self._private_key, algorithm=JWT_ALGORITHM)
        logger.info(
            "License issued",
            user_id=str(user_id),
            tier=tier,
            expires_in_days=TOKEN_VALIDITY_DAYS,
        )
        return token

    def validate(self, token: str) -> LicenseInfo:
        """Validate a JWT license token.

        Handles three cases:
        1. Valid token → full features for the tier
        2. Expired within grace period → degraded (local-only) features
        3. Expired beyond grace / invalid → expired/invalid

        Args:
            token: JWT string to validate.

        Returns:
            LicenseInfo with status, claims, and effective features.
        """
        if self._public_key is None:
            msg = "Cannot validate without a public key"
            raise ValueError(msg)

        try:
            decoded = jwt.decode(
                token,
                self._public_key,
                algorithms=[JWT_ALGORITHM],
                options={"require": ["sub", "tier", "features", "minds_max", "iat", "exp"]},
            )
            claims = LicenseClaims(
                sub=decoded["sub"],
                tier=decoded["tier"],
                features=decoded["features"],
                minds_max=decoded["minds_max"],
                iat=decoded["iat"],
                exp=decoded["exp"],
                refresh_before=decoded.get(
                    "refresh_before",
                    decoded["exp"] - (TOKEN_VALIDITY_DAYS - REFRESH_BEFORE_DAYS) * 86400,
                ),
            )
            return LicenseInfo(
                status=LicenseStatus.VALID,
                claims=claims,
                tier=claims.tier,
                features=claims.features,
                minds_max=claims.minds_max,
            )

        except jwt.ExpiredSignatureError:
            # Decode without verification to read claims for grace period check
            decoded = jwt.decode(
                token,
                self._public_key,
                algorithms=[JWT_ALGORITHM],
                options={"verify_exp": False},
            )
            claims = LicenseClaims(
                sub=decoded["sub"],
                tier=decoded["tier"],
                features=decoded["features"],
                minds_max=decoded["minds_max"],
                iat=decoded["iat"],
                exp=decoded["exp"],
                refresh_before=decoded.get(
                    "refresh_before",
                    decoded["exp"] - (TOKEN_VALIDITY_DAYS - REFRESH_BEFORE_DAYS) * 86400,
                ),
            )

            grace_end = claims.exp + GRACE_PERIOD_DAYS * 86400
            now = int(time.time())

            if now <= grace_end:
                days_remaining = max(0, (grace_end - now) // 86400)
                logger.warning(
                    "License in grace period",
                    account_id=claims.sub,
                    tier=claims.tier,
                    grace_days_remaining=days_remaining,
                )
                return LicenseInfo(
                    status=LicenseStatus.GRACE,
                    claims=claims,
                    tier=claims.tier,
                    features=GRACE_FEATURES,
                    minds_max=TIER_MIND_LIMITS["free"],
                    grace_days_remaining=days_remaining,
                )

            logger.warning("License expired beyond grace period", account_id=claims.sub)
            return LicenseInfo(status=LicenseStatus.EXPIRED, claims=claims)

        except jwt.InvalidTokenError:
            logger.warning("Invalid license token")
            return LicenseInfo(status=LicenseStatus.INVALID)

    def is_valid(self) -> bool:
        """Check if the currently cached token is valid.

        Returns:
            True if a token is cached and validates successfully.
        """
        if self._current_token is None:
            return False
        info = self.validate(self._current_token)
        return info.is_valid

    def set_token(self, token: str) -> LicenseInfo:
        """Set and validate a license token.

        Args:
            token: JWT string to cache and validate.

        Returns:
            LicenseInfo from validating the token.
        """
        info = self.validate(token)
        if info.status != LicenseStatus.INVALID:
            self._current_token = token
        return info

    async def start_refresh(self, callback: RefreshCallback) -> None:
        """Start the background 24h refresh loop.

        Args:
            callback: Async function that takes the current token and returns
                     a new token string, or None if refresh failed.
        """
        self._refresh_callback = callback
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info(
            "License refresh loop started",
            interval_hours=REFRESH_INTERVAL_SECONDS // 3600,
        )

    async def stop_refresh(self) -> None:
        """Stop the background refresh loop."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
            logger.info("License refresh loop stopped")

    async def _refresh_loop(self) -> None:
        """Background loop that refreshes the token every 24 hours."""
        while True:
            try:
                await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
                await self._do_refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("License refresh failed, will retry next cycle")

    async def _do_refresh(self) -> None:
        """Execute a single refresh attempt."""
        if self._refresh_callback is None or self._current_token is None:
            return

        try:
            new_token = await self._refresh_callback(self._current_token)
            if new_token is not None:
                info = self.validate(new_token)
                if info.status == LicenseStatus.VALID:
                    self._current_token = new_token
                    logger.info(
                        "License refreshed successfully",
                        tier=info.tier,
                        expires_in_days=TOKEN_VALIDITY_DAYS,
                    )
                else:
                    logger.warning("Refreshed token is not valid", status=info.status.value)
            else:
                logger.warning("Refresh callback returned None, keeping current token")
        except Exception:
            logger.exception("Error during license refresh")
