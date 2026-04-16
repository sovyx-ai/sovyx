"""License validator — offline JWT verification with embedded public key.

The daemon validates licenses locally using an Ed25519 public key.
Token issuance (private key signing) lives in ``sovyx-cloud``.

A 7-day grace period after expiry allows degraded local-only operation.

Ref: SPE-033 §3.3: LicenseService specification.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import jwt

from sovyx.observability.logging import get_logger
from sovyx.tiers import GRACE_FEATURES, TIER_MIND_LIMITS

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = get_logger(__name__)

TOKEN_VALIDITY_DAYS = 7
GRACE_PERIOD_DAYS = 7
JWT_ALGORITHM = "EdDSA"


class LicenseStatus(StrEnum):
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
        """Check if the token should be refreshed."""
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


class LicenseValidator:
    """Validate JWT licenses offline using an Ed25519 public key.

    This is the client-side validator that ships with the open-source
    daemon. It never has access to the private key — only the public
    key embedded at build time or fetched from the cloud once.
    """

    def __init__(self, public_key: Ed25519PublicKey | None = None) -> None:
        self._public_key = public_key

    def validate(self, token: str) -> LicenseInfo:
        """Validate a JWT license token.

        Flow:
            1. Valid token → full features for the tier.
            2. Expired within grace period → degraded (local-only).
            3. Expired beyond grace / invalid → expired/invalid.
        """
        if self._public_key is None:
            return LicenseInfo(status=LicenseStatus.INVALID)

        try:
            decoded: dict[str, Any] = jwt.decode(
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
                    decoded["exp"] - 2 * 86400,
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
                    decoded["exp"] - 2 * 86400,
                ),
            )

            now = int(time.time())
            grace_end = claims.exp + GRACE_PERIOD_DAYS * 86400
            if now < grace_end:
                days_remaining = max(0, (grace_end - now) // 86400)
                logger.warning(
                    "License in grace period",
                    account_id=claims.sub,
                    days_remaining=days_remaining,
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
