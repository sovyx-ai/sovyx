"""Integration test for v0.31.4 GAP 4 — voice auto-resume on daemon boot.

The contract: when ``MindConfig.voice_enabled=True`` is persisted from a
prior session, ``sovyx start`` MUST reconstruct the voice pipeline
without requiring the operator to ``POST /api/voice/enable`` again.

This test pins ``_auto_resume_voice_pipeline`` to the actual factory
signature so a future kwarg rename in :func:`create_voice_pipeline`
breaks at CI time, not in production at the operator's first restart.

v0.31.6 paranoid-closure T1.2 (C2) adds failure-path tests: a
``start()`` raise must NOT leak a zombie pipeline into the registry,
and the helper must best-effort tear down the bundle before re-raising.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.engine import bootstrap
from sovyx.voice.factory import create_voice_pipeline


class TestAutoResumeKwargContract:
    """The auto-resume call site uses only kwargs the factory accepts."""

    def test_every_kwarg_passed_exists_on_factory(self) -> None:
        factory_params = set(inspect.signature(create_voice_pipeline).parameters.keys())
        # Mirrors src/sovyx/engine/bootstrap.py::_auto_resume_voice_pipeline
        # — keep these two lists synchronised when the factory signature
        # changes.
        expected_kwargs = {
            "data_dir",
            "mind_id",
            "language",
            "voice_id",
            "wake_word_enabled",
            "input_device_name",
            "input_device_host_api",
            "allow_inoperative_capture",
            # v0.31.7 T2.1 (M1): auto-resume now wires the cognitive
            # bridge + event bus the same way the HTTP enable path does,
            # so the factory call site references both kwargs.
            "event_bus",
            "on_perception",
        }
        missing = expected_kwargs - factory_params
        assert not missing, (
            f"_auto_resume_voice_pipeline passes kwargs that don't exist on "
            f"create_voice_pipeline: {missing}. Update bootstrap.py to match "
            f"the new factory signature, OR add the missing parameters back "
            f"to the factory."
        )

    def test_module_exports_auto_resume_helper(self) -> None:
        """Renaming the helper would silently break the bootstrap call site."""
        assert hasattr(bootstrap, "_auto_resume_voice_pipeline")
        assert inspect.iscoroutinefunction(bootstrap._auto_resume_voice_pipeline)


def _make_bundle(
    *,
    start_exc: Exception | None = None,
    wake_word_enabled: bool = False,
    boot_preflight_warnings: tuple[dict[str, object], ...] = (),
) -> SimpleNamespace:
    """Build a fake voice bundle with mockable pipeline + capture_task.

    ``start_exc`` — if set, ``capture_task.start()`` raises it. Otherwise
    start succeeds (no-op).

    The pipeline carries the sub-component handles (``vad``, ``stt``,
    ``tts``, ``wake_word``) the route handler reads in
    ``_enable_voice_locked`` so the v0.31.7 T2.1 auto-resume mirror
    test can assert all sub-components reach the registry.
    """
    capture_task = SimpleNamespace(
        start=AsyncMock(side_effect=start_exc) if start_exc else AsyncMock(),
        stop=AsyncMock(),
    )
    pipeline = SimpleNamespace(
        stop=AsyncMock(),
        vad=SimpleNamespace(name="silero-vad-mock"),
        stt=SimpleNamespace(name="moonshine-stt-mock"),
        tts=SimpleNamespace(name="piper-tts-mock"),
        wake_word=SimpleNamespace(name="wake-word-mock"),
        config=SimpleNamespace(wake_word_enabled=wake_word_enabled),
    )
    return SimpleNamespace(
        pipeline=pipeline,
        capture_task=capture_task,
        boot_preflight_warnings=boot_preflight_warnings,
    )


def _make_mind_config() -> SimpleNamespace:
    return SimpleNamespace(
        id="test-mind",
        language="en",
        voice_id="test-voice",
        wake_word_enabled=False,
        voice_input_device_name=None,
        voice_input_device_host_api=None,
        voice_enabled=True,
    )


def _make_engine_config(tmp_path: Path) -> SimpleNamespace:
    # Mirror the real EngineConfig.tuning.voice shape the bootstrap reads
    # (W2.1 STT-failover gate). Default-OFF so these tests exercise the
    # no-failover path; the real pydantic config always has these fields.
    return SimpleNamespace(
        data_dir=tmp_path,
        tuning=SimpleNamespace(
            voice=SimpleNamespace(
                stt_failover_enabled=False,
                cloud_stt_timeout_seconds=30.0,
            ),
        ),
    )


class TestAutoResumeFailurePath:
    """v0.31.6 T1.2 (C2): start() failure must not corrupt the registry."""

    @pytest.mark.asyncio
    async def test_start_failure_does_not_leak_zombie(self, tmp_path: Path) -> None:
        """``start()`` raising leaves the registry untouched."""
        bundle = _make_bundle(start_exc=OSError("device unplugged"))

        # Registry is a strict mock: ``replace_instance`` MUST NOT be
        # called when start() fails. ``is_registered`` reflects that
        # the slots stay empty post-call.
        registry = MagicMock()
        registry.replace_instance = AsyncMock()
        registry.is_registered = MagicMock(return_value=False)

        mind_config = _make_mind_config()
        engine_config = _make_engine_config(tmp_path)

        with (
            patch.object(
                bootstrap,
                "_auto_resume_voice_pipeline",
                wraps=bootstrap._auto_resume_voice_pipeline,
            ),
            patch("sovyx.voice.factory.create_voice_pipeline", AsyncMock(return_value=bundle)),
            pytest.raises(OSError, match="device unplugged"),
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=mind_config,  # type: ignore[arg-type]
                engine_config=engine_config,  # type: ignore[arg-type]
                registry=registry,
            )

        registry.replace_instance.assert_not_awaited()

        # Importing the actual interfaces is fine — registry mock
        # returns False for both regardless of arg, but assert calls
        # we'd expect downstream code to make.
        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        assert registry.is_registered(VoicePipeline) is False
        assert registry.is_registered(AudioCaptureTask) is False

    @pytest.mark.asyncio
    async def test_start_failure_attempts_cleanup_best_effort(self, tmp_path: Path) -> None:
        """Cleanup runs on both pipeline and capture_task even if it raises."""
        bundle = _make_bundle(start_exc=RuntimeError("model load OOM"))
        # Cleanup itself raises — the original error MUST still propagate.
        bundle.capture_task.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        bundle.pipeline.stop = AsyncMock(side_effect=RuntimeError("pipeline stop failed"))

        registry = MagicMock()
        registry.replace_instance = AsyncMock()
        # v0.31.7 T2.1: auto-resume now probes the registry for
        # ``EventBus`` + ``CognitiveLoop`` BEFORE the factory call.
        # Both default-False so the start-failure path remains the
        # only thing under test.
        registry.is_registered = MagicMock(return_value=False)
        registry.resolve = AsyncMock()

        mind_config = _make_mind_config()
        engine_config = _make_engine_config(tmp_path)

        with (
            patch("sovyx.voice.factory.create_voice_pipeline", AsyncMock(return_value=bundle)),
            pytest.raises(RuntimeError, match="model load OOM"),
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=mind_config,  # type: ignore[arg-type]
                engine_config=engine_config,  # type: ignore[arg-type]
                registry=registry,
            )

        # Both teardown handles attempted exactly once each.
        bundle.capture_task.stop.assert_awaited_once()
        bundle.pipeline.stop.assert_awaited_once()
        # Registry remains untouched.
        registry.replace_instance.assert_not_awaited()


class TestAutoResumeFullSubComponentRegistration:
    """v0.31.7 paranoid-closure T2.1 (M1).

    Pre-v0.31.7 ``_auto_resume_voice_pipeline`` only published
    ``VoicePipeline`` and ``AudioCaptureTask`` into the registry.
    The HTTP enable path at
    ``dashboard/routes/voice.py::_enable_voice_locked`` published
    five MORE: ``SileroVAD``, ``STTEngine``, ``TTSEngine``,
    ``WakeWordDetector`` (when enabled), ``VoiceCognitiveBridge``,
    plus the ``BootPreflightWarningsStore`` (publish-or-refresh).
    Symptom: ``/api/voice/status`` reported "No engine configured"
    after auto-resume, breaking dashboard introspection AND making
    any ``await registry.resolve(STTEngine)`` call crash.
    These tests assert the FULL set lands in the registry exactly
    like the route handler does.
    """

    @pytest.mark.asyncio
    async def test_auto_resume_registers_full_sub_component_set(self, tmp_path: Path) -> None:
        """Auto-resume registers all 7 components when cogloop is wired."""
        bundle = _make_bundle(wake_word_enabled=True)

        # Track the exact ordered list of types passed to ``replace_instance``
        # so we can also assert the sequence matches the HTTP route's order
        # (just in case any future caller depends on an ordering invariant).
        replaced_types: list[type] = []

        async def _record(type_: type, _instance: object) -> None:
            replaced_types.append(type_)

        registry = MagicMock()
        registry.replace_instance = AsyncMock(side_effect=_record)
        # Cognitive loop + event bus must resolve so the bridge is built.
        from sovyx.cognitive.loop import CognitiveLoop
        from sovyx.engine.events import EventBus
        from sovyx.voice.health import BootPreflightWarningsStore

        registered_types = {EventBus, CognitiveLoop}

        def _is_registered(t: type) -> bool:
            return t in registered_types

        registry.is_registered = MagicMock(side_effect=_is_registered)
        registry.resolve = AsyncMock(
            side_effect=lambda t: MagicMock() if t in registered_types else MagicMock()
        )
        registry.register_instance = MagicMock()

        mind_config = _make_mind_config()
        # Enable wake-word so its registration path is exercised.
        mind_config.wake_word_enabled = True
        # MindConfig.llm.streaming used to choose bridge mode.
        mind_config.llm = SimpleNamespace(streaming=True)
        engine_config = _make_engine_config(tmp_path)

        with patch(
            "sovyx.voice.factory.create_voice_pipeline",
            AsyncMock(return_value=bundle),
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=mind_config,  # type: ignore[arg-type]
                engine_config=engine_config,  # type: ignore[arg-type]
                registry=registry,
            )

        # The 7 component types the HTTP route registers, in the
        # order ``_enable_voice_locked`` calls ``replace_instance``.
        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.cognitive_bridge import VoiceCognitiveBridge
        from sovyx.voice.pipeline._orchestrator import VoicePipeline
        from sovyx.voice.stt import STTEngine
        from sovyx.voice.tts_piper import TTSEngine
        from sovyx.voice.vad import SileroVAD
        from sovyx.voice.wake_word import WakeWordDetector

        expected_order = [
            VoicePipeline,
            AudioCaptureTask,
            SileroVAD,
            STTEngine,
            TTSEngine,
            WakeWordDetector,
            VoiceCognitiveBridge,
        ]
        assert replaced_types == expected_order, (
            f"auto-resume registration set diverged from HTTP enable path: "
            f"got {[t.__name__ for t in replaced_types]}, expected "
            f"{[t.__name__ for t in expected_order]}"
        )
        # BootPreflightWarningsStore is published via ``register_instance``
        # (not ``replace_instance``) on first creation — same pattern
        # the route uses.
        store_calls = [
            call
            for call in registry.register_instance.call_args_list
            if call.args and call.args[0] is BootPreflightWarningsStore
        ]
        assert len(store_calls) == 1, (
            f"BootPreflightWarningsStore should be registered exactly once "
            f"on first auto-resume; got {len(store_calls)} calls"
        )
        assert isinstance(store_calls[0].args[1], BootPreflightWarningsStore)

    @pytest.mark.asyncio
    async def test_auto_resume_skips_wake_word_when_disabled(self, tmp_path: Path) -> None:
        """``WakeWordDetector`` registration is gated on
        ``pipeline.config.wake_word_enabled`` — same conditional the
        HTTP route uses (``if bundle.pipeline.config.wake_word_enabled``).
        """
        bundle = _make_bundle(wake_word_enabled=False)

        replaced_types: list[type] = []

        async def _record(type_: type, _instance: object) -> None:
            replaced_types.append(type_)

        registry = MagicMock()
        registry.replace_instance = AsyncMock(side_effect=_record)
        registry.is_registered = MagicMock(return_value=False)  # no cogloop, no bus
        registry.register_instance = MagicMock()

        mind_config = _make_mind_config()
        engine_config = _make_engine_config(tmp_path)

        with patch(
            "sovyx.voice.factory.create_voice_pipeline",
            AsyncMock(return_value=bundle),
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=mind_config,  # type: ignore[arg-type]
                engine_config=engine_config,  # type: ignore[arg-type]
                registry=registry,
            )

        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.cognitive_bridge import VoiceCognitiveBridge
        from sovyx.voice.pipeline._orchestrator import VoicePipeline
        from sovyx.voice.stt import STTEngine
        from sovyx.voice.tts_piper import TTSEngine
        from sovyx.voice.vad import SileroVAD
        from sovyx.voice.wake_word import WakeWordDetector

        # No wake-word + no cogloop ⇒ neither WakeWordDetector nor
        # VoiceCognitiveBridge get published.
        assert WakeWordDetector not in replaced_types
        assert VoiceCognitiveBridge not in replaced_types
        # The audio-engine quintet (pipeline + capture + vad + stt + tts)
        # always lands.
        assert replaced_types == [
            VoicePipeline,
            AudioCaptureTask,
            SileroVAD,
            STTEngine,
            TTSEngine,
        ]

    @pytest.mark.asyncio
    async def test_auto_resume_registers_components_only_after_start_succeeds(
        self, tmp_path: Path
    ) -> None:
        """T1.2 contract: registry mutation runs strictly AFTER ``start()``
        returns. A ``start()`` raise must leave the registry untouched
        across the FULL sub-component set, not just the original two.
        """
        bundle = _make_bundle(start_exc=OSError("usb pulled"), wake_word_enabled=True)

        registry = MagicMock()
        registry.replace_instance = AsyncMock()
        registry.is_registered = MagicMock(return_value=False)
        registry.register_instance = MagicMock()

        mind_config = _make_mind_config()
        engine_config = _make_engine_config(tmp_path)

        with (
            patch(
                "sovyx.voice.factory.create_voice_pipeline",
                AsyncMock(return_value=bundle),
            ),
            pytest.raises(OSError, match="usb pulled"),
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=mind_config,  # type: ignore[arg-type]
                engine_config=engine_config,  # type: ignore[arg-type]
                registry=registry,
            )

        registry.replace_instance.assert_not_awaited()
        registry.register_instance.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_resume_partial_failure_does_not_rollback(self, tmp_path: Path) -> None:
        """T1.2 contract documented behaviour: if ``start()`` already
        succeeded but a later ``replace_instance`` raises, the
        previously-registered instances STAY in the registry. The
        helper does not attempt rollback (capture is already alive
        producing frames; partial registration > losing a working
        pipeline back to clean slate).
        """
        bundle = _make_bundle(wake_word_enabled=False)

        replaced_types: list[type] = []

        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.pipeline._orchestrator import VoicePipeline
        from sovyx.voice.stt import STTEngine
        from sovyx.voice.vad import SileroVAD

        # ``STTEngine`` is the 4th call in the registration order. We
        # raise BEFORE recording it so the recorded list is the
        # last-known-good prefix [VoicePipeline, AudioCaptureTask, SileroVAD]
        # — the assertion below is on what was successfully published
        # to the registry, not what was attempted.
        async def _record_then_fail(type_: type, _instance: object) -> None:
            if type_ is STTEngine:
                msg = "registry corrupted mid-publish"
                raise RuntimeError(msg)
            replaced_types.append(type_)

        registry = MagicMock()
        registry.replace_instance = AsyncMock(side_effect=_record_then_fail)
        registry.is_registered = MagicMock(return_value=False)
        registry.register_instance = MagicMock()

        mind_config = _make_mind_config()
        engine_config = _make_engine_config(tmp_path)

        with (
            patch(
                "sovyx.voice.factory.create_voice_pipeline",
                AsyncMock(return_value=bundle),
            ),
            pytest.raises(RuntimeError, match="registry corrupted mid-publish"),
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=mind_config,  # type: ignore[arg-type]
                engine_config=engine_config,  # type: ignore[arg-type]
                registry=registry,
            )

        # The 3 successful publishes are recorded; nothing was rolled back.
        assert replaced_types == [VoicePipeline, AudioCaptureTask, SileroVAD]
        # capture_task / pipeline ``stop()`` were NOT called — the helper
        # does not tear down post-start partial-publish failures.
        bundle.capture_task.stop.assert_not_called()
        bundle.pipeline.stop.assert_not_called()


_DROP_EVENT = "voice.perception_dropped_bridge_not_ready"


class TestOnPerceptionBridgeNotReady:
    """D2 boot race — a transcript arriving before the cognitive bridge is
    wired must emit an operator-actionable WARN (never vanish silently),
    and the bridge is now constructed BEFORE ``capture_task.start()`` so
    the window is structurally closed.
    """

    @pytest.fixture(autouse=True)
    def _structlog_stdlib_routing(self) -> Generator[None, None, None]:
        """Route structlog through stdlib logging so ``caplog`` observes the
        WARN — same pattern as ``tests/unit/voice/conftest.py``."""
        from sovyx.engine.config import LoggingConfig
        from sovyx.observability.logging import setup_logging

        setup_logging(LoggingConfig(level="DEBUG", console_format="json", log_file=None))
        yield

    @staticmethod
    def _registry_with_cogloop() -> MagicMock:
        from sovyx.cognitive.loop import CognitiveLoop

        registry = MagicMock()
        registry.replace_instance = AsyncMock()
        registry.is_registered = MagicMock(side_effect=lambda t: t is CognitiveLoop)
        registry.resolve = AsyncMock(return_value=MagicMock(spec=CognitiveLoop))
        registry.register_instance = MagicMock()
        return registry

    async def _capture_on_perception(self, tmp_path: Path) -> object:
        """Run auto-resume with a factory that captures ``on_perception``
        then aborts — reproducing the exact pre-bridge boot window (the
        callback exists, ``bridge_ref[0]`` is still None)."""
        captured: dict[str, object] = {}

        async def _capture_and_abort(**kwargs: object) -> None:
            captured.update(kwargs)
            raise RuntimeError("abort after capturing on_perception")

        with (
            patch("sovyx.voice.factory.create_voice_pipeline", _capture_and_abort),
            pytest.raises(Exception) as exc_info,
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=_make_mind_config(),  # type: ignore[arg-type]
                engine_config=_make_engine_config(tmp_path),  # type: ignore[arg-type]
                registry=self._registry_with_cogloop(),
            )
        assert type(exc_info.value).__name__ == "RuntimeError"
        on_perception = captured["on_perception"]
        assert callable(on_perception)
        return on_perception

    @staticmethod
    def _drop_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
        return [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == _DROP_EVENT
        ]

    @pytest.mark.asyncio
    async def test_transcript_before_bridge_wired_warns_and_drops(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bridge still None → structured WARN with mind_id + text length
        (never the transcript itself — privacy) and NO exception."""
        on_perception = await self._capture_on_perception(tmp_path)

        caplog.set_level(logging.WARNING, logger="sovyx.engine.bootstrap")
        transcript = "hello sovyx boot race"
        await on_perception(transcript, "test-mind")  # type: ignore[operator]

        records = self._drop_records(caplog)
        assert len(records) == 1
        payload = records[0]
        assert payload["mind_id"] == "test-mind"
        assert payload["text_length"] == len(transcript)
        assert "action_required" in payload
        # Privacy — the transcript text itself is never logged.
        assert transcript not in str(payload)

    @pytest.mark.asyncio
    async def test_empty_transcript_dropped_without_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Whitespace-only transcriptions are a normal condition — dropped
        with NO bridge-not-ready WARN (they'd be dropped bridge or not)."""
        on_perception = await self._capture_on_perception(tmp_path)

        caplog.set_level(logging.WARNING, logger="sovyx.engine.bootstrap")
        await on_perception("   ", "test-mind")  # type: ignore[operator]

        assert self._drop_records(caplog) == []

    @pytest.mark.asyncio
    async def test_bridge_wired_before_capture_start(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Structural closure: a transcript arriving DURING capture start
        (the old race window) now reaches the bridge — no drop, no WARN."""
        import sovyx.voice.cognitive_bridge as cb_mod

        processed: list[object] = []

        class _StubBridge:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def process(self, req: object) -> None:
                processed.append(req)

        captured: dict[str, object] = {}
        bundle = _make_bundle()

        async def _fake_factory(**kwargs: object) -> SimpleNamespace:
            captured.update(kwargs)
            return bundle

        async def _speak_during_start() -> None:
            await captured["on_perception"]("hi during boot", "test-mind")  # type: ignore[operator]

        bundle.capture_task.start = AsyncMock(side_effect=_speak_during_start)

        caplog.set_level(logging.WARNING, logger="sovyx.engine.bootstrap")
        with (
            patch("sovyx.voice.factory.create_voice_pipeline", _fake_factory),
            patch.object(cb_mod, "VoiceCognitiveBridge", _StubBridge),
        ):
            await bootstrap._auto_resume_voice_pipeline(
                mind_config=_make_mind_config(),  # type: ignore[arg-type]
                engine_config=_make_engine_config(tmp_path),  # type: ignore[arg-type]
                registry=self._registry_with_cogloop(),
            )

        assert len(processed) == 1
        assert processed[0].perception.content == "hi during boot"  # type: ignore[attr-defined]
        assert self._drop_records(caplog) == []
