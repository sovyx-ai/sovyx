"""Public read-only accessor mixin (extracted from ``_orchestrator.py``).

Owns the orchestrator's public read-only delegate surface — pure-
attribute properties that downstream consumers (factory wire-up,
dashboard endpoints, tests, RPC handlers) use to read pipeline
internals without touching private fields directly.

Pre-extraction this surface lived as 13 properties + 1 small method
on the single-class ``VoicePipeline`` god file. See CLAUDE.md anti-
pattern #16 for the carve-out rationale — seventh strike of the
Phase 5.F.19+ orchestrator split.

Zero behaviour change: every accessor is a single-line return of a
host-owned attribute (or one delegated method call for
``set_render_buffer``). No state mutation, no cross-mixin method
calls, no anti-pattern #32 hazards.

Anti-pattern #32 contract: zero cross-mixin method calls. The mixin
forward-declares the host-owned attributes inside ``if TYPE_CHECKING:``
so mypy strict resolves the references without creating runtime
attributes that would interfere with the host's own initialisation
order. The ``state`` property reads ``self._state`` which is a HOST-
owned property (defined on ``VoicePipeline`` itself, not extracted)
— MRO resolves the property descriptor as usual.

State the mixin reads (initialised on the HOST in
``VoicePipeline.__init__`` or via host-defined properties):

* ``_state`` — VoicePipelineState; backed by ``_state_value`` via the
  host's property+setter pair (state-mutation hooks live on host).
* ``_config: VoicePipelineConfig`` — immutable pipeline config.
* ``_output: AudioOutputQueue`` — audio output queue.
* ``_jarvis: JarvisIllusion`` — Jarvis Illusion controller.
* ``_running: bool`` — pipeline-active flag.
* ``_state_machine: PipelineStateMachine`` — bounded ring buffer owner
  (read by ``frame_history``).
* ``_vad / _stt / _tts / _wake_word`` — engine references.
* ``_vad_inference_timeouts: int`` — band-aid #50 lifetime counter.
* ``_false_wake_rejected_count: int`` — band-aid #46 lifetime counter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.voice._aec import RenderPcmSink
    from sovyx.voice.jarvis import JarvisIllusion
    from sovyx.voice.pipeline._config import VoicePipelineConfig
    from sovyx.voice.pipeline._frame_types import PipelineFrame
    from sovyx.voice.pipeline._output_queue import AudioOutputQueue
    from sovyx.voice.pipeline._state import VoicePipelineState
    from sovyx.voice.pipeline._state_machine import PipelineStateMachine
    from sovyx.voice.stt import STTEngine
    from sovyx.voice.tts_piper import TTSEngine
    from sovyx.voice.vad import SileroVAD
    from sovyx.voice.wake_word import WakeWordDetector


class PublicAccessorsMixin:
    """Pure read-only delegate surface for orchestrator internals.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the instance fields in
    ``__init__``; this mixin owns the read-only public surface that
    exposes them to downstream consumers.

    See module docstring for the full responsibility carve-out.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads. Declared TYPE_CHECKING
        # so mypy strict resolves the references without creating
        # runtime attributes that would interfere with the host's own
        # initialisation order.
        _state: VoicePipelineState
        _config: VoicePipelineConfig
        _output: AudioOutputQueue
        _jarvis: JarvisIllusion
        _running: bool
        _state_machine: PipelineStateMachine
        _vad: SileroVAD
        _stt: STTEngine
        _tts: TTSEngine
        _wake_word: WakeWordDetector
        _vad_inference_timeouts: int
        _false_wake_rejected_count: int
        _last_stt_latency_ms: float | None

    @property
    def state(self) -> VoicePipelineState:
        """Current pipeline state."""
        return self._state

    @property
    def config(self) -> VoicePipelineConfig:
        """Pipeline configuration."""
        return self._config

    @property
    def output(self) -> AudioOutputQueue:
        """Audio output queue."""
        return self._output

    def set_render_buffer(self, buffer: RenderPcmSink | None) -> None:
        """Wire (or unwire) the AEC render-PCM sink at the orchestrator level.

        Phase 4 / T4.4.d wiring helper. Delegates to
        :meth:`AudioOutputQueue.set_render_buffer` so the factory only
        needs one call to register the shared
        :class:`~sovyx.voice._render_pcm_buffer.RenderPcmBuffer`
        instance with the playback path. The capture-side registration
        (FrameNormalizer's render_provider) is plumbed separately
        through :class:`AudioCaptureTask` at construction time —
        the same buffer instance implements both Protocols, so a
        single buffer flows producer→consumer through the queue and
        the normalizer.
        """
        self._output.set_render_buffer(buffer)

    @property
    def jarvis(self) -> JarvisIllusion:
        """Jarvis Illusion controller."""
        return self._jarvis

    @property
    def is_running(self) -> bool:
        """Whether the pipeline is active."""
        return self._running

    @property
    def frame_history(self) -> tuple[PipelineFrame, ...]:
        """Public accessor for the bounded frame ring buffer (Step 15).

        Returns a tuple snapshot (oldest-first) of every frame the
        orchestrator has recorded since boot OR the last
        :meth:`PipelineStateMachine.reset` call. The deque under the
        hood is bounded at the state machine's ``history_capacity``
        (default 256), so the snapshot is always at most that size.

        Mission §1.1 Hybrid Option C — observability surface for the
        Pipecat-aligned typed frames recorded at the 5 transition sites
        (Step 13) plus the BargeInInterruptionFrame at every
        cancel_speech_chain exit (Step 14).

        Consumers:

        * Dashboard ``GET /api/voice/frame-history`` (registered in
          ``src/sovyx/dashboard/routes/voice.py``).
        * Soak validation harness (Step 16).
        * Operator forensics ("what frames fired during this turn?").

        The snapshot is immutable — caller mutations cannot leak
        back into the deque.
        """
        return self._state_machine.frame_history()

    @property
    def vad(self) -> SileroVAD:
        """Voice activity detector used by this pipeline."""
        return self._vad

    @property
    def stt(self) -> STTEngine:
        """Speech-to-text engine used by this pipeline."""
        return self._stt

    @property
    def tts(self) -> TTSEngine:
        """Text-to-speech engine used by this pipeline."""
        return self._tts

    @property
    def wake_word(self) -> WakeWordDetector:
        """Wake word detector used by this pipeline."""
        return self._wake_word

    @property
    def vad_inference_timeout_count(self) -> int:
        """Lifetime count of VAD inferences that exceeded
        ``_VAD_INFERENCE_TIMEOUT_S`` (band-aid #50). Non-zero means
        the host has experienced sustained CPU pressure or an ONNX
        session anomaly; pair with ``voice.vad.inference_timeout``
        WARN events for attribution."""
        return self._vad_inference_timeouts

    @property
    def last_stt_latency_ms(self) -> float | None:
        """Most-recent STT-decode latency in milliseconds, or ``None``
        before the first utterance completes.

        LIVE-2 Phase 3 (P1-3) — the orchestrator measures this at the
        STT-complete boundary; this accessor lets ``/api/voice/status``
        report a real "Pipeline latency" instead of a permanent ``—``.
        It is the last single-turn decode latency, not a rolling average.
        """
        return self._last_stt_latency_ms

    @property
    def false_wake_rejected_count(self) -> int:
        """Lifetime count of utterances rejected by the band-aid #46
        false-wake confidence gate. Always 0 unless the operator has
        opted-in by setting :attr:`VoicePipelineConfig.false_wake_min_confidence`
        to a non-zero threshold. Non-zero means the wake-word stage
        is firing on noise the STT engine then reported as low-
        confidence — pair with ``voice.wake.false_positive_rejected``
        WARN events for the per-rejection trace."""
        return self._false_wake_rejected_count
