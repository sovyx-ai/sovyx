"""Schema-migration registry for calibration profiles.

Replaces the v0.30.15-32 ``if schema_version != CURRENT: raise`` naive
gate with a chain-of-pure-functions migration registry. When the schema
bumps (v1 → v2 → v3 …), each step is a registered pure function in
:data:`MIGRATIONS`; the loader walks the chain edge-by-edge until the
profile reaches the running runtime's version.

Design (mission ``MISSION-voice-calibration-extreme-audit-2026-05-06.md`` §9):

* Explicit ``(from_version, to_version)`` edges (NOT implicit
  incrementing). Catches missing migrations as type-checkable gaps;
  if v3 ever ships skipping v2, the registry has no path v1→v3 →
  typed :class:`CalibrationProfileMigrationError`.
* Each migration MUST increment ``schema_version`` exactly once; the
  walker validates after each step and rejects forgetful migrations.
* Migrations are PURE FUNCTIONS over ``dict[str, Any]``; tests can
  exercise them in isolation without the rest of the persistence
  stack.
* Future-version profiles (running v1, profile claims v999) raise
  with a clear "downgrade not supported" message — operator
  regenerates via ``--calibrate``.

Adding a new migration when the schema bumps:

1. Bump ``CALIBRATION_PROFILE_SCHEMA_VERSION`` in ``schema.py``.
2. Create ``_migrations/v<N>_to_v<N+1>.py`` exporting a
   ``@register_migration(N, N+1)`` function.
3. Replace the identity (the v1→v2 placeholder lives in
   ``_migrations/v1_to_v2.py``) with the real reshape logic.
4. Add tests that fix the new shape's invariants.

History: introduced in v0.30.33 as P5.T1 of mission
``MISSION-voice-calibration-extreme-audit-2026-05-06.md`` §9.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sovyx.voice.calibration._persistence import CalibrationProfileLoadError

__all__ = [
    "MIGRATIONS",
    "CalibrationProfileMigrationError",
    "MigrationFunc",
    "migrate_to_current",
    "register_migration",
]


MigrationFunc = Callable[[dict[str, Any]], dict[str, Any]]
"""Type alias: pure-function shape every migration step satisfies.

The function MUST be pure (no IO, no time, no randomness) so the
chain walker can re-run it deterministically and the property test
can verify idempotency. The function MUST also bump
``raw["schema_version"]`` exactly once; the walker validates this
post-step and raises :class:`CalibrationProfileMigrationError` on
forgetful migrations.
"""


class CalibrationProfileMigrationError(CalibrationProfileLoadError):
    """Raised when the migration chain cannot complete cleanly.

    Subclasses :class:`CalibrationProfileLoadError` so existing
    operator surfaces (CLI exit codes, dashboard error rendering)
    treat migration failures and load failures uniformly.

    Attributes:
        source_version: The schema_version the profile carried at
            invocation time.
        target_version: The runtime's
            :data:`CALIBRATION_PROFILE_SCHEMA_VERSION` at invocation.
        step_failed: ``__name__`` of the migration function that
            raised, or ``None`` when the failure happened in the
            walker itself (e.g. no migration registered, downgrade
            attempt, version-not-bumped).
        reason: Free-form operator-facing explanation. Closed-enum-ish:
            ``"no migration registered for v{N}→v{N+1}"`` /
            ``"profile newer than runtime; downgrade not supported"``
            / ``"must bump schema_version to {N}, got {M}"``.
        cause: The original exception when ``step_failed`` is set;
            chained via ``raise ... from`` for full traceback context.
    """

    def __init__(
        self,
        *,
        source_version: int,
        target_version: int,
        step_failed: str | None = None,
        reason: str = "",
        cause: Exception | None = None,
    ) -> None:
        self.source_version = source_version
        self.target_version = target_version
        self.step_failed = step_failed
        self.reason = reason
        self.cause = cause
        msg = f"calibration profile migration v{source_version}→v{target_version} failed"
        if step_failed:
            msg += f" at step {step_failed!r}"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


# Registry: ``(from_version, to_version) -> migration function``.
# Populated by side-effect when the per-step modules in this package
# import (each calls :func:`register_migration`). The registry is
# module-private — callers go through :func:`migrate_to_current`.
MIGRATIONS: dict[tuple[int, int], MigrationFunc] = {}


def register_migration(
    from_v: int,
    to_v: int,
) -> Callable[[MigrationFunc], MigrationFunc]:
    """Decorator: register a migration function for the ``(from, to)`` edge.

    Example::

        @register_migration(from_v=1, to_v=2)
        def migrate_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
            raw["new_field"] = compute_default(raw)
            raw["schema_version"] = 2
            return raw

    Multiple registrations for the same edge overwrite — useful for
    test-time monkey-patching, but production code should not register
    the same edge twice.
    """

    def _decorator(fn: MigrationFunc) -> MigrationFunc:
        MIGRATIONS[(from_v, to_v)] = fn
        return fn

    return _decorator


def _import_per_step_modules() -> None:
    """Trigger registration of every shipped per-step migration.

    Idempotent: importing a module twice doesn't re-register because
    Python caches the import. The walker calls this on first invocation
    so callers don't have to remember the side-effect import order.
    """
    # Local import keeps the registry-foundation module light at top level.
    from sovyx.voice.calibration._migrations import v1_to_v2  # noqa: F401, PLC0415


def migrate_to_current(
    raw: dict[str, Any],
    *,
    target_version: int,
    path: Any = None,  # noqa: ANN401 -- pathlib.Path forward ref; kept Any to avoid circular import
) -> dict[str, Any]:
    """Walk the migration chain from ``raw["schema_version"]`` to ``target_version``.

    Returns a new dict with ``schema_version == target_version``. The
    walker mutates a copy so the input is not modified in-place
    (defensive against callers who hand off a dict they intend to
    re-use).

    Args:
        raw: The profile dict freshly parsed from JSON; MUST carry
            an integer ``schema_version`` field.
        target_version: The runtime's
            :data:`CALIBRATION_PROFILE_SCHEMA_VERSION`. Walker stops
            once the profile reaches this version.
        path: Optional :class:`pathlib.Path` of the source file; used
            only for forensic logging in the load path. The walker
            itself does no IO.

    Returns:
        A dict whose ``schema_version`` equals ``target_version``.

    Raises:
        CalibrationProfileMigrationError: when the input version is
            missing/non-int/greater-than-target, when no migration is
            registered for an intermediate edge, when a registered
            migration raises, or when a migration forgets to bump
            ``schema_version``.
    """
    _import_per_step_modules()
    raw_dict = dict(raw)  # Copy; walker mutates the copy, not the input.
    raw_schema = raw_dict.get("schema_version")
    if not isinstance(raw_schema, int):
        raise CalibrationProfileMigrationError(
            source_version=-1,
            target_version=target_version,
            reason=(
                f"profile schema_version is missing or non-int ({raw_schema!r}); cannot migrate"
            ),
        )
    current = raw_schema
    if current == target_version:
        return raw_dict
    if current > target_version:
        raise CalibrationProfileMigrationError(
            source_version=current,
            target_version=target_version,
            reason=(
                "profile newer than runtime; downgrade not supported. "
                "Upgrade Sovyx OR regenerate the profile via "
                "`sovyx doctor voice --calibrate`."
            ),
        )

    while current < target_version:
        next_v = current + 1
        fn = MIGRATIONS.get((current, next_v))
        if fn is None:
            raise CalibrationProfileMigrationError(
                source_version=raw_schema,
                target_version=target_version,
                reason=f"no migration registered for v{current}→v{next_v}",
            )
        try:
            raw_dict = fn(raw_dict)
        except (KeyError, TypeError, ValueError) as exc:
            raise CalibrationProfileMigrationError(
                source_version=raw_schema,
                target_version=target_version,
                step_failed=fn.__name__,
                reason=f"step raised {type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc

        # Validate: migration MUST bump schema_version to next_v.
        new_v = raw_dict.get("schema_version")
        if new_v != next_v:
            raise CalibrationProfileMigrationError(
                source_version=raw_schema,
                target_version=target_version,
                step_failed=fn.__name__,
                reason=(f"migration must bump schema_version to {next_v}, got {new_v!r}"),
            )
        current = next_v

    # path is reserved for future telemetry; reference it so static
    # analyzers don't flag it unused while we keep the parameter shape
    # stable for the load-path caller.
    _ = path
    return raw_dict
