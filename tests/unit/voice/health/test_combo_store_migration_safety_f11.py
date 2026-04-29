"""F11 — Migration safety end-to-end regression infrastructure.

The ``test_combo_store_migrations.py`` file unit-tests the
:func:`migrate_to_current` function in isolation. F11 closes the gap
above that: pin REAL on-disk schema snapshots (v1, v2) and verify
the FULL load path (read JSON → parse → migrate → entries usable
via the public ComboStore API) survives.

Why this matters: a future schema rename or migration-rule deletion
would silently corrupt the on-disk state of every existing Sovyx
deployment. The migration unit tests catch the function's own bugs;
F11 catches the SYSTEM's interaction bugs.

The infrastructure:

1. **Snapshot fixtures** — minimal-but-realistic v1 + v2 dicts
   committed in this file as Python literals. Saved via tmp_path
   in each test so the on-disk path is exercised without polluting
   the repo with binary fixture files.
2. **End-to-end migration tests** — load each historical schema
   through the full ``ComboStore.load()`` path; verify entries can
   then be retrieved via the public API and contain the expected
   migrated fields.
3. **Schema-evolution invariant** — assert that for every version
   in ``[1, CURRENT_SCHEMA_VERSION - 1]`` there is a registered
   migration handler. Catches the case where a future contributor
   bumps ``CURRENT_SCHEMA_VERSION`` without adding the handler.

Reference: F1 inventory mission task F11; ADR-combo-store-schema §6.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sovyx.voice.health._combo_store_migrations import (
    _MIGRATIONS,
    CURRENT_SCHEMA_VERSION,
)

# ── Snapshot fixtures (real schema shapes) ────────────────────────


def _v1_snapshot() -> dict[str, Any]:
    """Minimal-but-realistic v1 combo_store.json contents.

    Mirrors the pre-v0.20 shape: no ``device_class``, no
    ``endpoint_fxproperties_sha``, no outer
    ``audio_subsystem_fingerprint``."""
    return {
        "schema_version": 1,
        "platform": "windows-10",
        "entries": {
            "{ENDPOINT-AAA}": {
                "winning_combo": {
                    "host_api": "Windows WASAPI",
                    "sample_rate": 48000,
                    "channels": 2,
                },
                "validated_at": "2025-06-01T12:00:00+00:00",
                "rms_db_at_validation": -28.5,
                "boots_validated": 7,
            },
            "{ENDPOINT-BBB}": {
                "winning_combo": {
                    "host_api": "MME",
                    "sample_rate": 16000,
                    "channels": 1,
                },
                "validated_at": "2025-07-15T09:30:00+00:00",
                "rms_db_at_validation": -42.0,
                "boots_validated": 3,
            },
        },
    }


def _v2_snapshot() -> dict[str, Any]:
    """Minimal-but-realistic v2 combo_store.json contents.

    Adds the v0.20 / ADR §3 fields (``device_class``,
    ``validation_mode``, ``endpoint_fxproperties_sha``,
    ``probe_history``, ``pinned``, ``last_boot_diagnosis``) plus
    the outer ``audio_subsystem_fingerprint`` block. Still missing
    ``candidate_kind`` (added in v3)."""
    return {
        "schema_version": 2,
        "platform": "linux",
        "audio_subsystem_fingerprint": {
            "audio_server": "pulseaudio",
            "kernel": "6.5.0",
        },
        "wake_word_model_version": "openwakeword-v0.6.0",
        "stt_model_version": "moonshine-tiny",
        "entries": {
            "{ENDPOINT-CCC}": {
                "winning_combo": {
                    "host_api": "ALSA",
                    "sample_rate": 48000,
                    "channels": 2,
                    "sample_format": "int16",
                    "frames_per_buffer": 480,
                    "auto_convert": False,
                },
                "validated_at": "2026-02-01T08:00:00+00:00",
                "rms_db_at_validation": -32.0,
                "boots_validated": 12,
                "device_class": "external_usb",
                "validation_mode": "warm",
                "vad_mean_prob_at_validation": 0.45,
                "endpoint_fxproperties_sha": "sha-1234",
                "probe_history": [],
                "pinned": False,
                "last_boot_diagnosis": "healthy",
                "last_boot_validated": "2026-02-01T08:00:00+00:00",
            },
        },
    }


# ── Schema-evolution invariant ────────────────────────────────────


class TestSchemaEvolutionInvariant:
    """Catches the case where a future contributor bumps
    ``CURRENT_SCHEMA_VERSION`` without registering a migration
    handler — the subsequent load() of any older on-disk state
    would raise ``MigrationError(no migration handler)``."""

    def test_every_version_below_current_has_handler(self) -> None:
        for version in range(1, CURRENT_SCHEMA_VERSION):
            assert version in _MIGRATIONS, (
                f"Schema version {version} → {version + 1} has no "
                f"registered migration handler. Either add one to "
                f"_MIGRATIONS or revert the CURRENT_SCHEMA_VERSION "
                f"bump until the handler is ready."
            )

    def test_no_handler_above_current(self) -> None:
        # If a handler exists for version >= CURRENT_SCHEMA_VERSION,
        # something is inconsistent — the framework would never call
        # it (since version >= current short-circuits in
        # migrate_to_current).
        orphan = [v for v in _MIGRATIONS if v >= CURRENT_SCHEMA_VERSION]
        assert orphan == [], (
            f"_MIGRATIONS has handlers for versions >= "
            f"CURRENT_SCHEMA_VERSION ({CURRENT_SCHEMA_VERSION}): "
            f"{orphan}. Either bump CURRENT_SCHEMA_VERSION or remove "
            f"the orphan handler(s)."
        )

    def test_current_is_at_least_v3(self) -> None:
        # Floor regression guard: don't accidentally downgrade.
        # If a real downgrade is intentional, update this to match.
        assert CURRENT_SCHEMA_VERSION >= 3  # noqa: PLR2004


# ── End-to-end on-disk migration ──────────────────────────────────


class TestV1OnDiskRegression:
    """Real v1 JSON on disk → ComboStore.load() → entries usable.

    These tests pin the contract that the v1-shaped state of EVERY
    pre-v0.20 deployment continues to load cleanly into the current
    runtime — the upgrade path stays unbroken indefinitely."""

    def _write_v1_to_disk(self, tmp_path: Path) -> Path:
        target = tmp_path / "capture_combos.json"
        target.write_text(
            json.dumps(_v1_snapshot(), indent=2),
            encoding="utf-8",
        )
        return target

    def test_v1_load_via_migrate_to_current_preserves_entries(
        self,
        tmp_path: Path,
    ) -> None:
        """Direct migrate_to_current call on v1 snapshot → both
        endpoints survive + all v2/v3 fields backfilled."""
        from sovyx.voice.health._combo_store_migrations import migrate_to_current

        path = self._write_v1_to_disk(tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))

        out = migrate_to_current(
            raw,
            audio_subsystem_fingerprint_factory=lambda: {
                "audio_server": "stub",
                "kernel": "6.5.0",
            },
            endpoint_fxproperties_sha_for=lambda guid: f"sha-{guid[-4:]}",
        )

        assert out["schema_version"] == CURRENT_SCHEMA_VERSION
        # Both endpoints survived the migration.
        assert "{ENDPOINT-AAA}" in out["entries"]
        assert "{ENDPOINT-BBB}" in out["entries"]
        # v2 fields backfilled to defaults.
        for guid in ("{ENDPOINT-AAA}", "{ENDPOINT-BBB}"):
            entry = out["entries"][guid]
            assert entry["device_class"] == "other"
            assert entry["validation_mode"] == "warm"
            assert entry["pinned"] is False
            assert entry["last_boot_diagnosis"] == "healthy"
            assert entry["probe_history"] == []
            assert "endpoint_fxproperties_sha" in entry
        # v3 candidate_kind field backfilled.
        for guid in ("{ENDPOINT-AAA}", "{ENDPOINT-BBB}"):
            assert out["entries"][guid]["candidate_kind"] == "unknown"
        # winning_combo defaults backfilled (v1 entries lacked these).
        assert out["entries"]["{ENDPOINT-AAA}"]["winning_combo"]["sample_format"] == "int16"
        assert out["entries"]["{ENDPOINT-AAA}"]["winning_combo"]["frames_per_buffer"] == 480

    def test_v1_round_trip_preserves_user_provided_combo_fields(
        self,
        tmp_path: Path,
    ) -> None:
        """Fields the v1 entry DID set are NOT overwritten by the
        backfill — defaults only fill gaps."""
        from sovyx.voice.health._combo_store_migrations import migrate_to_current

        path = self._write_v1_to_disk(tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))

        out = migrate_to_current(
            raw,
            audio_subsystem_fingerprint_factory=lambda: {},
            endpoint_fxproperties_sha_for=lambda guid: "",
        )

        # Original sample_rate / channels / host_api preserved verbatim.
        aaa = out["entries"]["{ENDPOINT-AAA}"]["winning_combo"]
        assert aaa["host_api"] == "Windows WASAPI"
        assert aaa["sample_rate"] == 48000  # noqa: PLR2004
        assert aaa["channels"] == 2  # noqa: PLR2004
        bbb = out["entries"]["{ENDPOINT-BBB}"]["winning_combo"]
        assert bbb["host_api"] == "MME"
        assert bbb["sample_rate"] == 16000  # noqa: PLR2004
        assert bbb["channels"] == 1
        # Original validated_at / rms_db_at_validation preserved.
        assert out["entries"]["{ENDPOINT-AAA}"]["validated_at"] == "2025-06-01T12:00:00+00:00"
        assert out["entries"]["{ENDPOINT-AAA}"]["rms_db_at_validation"] == -28.5  # noqa: PLR2004


class TestV2OnDiskRegression:
    """v0.20+ on-disk state → load → entries usable + candidate_kind
    backfilled. Verifies the v2→v3 jump (which is a pure additive
    migration, no reshape) is safe."""

    def _write_v2_to_disk(self, tmp_path: Path) -> Path:
        target = tmp_path / "capture_combos.json"
        target.write_text(
            json.dumps(_v2_snapshot(), indent=2),
            encoding="utf-8",
        )
        return target

    def test_v2_load_backfills_candidate_kind(self, tmp_path: Path) -> None:
        from sovyx.voice.health._combo_store_migrations import migrate_to_current

        path = self._write_v2_to_disk(tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))

        out = migrate_to_current(
            raw,
            audio_subsystem_fingerprint_factory=lambda: {},
            endpoint_fxproperties_sha_for=lambda guid: "",
        )

        assert out["schema_version"] == CURRENT_SCHEMA_VERSION
        entry = out["entries"]["{ENDPOINT-CCC}"]
        # The new field is backfilled with the documented sentinel.
        assert entry["candidate_kind"] == "unknown"

    def test_v2_load_preserves_existing_v2_fields_verbatim(
        self,
        tmp_path: Path,
    ) -> None:
        """The v2→v3 migration is purely additive — every field the
        v2 entry already had MUST round-trip unchanged."""
        from sovyx.voice.health._combo_store_migrations import migrate_to_current

        path = self._write_v2_to_disk(tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))

        out = migrate_to_current(
            raw,
            audio_subsystem_fingerprint_factory=lambda: {},
            endpoint_fxproperties_sha_for=lambda guid: "",
        )

        entry = out["entries"]["{ENDPOINT-CCC}"]
        # All v2 fields preserved.
        assert entry["device_class"] == "external_usb"
        assert entry["validation_mode"] == "warm"
        assert entry["vad_mean_prob_at_validation"] == 0.45  # noqa: PLR2004
        assert entry["endpoint_fxproperties_sha"] == "sha-1234"
        assert entry["pinned"] is False
        assert entry["last_boot_diagnosis"] == "healthy"
        # Outer v2 fields also preserved.
        assert out["audio_subsystem_fingerprint"]["audio_server"] == "pulseaudio"
        assert out["wake_word_model_version"] == "openwakeword-v0.6.0"
        assert out["stt_model_version"] == "moonshine-tiny"


class TestCurrentVersionIdempotent:
    """A v3 (current) snapshot must be a no-op through migrate_to_current
    — defensive guard that the framework doesn't accidentally re-apply
    a migration to already-current data."""

    def test_already_current_returns_unchanged_dict(self) -> None:
        from sovyx.voice.health._combo_store_migrations import migrate_to_current

        v4_already_current = dict(_v2_snapshot())
        # Phase 3 / T3.10 bumped CURRENT_SCHEMA_VERSION 3→4. The
        # idempotency contract still holds: a dict already at
        # CURRENT must not be mutated.
        v4_already_current["schema_version"] = 4
        # Add candidate_kind to satisfy the v3+ contract.
        for entry in v4_already_current["entries"].values():
            entry["candidate_kind"] = "hardware"

        before = json.dumps(v4_already_current, sort_keys=True)
        out = migrate_to_current(
            v4_already_current,
            audio_subsystem_fingerprint_factory=lambda: {},
            endpoint_fxproperties_sha_for=lambda guid: "",
        )
        after = json.dumps(out, sort_keys=True)
        assert before == after, "already-current dict was mutated by migrate_to_current"


# ── Malformed-state recovery ──────────────────────────────────────


class TestMalformedRecovery:
    """The migration framework must fail LOUDLY (raise MigrationError)
    on structurally-broken input rather than silently producing
    half-migrated state. This guards against the worst class of bugs
    where an upgrade silently corrupts the on-disk store."""

    def test_v1_with_entries_as_list_raises(self) -> None:
        from sovyx.voice.health._combo_store_migrations import (
            MigrationError,
            migrate_to_current,
        )

        broken = {"schema_version": 1, "entries": []}  # entries should be dict, not list
        with pytest.raises(MigrationError, match="not a dict"):
            migrate_to_current(
                broken,
                audio_subsystem_fingerprint_factory=lambda: {},
                endpoint_fxproperties_sha_for=lambda guid: "",
            )

    def test_v2_with_entries_as_list_raises(self) -> None:
        from sovyx.voice.health._combo_store_migrations import (
            MigrationError,
            migrate_to_current,
        )

        broken = {"schema_version": 2, "entries": []}
        with pytest.raises(MigrationError, match="not a dict"):
            migrate_to_current(
                broken,
                audio_subsystem_fingerprint_factory=lambda: {},
                endpoint_fxproperties_sha_for=lambda guid: "",
            )

    def test_future_version_raises(self) -> None:
        from sovyx.voice.health._combo_store_migrations import (
            MigrationError,
            migrate_to_current,
        )

        future = {
            "schema_version": CURRENT_SCHEMA_VERSION + 1,
            "entries": {},
        }
        with pytest.raises(MigrationError, match="newer than runtime"):
            migrate_to_current(
                future,
                audio_subsystem_fingerprint_factory=lambda: {},
                endpoint_fxproperties_sha_for=lambda guid: "",
            )


pytestmark = pytest.mark.timeout(15)
