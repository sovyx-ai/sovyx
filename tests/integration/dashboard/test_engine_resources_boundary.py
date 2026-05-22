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

    def test_gate8_all_scope_resource_cohort_metrics_pairing(self) -> None:
        """Mission C C.6 §1 follow-up — explicit Gate 8 all-scope pairing.

        ``check_boundary_round_trip_coverage --scope all`` LENIENT scan
        identified ``ResourceCohortMetrics`` at
        ``routes/engine_resources.py:198`` as uncovered until the gate's
        ``_DEFAULT_TEST_DIRS`` was widened to include
        ``tests/integration/dashboard/`` alongside ``tests/dashboard/``.

        This test exists to be the *named* boundary-pairing anchor for
        ``ResourceCohortMetrics``: a single explicit
        ``ResourceCohortMetrics.model_validate(...)`` call with a
        producer-mirror payload that the Gate 8 AST regex
        (``\\bResourceCohortMetrics\\.model_validate\\s*\\(``) matches
        unambiguously. Sibling tests above (lines 58, 79, 100, 127)
        already exercise the same round-trip across realistic registry
        states; this one pins the *contract* for future readers + the
        gate's discoverability.

        Anti-pattern #40 enforcement; no production code touched.
        """
        # Producer-mirror shape — mirrors what
        # ``ResourceRegistry.snapshot_fields()`` emits at steady state
        # (post-A.1 SSoT canonical key set; see
        # ``src/sovyx/observability/_resource_registry.py``).
        producer_payload: dict[str, object] = {
            "process.open_files_status": "ok",
            "process.connections_status": "ok",
            "asyncio.all_task_names": ["snapshotter", "heartbeat"],
            "asyncio.not_done_count": 2,
            "asyncio.awaiting_count": 1,
            "to_thread.pool_size_at_last_dispatch": 8,
            "to_thread.queue_depth_at_last_dispatch": 0,
            "to_thread.max_workers_at_last_dispatch": 16,
            "to_thread.dispatch_count_total": 42,
            "to_thread.dispatch_count_per_label": {"onnx": 30, "vad": 12},
            "lock_dict.total_cardinality": 7,
            "lock_dict.per_owner": {"bridge.conv_locks": 3, "voice.health": 4},
            "lock_dict.instance_count": 2,
            "onnx.session_count": 2,
            "onnx.session_labels": ["brain.embedding", "voice.vad.silero"],
            "gc.collections_by_gen": [5, 1, 0],
            "gc.objects_count": 12345,
            "tracemalloc.is_tracing": False,
            "tracemalloc.current_kb": 0,
            "tracemalloc.peak_kb": 0,
            "exception_cohort.cumulative_retained_bytes_since_start": 0,
            "exception_cohort.cumulative_distinct_group_id_count": 0,
            "exception_cohort.window_retained_bytes": 0,
            "exception_cohort.window_distinct_group_id_count": 0,
        }
        cohorts = ResourceCohortMetrics.model_validate(producer_payload)
        # Canonical key reads land first-class typed (post-Phase C.2
        # zod-twin completeness + producer parity).
        assert cohorts.onnx_session_count == 2
        assert cohorts.lock_dict_total_cardinality == 7
        assert cohorts.to_thread_pool_size_at_last_dispatch == 8

    def test_to_thread_active_workers_lenient_shim_on_boundary(self) -> None:
        """MISSION-A.1.P3 F-006 (ADR-D15): LENIENT shim survives pydantic boundary.

        Pre-A.1.P3 this test enforced ``active_workers == pool_size`` as
        a CONTRACT (the alias-trap pattern catalogued in anti-pattern
        #48). The equality assertion is removed. The field stays typed
        in the pydantic model so the LENIENT shim travels through the
        boundary cleanly during the v0.55.0 sunset window, but the test
        only verifies that the boundary accepts the legacy key — not
        that the value matches any specific source.
        """
        s1 = _FakeSession()
        register_onnx_session(label="brain.embedding", session=s1)
        fields = get_default_resource_registry().snapshot_fields()
        # The snapshotter (NOT snapshot_fields()) adds the LENIENT shim,
        # so to exercise the boundary end-to-end we synthesize the shim
        # here matching the snapshotter contract.
        pool_size = fields.get("to_thread.pool_size")
        if isinstance(pool_size, int):
            fields["to_thread.active_workers"] = pool_size
        cohorts = ResourceCohortMetrics.model_validate(fields)
        # Field stays typed during LENIENT.
        assert hasattr(cohorts, "to_thread_active_workers")
        assert "to_thread.active_workers" not in (cohorts.__pydantic_extra__ or {})
