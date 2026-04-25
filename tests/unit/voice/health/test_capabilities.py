"""Tests for :mod:`sovyx.voice.health._capabilities` (X1 Phase 1).

Covers the Capability enum, CapabilityResolver caching + dispatch,
fail-closed behaviour on broken probes, and the singleton accessor.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.11.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from sovyx.voice.health._capabilities import (
    _PROBE_FNS,
    Capability,
    CapabilityNotAvailableError,
    CapabilityResolver,
    get_default_resolver,
    reset_default_resolver_for_tests,
)


def _stub_probes(
    overrides: dict[Capability, bool] | None = None,
) -> dict[Capability, Any]:
    """Build a probe map: every Capability returns False unless
    overridden via ``overrides``."""
    overrides = overrides or {}
    out: dict[Capability, Any] = {}
    for cap in Capability:
        verdict = overrides.get(cap, False)
        out[cap] = lambda v=verdict: v
    return out


# ── Capability enum ───────────────────────────────────────────────


class TestCapabilityEnum:
    def test_all_values_lowercase_snake(self) -> None:
        for cap in Capability:
            assert cap.value == cap.value.lower()
            assert " " not in cap.value

    def test_str_enum_value_comparison(self) -> None:
        """Anti-pattern #9 — string equality must work (xdist-safe)."""
        assert Capability.WASAPI_EXCLUSIVE == "wasapi_exclusive"

    def test_probe_table_covers_every_capability(self) -> None:
        """Import-time guard surface — every Capability must have
        a probe registered."""
        assert set(_PROBE_FNS.keys()) == set(Capability)


# ── CapabilityResolver — cache + has() ─────────────────────────────


class TestResolverCache:
    def test_has_returns_true_for_present_capability(self) -> None:
        probes = _stub_probes({Capability.ONNX_INFERENCE: True})
        resolver = CapabilityResolver(probes=probes)
        assert resolver.has(Capability.ONNX_INFERENCE) is True

    def test_has_returns_false_for_absent_capability(self) -> None:
        probes = _stub_probes()
        resolver = CapabilityResolver(probes=probes)
        assert resolver.has(Capability.WASAPI_EXCLUSIVE) is False

    def test_probe_called_only_once_per_capability(self) -> None:
        call_count = {"n": 0}

        def counting_probe() -> bool:
            call_count["n"] += 1
            return True

        probes = _stub_probes()
        probes[Capability.WASAPI_EXCLUSIVE] = counting_probe
        resolver = CapabilityResolver(probes=probes)
        for _ in range(10):
            resolver.has(Capability.WASAPI_EXCLUSIVE)
        assert call_count["n"] == 1

    def test_broken_probe_fails_closed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Probe raising → resolver returns False + emits WARN."""
        import logging

        def broken_probe() -> bool:
            msg = "probe failed"
            raise RuntimeError(msg)

        probes = _stub_probes()
        probes[Capability.WASAPI_EXCLUSIVE] = broken_probe
        resolver = CapabilityResolver(probes=probes)
        with caplog.at_level(logging.WARNING):
            verdict = resolver.has(Capability.WASAPI_EXCLUSIVE)
        assert verdict is False
        assert any("voice.capability.probe_failed" in str(r.msg) for r in caplog.records)

    def test_reset_cache_re_probes(self) -> None:
        verdict = {"v": True}

        def togglable_probe() -> bool:
            return verdict["v"]

        probes = _stub_probes()
        probes[Capability.WASAPI_EXCLUSIVE] = togglable_probe
        resolver = CapabilityResolver(probes=probes)
        assert resolver.has(Capability.WASAPI_EXCLUSIVE) is True
        verdict["v"] = False
        # Cached → still True.
        assert resolver.has(Capability.WASAPI_EXCLUSIVE) is True
        resolver.reset_cache()
        assert resolver.has(Capability.WASAPI_EXCLUSIVE) is False

    def test_cached_results_snapshot(self) -> None:
        probes = _stub_probes({Capability.ONNX_INFERENCE: True})
        resolver = CapabilityResolver(probes=probes)
        resolver.has(Capability.ONNX_INFERENCE)
        resolver.has(Capability.WASAPI_EXCLUSIVE)
        snap = resolver.cached_results()
        assert snap == {
            Capability.ONNX_INFERENCE: True,
            Capability.WASAPI_EXCLUSIVE: False,
        }


# ── Constructor validation ────────────────────────────────────────


class TestResolverInit:
    def test_default_uses_module_probes(self) -> None:
        resolver = CapabilityResolver()
        # Should have all capabilities resolvable (no KeyError).
        for cap in Capability:
            resolver.has(cap)

    def test_partial_probes_rejected(self) -> None:
        partial = {Capability.ONNX_INFERENCE: lambda: True}
        with pytest.raises(ValueError, match="must cover every Capability"):
            CapabilityResolver(probes=partial)

    def test_platform_property_exposes_sys_platform(self) -> None:
        import sys

        resolver = CapabilityResolver()
        assert resolver.platform == sys.platform


# ── require() ─────────────────────────────────────────────────────


class TestRequire:
    def test_present_capability_does_not_raise(self) -> None:
        probes = _stub_probes({Capability.ONNX_INFERENCE: True})
        resolver = CapabilityResolver(probes=probes)
        resolver.require(Capability.ONNX_INFERENCE)  # no exception

    def test_absent_capability_raises(self) -> None:
        probes = _stub_probes()
        resolver = CapabilityResolver(probes=probes)
        with pytest.raises(CapabilityNotAvailableError) as exc_info:
            resolver.require(Capability.WASAPI_EXCLUSIVE)
        assert exc_info.value.capability is Capability.WASAPI_EXCLUSIVE


# ── dispatch() ────────────────────────────────────────────────────


class TestDispatch:
    def test_dispatch_calls_first_present_handler(self) -> None:
        probes = _stub_probes(
            {
                Capability.WASAPI_EXCLUSIVE: False,
                Capability.PIPEWIRE_MODULE_ECHO_CANCEL: True,
                Capability.ALSA_UCM_CONFIG: True,
            }
        )
        resolver = CapabilityResolver(probes=probes)
        called = {"first": False, "second": False, "third": False}

        def first() -> str:
            called["first"] = True
            return "first"

        def second() -> str:
            called["second"] = True
            return "second"

        def third() -> str:
            called["third"] = True
            return "third"

        result = resolver.dispatch(
            {
                Capability.WASAPI_EXCLUSIVE: first,
                Capability.PIPEWIRE_MODULE_ECHO_CANCEL: second,
                Capability.ALSA_UCM_CONFIG: third,
            }
        )
        assert result == "second"
        assert called == {"first": False, "second": True, "third": False}

    def test_dispatch_falls_through_to_default(self) -> None:
        probes = _stub_probes()
        resolver = CapabilityResolver(probes=probes)
        result = resolver.dispatch(
            {Capability.WASAPI_EXCLUSIVE: lambda: "primary"},
            default=lambda: "fallback",
        )
        assert result == "fallback"

    def test_dispatch_raises_when_none_present_and_no_default(self) -> None:
        probes = _stub_probes()
        resolver = CapabilityResolver(probes=probes)
        with pytest.raises(CapabilityNotAvailableError) as exc_info:
            resolver.dispatch({Capability.WASAPI_EXCLUSIVE: lambda: "x"})
        # Reports the FIRST capability (caller's preferred choice).
        assert exc_info.value.capability is Capability.WASAPI_EXCLUSIVE

    def test_dispatch_empty_handlers_rejected(self) -> None:
        probes = _stub_probes()
        resolver = CapabilityResolver(probes=probes)
        with pytest.raises(ValueError, match="must not be empty"):
            resolver.dispatch({})


# ── ONNX probe (the only real Phase-1 probe) ──────────────────────


class TestOnnxProbe:
    def test_onnx_probe_true_when_module_importable(self) -> None:
        """ONNX runtime is a Sovyx hard dep — should always
        resolve True in CI/dev."""
        from sovyx.voice.health._capabilities import _probe_onnx_inference

        assert _probe_onnx_inference() is True

    def test_onnx_probe_false_when_import_blocked(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulate the locked-down enterprise image case where
        onnxruntime is absent."""
        import builtins

        from sovyx.voice.health._capabilities import _probe_onnx_inference

        real_import = builtins.__import__

        def deny_onnx(name: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if name == "onnxruntime":
                msg = "no module"
                raise ImportError(msg)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", deny_onnx)
        assert _probe_onnx_inference() is False


# ── Singleton accessor ────────────────────────────────────────────


class TestSingleton:
    def test_get_default_returns_same_instance(self) -> None:
        reset_default_resolver_for_tests()
        a = get_default_resolver()
        b = get_default_resolver()
        assert a is b

    def test_reset_singleton_creates_new_instance(self) -> None:
        reset_default_resolver_for_tests()
        a = get_default_resolver()
        reset_default_resolver_for_tests()
        b = get_default_resolver()
        assert a is not b


# ── Thread safety ─────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_probes_call_probe_once(self) -> None:
        call_count = {"n": 0}
        lock = threading.Lock()

        def counting_probe() -> bool:
            with lock:
                call_count["n"] += 1
            # Add tiny delay so concurrent threads collide.
            return True

        probes = _stub_probes()
        probes[Capability.WASAPI_EXCLUSIVE] = counting_probe
        resolver = CapabilityResolver(probes=probes)

        n_threads = 16
        barrier = threading.Barrier(n_threads)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            verdict = resolver.has(Capability.WASAPI_EXCLUSIVE)
            with results_lock:
                results.append(verdict)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All threads got the same answer.
        assert all(results)
        assert len(results) == n_threads
        # Probe ran exactly once despite concurrent access.
        assert call_count["n"] == 1
