"""End-to-end integration tests for the observability logging pipeline.

Exercises ``setup_logging`` with a full ``ObservabilityConfig`` and asserts
that emitted records arrive on disk in JSON form with all expected
augmentations: envelope fields (schema_version, host, sovyx_version,
sequence_no), structlog standard metadata (level, logger, timestamp),
SecretMasker redaction, PIIRedactor verbosity modes, saga/span context
propagation, async-queue drain, ring-buffer crash dump, and clean
teardown via ``shutdown_logging``.
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

from sovyx.engine.config import LoggingConfig, ObservabilityConfig
from sovyx.observability.logging import (
    bound_request_context,
    get_logger,
    setup_logging,
    shutdown_logging,
)
from sovyx.observability.saga import async_saga_scope, current_saga_id, saga_scope
from sovyx.observability.schema import SCHEMA_VERSION


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
    """Build an ObservabilityConfig with every feature flag OFF unless explicitly enabled."""
    base: dict[str, Any] = {
        "features": {
            "async_queue": True,
            "pii_redaction": True,
            "saga_propagation": True,
            "voice_telemetry": False,
            "startup_cascade": False,
            "plugin_introspection": False,
            "anomaly_detection": False,
            "tamper_chain": False,
            "schema_validation": False,
            "metrics_exporter": False,
        },
    }
    base.update(overrides)
    return ObservabilityConfig.model_validate(base)


class TestEndToEndPipeline:
    """A single ``logger.info`` call lands on disk with envelope + standard fields."""

    def test_record_lands_in_file_with_envelope(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = tmp_path / "logs" / "sovyx.log"
        log_cfg = LoggingConfig(level="DEBUG", console_format="json", log_file=log_file)
        obs_cfg = _make_obs_config()
        setup_logging(log_cfg, obs_cfg, data_dir=tmp_path)

        log = get_logger("integration.test")
        log.info("integration.smoke", flow="end-to-end")

        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)
        records = _read_jsonl(log_file)
        assert any(r.get("event") == "integration.smoke" for r in records)
        smoke = next(r for r in records if r.get("event") == "integration.smoke")

        # Envelope fields injected by EnvelopeProcessor.
        assert smoke["schema_version"] == SCHEMA_VERSION
        assert smoke["host"]
        assert smoke["sovyx_version"]
        assert isinstance(smoke["process_id"], int)
        assert isinstance(smoke["sequence_no"], int)

        # structlog standard metadata.
        assert smoke["level"] == "info"
        assert smoke["logger"] == "integration.test"
        assert "timestamp" in smoke
        assert smoke["flow"] == "end-to-end"

    def test_secret_masker_redacts_token_field(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = tmp_path / "logs" / "sovyx.log"
        setup_logging(
            LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
            _make_obs_config(),
            data_dir=tmp_path,
        )
        log = get_logger("integration.secret")
        log.info("auth.attempt", api_key="sk-VERYLONGTOKENABCDEF12345")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        rec = next(r for r in records if r.get("event") == "auth.attempt")
        assert rec["api_key"] != "sk-VERYLONGTOKENABCDEF12345"
        assert "sk-" in rec["api_key"]
        assert "..." in rec["api_key"]

    def test_pii_redactor_masks_email_in_user_message(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = tmp_path / "logs" / "sovyx.log"
        # Default PII config has user_messages='redacted' — emails get masked.
        setup_logging(
            LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
            _make_obs_config(),
            data_dir=tmp_path,
        )
        log = get_logger("integration.pii")
        # Synthetic, non-real address per the §22.3 fixture rules.
        log.info(
            "chat.message",
            user_message="reach me at synthetic.user@example-fake.test please",
        )
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        rec = next(r for r in _read_jsonl(log_file) if r.get("event") == "chat.message")
        assert "synthetic.user@example-fake.test" not in rec["user_message"]


class TestSagaPropagation:
    """saga_id / span_id from ``saga_scope`` ride along on every emitted record."""

    def test_saga_id_appears_on_records_inside_scope(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = tmp_path / "logs" / "sovyx.log"
        setup_logging(
            LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
            _make_obs_config(),
            data_dir=tmp_path,
        )
        log = get_logger("integration.saga")
        with saga_scope("test.saga") as saga_id:
            log.info("inside.saga", marker=True)
            assert current_saga_id() == saga_id
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        inside = [r for r in records if r.get("event") == "inside.saga"]
        assert len(inside) == 1
        assert inside[0]["saga_id"] == saga_id

    def test_records_outside_scope_have_no_saga_id(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = tmp_path / "logs" / "sovyx.log"
        setup_logging(
            LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
            _make_obs_config(),
            data_dir=tmp_path,
        )
        log = get_logger("integration.saga")
        log.info("outside.saga.before")
        with saga_scope("temp"):
            log.info("inside.saga")
        log.info("outside.saga.after")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        before = next(r for r in records if r.get("event") == "outside.saga.before")
        after = next(r for r in records if r.get("event") == "outside.saga.after")
        inside = next(r for r in records if r.get("event") == "inside.saga")
        assert "saga_id" not in before
        assert "saga_id" not in after
        assert "saga_id" in inside

    @pytest.mark.asyncio
    async def test_async_saga_scope_propagates_id(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = tmp_path / "logs" / "sovyx.log"
        setup_logging(
            LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
            _make_obs_config(),
            data_dir=tmp_path,
        )
        log = get_logger("integration.saga.async")
        async with async_saga_scope("async.test") as saga_id:
            log.info("inside.async.saga", n=1)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        records = _read_jsonl(log_file)
        inside = next(r for r in records if r.get("event") == "inside.async.saga")
        assert inside["saga_id"] == saga_id


class TestRequestContextBinding:
    """``bound_request_context`` injects mind_id / conversation_id / request_id."""

    def test_request_context_fields_appear_on_records(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = tmp_path / "logs" / "sovyx.log"
        setup_logging(
            LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
            _make_obs_config(),
            data_dir=tmp_path,
        )
        log = get_logger("integration.ctx")
        with bound_request_context(
            mind_id="mind-7", conversation_id="conv-99", request_id="req-abc"
        ):
            log.info("ctx.event")
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        rec = next(r for r in _read_jsonl(log_file) if r.get("event") == "ctx.event")
        assert rec["mind_id"] == "mind-7"
        assert rec["conversation_id"] == "conv-99"
        assert rec["request_id"] == "req-abc"


class TestSequenceMonotonicity:
    """``sequence_no`` is monotonic across emissions from the same process."""

    def test_sequence_increases(self, tmp_path: Path, _clean_state: None) -> None:
        log_file = tmp_path / "logs" / "sovyx.log"
        setup_logging(
            LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
            _make_obs_config(),
            data_dir=tmp_path,
        )
        log = get_logger("integration.seq")
        for i in range(5):
            log.info("seq.event", n=i)
        shutdown_logging(timeout=3.0)
        _wait_for_file(log_file)

        seq_records = [r for r in _read_jsonl(log_file) if r.get("event") == "seq.event"]
        assert len(seq_records) == 5
        seqs = [int(r["sequence_no"]) for r in seq_records]
        assert seqs == sorted(seqs), f"sequence_no must be monotonic, got {seqs}"


class TestRingBufferCrashDump:
    """Crash dump path receives the in-memory ring buffer on demand."""

    def test_dump_path_receives_recent_records(self, tmp_path: Path, _clean_state: None) -> None:
        crash_path = tmp_path / "crash" / "dump.jsonl"
        log_file = tmp_path / "logs" / "sovyx.log"
        obs_cfg = _make_obs_config(crash_dump_path=crash_path)
        setup_logging(
            LoggingConfig(level="DEBUG", console_format="json", log_file=log_file),
            obs_cfg,
            data_dir=tmp_path,
        )
        log = get_logger("integration.ring")
        log.info("ring.target", payload="x")

        # Find the live RingBufferHandler attached by setup_logging and trigger
        # an explicit dump — install_crash_hooks wires this to fire on
        # excepthook / atexit / asyncio errors; we exercise the dump path
        # directly so the test doesn't have to crash the interpreter.
        from sovyx.observability.ringbuffer import RingBufferHandler  # noqa: PLC0415

        ring = next(h for h in logging.getLogger().handlers if isinstance(h, RingBufferHandler))
        ring.dump_to_file(crash_path)

        assert crash_path.exists()
        lines = crash_path.read_text(encoding="utf-8").splitlines()
        assert lines, "ring buffer should have at least one entry"
        # Each line is ``{"ts": float, "msg": <serialised payload>}``.
        decoded = [json.loads(line) for line in lines]
        # The serialised msg field is the JSON-rendered structlog payload, so
        # we look for our event marker as a substring.
        assert any("ring.target" in str(entry["msg"]) for entry in decoded)


class TestIdempotentSetup:
    """``setup_logging`` is safe to call multiple times — no handler accumulation."""

    def test_repeated_setup_does_not_accumulate_handlers(
        self, tmp_path: Path, _clean_state: None
    ) -> None:
        log_file = tmp_path / "logs" / "sovyx.log"
        cfg = LoggingConfig(level="DEBUG", console_format="json", log_file=log_file)
        obs = _make_obs_config()
        for _ in range(3):
            setup_logging(cfg, obs, data_dir=tmp_path)
        # Exactly: 1 console StreamHandler + 1 async/file handler + 1 ring buffer.
        # No accumulation across the three calls.
        kinds = [type(h).__name__ for h in logging.getLogger().handlers]
        assert kinds.count("AsyncQueueHandler") == 1
        assert kinds.count("RingBufferHandler") == 1
        # StreamHandler may be subclassed; just ensure exactly one stream-style.
        stream_count = sum(
            1 for h in logging.getLogger().handlers if isinstance(h, logging.StreamHandler)
        )
        # NOTE: ring buffer + async are not StreamHandler subclasses, so the
        # stream count corresponds exactly to the console handler.
        assert stream_count == 1
