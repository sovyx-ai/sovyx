"""Service tier definitions — informational only.

These enums and feature maps describe the Sovyx tier structure. They
are used by the local daemon for display, configuration, and offline
license validation. Tier resolution (activating a tier after payment)
requires ``sovyx-cloud``.

Ref: gtm-strategy.md §5, IMPL-SUP-006.
"""

from __future__ import annotations

from enum import StrEnum


class ServiceTier(StrEnum):
    """Sovyx service tiers (informational — resolution requires sovyx-cloud)."""

    FREE = "free"
    STARTER = "starter"
    SYNC = "sync"
    CLOUD = "cloud"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


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

VALID_TIERS: frozenset[str] = frozenset(TIER_FEATURES.keys())

GRACE_FEATURES: list[str] = []
