"""Boot-time secret-rotation hygiene check (§22.4).

When ``EngineConfig.security.secrets_rotated_at`` is older than
``rotation_warn_days`` (default 90), the daemon emits
``security.secrets.rotation_overdue`` so dashboards / SIEM pipelines
can flag the deployment to the operator.

Why a warning, not a hard failure:
    A daemon that refuses to start because a secret is 91 days old is
    operationally worse than one that runs and prompts the operator to
    rotate. Operators that want fail-closed behavior can wire an alert
    on the emitted event (it is a single envelope with a fixed name).

Why we don't compare hashes here:
    Hash-based rotation detection (§22.4 step 3, ``config.secret.rotated``
    with ``old_hash[:8]`` / ``new_hash[:8]``) requires the post-v0.21.0
    ``sovyx reload-config`` CLI. This module covers the v0.21.0 deliverable
    only — the boot-time *age* check. The hash-emit path lands once the
    reload command exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import SecurityConfig

logger = get_logger(__name__)

_EVENT_OVERDUE = "security.secrets.rotation_overdue"
_EVENT_OK = "security.secrets.rotation_ok"
_EVENT_UNKNOWN = "security.secrets.rotation_unknown"


def check_secret_rotation(security: SecurityConfig) -> None:
    """Emit one rotation-status envelope based on *security*.

    Three outcomes:
        * ``secrets_rotated_at`` is None — emit ``rotation_unknown`` at
          INFO. Fresh installs land here; no nag, just a single
          breadcrumb so the operator can see the check ran.
        * Age ≤ ``rotation_warn_days`` — emit ``rotation_ok`` at INFO
          with the computed age so the dashboard config view can
          render "rotated 14 days ago" without having to recompute.
        * Age > ``rotation_warn_days`` — emit ``rotation_overdue`` at
          WARNING with both the age and the threshold so SIEM rules
          can choose to escalate by margin (1 day vs. 1 year overdue).

    Naive datetimes are treated as UTC so YAML-supplied
    ``2026-04-20`` works without forcing operators to write timezone
    suffixes.
    """
    rotated_at = security.secrets_rotated_at
    if rotated_at is None:
        logger.info(
            _EVENT_UNKNOWN,
            **{
                "security.rotation.warn_days": security.rotation_warn_days,
            },
        )
        return

    if rotated_at.tzinfo is None:
        rotated_at = rotated_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    age_seconds = max(0, int((now - rotated_at).total_seconds()))
    age_days = age_seconds // 86400
    threshold_days = security.rotation_warn_days

    common_fields = {
        "security.rotation.last_rotated_at": rotated_at.isoformat(),
        "security.rotation.age_days": age_days,
        "security.rotation.warn_days": threshold_days,
    }
    if age_days > threshold_days:
        logger.warning(
            _EVENT_OVERDUE,
            **common_fields,
            **{"security.rotation.overdue_days": age_days - threshold_days},
        )
    else:
        logger.info(_EVENT_OK, **common_fields)


__all__ = [
    "check_secret_rotation",
]
