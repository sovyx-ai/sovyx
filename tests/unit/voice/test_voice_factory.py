"""Tests for sovyx.voice.factory — voice pipeline factory."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.factory import (
    VoiceBundle,
    VoiceFactoryError,
    _create_kokoro_tts,
    _create_wake_word_stub,
)
from sovyx.voice.factory import _validate as factory_validate_mod


class TestVoiceFactoryError:
    """VoiceFactoryError stores missing_models."""

    def test_message(self) -> None:
        err = VoiceFactoryError("no tts")
        assert str(err) == "no tts"
        assert err.missing_models == []

    def test_with_missing_models(self) -> None:
        models = [{"name": "piper-tts", "install_command": "pip install piper-tts"}]
        err = VoiceFactoryError("missing", missing_models=models)
        assert err.missing_models == models


class TestCreateWakeWordStub:
    """_create_wake_word_stub returns a usable no-op object."""

    def test_stub_returns_object(self) -> None:
        stub = _create_wake_word_stub()
        assert stub is not None

    def test_stub_process_frame(self) -> None:
        stub = _create_wake_word_stub()
        result = stub.process_frame(b"\x00" * 1024)
        assert result.detected is False


class TestCreateSttLanguagePassthrough:
    """Bug #1 (Mission ``MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
    §Phase 1 T1.1) — :func:`_create_stt` MUST forward the requested
    language into :class:`MoonshineConfig` instead of silently dropping
    it.

    Forensic anchor: ``c:\\Users\\guipe\\Downloads\\logs_01.txt`` line
    855 logs ``voice_factory_creating_stt language=pt-br`` immediately
    followed by line 857 ``Initializing MoonshineSTT language=en`` —
    the parameter was suppressed by a stale ``# noqa: ANN401, ARG001``
    pin and never reached the engine.

    The fix's safety contract: Moonshine v2 only ships
    ``ar/en/es/ja/ko/uk/vi/zh``; an unsupported language MUST be coerced
    to English with a structured WARN (rather than crashing
    ``await stt.initialize()`` with ``ValueError("Language not found:
    pt-br ...")``). These tests pin both the forwarding behaviour and
    the safety contract.
    """

    _VALIDATE_LOGGER = "sovyx.voice.factory._validate"

    def test_supported_language_is_forwarded_to_moonshine_config(self) -> None:
        with patch("sovyx.voice.stt.MoonshineSTT") as mock_stt_cls:
            mock_stt_cls.return_value = MagicMock(name="stt")
            factory_validate_mod._create_stt("es")
            mock_stt_cls.assert_called_once()
            kwargs = mock_stt_cls.call_args.kwargs
            assert "config" in kwargs
            assert kwargs["config"].language == "es"

    def test_supported_language_emits_wired_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=self._VALIDATE_LOGGER)
        with patch("sovyx.voice.stt.MoonshineSTT") as mock_stt_cls:
            mock_stt_cls.return_value = MagicMock(name="stt")
            factory_validate_mod._create_stt("ja")
        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == "voice.factory.stt_language_wired"
        ]
        assert len(events) == 1, f"expected one wired event, got {[r.msg for r in caplog.records]}"
        assert events[0]["voice.language"] == "ja"
        assert events[0]["voice.engine"] == "moonshine"

    def test_unsupported_language_coerces_to_english(self) -> None:
        with patch("sovyx.voice.stt.MoonshineSTT") as mock_stt_cls:
            mock_stt_cls.return_value = MagicMock(name="stt")
            factory_validate_mod._create_stt("pt-br")
            kwargs = mock_stt_cls.call_args.kwargs
            assert kwargs["config"].language == "en"

    def test_unsupported_language_emits_warn_with_actionable_remediation(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger=self._VALIDATE_LOGGER)
        with patch("sovyx.voice.stt.MoonshineSTT") as mock_stt_cls:
            mock_stt_cls.return_value = MagicMock(name="stt")
            factory_validate_mod._create_stt("pt-br")
        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.factory.stt_language_unsupported"
        ]
        assert len(events) == 1
        evt = events[0]
        assert evt["voice.requested_language"] == "pt-br"
        assert evt["voice.coerced_language"] == "en"
        assert evt["voice.engine"] == "moonshine"
        # Operator-actionable remediation: must list supported languages
        # AND mention that English will be used until they fix it.
        action = evt["voice.action_required"]
        assert "Moonshine" in action
        assert "English" in action
        for code in ("ar", "en", "es", "ja", "ko", "uk", "vi", "zh"):
            assert code in action

    def test_empty_language_defaults_to_english_back_compat(self) -> None:
        # Legacy test stubs at lines 71/113/150/334/513/568/864 of this
        # file pre-T1.1 used ``factory_mod._create_stt = lambda *a, **kw:
        # fake_stt`` — the lambda accepts both no-args and arbitrary
        # args. The new ``language: str = "en"`` default keeps zero-arg
        # callers wired to English without breaking them.
        with patch("sovyx.voice.stt.MoonshineSTT") as mock_stt_cls:
            mock_stt_cls.return_value = MagicMock(name="stt")
            factory_validate_mod._create_stt()
            kwargs = mock_stt_cls.call_args.kwargs
            assert kwargs["config"].language == "en"

    def test_uppercase_language_is_normalised(self) -> None:
        # Operators may type ``EN`` or ``En`` in mind.yaml; the
        # case-insensitive normalisation prevents a silent miss
        # against the lowercase frozenset.
        with patch("sovyx.voice.stt.MoonshineSTT") as mock_stt_cls:
            mock_stt_cls.return_value = MagicMock(name="stt")
            factory_validate_mod._create_stt("EN")
            kwargs = mock_stt_cls.call_args.kwargs
            assert kwargs["config"].language == "en"

    def test_whitespace_language_is_stripped(self) -> None:
        with patch("sovyx.voice.stt.MoonshineSTT") as mock_stt_cls:
            mock_stt_cls.return_value = MagicMock(name="stt")
            factory_validate_mod._create_stt("  es  ")
            kwargs = mock_stt_cls.call_args.kwargs
            assert kwargs["config"].language == "es"


# Mission ``MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
# §Phase 1 T1.2 — defense-in-depth WARN when ``create_voice_pipeline``
# is invoked with the literal ``mind_id="default"`` sentinel.
#
# The WARN sits inline at ``voice/factory/__init__.py`` just before the
# ``VoicePipelineConfig(...)`` construction. It is purely observability
# — fires only when SOMEONE bypasses the canonical dashboard resolver
# (``sovyx.dashboard._shared.resolve_active_mind_id_for_request``) and
# hits the factory directly with the sentinel. The behavioural fix
# lives in the dashboard route (covered by
# ``TestEnableVoiceMindIdResolution`` in ``test_voice_routes.py``).
# An integration test that drives the full async ``create_voice_pipeline``
# would be ~200 LOC of fixtures for a 5-line WARN — the cost / value
# does not justify it. Code review + the production-path integration
# tests are the right coverage gate here.


class TestFactoryInitializesSTT:
    """Regression: MoonshineSTT.initialize() is called during pipeline factory.

    Before this was wired up, :func:`create_voice_pipeline` constructed
    a :class:`MoonshineSTT` but left it in ``STTState.UNINITIALIZED`` —
    every VAD-triggered transcribe() then raised ``RuntimeError("STT not
    initialized")`` and the utterance was silently dropped. This test
    guards the factory contract so the regression cannot recur.
    """

    @pytest.mark.asyncio()
    async def test_initialize_called_before_pipeline_start(self, tmp_path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        import sovyx.voice.factory as factory_mod

        fake_pipeline = MagicMock()
        fake_pipeline.start = AsyncMock()

        fake_stt = MagicMock()
        fake_stt.initialize = AsyncMock()
        fake_stt.state = None  # skip STTState.READY assertion branch

        original_create_vad = factory_mod._create_vad
        original_create_stt = factory_mod._create_stt
        original_create_piper = factory_mod._create_piper_tts
        factory_mod._create_vad = lambda *a, **kw: MagicMock()
        factory_mod._create_stt = lambda *a, **kw: fake_stt
        factory_mod._create_piper_tts = lambda *a, **kw: MagicMock()

        try:
            with (
                patch.object(
                    factory_mod, "ensure_silero_vad", new=AsyncMock(return_value=vad_file)
                ),
                patch.object(factory_mod, "detect_tts_engine", return_value="piper"),
                patch("sovyx.voice.device_enum.resolve_device", return_value=None),
                patch(
                    "sovyx.voice.pipeline._orchestrator.VoicePipeline",
                    return_value=fake_pipeline,
                ),
            ):
                await factory_mod.create_voice_pipeline(model_dir=tmp_path)

            fake_stt.initialize.assert_awaited_once()
        finally:
            factory_mod._create_vad = original_create_vad
            factory_mod._create_stt = original_create_stt
            factory_mod._create_piper_tts = original_create_piper

    @pytest.mark.asyncio()
    async def test_not_ready_state_raises_voice_factory_error(self, tmp_path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sovyx.voice.stt import STTState

        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        import sovyx.voice.factory as factory_mod

        fake_stt = MagicMock()
        fake_stt.initialize = AsyncMock()
        fake_stt.state = STTState.UNINITIALIZED  # pretend initialize failed silently

        original_create_vad = factory_mod._create_vad
        original_create_stt = factory_mod._create_stt
        original_create_piper = factory_mod._create_piper_tts
        factory_mod._create_vad = lambda *a, **kw: MagicMock()
        factory_mod._create_stt = lambda *a, **kw: fake_stt
        factory_mod._create_piper_tts = lambda *a, **kw: MagicMock()

        try:
            with (
                patch.object(
                    factory_mod, "ensure_silero_vad", new=AsyncMock(return_value=vad_file)
                ),
                patch.object(factory_mod, "detect_tts_engine", return_value="piper"),
                patch("sovyx.voice.device_enum.resolve_device", return_value=None),
                pytest.raises(Exception) as exc_info,
            ):
                await factory_mod.create_voice_pipeline(model_dir=tmp_path)
            assert type(exc_info.value).__name__ == "VoiceFactoryError"
            assert "STTState.READY" in str(exc_info.value)
        finally:
            factory_mod._create_vad = original_create_vad
            factory_mod._create_stt = original_create_stt
            factory_mod._create_piper_tts = original_create_piper


class TestFactoryContract:
    """Verify factory raises VoiceFactoryError when no TTS available."""

    @pytest.mark.asyncio()
    async def test_no_tts_raises(self, tmp_path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        import sovyx.voice.factory as factory_mod

        original_create_vad = factory_mod._create_vad
        original_create_stt = factory_mod._create_stt

        factory_mod._create_vad = lambda *a, **kw: MagicMock()
        fake_stt = MagicMock()
        fake_stt.initialize = AsyncMock()
        fake_stt.state = None  # skip STTState.READY assertion branch
        factory_mod._create_stt = lambda *a, **kw: fake_stt
        try:
            with (
                patch.object(
                    factory_mod, "ensure_silero_vad", new=AsyncMock(return_value=vad_file)
                ),
                patch.object(factory_mod, "detect_tts_engine", return_value="none"),
                pytest.raises(Exception) as exc_info,
            ):
                await factory_mod.create_voice_pipeline(model_dir=tmp_path)
            assert type(exc_info.value).__name__ == "VoiceFactoryError"
        finally:
            factory_mod._create_vad = original_create_vad
            factory_mod._create_stt = original_create_stt


class TestVoiceBundle:
    """VoiceBundle wraps the pipeline and its capture task."""

    def test_fields(self) -> None:
        from unittest.mock import MagicMock

        pipeline = MagicMock()
        capture = MagicMock()
        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)
        assert bundle.pipeline is pipeline
        assert bundle.capture_task is capture

    def test_v13_boot_preflight_warnings_default_empty(self) -> None:
        """v1.3 §4.6 introduced ``boot_preflight_warnings``; unset → ()."""
        from unittest.mock import MagicMock

        bundle = VoiceBundle(pipeline=MagicMock(), capture_task=MagicMock())
        assert bundle.boot_preflight_warnings == ()

    def test_v13_boot_preflight_warnings_carried(self) -> None:
        from unittest.mock import MagicMock

        warnings = (
            {"code": "linux_mixer_saturated", "hint": "reset"},
            {"code": "other", "hint": "-"},
        )
        bundle = VoiceBundle(
            pipeline=MagicMock(),
            capture_task=MagicMock(),
            boot_preflight_warnings=warnings,
        )
        assert bundle.boot_preflight_warnings == warnings


class TestRunBootPreflight:
    """v1.3 §4.6 L6 — ``_run_boot_preflight`` aggregates step 9 failures."""

    @pytest.mark.asyncio()
    async def test_passing_check_returns_empty_list(self) -> None:
        """Non-Linux (or Linux + clean mixer) produces no warnings."""
        from unittest.mock import patch

        import sovyx.voice.factory as factory_mod
        import sovyx.voice.health as voice_health_mod
        from sovyx.engine.config import VoiceTuningConfig

        async def _clean() -> tuple[bool, str, dict[str, object]]:
            return True, "", {"platform": "darwin", "skipped": True}

        # _run_boot_preflight imports ``check_linux_mixer_sanity`` from
        # ``sovyx.voice.health`` (the re-export namespace) inside the
        # function body, so the stable patch target is the attribute on
        # that alias, not on the underlying ``_linux_mixer_check`` module
        # (CLAUDE.md §11).
        with patch.object(
            voice_health_mod,
            "check_linux_mixer_sanity",
            return_value=_clean,
        ):
            warnings = await factory_mod._run_boot_preflight(tuning=VoiceTuningConfig())
        assert warnings == []

    @pytest.mark.asyncio()
    async def test_failing_check_produces_warning_dict(self) -> None:
        from unittest.mock import patch

        import sovyx.voice.factory as factory_mod
        import sovyx.voice.health as voice_health_mod
        from sovyx.engine.config import VoiceTuningConfig

        async def _saturated() -> tuple[bool, str, dict[str, object]]:
            return (
                False,
                "mixer saturated; run --fix",
                {"aggregated_boost_db": 42.0},
            )

        with patch.object(
            voice_health_mod,
            "check_linux_mixer_sanity",
            return_value=_saturated,
        ):
            warnings = await factory_mod._run_boot_preflight(tuning=VoiceTuningConfig())
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning["code"] == "linux_mixer_saturated"
        assert warning["severity"] == "warning"
        assert warning["hint"] == "mixer saturated; run --fix"
        assert warning["details"] == {"aggregated_boost_db": 42.0}


class TestBootPreflightMarkerLifecycle:
    """v1.3 §4.6.4 + §-1C #1 — marker write on warnings / clear on pass."""

    @pytest.mark.asyncio()
    async def test_marker_cleared_when_preflight_passes_after_prior_saturation(
        self,
        tmp_path,
    ) -> None:
        """The v1.3 alt (e) fix: a passing boot clears a stale marker.

        Calls the marker-helper pair directly (rather than the full
        factory) because the factory wires many other moving parts
        unrelated to the marker lifecycle. The factory invokes the
        same helpers on every boot, so asserting on the helpers'
        composition covers the contract.
        """
        from unittest.mock import AsyncMock, patch

        import sovyx.voice.factory as factory_mod
        from sovyx.engine.config import VoiceTuningConfig
        from sovyx.voice.health import (
            read_preflight_warnings_file,
            write_preflight_warnings_file,
        )

        # Seed a stale marker from a prior saturated boot.
        write_preflight_warnings_file(
            [{"code": "linux_mixer_saturated", "hint": "stale"}],
            data_dir=tmp_path,
        )
        assert read_preflight_warnings_file(data_dir=tmp_path) != []

        # Simulate a passing boot by reproducing the factory's marker
        # branch in isolation.
        with patch.object(factory_mod, "_run_boot_preflight", new=AsyncMock(return_value=[])):
            from sovyx.voice.health import (
                clear_preflight_warnings_file,
            )
            from sovyx.voice.health import (
                write_preflight_warnings_file as write_fn,
            )

            # Mirror the factory's two branches:
            boot_warnings = await factory_mod._run_boot_preflight(
                tuning=VoiceTuningConfig(),
            )
            if boot_warnings:
                write_fn(warnings=boot_warnings, data_dir=tmp_path)
            else:
                clear_preflight_warnings_file(data_dir=tmp_path)

        # The stale marker must be gone.
        assert read_preflight_warnings_file(data_dir=tmp_path) == []


class TestFactoryWiresDeviceAndCapture:
    """create_voice_pipeline passes input_device through to the capture task."""

    @pytest.mark.asyncio()
    async def test_input_device_forwarded(self, tmp_path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        import sovyx.voice.factory as factory_mod

        fake_pipeline = MagicMock()
        fake_pipeline.start = AsyncMock()

        original_create_vad = factory_mod._create_vad
        original_create_stt = factory_mod._create_stt
        original_create_piper = factory_mod._create_piper_tts
        factory_mod._create_vad = lambda *a, **kw: MagicMock()
        fake_stt = MagicMock()
        fake_stt.initialize = AsyncMock()
        fake_stt.state = None  # skip STTState.READY assertion branch
        factory_mod._create_stt = lambda *a, **kw: fake_stt
        factory_mod._create_piper_tts = lambda *a, **kw: MagicMock()

        try:
            with (
                patch.object(
                    factory_mod, "ensure_silero_vad", new=AsyncMock(return_value=vad_file)
                ),
                patch.object(factory_mod, "detect_tts_engine", return_value="piper"),
                patch("sovyx.voice.device_enum.resolve_device", return_value=None),
                patch(
                    "sovyx.voice.pipeline._orchestrator.VoicePipeline",
                    return_value=fake_pipeline,
                ),
            ):
                bundle = await factory_mod.create_voice_pipeline(
                    model_dir=tmp_path,
                    input_device=7,
                    output_device=3,
                )
            assert bundle.pipeline is fake_pipeline
            assert bundle.capture_task.input_device == 7
            fake_pipeline.start.assert_awaited_once()
        finally:
            factory_mod._create_vad = original_create_vad
            factory_mod._create_stt = original_create_stt
            factory_mod._create_piper_tts = original_create_piper


class TestCreateKokoroTtsResolution:
    """_create_kokoro_tts resolves voice_id + language via the voice catalog.

    Regression: before Phase 3, the Kokoro engine was always constructed
    with the hardcoded ``af_bella`` default, so a mind with
    ``language="pt"`` still spoke English through the live pipeline.
    """

    def test_explicit_voice_id_wins_over_language(self, tmp_path) -> None:
        """voice_id in the catalog → use it + its declared language.

        The catalog's language always wins: a ``pf_dora`` voice stays
        pt-br even if the caller passed ``language="en"``. This keeps
        the per-voice phoneme model in sync with the speaker identity.
        """
        import sovyx.voice.factory as factory_mod

        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["model_dir"] = model_dir
                captured["config"] = config

        with (
            patch.object(factory_mod, "__name__", "sovyx.voice.factory"),
            patch(
                "sovyx.voice.tts_kokoro.KokoroTTS",
                _FakeKokoro,
            ),
        ):
            _create_kokoro_tts(tmp_path, voice_id="pf_dora", language="en")

        config = captured["config"]
        assert config is not None
        assert config.voice == "pf_dora"
        assert config.language == "pt-br"

    def test_empty_voice_id_picks_recommended_for_pt(self, tmp_path) -> None:
        """language="pt" without voice_id → a p-prefix (Portuguese) voice."""
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="", language="pt")

        config = captured["config"]
        assert config is not None
        # The Kokoro naming convention pins Portuguese voices to the
        # ``p`` prefix — if this ever regresses, the coherence bug is back.
        assert config.voice.startswith("p")
        assert config.language == "pt-br"

    def test_empty_voice_id_picks_recommended_for_ja(self, tmp_path) -> None:
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="", language="ja")

        config = captured["config"]
        assert config is not None
        assert config.voice.startswith("j")
        assert config.language == "ja"

    def test_unknown_voice_id_falls_back_to_language(self, tmp_path) -> None:
        """A voice_id not in the catalog shouldn't crash — fall back."""
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="zz_nobody", language="fr")

        config = captured["config"]
        assert config is not None
        assert config.voice.startswith("f")
        assert config.language == "fr"

    def test_unsupported_language_uses_kokoro_defaults(self, tmp_path) -> None:
        """An exotic language the catalog doesn't cover keeps the pipeline bootable."""
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="", language="xx-yy")

        # No config passed → KokoroTTS uses its hardcoded defaults.
        assert captured["config"] is None

    def test_en_defaults_to_en_us(self, tmp_path) -> None:
        """Bare ``en`` should canonicalise to ``en-us`` (matches server aliases)."""
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="", language="en")

        config = captured["config"]
        assert config is not None
        assert config.language == "en-us"


class TestDeafSignalCoordinatorWiring:
    """``create_voice_pipeline`` wires the :class:`CaptureIntegrityCoordinator`.

    Phase 1. The factory must:

    * Resolve ``voice_clarity_active`` via :func:`_detect_voice_clarity_active`
      **before** constructing the pipeline (dashboard attribution only
      — the coordinator's integrity probe is now the authoritative gate).
    * Wire an ``on_deaf_signal`` callback whose closure late-binds to
      the coordinator constructed *after* the pipeline. Calling the
      callback must delegate to
      :meth:`CaptureIntegrityCoordinator.handle_deaf_signal`.
    """

    @pytest.mark.asyncio()
    async def test_voice_clarity_active_threaded_into_pipeline(self, tmp_path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        import sovyx.voice.factory as factory_mod

        fake_pipeline = MagicMock()
        fake_pipeline.start = AsyncMock()

        original_create_vad = factory_mod._create_vad
        original_create_stt = factory_mod._create_stt
        original_create_piper = factory_mod._create_piper_tts
        factory_mod._create_vad = lambda *a, **kw: MagicMock()
        fake_stt = MagicMock()
        fake_stt.initialize = AsyncMock()
        fake_stt.state = None
        factory_mod._create_stt = lambda *a, **kw: fake_stt
        factory_mod._create_piper_tts = lambda *a, **kw: MagicMock()

        captured_kwargs: dict[str, object] = {}

        def _capture_pipeline(**kwargs: object) -> object:
            captured_kwargs.update(kwargs)
            return fake_pipeline

        try:
            with (
                patch.object(
                    factory_mod, "ensure_silero_vad", new=AsyncMock(return_value=vad_file)
                ),
                patch.object(factory_mod, "detect_tts_engine", return_value="piper"),
                patch("sovyx.voice.device_enum.resolve_device", return_value=None),
                patch.object(factory_mod, "_detect_voice_clarity_active", return_value=True),
                patch(
                    "sovyx.voice.pipeline._orchestrator.VoicePipeline",
                    side_effect=_capture_pipeline,
                ),
            ):
                await factory_mod.create_voice_pipeline(model_dir=tmp_path)

            assert captured_kwargs["voice_clarity_active"] is True
            # ``auto_bypass_enabled`` defaults to the tuning flag (True).
            assert captured_kwargs["auto_bypass_enabled"] is True
            # The callback must be awaitable — late binding via closure.
            callback = captured_kwargs["on_deaf_signal"]
            assert callable(callback)
        finally:
            factory_mod._create_vad = original_create_vad
            factory_mod._create_stt = original_create_stt
            factory_mod._create_piper_tts = original_create_piper

    @pytest.mark.asyncio()
    async def test_deaf_signal_callback_delegates_to_coordinator(self, tmp_path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        import sovyx.voice.factory as factory_mod
        import sovyx.voice.health.capture_integrity as integrity_mod

        fake_pipeline = MagicMock()
        fake_pipeline.start = AsyncMock()

        original_create_vad = factory_mod._create_vad
        original_create_stt = factory_mod._create_stt
        original_create_piper = factory_mod._create_piper_tts
        factory_mod._create_vad = lambda *a, **kw: MagicMock()
        fake_stt = MagicMock()
        fake_stt.initialize = AsyncMock()
        fake_stt.state = None
        factory_mod._create_stt = lambda *a, **kw: fake_stt
        factory_mod._create_piper_tts = lambda *a, **kw: MagicMock()

        captured_kwargs: dict[str, object] = {}

        def _capture_pipeline(**kwargs: object) -> object:
            captured_kwargs.update(kwargs)
            return fake_pipeline

        fake_coordinator = MagicMock()
        fake_coordinator.handle_deaf_signal = AsyncMock(return_value=[])

        def _build_coordinator(**kwargs: object) -> object:  # noqa: ARG001
            return fake_coordinator

        try:
            with (
                patch.object(
                    factory_mod, "ensure_silero_vad", new=AsyncMock(return_value=vad_file)
                ),
                patch.object(factory_mod, "detect_tts_engine", return_value="piper"),
                patch("sovyx.voice.device_enum.resolve_device", return_value=None),
                patch.object(factory_mod, "_detect_voice_clarity_active", return_value=True),
                patch(
                    "sovyx.voice.pipeline._orchestrator.VoicePipeline",
                    side_effect=_capture_pipeline,
                ),
                patch.object(
                    integrity_mod,
                    "CaptureIntegrityCoordinator",
                    side_effect=_build_coordinator,
                ),
            ):
                await factory_mod.create_voice_pipeline(model_dir=tmp_path)

            callback = captured_kwargs["on_deaf_signal"]
            result = await callback()  # type: ignore[misc]

            fake_coordinator.handle_deaf_signal.assert_awaited_once()
            assert result == []
        finally:
            factory_mod._create_vad = original_create_vad
            factory_mod._create_stt = original_create_stt
            factory_mod._create_piper_tts = original_create_piper

    def test_detect_voice_clarity_active_swallows_detector_errors(self) -> None:
        """Detector failures must never break pipeline startup.

        Users on locked-down Windows installs hit registry ACLs that
        the detector cannot read. The factory must still boot — just
        with auto-bypass disabled (opt-in on failure).
        """
        import sovyx.voice.factory as factory_mod

        with patch(
            "sovyx.voice._apo_detector.detect_capture_apos",
            side_effect=PermissionError("registry ACL"),
        ):
            active = factory_mod._detect_voice_clarity_active("FakeMic")
        assert active is False


class TestKernelInvalidatedFailoverEmits:
    """§4.4.7 fail-over must emit the ``action="failover"`` telemetry.

    Dashboards and SRE alerts discriminate successful fail-over
    (``action="failover"``) from "no alternative endpoint" via the
    kernel-invalidated counter. If the fail-over site forgets to emit,
    ``{action="failover"}`` reports zero forever even when users are
    being saved by it.
    """

    @pytest.mark.asyncio()
    async def test_failover_emits_metric_with_alternative_host_api(self, tmp_path) -> None:
        from dataclasses import dataclass, field
        from unittest.mock import AsyncMock, MagicMock

        import sovyx.voice.factory as factory_mod
        from sovyx.voice.device_enum import DeviceEntry

        @dataclass
        class _Result:
            source: str
            winning_combo: object | None = None
            attempts_count: int = 0
            winning_candidate: object | None = field(default=None)

        original = DeviceEntry(
            index=3,
            name="Mic A (stuck)",
            canonical_name="mic a (stuck)",
            host_api_index=0,
            host_api_name="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48000,
            is_os_default=True,
        )
        alternative = DeviceEntry(
            index=5,
            name="Mic B (healthy)",
            canonical_name="mic b (healthy)",
            host_api_index=0,
            host_api_name="Windows DirectSound",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48000,
            is_os_default=False,
        )

        tuning = MagicMock()
        tuning.kernel_invalidated_failover_enabled = True

        # Failover re-cascade lands HEALTHY so the verdict gate does not
        # short-circuit the return with CaptureInoperativeError — this
        # test is about the telemetry emission, not Bug D.
        run_cascade = AsyncMock(
            side_effect=[
                _Result(source="quarantined"),
                _Result(source="cascade", winning_combo=object(), attempts_count=1),
            ]
        )
        record = MagicMock()

        with (
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                return_value=[],
            ),
            patch(
                "sovyx.voice.device_enum.enumerate_devices",
                return_value=[original, alternative],
            ),
            patch(
                "sovyx.voice.health._factory_integration.run_boot_cascade_for_candidates",
                new=run_cascade,
            ),
            patch(
                "sovyx.voice.health._factory_integration.derive_endpoint_guid",
                return_value="{guid-A}",
            ),
            patch(
                "sovyx.voice.health._factory_integration.select_alternative_endpoint",
                return_value=alternative,
            ),
            patch(
                "sovyx.voice.health._metrics.record_kernel_invalidated_event",
                new=record,
            ),
        ):
            out = await factory_mod._run_vchl_boot_cascade(
                resolved=original, data_dir=tmp_path, tuning=tuning
            )

        assert out is alternative
        record.assert_called_once()
        kwargs = record.call_args.kwargs
        assert kwargs["action"] == "failover"
        assert kwargs["host_api"] == "Windows DirectSound"
        assert kwargs["platform"]

    @pytest.mark.asyncio()
    async def test_no_alternative_does_not_emit_failover(self, tmp_path) -> None:
        from dataclasses import dataclass, field
        from unittest.mock import AsyncMock, MagicMock

        import sovyx.voice.factory as factory_mod
        from sovyx.voice._capture_task import CaptureInoperativeError
        from sovyx.voice.device_enum import DeviceEntry

        @dataclass
        class _Result:
            source: str
            winning_combo: object | None = None
            attempts_count: int = 0
            winning_candidate: object | None = field(default=None)

        original = DeviceEntry(
            index=3,
            name="Mic A",
            canonical_name="mic a",
            host_api_index=0,
            host_api_name="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48000,
            is_os_default=True,
        )
        tuning = MagicMock()
        tuning.kernel_invalidated_failover_enabled = True
        record = MagicMock()

        with (
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                return_value=[],
            ),
            patch(
                "sovyx.voice.device_enum.enumerate_devices",
                return_value=[original],
            ),
            patch(
                "sovyx.voice.health._factory_integration.run_boot_cascade_for_candidates",
                new=AsyncMock(return_value=_Result(source="quarantined")),
            ),
            patch(
                "sovyx.voice.health._factory_integration.derive_endpoint_guid",
                return_value="{guid-A}",
            ),
            patch(
                "sovyx.voice.health._factory_integration.select_alternative_endpoint",
                return_value=None,
            ),
            patch(
                "sovyx.voice.health._metrics.record_kernel_invalidated_event",
                new=record,
            ),
            pytest.raises(CaptureInoperativeError) as exc_info,
        ):
            await factory_mod._run_vchl_boot_cascade(
                resolved=original, data_dir=tmp_path, tuning=tuning
            )

        # v0.20.2 / Bug D — quarantined + no alternative is an INOPERATIVE
        # verdict; the helper now refuses to boot a deaf pipeline.
        assert exc_info.value.reason == "no_alternative_endpoint"
        assert exc_info.value.device == original.index
        record.assert_not_called()


class TestVoiceClarityRecomputedAfterFailover:
    """Bug_006 regression: APO detection targets the post-cascade device.

    The VoiceClarity APO lives per-endpoint. When §4.4.7 fail-over
    rebinds ``resolved`` to a different mic, the detector must run
    against that new name — otherwise ``voice_pipeline_created``
    advertises ``voice_clarity_active`` for the wrong device and
    auto-bypass arms on a mic whose APO state was never probed.
    """

    @pytest.mark.asyncio()
    async def test_detector_receives_post_cascade_device_name(self, tmp_path) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from unittest.mock import patch as _patch

        import sovyx.voice.factory as factory_mod
        from sovyx.voice.device_enum import DeviceEntry

        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        original = DeviceEntry(
            index=3,
            name="Original Mic",
            canonical_name="original mic",
            host_api_index=0,
            host_api_name="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48000,
            is_os_default=True,
        )
        replacement = DeviceEntry(
            index=7,
            name="Replacement Mic",
            canonical_name="replacement mic",
            host_api_index=0,
            host_api_name="Windows DirectSound",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48000,
            is_os_default=False,
        )

        fake_pipeline = MagicMock()
        fake_pipeline.start = AsyncMock()

        detector_calls: list[str | None] = []

        def _detector(name: str | None) -> bool:
            detector_calls.append(name)
            return False

        captured_kwargs: dict[str, object] = {}

        def _capture_pipeline(**kwargs: object) -> object:
            captured_kwargs.update(kwargs)
            return fake_pipeline

        original_create_vad = factory_mod._create_vad
        original_create_stt = factory_mod._create_stt
        original_create_piper = factory_mod._create_piper_tts
        factory_mod._create_vad = lambda *a, **kw: MagicMock()
        fake_stt = MagicMock()
        fake_stt.initialize = AsyncMock()
        fake_stt.state = None
        factory_mod._create_stt = lambda *a, **kw: fake_stt
        factory_mod._create_piper_tts = lambda *a, **kw: MagicMock()

        try:
            with (
                _patch.object(
                    factory_mod, "ensure_silero_vad", new=AsyncMock(return_value=vad_file)
                ),
                _patch.object(factory_mod, "detect_tts_engine", return_value="piper"),
                _patch(
                    "sovyx.voice.device_enum.resolve_device",
                    return_value=original,
                ),
                _patch.object(factory_mod, "_detect_voice_clarity_active", side_effect=_detector),
                _patch.object(
                    factory_mod,
                    "_run_vchl_boot_cascade",
                    new=AsyncMock(return_value=replacement),
                ),
                _patch(
                    "sovyx.voice.pipeline._orchestrator.VoicePipeline",
                    side_effect=_capture_pipeline,
                ),
            ):
                await factory_mod.create_voice_pipeline(model_dir=tmp_path)

            assert detector_calls, "detector never called"
            assert detector_calls[-1] == "Replacement Mic", (
                f"detector saw {detector_calls!r} — last call must target the "
                "post-cascade (fail-over) device, not the original."
            )
        finally:
            factory_mod._create_vad = original_create_vad
            factory_mod._create_stt = original_create_stt
            factory_mod._create_piper_tts = original_create_piper


class TestCascadeVerdictRaisesInoperative:
    """v0.20.2 §4.4.7 / Bug D — cascade INOPERATIVE must block the boot.

    Pre-v0.20.2 ``_run_vchl_boot_cascade`` returned the original device
    even when every viable combo failed, letting the legacy opener fall
    through to MME shared and silently boot a deaf pipeline. The helper
    now classifies the final cascade result and raises
    :class:`CaptureInoperativeError` on INOPERATIVE so the dashboard
    ``/api/voice/enable`` route can surface a proper 503.
    """

    @pytest.mark.asyncio()
    async def test_exhausted_cascade_raises_capture_inoperative(self, tmp_path) -> None:
        from dataclasses import dataclass, field
        from unittest.mock import AsyncMock, MagicMock

        import sovyx.voice.factory as factory_mod
        from sovyx.voice._capture_task import CaptureInoperativeError
        from sovyx.voice.device_enum import DeviceEntry

        @dataclass
        class _Result:
            source: str
            winning_combo: object | None = None
            attempts_count: int = 0
            winning_candidate: object | None = field(default=None)

        original = DeviceEntry(
            index=4,
            name="Dead Mic",
            canonical_name="dead mic",
            host_api_index=0,
            host_api_name="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48000,
            is_os_default=True,
        )
        tuning = MagicMock()
        tuning.kernel_invalidated_failover_enabled = True

        with (
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                return_value=[],
            ),
            patch(
                "sovyx.voice.device_enum.enumerate_devices",
                return_value=[original],
            ),
            patch(
                "sovyx.voice.health._factory_integration.run_boot_cascade_for_candidates",
                new=AsyncMock(return_value=_Result(source="none", attempts_count=6)),
            ),
            patch(
                "sovyx.voice.health._factory_integration.select_alternative_endpoint",
                return_value=None,
            ),
            pytest.raises(CaptureInoperativeError) as exc_info,
        ):
            await factory_mod._run_vchl_boot_cascade(
                resolved=original, data_dir=tmp_path, tuning=tuning
            )

        assert exc_info.value.reason == "no_winner"
        assert exc_info.value.attempts == 6
        assert exc_info.value.device == 4
        assert exc_info.value.host_api == "Windows WASAPI"

    @pytest.mark.asyncio()
    async def test_healthy_cascade_returns_device(self, tmp_path) -> None:
        from dataclasses import dataclass, field
        from unittest.mock import AsyncMock, MagicMock

        import sovyx.voice.factory as factory_mod
        from sovyx.voice.device_enum import DeviceEntry

        @dataclass
        class _Result:
            source: str
            winning_combo: object | None = None
            attempts_count: int = 0
            winning_candidate: object | None = field(default=None)

        original = DeviceEntry(
            index=2,
            name="Happy Mic",
            canonical_name="happy mic",
            host_api_index=0,
            host_api_name="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48000,
            is_os_default=True,
        )
        tuning = MagicMock()
        tuning.kernel_invalidated_failover_enabled = True

        with (
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                return_value=[],
            ),
            patch(
                "sovyx.voice.device_enum.enumerate_devices",
                return_value=[original],
            ),
            patch(
                "sovyx.voice.health._factory_integration.run_boot_cascade_for_candidates",
                new=AsyncMock(
                    return_value=_Result(
                        source="cascade", winning_combo=object(), attempts_count=1
                    ),
                ),
            ),
        ):
            out = await factory_mod._run_vchl_boot_cascade(
                resolved=original, data_dir=tmp_path, tuning=tuning
            )

        assert out is original

    @pytest.mark.asyncio()
    async def test_allow_inoperative_capture_suppresses_raise(self, tmp_path) -> None:
        """The escape hatch preserves pre-v0.20.2 behaviour for tests.

        When operators / callers explicitly opt in via
        ``allow_inoperative_capture=True`` the helper returns the
        original device instead of raising — useful for the legacy
        opener path where deafness is observed downstream.
        """
        from dataclasses import dataclass, field
        from unittest.mock import AsyncMock, MagicMock

        import sovyx.voice.factory as factory_mod
        from sovyx.voice.device_enum import DeviceEntry

        @dataclass
        class _Result:
            source: str
            winning_combo: object | None = None
            attempts_count: int = 0
            winning_candidate: object | None = field(default=None)

        original = DeviceEntry(
            index=4,
            name="Dead Mic",
            canonical_name="dead mic",
            host_api_index=0,
            host_api_name="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48000,
            is_os_default=True,
        )
        tuning = MagicMock()
        tuning.kernel_invalidated_failover_enabled = True

        with (
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                return_value=[],
            ),
            patch(
                "sovyx.voice.device_enum.enumerate_devices",
                return_value=[original],
            ),
            patch(
                "sovyx.voice.health._factory_integration.run_boot_cascade_for_candidates",
                new=AsyncMock(return_value=_Result(source="none", attempts_count=3)),
            ),
            patch(
                "sovyx.voice.health._factory_integration.select_alternative_endpoint",
                return_value=None,
            ),
        ):
            out = await factory_mod._run_vchl_boot_cascade(
                resolved=original,
                data_dir=tmp_path,
                tuning=tuning,
                allow_inoperative_capture=True,
            )

        assert out is original

    @pytest.mark.asyncio()
    async def test_cascade_dispatch_exception_does_not_raise(self, tmp_path) -> None:
        """A crash inside the cascade must fall back to DEGRADED, not INOPERATIVE.

        The except block swallows the exception and ``final_result``
        stays ``None``, which classifies as DEGRADED — the helper
        returns the original device so the legacy opener owns the path.
        """
        from unittest.mock import AsyncMock, MagicMock

        import sovyx.voice.factory as factory_mod
        from sovyx.voice.device_enum import DeviceEntry

        original = DeviceEntry(
            index=2,
            name="Mic",
            canonical_name="mic",
            host_api_index=0,
            host_api_name="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=48000,
            is_os_default=True,
        )
        tuning = MagicMock()
        tuning.kernel_invalidated_failover_enabled = True

        with (
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                return_value=[],
            ),
            patch(
                "sovyx.voice.device_enum.enumerate_devices",
                return_value=[original],
            ),
            patch(
                "sovyx.voice.health._factory_integration.run_boot_cascade_for_candidates",
                new=AsyncMock(side_effect=RuntimeError("probe crashed")),
            ),
        ):
            out = await factory_mod._run_vchl_boot_cascade(
                resolved=original, data_dir=tmp_path, tuning=tuning
            )

        assert out is original
