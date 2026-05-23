"""Mission OX-1.A — unit tests for ``cli/_startup_readiness.py``.

Coverage:

* :class:`OX1Config` defaults are both False (staged-adoption).
* :class:`EnvelopeProcessor` ``session_id_alias=True`` adds the
  ``engine_session_id`` key; ``session_id_alias=False`` (default)
  does NOT.
* ``print_startup_readiness`` renders the 6-line block under each
  of: all-healthy, LLM not registered, voice disabled, brain not
  registered, multi-degraded + ack-active states.
* The helper NEVER propagates exceptions — even when every
  ``registry.resolve`` raises, the call returns cleanly with
  ``unknown`` cells.

xdist-safe per anti-pattern #8 — no isinstance against private
classes; exception identity checked by ``type(exc).__name__``.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from sovyx.cli._startup_readiness import print_startup_readiness
from sovyx.engine.config import OX1Config
from sovyx.observability.envelope import SERVICE_INSTANCE_ID, EnvelopeProcessor

# ── OX1Config defaults ──────────────────────────────────────────────────────


class TestOX1ConfigDefaults:
    """Both OX-1.A flags default OFF (staged-adoption pledge)."""

    def test_startup_readiness_enabled_defaults_false(self) -> None:
        cfg = OX1Config()
        assert cfg.startup_readiness_enabled is False

    def test_session_id_in_logs_defaults_false(self) -> None:
        cfg = OX1Config()
        assert cfg.session_id_in_logs is False

    def test_env_var_enables_startup_readiness(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_OX1__STARTUP_READINESS_ENABLED", "true")
        cfg = OX1Config()
        assert cfg.startup_readiness_enabled is True

    def test_env_var_enables_session_id_in_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_OX1__SESSION_ID_IN_LOGS", "true")
        cfg = OX1Config()
        assert cfg.session_id_in_logs is True


# ── EnvelopeProcessor ───────────────────────────────────────────────────────


class TestEnvelopeProcessorSessionIdAlias:
    """``engine_session_id`` only appears when ``session_id_alias=True``."""

    def test_alias_absent_by_default(self) -> None:
        proc = EnvelopeProcessor()
        event_dict: dict[str, Any] = {}
        result = proc(logger=None, method_name="info", event_dict=event_dict)
        assert "engine_session_id" not in result
        # Canonical OTel key always present.
        assert result["service.instance.id"] == SERVICE_INSTANCE_ID

    def test_alias_present_when_enabled(self) -> None:
        proc = EnvelopeProcessor(session_id_alias=True)
        event_dict: dict[str, Any] = {}
        result = proc(logger=None, method_name="info", event_dict=event_dict)
        assert result["engine_session_id"] == SERVICE_INSTANCE_ID
        # Canonical and alias agree by construction.
        assert result["engine_session_id"] == result["service.instance.id"]

    def test_alias_does_not_overwrite_explicit_value(self) -> None:
        """Forwarded entries (anti-pattern #11) preserve their own value."""
        proc = EnvelopeProcessor(session_id_alias=True)
        event_dict: dict[str, Any] = {"engine_session_id": "forwarded-from-peer"}
        result = proc(logger=None, method_name="info", event_dict=event_dict)
        assert result["engine_session_id"] == "forwarded-from-peer"


# ── print_startup_readiness ─────────────────────────────────────────────────


class _StubMindConfig:
    """Minimal stand-in for :class:`MindConfig` — only the attributes
    the readiness helper reads."""

    def __init__(self, *, voice_enabled: bool = False, device: str | None = None) -> None:
        self.voice_enabled = voice_enabled
        self.voice = _StubVoice(device) if device is not None else None


class _StubVoice:
    def __init__(self, device: str) -> None:
        self.input_device_name = device


class _StubRouter:
    """Stub :class:`LLMRouter` exposing only ``discovery_report``."""

    def __init__(self, report: Any) -> None:  # noqa: ANN401
        self.discovery_report = report


class _StubReport:
    """Stub :class:`LLMRouterDiscoveryReport`."""

    def __init__(
        self,
        verdict: str,
        available: int,
        configured: int,
        default: str | None = "ollama",
    ) -> None:
        self.verdict = _StubVerdict(verdict)
        self.available_count = available
        self.configured_count = configured
        self.default_provider = default


class _StubVerdict:
    def __init__(self, value: str) -> None:
        self.value = value


class _StubBrain:
    def __init__(self, *, ready: bool) -> None:
        self.embedding_model_ready = ready


class _StubDegradedStore:
    def __init__(self, axes: list[str]) -> None:
        self._axes = axes

    def snapshot(self) -> list[Any]:
        return [_StubDegradedEntry(a) for a in self._axes]


class _StubDegradedEntry:
    def __init__(self, axis: str) -> None:
        self.axis = axis


class _StubAcksStore:
    def __init__(self, count: int) -> None:
        self._count = count

    async def list_active_acks(self) -> list[Any]:
        return [object()] * self._count


def _make_registry(**services: Any) -> Any:  # noqa: ANN401
    """Build a stub ``ServiceRegistry`` that resolves only the given
    types. Anything else raises a ``KeyError``-shaped Exception so
    the helper exercises its defensive ``except Exception`` paths.
    """
    from sovyx.brain.service import BrainService
    from sovyx.engine._degraded_store import EngineDegradedStore
    from sovyx.engine._operator_acks_store import OperatorAcksStore
    from sovyx.llm.router import LLMRouter

    type_map = {
        LLMRouter: services.get("llm"),
        BrainService: services.get("brain"),
        EngineDegradedStore: services.get("degraded"),
        OperatorAcksStore: services.get("acks"),
    }

    registry = AsyncMock()

    async def _resolve(cls: type) -> Any:  # noqa: ANN401
        instance = type_map.get(cls)
        if instance is None:
            raise LookupError(f"unregistered: {cls.__name__}")
        return instance

    registry.resolve = AsyncMock(side_effect=_resolve)
    return registry


def _capture_console() -> tuple[Console, io.StringIO]:
    """Return a (console, buffer) pair so tests can assert on rendered output."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=120)
    return console, buf


@pytest.mark.asyncio
async def test_all_healthy_renders_six_block() -> None:
    """Healthy boot: green LLM, voice enabled, embedding ready, 0
    degraded, no acks."""
    console, buf = _capture_console()
    registry = _make_registry(
        llm=_StubRouter(_StubReport("fully_available", 2, 2)),
        brain=_StubBrain(ready=True),
        degraded=_StubDegradedStore([]),
        acks=_StubAcksStore(0),
    )
    mind = _StubMindConfig(voice_enabled=True, device="USB Mic")

    await print_startup_readiness(console, registry, mind)

    output = buf.getvalue()
    assert "Startup readiness" in output
    assert "session=" in output and SERVICE_INSTANCE_ID[:12] in output
    assert "fully_available" in output
    assert "(2/2 providers" in output
    assert "USB Mic" in output
    assert "ready" in output
    assert "0 axes" in output
    assert "no active acks" in output


@pytest.mark.asyncio
async def test_llm_not_registered_degrades_gracefully() -> None:
    """LLM router unresolvable — helper renders ``unknown`` rather
    than raising."""
    console, buf = _capture_console()
    registry = _make_registry(
        brain=_StubBrain(ready=True),
        degraded=_StubDegradedStore([]),
        acks=_StubAcksStore(0),
    )  # no llm
    mind = _StubMindConfig(voice_enabled=False)

    await print_startup_readiness(console, registry, mind)

    output = buf.getvalue()
    assert "unknown" in output
    assert "router not registered" in output


@pytest.mark.asyncio
async def test_voice_disabled_renders_disabled() -> None:
    console, buf = _capture_console()
    registry = _make_registry(
        llm=_StubRouter(_StubReport("no_provider_configured", 0, 0, None)),
        brain=_StubBrain(ready=True),
        degraded=_StubDegradedStore([]),
        acks=_StubAcksStore(0),
    )
    mind = _StubMindConfig(voice_enabled=False)

    await print_startup_readiness(console, registry, mind)

    output = buf.getvalue()
    assert "Voice:" in output
    assert "disabled" in output


@pytest.mark.asyncio
async def test_voice_enabled_without_device_renders_dash() -> None:
    console, buf = _capture_console()
    registry = _make_registry(
        llm=_StubRouter(None),  # pre-first-tick (no report yet)
        brain=_StubBrain(ready=True),
        degraded=_StubDegradedStore([]),
        acks=_StubAcksStore(0),
    )
    mind = _StubMindConfig(voice_enabled=True)  # no device

    await print_startup_readiness(console, registry, mind)

    output = buf.getvalue()
    assert "enabled" in output
    assert "device=—" in output
    assert "no discovery report yet" in output


@pytest.mark.asyncio
async def test_brain_not_registered_degrades_gracefully() -> None:
    console, buf = _capture_console()
    registry = _make_registry(
        llm=_StubRouter(_StubReport("fully_available", 1, 1)),
        degraded=_StubDegradedStore([]),
        acks=_StubAcksStore(0),
    )  # no brain
    mind = _StubMindConfig(voice_enabled=False)

    await print_startup_readiness(console, registry, mind)

    output = buf.getvalue()
    assert "Embedding:" in output
    assert "unknown" in output
    assert "brain not registered" in output


@pytest.mark.asyncio
async def test_multi_degraded_and_acks_render_red_and_count() -> None:
    """3 distinct axes degraded + 2 active acks → red ``3 axes`` plus
    ``2 active acks`` rendered."""
    console, buf = _capture_console()
    registry = _make_registry(
        llm=_StubRouter(_StubReport("partial_health", 1, 2)),
        brain=_StubBrain(ready=False),
        degraded=_StubDegradedStore(["voice", "llm", "engine_resources"]),
        acks=_StubAcksStore(2),
    )
    mind = _StubMindConfig(voice_enabled=True, device="—")

    await print_startup_readiness(console, registry, mind)

    output = buf.getvalue()
    assert "3 axes" in output
    assert "2 active acks" in output
    assert "partial_health" in output
    assert "not ready" in output
    assert "FTS5 fallback" in output


@pytest.mark.asyncio
async def test_every_subsystem_raises_still_returns_cleanly() -> None:
    """Worst case: every resolve raises. Helper must still print 6
    lines and return without raising."""
    console, buf = _capture_console()
    registry = _make_registry()  # nothing registered → all raise
    mind = _StubMindConfig(voice_enabled=False)

    # MUST NOT raise.
    await print_startup_readiness(console, registry, mind)

    output = buf.getvalue()
    assert "Startup readiness" in output
    assert "LLM:" in output
    assert "Voice:" in output
    assert "Embedding:" in output
    assert "Degraded:" in output


@pytest.mark.asyncio
async def test_session_id_prefix_matches_envelope_constant() -> None:
    """The rendered session-id prefix MUST equal the first 12 hex
    chars of :data:`SERVICE_INSTANCE_ID` (cross-reference invariant
    for log grep)."""
    console, buf = _capture_console()
    registry = _make_registry(
        llm=_StubRouter(_StubReport("fully_available", 1, 1)),
        brain=_StubBrain(ready=True),
        degraded=_StubDegradedStore([]),
        acks=_StubAcksStore(0),
    )
    mind = _StubMindConfig(voice_enabled=False)

    await print_startup_readiness(console, registry, mind)

    assert SERVICE_INSTANCE_ID[:12] in buf.getvalue()
