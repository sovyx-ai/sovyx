"""Tests for the P5 schema-migration registry (v0.30.33).

The registry replaces the v0.30.15-32 ``if schema_version != CURRENT:
raise`` naive gate with a chain-of-pure-functions migration path. Each
registered ``(from_v, to_v) -> MigrationFunc`` edge is a pure function;
the chain walker (``migrate_to_current``) walks edges step-by-step
until the profile reaches the runtime's
:data:`CALIBRATION_PROFILE_SCHEMA_VERSION`.

Coverage:

* identity v1→v2 round-trip (the shipped placeholder)
* same-version is a no-op (returns input copy)
* missing migration raises typed error with from/to versions
* migration that doesn't bump schema_version → typed error
* migration that bumps to wrong version → typed error
* future-version profile (downgrade attempt) → typed error
* missing/non-int schema_version → typed error
* dry-run path returns migrated dict (validated separately in
  ``test_persistence.py``)
* registered migration that raises KeyError/TypeError/ValueError →
  typed error with cause chained
* multi-hop chain v1→v2→v3 walks both edges
"""

from __future__ import annotations

from typing import Any

import pytest

from sovyx.voice.calibration._migrations import (
    MIGRATIONS,
    CalibrationProfileMigrationError,
    migrate_to_current,
    register_migration,
)

# ════════════════════════════════════════════════════════════════════
# Identity placeholder + same-version no-op
# ════════════════════════════════════════════════════════════════════


class TestShippedV1ToV2Identity:
    def test_identity_bumps_schema_version_only(self) -> None:
        result = migrate_to_current({"schema_version": 1, "profile_id": "x"}, target_version=2)
        assert result["schema_version"] == 2
        assert result["profile_id"] == "x"

    def test_same_version_is_no_op(self) -> None:
        before = {"schema_version": 1, "foo": "bar"}
        result = migrate_to_current(before, target_version=1)
        assert result == before

    def test_walker_does_not_mutate_input(self) -> None:
        # Defensive: callers handing us a dict they intend to re-use
        # MUST NOT see schema_version flipped under their feet.
        original = {"schema_version": 1, "profile_id": "x"}
        original_copy = dict(original)
        migrate_to_current(original, target_version=2)
        assert original == original_copy


# ════════════════════════════════════════════════════════════════════
# Walker error paths
# ════════════════════════════════════════════════════════════════════


class TestWalkerErrors:
    def test_future_version_rejects_downgrade(self) -> None:
        with pytest.raises(CalibrationProfileMigrationError) as exc:
            migrate_to_current({"schema_version": 999}, target_version=1)
        assert exc.value.source_version == 999
        assert exc.value.target_version == 1
        assert "downgrade not supported" in str(exc.value)

    def test_missing_schema_version_raises(self) -> None:
        with pytest.raises(CalibrationProfileMigrationError) as exc:
            migrate_to_current({}, target_version=1)
        assert "missing or non-int" in str(exc.value)

    def test_non_int_schema_version_raises(self) -> None:
        with pytest.raises(CalibrationProfileMigrationError) as exc:
            migrate_to_current({"schema_version": "v1"}, target_version=1)
        assert "missing or non-int" in str(exc.value)

    def test_no_migration_registered(self) -> None:
        # Temporarily drop the (1, 2) edge so the walker has no path.
        # The fixture-style save+restore keeps test isolation clean.
        saved = MIGRATIONS.pop((1, 2))
        try:
            with pytest.raises(CalibrationProfileMigrationError) as exc:
                migrate_to_current({"schema_version": 1}, target_version=2)
            assert exc.value.source_version == 1
            assert exc.value.target_version == 2
            assert "no migration registered" in str(exc.value)
        finally:
            MIGRATIONS[(1, 2)] = saved

    def test_migration_that_doesnt_bump_version_raises(self) -> None:
        @register_migration(from_v=42, to_v=43)
        def _broken(raw: dict[str, Any]) -> dict[str, Any]:
            return raw  # forgot to bump

        try:
            with pytest.raises(CalibrationProfileMigrationError) as exc:
                migrate_to_current({"schema_version": 42}, target_version=43)
            assert "must bump schema_version" in str(exc.value)
            assert exc.value.step_failed == "_broken"
        finally:
            del MIGRATIONS[(42, 43)]

    def test_migration_that_bumps_to_wrong_version_raises(self) -> None:
        @register_migration(from_v=50, to_v=51)
        def _wrong_bump(raw: dict[str, Any]) -> dict[str, Any]:
            raw["schema_version"] = 999  # bumped to the wrong target
            return raw

        try:
            with pytest.raises(CalibrationProfileMigrationError) as exc:
                migrate_to_current({"schema_version": 50}, target_version=51)
            assert "must bump schema_version to 51" in str(exc.value)
            assert exc.value.step_failed == "_wrong_bump"
        finally:
            del MIGRATIONS[(50, 51)]

    def test_migration_raising_keyerror_chained(self) -> None:
        @register_migration(from_v=60, to_v=61)
        def _keyerror_step(raw: dict[str, Any]) -> dict[str, Any]:
            return raw["this_does_not_exist"]  # type: ignore[no-any-return]

        try:
            with pytest.raises(CalibrationProfileMigrationError) as exc:
                migrate_to_current({"schema_version": 60}, target_version=61)
            assert exc.value.step_failed == "_keyerror_step"
            assert "KeyError" in str(exc.value)
            assert isinstance(exc.value.cause, KeyError)
        finally:
            del MIGRATIONS[(60, 61)]

    def test_migration_raising_runtimeerror_chained(self) -> None:
        # QA-FIX-4 (v0.31.0-rc.2): pre-rc.2 walker caught only
        # (KeyError, TypeError, ValueError). RuntimeError /
        # AttributeError / OSError / AssertionError from a
        # migration propagated uncaught to the loader, defeating the
        # typed-error contract. Post-rc.2 the walker wraps any
        # non-typed exception uniformly.
        @register_migration(from_v=80, to_v=81)
        def _runtimeerror_step(raw: dict[str, Any]) -> dict[str, Any]:
            msg = "schema invariant violation"
            raise RuntimeError(msg)

        try:
            with pytest.raises(CalibrationProfileMigrationError) as exc:
                migrate_to_current({"schema_version": 80}, target_version=81)
            assert exc.value.step_failed == "_runtimeerror_step"
            assert "RuntimeError" in str(exc.value)
            assert isinstance(exc.value.cause, RuntimeError)
        finally:
            del MIGRATIONS[(80, 81)]

    def test_migration_raising_attributeerror_chained(self) -> None:
        @register_migration(from_v=90, to_v=91)
        def _attrerror_step(raw: dict[str, Any]) -> dict[str, Any]:
            return raw.this_does_not_exist  # type: ignore[attr-defined,no-any-return]

        try:
            with pytest.raises(CalibrationProfileMigrationError) as exc:
                migrate_to_current({"schema_version": 90}, target_version=91)
            assert exc.value.step_failed == "_attrerror_step"
            assert "AttributeError" in str(exc.value)
            assert isinstance(exc.value.cause, AttributeError)
        finally:
            del MIGRATIONS[(90, 91)]

    def test_migration_raising_typed_error_does_not_double_wrap(self) -> None:
        # When a migration explicitly raises the typed error itself
        # (e.g. it ran custom validation + decided the input was
        # un-migratable), the walker MUST propagate as-is rather
        # than wrapping in a second CalibrationProfileMigrationError
        # (which would lose the original step_failed/cause).
        @register_migration(from_v=110, to_v=111)
        def _typed_error_step(raw: dict[str, Any]) -> dict[str, Any]:
            raise CalibrationProfileMigrationError(
                source_version=110,
                target_version=111,
                step_failed="custom_validator",
                reason="custom validation failed",
            )

        try:
            with pytest.raises(CalibrationProfileMigrationError) as exc:
                migrate_to_current({"schema_version": 110}, target_version=111)
            # The step_failed comes from the inner raise, not the
            # walker's wrap.
            assert exc.value.step_failed == "custom_validator"
            assert "custom validation failed" in str(exc.value)
        finally:
            del MIGRATIONS[(110, 111)]

    def test_multi_hop_chain_walks_both_edges(self) -> None:
        @register_migration(from_v=70, to_v=71)
        def _step_70_71(raw: dict[str, Any]) -> dict[str, Any]:
            raw["schema_version"] = 71
            raw["added_by_70_71"] = True
            return raw

        @register_migration(from_v=71, to_v=72)
        def _step_71_72(raw: dict[str, Any]) -> dict[str, Any]:
            raw["schema_version"] = 72
            raw["added_by_71_72"] = True
            return raw

        try:
            result = migrate_to_current({"schema_version": 70}, target_version=72)
            assert result["schema_version"] == 72
            assert result["added_by_70_71"] is True
            assert result["added_by_71_72"] is True
        finally:
            del MIGRATIONS[(70, 71)]
            del MIGRATIONS[(71, 72)]


# ════════════════════════════════════════════════════════════════════
# Registry mechanics
# ════════════════════════════════════════════════════════════════════


class TestRegistry:
    def test_v1_to_v2_edge_registered_on_import(self) -> None:
        assert (1, 2) in MIGRATIONS
        assert callable(MIGRATIONS[(1, 2)])

    def test_register_migration_decorator_overwrites_same_edge(self) -> None:
        original = MIGRATIONS.get((100, 101))

        @register_migration(from_v=100, to_v=101)
        def _first(raw: dict[str, Any]) -> dict[str, Any]:
            raw["schema_version"] = 101
            raw["who"] = "first"
            return raw

        @register_migration(from_v=100, to_v=101)
        def _second(raw: dict[str, Any]) -> dict[str, Any]:
            raw["schema_version"] = 101
            raw["who"] = "second"
            return raw

        try:
            assert MIGRATIONS[(100, 101)] is _second
            result = migrate_to_current({"schema_version": 100}, target_version=101)
            assert result["who"] == "second"
        finally:
            if original is not None:
                MIGRATIONS[(100, 101)] = original
            else:
                MIGRATIONS.pop((100, 101), None)


# ════════════════════════════════════════════════════════════════════
# Persistence integration: load v1 file → v2 profile (when v2 ships)
# Today both equal v1 so this verifies the dry-run path; when v2
# actually bumps schema.py, this test will exercise the real chain.
# ════════════════════════════════════════════════════════════════════


class TestPersistenceIntegration:
    def test_inspect_migrated_profile_dict_returns_migrated(self, tmp_path: Path) -> None:
        from sovyx.voice.calibration._persistence import (
            inspect_migrated_profile_dict,
        )

        # Write a minimal v1-shaped JSON straight to disk; the inspect
        # function short-circuits before _profile_from_dict so we
        # don't need the full required-field set.
        target = tmp_path / "default" / "calibration.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"schema_version": 1, "profile_id": "synthetic"}', encoding="utf-8")
        result = inspect_migrated_profile_dict(data_dir=tmp_path, mind_id="default")
        assert isinstance(result, dict)
        assert result["schema_version"] == 1  # CURRENT == 1; identity walk no-op
        assert result["profile_id"] == "synthetic"


# Late-import to keep top-of-file imports consistent with the rest of
# the suite. Path is the only stdlib symbol the integration test uses.
from pathlib import Path  # noqa: E402
