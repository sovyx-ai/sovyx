"""Tests for sovyx.observability.envelope — EnvelopeProcessor injection."""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Generator
from typing import Any

import pytest
import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from sovyx.observability.envelope import (
    SERVICE_INSTANCE_ID,
    SERVICE_NAMESPACE,
    EnvelopeProcessor,
)
from sovyx.observability.schema import SCHEMA_VERSION


@pytest.fixture(autouse=True)
def _clean_contextvars() -> Generator[None, None, None]:
    """Each test starts with a fresh structlog contextvar bag."""
    clear_contextvars()
    yield
    clear_contextvars()


def _call(
    processor: EnvelopeProcessor,
    event_dict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke the processor with a minimal stub logger and return the augmented dict."""
    return dict(processor(structlog.get_logger(), "info", event_dict or {}))


class TestEnvelopeProcessorEnvelopeFields:
    """The five canonical envelope fields land on every record."""

    def test_schema_version_is_constant(self) -> None:
        result = _call(EnvelopeProcessor())
        assert result["schema_version"] == SCHEMA_VERSION

    def test_process_id_matches_current_pid(self) -> None:
        result = _call(EnvelopeProcessor())
        assert result["process_id"] == os.getpid()

    def test_host_is_resolved_at_construction_time(self) -> None:
        result = _call(EnvelopeProcessor())
        assert result["host"]
        assert isinstance(result["host"], str)

    def test_sovyx_version_present(self) -> None:
        result = _call(EnvelopeProcessor())
        assert result["sovyx_version"]
        assert isinstance(result["sovyx_version"], str)


class TestEnvelopeProcessorOtelTwins:
    """OTel semconv mirrors must ride alongside legacy envelope fields."""

    def test_service_namespace_matches_module_constant(self) -> None:
        result = _call(EnvelopeProcessor())
        assert result["service.namespace"] == SERVICE_NAMESPACE

    def test_service_instance_id_matches_module_constant(self) -> None:
        result = _call(EnvelopeProcessor())
        assert result["service.instance.id"] == SERVICE_INSTANCE_ID

    def test_runtime_attributes_use_python_metadata(self) -> None:
        result = _call(EnvelopeProcessor())
        assert result["process.runtime.name"] == sys.implementation.name
        assert result["process.runtime.version"] == platform.python_version()


class TestEnvelopeProcessorSequenceNo:
    """sequence_no must be monotonic and per-instance."""

    def test_sequence_no_starts_at_zero(self) -> None:
        result = _call(EnvelopeProcessor())
        assert result["sequence_no"] == 0

    def test_sequence_no_increments_monotonically(self) -> None:
        proc = EnvelopeProcessor()
        first = _call(proc)["sequence_no"]
        second = _call(proc)["sequence_no"]
        third = _call(proc)["sequence_no"]
        assert (first, second, third) == (0, 1, 2)

    def test_sequence_no_is_per_processor(self) -> None:
        first = _call(EnvelopeProcessor())["sequence_no"]
        second = _call(EnvelopeProcessor())["sequence_no"]
        assert first == 0
        assert second == 0

    def test_sequence_no_advanced_only_when_contributed(self) -> None:
        proc = EnvelopeProcessor()
        # Forwarded entry already carries its own sequence_no.
        assert _call(proc, {"sequence_no": 999})["sequence_no"] == 999
        # Local counter must NOT have advanced — next local emit = 0.
        assert _call(proc)["sequence_no"] == 0


class TestEnvelopeProcessorPreservesExisting:
    """The processor never overwrites a value the caller already set."""

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("schema_version", "9.9.9"),
            ("process_id", 424242),
            ("host", "remote-forwarder"),
            ("sovyx_version", "0.0.0-forwarded"),
            ("service.namespace", "tenant-x"),
            ("service.instance.id", "external-instance-uuid"),
        ],
    )
    def test_existing_value_wins(self, key: str, value: Any) -> None:  # noqa: ANN401
        proc = EnvelopeProcessor()
        result = _call(proc, {key: value})
        assert result[key] == value


class TestEnvelopeProcessorContextualIds:
    """saga_id / span_id / event_id / cause_id must be lifted from contextvars when present."""

    def test_lifts_saga_id_from_contextvars(self) -> None:
        bind_contextvars(saga_id="saga-abc")
        result = _call(EnvelopeProcessor())
        assert result["saga_id"] == "saga-abc"

    def test_lifts_all_contextual_ids(self) -> None:
        bind_contextvars(
            saga_id="s1",
            span_id="sp1",
            event_id="ev1",
            cause_id="c1",
        )
        result = _call(EnvelopeProcessor())
        assert result["saga_id"] == "s1"
        assert result["span_id"] == "sp1"
        assert result["event_id"] == "ev1"
        assert result["cause_id"] == "c1"

    def test_absent_contextvars_do_not_inject_keys(self) -> None:
        result = _call(EnvelopeProcessor())
        for key in ("saga_id", "span_id", "event_id", "cause_id"):
            assert key not in result

    def test_existing_contextual_id_wins_over_contextvars(self) -> None:
        bind_contextvars(saga_id="from-context")
        result = _call(EnvelopeProcessor(), {"saga_id": "from-call"})
        assert result["saga_id"] == "from-call"


class TestEnvelopeProcessorImmutability:
    """The processor must never mutate the cached field dict."""

    def test_cached_dict_unchanged_across_calls(self) -> None:
        proc = EnvelopeProcessor()
        _call(proc, {"host": "override-1"})
        _call(proc, {"host": "override-2"})
        # Re-emit with empty dict — cached host must still be the original.
        clean = _call(proc)
        assert clean["host"] == platform.node() or clean["host"] == "unknown"
