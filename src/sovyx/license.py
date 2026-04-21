"""License validator — offline JWT verification with embedded public key.

The daemon validates licenses locally using an Ed25519 public key.
Token issuance (private key signing) lives in ``sovyx-cloud``.

A 7-day grace period after expiry allows degraded local-only operation.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import jwt

from sovyx.observability.audit import get_audit_logger
from sovyx.observability.logging import get_logger
from sovyx.tiers import GRACE_FEATURES, TIER_MIND_LIMITS

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = get_logger(__name__)
audit_logger = get_audit_logger()


def _hash_subject(subject: str) -> str:
    """SHA-256 the account id so audit logs never carry raw UUIDs.

    Truncated to 16 hex chars (64 bits) — wide enough that operators
    can correlate validations for the same account across an audit
    file without exposing the account uuid that, paired with public
    metadata, could de-anonymise a user.
    """
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]

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
            audit_logger.warning(
                "audit.license.invalid",
                **{
                    "license.reason": "no_public_key",
                    "license.subject_hash": None,
                    "license.tier": None,
                    "license.expiry": None,
                },
            )
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
            audit_logger.info(
                "audit.license.validated",
                **{
                    "license.subject_hash": _hash_subject(claims.sub),
                    "license.tier": claims.tier,
                    "license.expiry": claims.exp,
                    "license.minds_max": claims.minds_max,
                    "license.feature_count": len(claims.features),
                },
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
            subject_hash = _hash_subject(claims.sub)
            if now < grace_end:
                days_remaining = max(0, (grace_end - now) // 86400)
                audit_logger.warning(
                    "audit.license.grace",
                    **{
                        "license.subject_hash": subject_hash,
                        "license.tier": claims.tier,
                        "license.expiry": claims.exp,
                        "license.grace_days_remaining": days_remaining,
                    },
                )
                return LicenseInfo(
                    status=LicenseStatus.GRACE,
                    claims=claims,
                    tier=claims.tier,
                    features=GRACE_FEATURES,
                    minds_max=TIER_MIND_LIMITS["free"],
                    grace_days_remaining=days_remaining,
                )

            audit_logger.warning(
                "audit.license.expired",
                **{
                    "license.subject_hash": subject_hash,
                    "license.tier": claims.tier,
                    "license.expiry": claims.exp,
                    "license.expired_for_seconds": now - grace_end,
                },
            )
            return LicenseInfo(status=LicenseStatus.EXPIRED, claims=claims)

        except jwt.InvalidTokenError as exc:
            audit_logger.warning(
                "audit.license.invalid",
                **{
                    "license.reason": type(exc).__name__,
                    "license.subject_hash": None,
                    "license.tier": None,
                    "license.expiry": None,
                },
            )
            return LicenseInfo(status=LicenseStatus.INVALID)
