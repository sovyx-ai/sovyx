"""Unit tests for v1.3 §4.6 L6 + §4.8 L7 — boot preflight warning
surface: in-memory store + filesystem marker + stale-marker cleanup.

Named ``test_preflight_warnings_store.py`` (not ``test_preflight.py``)
so the existing ``test_preflight.py`` — which covers the orchestrator
— stays untouched. The two files exercise orthogonal contracts.
"""

from __future__ import annotations

import json
from pathlib import Path

from sovyx.voice.health import (
    BootPreflightWarningsStore,
    clear_preflight_warnings_file,
    preflight_warnings_file_path,
    read_preflight_warnings_file,
    write_preflight_warnings_file,
)


class TestBootPreflightWarningsStore:
    """§4.6.3 store contract — mutable, snapshot-copied, clear-resettable."""

    def test_starts_empty(self) -> None:
        store = BootPreflightWarningsStore()
        assert store.warnings == []
        assert store.snapshot() == []

    def test_set_warnings_replaces_snapshot(self) -> None:
        store = BootPreflightWarningsStore()
        store.set_warnings([{"code": "x", "hint": "fix me"}])
        assert store.warnings == [{"code": "x", "hint": "fix me"}]

        # Re-enable: fresh snapshot replaces the prior one (never appends).
        store.set_warnings([{"code": "y"}])
        assert store.warnings == [{"code": "y"}]

    def test_snapshot_is_defensive_copy(self) -> None:
        store = BootPreflightWarningsStore()
        store.set_warnings([{"code": "a"}])
        snap = store.snapshot()
        snap.append({"code": "injected"})
        # Mutating the returned list must not leak back.
        assert store.warnings == [{"code": "a"}]

    def test_clear_resets_to_empty(self) -> None:
        store = BootPreflightWarningsStore()
        store.set_warnings([{"code": "a"}, {"code": "b"}])
        store.clear()
        assert store.warnings == []


class TestPreflightWarningsFileRoundtrip:
    """§4.8.1 file contract — atomic write + tolerant read + idempotent clear."""

    def test_path_uses_data_dir_override(self, tmp_path: Path) -> None:
        path = preflight_warnings_file_path(data_dir=tmp_path)
        assert path == tmp_path / "preflight_warnings.json"

    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        warnings = [
            {"code": "linux_mixer_saturated", "hint": "reset mic gain"},
            {"code": "another_warning"},
        ]
        write_preflight_warnings_file(warnings, data_dir=tmp_path)
        assert read_preflight_warnings_file(data_dir=tmp_path) == warnings

    def test_write_is_atomic_via_tempfile(self, tmp_path: Path) -> None:
        write_preflight_warnings_file(
            [{"code": "x"}],
            data_dir=tmp_path,
        )
        # The tmp sibling must not linger after a successful write.
        siblings = list(tmp_path.iterdir())
        names = {p.name for p in siblings}
        assert "preflight_warnings.json" in names
        assert "preflight_warnings.json.tmp" not in names

    def test_read_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_preflight_warnings_file(data_dir=tmp_path) == []

    def test_read_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        path = preflight_warnings_file_path(data_dir=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        assert read_preflight_warnings_file(data_dir=tmp_path) == []

    def test_read_non_dict_payload_returns_empty(self, tmp_path: Path) -> None:
        path = preflight_warnings_file_path(data_dir=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(["bare", "list"]), encoding="utf-8")
        assert read_preflight_warnings_file(data_dir=tmp_path) == []

    def test_read_payload_with_non_list_warnings_returns_empty(
        self,
        tmp_path: Path,
    ) -> None:
        path = preflight_warnings_file_path(data_dir=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema_version": 1, "warnings": "oops"}),
            encoding="utf-8",
        )
        assert read_preflight_warnings_file(data_dir=tmp_path) == []

    def test_read_filters_non_dict_entries(self, tmp_path: Path) -> None:
        path = preflight_warnings_file_path(data_dir=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "warnings": [
                        {"code": "ok"},
                        "bogus-string",
                        42,
                        {"code": "also_ok"},
                    ],
                },
            ),
            encoding="utf-8",
        )
        assert read_preflight_warnings_file(data_dir=tmp_path) == [
            {"code": "ok"},
            {"code": "also_ok"},
        ]

    def test_clear_removes_file(self, tmp_path: Path) -> None:
        write_preflight_warnings_file([{"code": "x"}], data_dir=tmp_path)
        path = preflight_warnings_file_path(data_dir=tmp_path)
        assert path.exists()

        clear_preflight_warnings_file(data_dir=tmp_path)
        assert not path.exists()

    def test_clear_missing_file_is_noop(self, tmp_path: Path) -> None:
        # Must not raise on absent file — this is the common case for
        # fresh installs that never saturated.
        clear_preflight_warnings_file(data_dir=tmp_path)


class TestBootPreflightPassClearsStaleMarker:
    """v1.3 §-1C #1 alternative (e) — boot preflight that passes cleans
    any marker left behind by a prior saturated boot.

    Verified indirectly via the helper function that the factory
    invokes; a fuller factory-level test (mocking
    ``_run_boot_preflight``) lives in ``test_voice_factory.py`` T9.
    """

    def test_sequential_write_then_clear_idempotence(self, tmp_path: Path) -> None:
        # Saturated boot writes a marker.
        write_preflight_warnings_file(
            [{"code": "linux_mixer_saturated", "hint": "stale"}],
            data_dir=tmp_path,
        )
        assert read_preflight_warnings_file(data_dir=tmp_path) != []

        # Next (clean) boot clears it.
        clear_preflight_warnings_file(data_dir=tmp_path)
        assert read_preflight_warnings_file(data_dir=tmp_path) == []

        # A second clean boot is a no-op.
        clear_preflight_warnings_file(data_dir=tmp_path)
        assert read_preflight_warnings_file(data_dir=tmp_path) == []
