"""Quality Gate 8 boundary round-trip — Mission H4 §T3.1.

Asserts that the producer dict shape (the live
:meth:`ResourceRegistry.snapshot_fields()` output) validates cleanly
through the route boundary's :class:`EngineResourcesResponse` model.
Anti-pattern #40 enforcement: the producer dict and the response model
MUST stay shape-compatible across both H4 phases + future extensions.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T3.1 + §4.7 ADR-D7 + §10.2.
"""

from __future__ import annotations

import pytest

from sovyx.dashboard.routes.engine_resources import (
    EngineResourcesResponse,
    ResourceCohortMetrics,
    _build_response,
)
from sovyx.observability._resource_registry import (
    get_default_resource_registry,
    register_lock_dict,
    register_onnx_session,
    reset_default_resource_registry,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_default_resource_registry()
    yield
    reset_default_resource_registry()


class _FakeSession:
    pass


class _FakeLockDict:
    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size


class TestEngineResourcesBoundaryRoundTrip:
    """Producer-to-boundary round-trip — anti-pattern #40."""

    def test_empty_registry_validates(self) -> None:
        """An empty registry produces a payload that satisfies the schema."""
        registry = get_default_resource_registry()
        fields = registry.snapshot_fields()
        # Validates via the same path the route uses.
        cohorts = ResourceCohortMetrics.model_validate(fields)
        # Defaults reflect empty state.
        assert cohorts.onnx_session_count == 0
        assert cohorts.lock_dict_total_cardinality == 0

    def test_populated_registry_validates(self) -> None:
        # Bind to locals so weakref-tracked sessions/dicts stay alive
        # for the duration of the assertion (not GC'd before
        # snapshot_fields() reads them).
        s1 = _FakeSession()
        s2 = _FakeSession()
        d1 = _FakeLockDict(12)
        d2 = _FakeLockDict(5)
        register_onnx_session(label="brain.embedding", session=s1)
        register_onnx_session(label="voice.vad.silero", session=s2)
        register_lock_dict(owner_id="bridge.conv_locks", dict_ref=d1)
        register_lock_dict(
            owner_id="voice.health.watchdog.lifecycle_locks",
            dict_ref=d2,
        )
        fields = get_default_resource_registry().snapshot_fields()
        cohorts = ResourceCohortMetrics.model_validate(fields)
        assert cohorts.onnx_session_count == 2
        assert set(cohorts.onnx_session_labels) == {"brain.embedding", "voice.vad.silero"}
        assert cohorts.lock_dict_total_cardinality == 17
        assert cohorts.lock_dict_instance_count == 2

    def test_full_response_envelope_validates(self) -> None:
        """``_build_response`` returns a fully-validated EngineResourcesResponse."""
        response = _build_response()
        assert isinstance(response, EngineResourcesResponse)
        assert response.observed_at_unix > 0
        # canonical + legacy_alias counts come from the SSoT mapping.
        assert response.canonical_field_count > 0
        assert response.legacy_alias_count >= 1  # at least system.rss_bytes alias

    def test_unknown_extra_fields_forward_additive(self) -> None:
        """``extra="allow"`` permits Phase 1.D extensions without breaking the model."""
        fields = get_default_resource_registry().snapshot_fields()
        # Simulate a Phase 1.D extension: governor.budget_state field.
        fields["cohort_governor.budget_state"] = "ok"
        fields["cohort_governor.circuit_breaker_engaged"] = False
        cohorts = ResourceCohortMetrics.model_validate(fields)
        # The base model exposes the canonical aliases; the extra fields
        # land in cohorts.__pydantic_extra__.
        extra = cohorts.__pydantic_extra__ or {}
        assert "cohort_governor.budget_state" in extra
        assert "cohort_governor.circuit_breaker_engaged" in extra
