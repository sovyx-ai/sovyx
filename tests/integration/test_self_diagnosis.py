"""End-to-end integration tests for the startup self-diagnosis cascade.

``sovyx.observability.self_diagnosis.run_startup_cascade`` replaces the
old .ps1 forensic helpers: it emits an ordered stream of ``startup.*``
events inside a single ``startup`` saga so operators can reconstruct a
daemon's boot by filtering one ``saga_id`` in the dashboard. These
tests exercise the cascade end-to-end against the real structlog
pipeline — the canonical events must all land on disk, share a saga,
be ordered as documented, and isolate individual helper failures
through ``startup.step.failed`` without aborting the sequence.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.contextvars import clear_contextvars

from sovyx.engine.config import EngineConfig, LoggingConfig, ObservabilityConfig
from sovyx.engine.registry import ServiceRegistry
from sovyx.observability import self_diagnosis
from sovyx.observability.logging import (
    get_logger,
    setup_logging,
    shutdown_logging,
)


@pytest.fixture()
def _clean_state() -> Generator[None, None, None]:
    """Tear down logging + structlog contextvars between tests."""
    clear_contextvars()
    yield
    shutdown_logging(timeout=2.0)
    structlog.reset_defaults()
    clear_contextvars()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def _wait_for_file(path: Path, *, timeout: float = 3.0) -> None:
    """Block until *path* has at least one byte or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.02)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Return every JSON object from *path* (one per line)."""
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _make_obs_config(**overrides: Any) -> ObservabilityConfig:
    """Build an ObservabilityConfig with the startup cascade flag ON."""
    base: dict[str, Any] = {
        "features": {
            "async_queue": True,
            "pii_redaction": True,
            "saga_propagation": True,
            "voice_telemetry": False,
            "startup_cascade": True,
            "plugin_introspection": False,
            "anomaly_detection": False,
            "tamper_chain": False,
            "schema_validation": False,
            "metrics_exporter": False,
        },
    }
    base.update(overrides)
    return ObservabilityConfig.model_validate(base)


def _setup_logging_under_tmp(tmp_path: Path) -> Path:
    """Install setup_logging with a tmp-resident JSON log file."""
    log_file = tmp_path / "logs" / "diag.log"
    obs_cfg = _make_obs_config()
    setup_logging(
        LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
        obs_cfg,
        data_dir=tmp_path,
    )
    return log_file


def _make_engine_config(tmp_path: Path) -> EngineConfig:
    """Build an EngineConfig rooted in *tmp_path* (validators resolve paths)."""
    cfg = EngineConfig(data_dir=tmp_path)
    # The validators set log.log_file → tmp_path/logs/sovyx.log. The cascade
    # probes that file's existence; touching it keeps the step happy.
    cfg.log.log_file.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    cfg.log.log_file.touch()  # type: ignore[union-attr]
    return cfg


_EXPECTED_STEP_NAMES: tuple[str, ...] = (
    "startup.platform",
    "startup.hardware",
    "startup.audio.devices",
    "startup.audio.apo_scan",
    "startup.network",
    "startup.filesystem",
    "startup.models",
    "startup.config.provenance",
    "startup.health.snapshot",
)


def _step_outcomes(records: list[dict[str, Any]]) -> dict[str, str]:
    """Map each expected step name → "ok" | "failed" | "missing"."""
    outcomes: dict[str, str] = dict.fromkeys(_EXPECTED_STEP_NAMES, "missing")
    for rec in records:
        event = rec.get("event")
        if event in outcomes:
            outcomes[event] = "ok"
        elif event == "startup.step.failed":
            step = rec.get("startup.step")
            if isinstance(step, str) and step in outcomes:
                outcomes[step] = "failed"
    return outcomes


class TestCascadeCompleteness:
    """Every documented step either succeeds or emits ``startup.step.failed``."""

    @pytest.mark.asyncio
    async def test_every_step_accounted_for(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()

        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        outcomes = _step_outcomes(records)
        missing = [name for name, state in outcomes.items() if state == "missing"]
        assert not missing, f"cascade dropped steps: {missing}"

    @pytest.mark.asyncio
    async def test_startup_completed_is_last_startup_event(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()

        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        startup_events = [
            rec
            for rec in records
            if isinstance(rec.get("event"), str) and str(rec["event"]).startswith("startup.")
        ]
        assert startup_events, "cascade produced no startup.* events"
        assert startup_events[-1]["event"] == "startup.completed"


class TestCascadeSagaBinding:
    """Every event emitted by the cascade shares the ``startup`` saga_id."""

    @pytest.mark.asyncio
    async def test_every_startup_event_carries_same_saga_id(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()

        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        startup_events = [
            rec
            for rec in records
            if isinstance(rec.get("event"), str) and str(rec["event"]).startswith("startup.")
        ]
        saga_ids = {rec.get("saga_id") for rec in startup_events}
        assert None not in saga_ids, "some startup events emitted outside the saga"
        assert len(saga_ids) == 1, f"multiple saga_ids present: {saga_ids}"

    @pytest.mark.asyncio
    async def test_log_outside_saga_has_no_saga_id(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()
        # A record emitted *before* the cascade must not inherit the saga.
        outside = get_logger("diag.outside")
        outside.info("outside.pre")
        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        pre = next(r for r in records if r.get("event") == "outside.pre")
        assert "saga_id" not in pre


class TestCascadeOrder:
    """Steps appear in the operator-documented order."""

    @pytest.mark.asyncio
    async def test_documented_step_order_is_preserved(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()

        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        # Extract the *first* occurrence position for each expected step,
        # allowing either the success event or a matching step.failed entry.
        positions: dict[str, int] = {}
        for idx, rec in enumerate(records):
            event = rec.get("event")
            if event in _EXPECTED_STEP_NAMES and event not in positions:
                positions[event] = idx
            elif event == "startup.step.failed":
                step = rec.get("startup.step")
                if (
                    isinstance(step, str)
                    and step in _EXPECTED_STEP_NAMES
                    and step not in positions
                ):
                    positions[step] = idx
        ordered = [positions[name] for name in _EXPECTED_STEP_NAMES if name in positions]
        assert ordered == sorted(ordered), f"cascade reordered steps: {positions}"


class TestCascadeFailureIsolation:
    """A raising helper must NOT abort the cascade — it emits step.failed instead."""

    @pytest.mark.asyncio
    async def test_helper_failure_emits_step_failed_and_cascade_continues(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _clean_state: None,
    ) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()

        async def _boom() -> None:
            msg = "psutil import exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(self_diagnosis, "_emit_hardware", _boom)

        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        failed = [r for r in records if r.get("event") == "startup.step.failed"]
        assert any(r.get("startup.step") == "startup.hardware" for r in failed), (
            f"expected a step.failed for startup.hardware, got: {failed}"
        )
        # The cascade must have continued all the way to startup.completed.
        assert any(r.get("event") == "startup.completed" for r in records)
        # A failed hardware step must NOT emit a successful startup.hardware.
        assert not any(r.get("event") == "startup.hardware" for r in records)

    @pytest.mark.asyncio
    async def test_step_failed_includes_error_type_and_duration(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _clean_state: None,
    ) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()

        async def _boom(_cfg: object) -> None:
            msg = "network unreachable"
            raise ConnectionError(msg)

        monkeypatch.setattr(self_diagnosis, "_emit_network", _boom)

        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        failed = next(
            r
            for r in records
            if r.get("event") == "startup.step.failed"
            and r.get("startup.step") == "startup.network"
        )
        assert failed["startup.error_type"] == "ConnectionError"
        assert "network unreachable" in failed["startup.error"]
        assert isinstance(failed["startup.duration_ms"], (int, float))
        assert failed["level"] == "warning"


class TestConfigProvenanceStep:
    """The provenance step emits one ``config.value.resolved`` per field + a summary."""

    @pytest.mark.asyncio
    async def test_provenance_summary_carries_field_count(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()

        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        summary = next(r for r in records if r.get("event") == "startup.config.provenance")
        value_records = [r for r in records if r.get("event") == "config.value.resolved"]
        assert summary["cfg.field_count"] == len(value_records)
        assert summary["cfg.field_count"] > 0
        # Every resolved record must carry the three provenance dimensions.
        for rec in value_records[:5]:
            assert "cfg.field" in rec
            assert "cfg.source" in rec
            assert "cfg.env_key" in rec


class TestHealthSnapshotStep:
    """Empty registry reports ``health.registry_present=False``."""

    @pytest.mark.asyncio
    async def test_health_snapshot_reports_no_registry(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()  # nothing registered.

        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        snap = next(r for r in records if r.get("event") == "startup.health.snapshot")
        assert snap["health.registry_present"] is False
        assert snap["health.summary"] == {}


class TestPlatformStepShape:
    """``startup.platform`` always carries the full fingerprint dict."""

    @pytest.mark.asyncio
    async def test_platform_fields_present(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = _setup_logging_under_tmp(tmp_path)
        cfg = _make_engine_config(tmp_path)
        registry = ServiceRegistry()

        await self_diagnosis.run_startup_cascade(cfg, registry)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        # Platform is the first step so it can never legitimately fail unless
        # the stdlib platform module itself is broken — assert it succeeded.
        platform_rec = next(r for r in records if r.get("event") == "startup.platform")
        for key in (
            "platform.system",
            "platform.release",
            "platform.version",
            "platform.machine",
            "platform.node",
            "platform.python_version",
            "platform.python_implementation",
            "platform.sys_platform",
        ):
            assert key in platform_rec, f"missing {key} on startup.platform"
