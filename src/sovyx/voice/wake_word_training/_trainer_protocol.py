"""Pluggable trainer backend protocol — Phase 8 / T8.13.

Sovyx ships the **training pipeline foundation** without bundling a
specific wake-word training backend. Operators register a backend
that implements :class:`TrainerBackend` and the orchestrator drives
it via the Protocol contract.

Why pluggable + no default (verified 2026-05-02):

The pluggable-Protocol design was chosen after reviewing every
candidate that could plausibly ship as the Sovyx default. None
qualifies:

* **OpenWakeWord 0.6.0** (the canonical OSS option): the official
  ``[full]`` extra (NOT ``[training]`` — that name does not exist)
  pins ``tensorflow-cpu==2.8.1`` + ``protobuf>=3.20,<4`` + ``onnx==1.14.0``.
  Those are 4-year-old pins incompatible with Sovyx's stack
  (Python 3.11/3.12, modern ``onnxruntime>=1.18``, modern protobuf).
  The latest release is dated 2024-02-11; the project has been
  effectively dormant since then. There is also no documented
  programmatic training API — the canonical training surface is a
  Google Colab notebook referenced in their README, not a Python
  library entry-point. Wrapping notebook code is fragile.
* **lgpearson1771/openwakeword-trainer** (third-party fork, MIT,
  active): ships compatibility patches for modern torchaudio /
  speechbrain / Piper, but is a CLI/script pipeline (YAML-driven
  ``train_wakeword.py``), not a Python library — wrapping it
  requires shelling out + parsing YAML + tracking output paths,
  which is fragile and not enterprise-grade.
* **Sherpa-ONNX** (k2-fsa): uses *open-vocabulary* keyword spotting
  (one generic ASR model + a keywords text file, NO per-keyword
  training). The Sovyx STT-fallback path (T8.17 - T8.19) already
  covers this no-training-needed scenario; adding it as a "trainer"
  would be a semantic mismatch.
* **Custom enterprise deployments**: any internal ML platform
  trivially implements ``TrainerBackend`` against its own pipeline.

Bundling any of the above as the Sovyx default would either force
every install to carry an incompatible / dormant / non-API dep
cluster, OR misrepresent what the backend actually does. The
pluggable Protocol is the correct architectural answer: lean
default install, operators bring their own trainer.

Reference architecture:
* ``register_default_backend`` is the single registration point
  (one process, one default backend).
* The CLI / dashboard / orchestrator query
  ``resolve_default_backend`` and raise
  :class:`NoBackendRegisteredError` with verified operator
  guidance when none is registered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class TrainingCancelledError(Exception):
    """Raised by a backend when the orchestrator's cancel-check
    callback returns ``True`` mid-training.

    The orchestrator catches this specifically (vs. generic
    ``Exception``) so it can transition to ``CANCELLED`` rather than
    ``FAILED`` — important for dashboard rendering: cancelled jobs
    are operator-initiated; failed jobs are surfaces to investigate.
    """


class NoBackendRegisteredError(RuntimeError):
    """Raised when the orchestrator tries to start training without
    a registered backend.

    The error message includes installation hints for the canonical
    backend options so operators can diagnose without consulting
    docs first.
    """


@runtime_checkable
class TrainerBackend(Protocol):
    """Contract every wake-word training backend must implement.

    Backends are stateless functors — one ``train`` call per training
    job. The orchestrator handles state machine + progress logging
    + cancel/resume; the backend handles the ML loop only.

    Naming: ``TrainerBackend`` rather than ``Trainer`` to avoid
    confusion with cognitive-loop training surfaces (which don't
    exist today but might in the future).
    """

    @property
    def name(self) -> str:
        """Stable identifier for this backend (e.g.
        ``"openwakeword"``, ``"sherpa-onnx"``). Logged in progress
        events + telemetry; renaming is a breaking schema change."""
        ...

    def train(
        self,
        *,
        wake_word: str,
        language: str,
        positive_samples: list[Path],
        negative_samples: list[Path],
        output_path: Path,
        on_progress: Callable[[float, str], None],
        cancel_check: Callable[[], bool],
    ) -> Path:
        """Run the training loop and return the path to the trained ONNX.

        Args:
            wake_word: The wake word being trained (with original
                diacritics intact). The backend MAY use it to seed
                phoneme tables; the actual recognition target is
                the audio in ``positive_samples``.
            language: BCP-47 language tag for any phoneme / locale
                handling the backend needs.
            positive_samples: Paths to ``.wav`` files containing
                the wake word. Caller (orchestrator) generates these
                via Kokoro TTS during the SYNTHESIZING phase.
            negative_samples: Paths to ``.wav`` files containing
                non-wake utterances (random phrases, ambient noise).
                Same format as ``positive_samples``.
            output_path: Where to write the trained ``.onnx``. The
                backend creates the parent directory if needed.
            on_progress: Callback invoked periodically with
                ``(fraction_complete: float, message: str)``. Backends
                SHOULD call at least once per epoch + once per
                significant phase transition. ``fraction_complete``
                is 0.0 to 1.0.
            cancel_check: Callback invoked periodically; when it
                returns ``True``, the backend MUST raise
                :class:`TrainingCancelledError` at the next
                checkpoint boundary. Backends that can't honour
                cancellation cleanly should poll less often (the
                orchestrator's CANCELLED transition can wait up to
                30 s without operator confusion).

        Returns:
            The path to the trained ``.onnx``. Typically equals
            ``output_path`` but the backend MAY return a
            different path if it wrote to a temp location first
            (orchestrator preserves whichever is returned).

        Raises:
            TrainingCancelledError: ``cancel_check`` returned True.
            Exception: Any other backend-specific failure;
                orchestrator transitions to FAILED with the
                exception's str() in ``error_summary``.
        """
        ...


# ── Default-backend registry (one per process) ──────────────────────


_DEFAULT_BACKEND: TrainerBackend | None = None


def register_default_backend(backend: TrainerBackend) -> None:
    """Register the process-default training backend.

    Idempotent: registering the same backend twice is a no-op.
    Re-registering a DIFFERENT backend replaces the previous one
    (useful for tests injecting a mock; production code should
    register exactly once at boot).

    Args:
        backend: A :class:`TrainerBackend`-compatible object. The
            ``runtime_checkable`` Protocol means duck-typing works —
            any object with the right method signature is accepted.

    Raises:
        TypeError: ``backend`` doesn't satisfy the Protocol.
    """
    global _DEFAULT_BACKEND  # noqa: PLW0603
    if not isinstance(backend, TrainerBackend):
        msg = (
            f"{backend!r} does not satisfy the TrainerBackend protocol "
            f"(needs ``name`` property + ``train`` method)"
        )
        raise TypeError(msg)
    _DEFAULT_BACKEND = backend


def resolve_default_backend() -> TrainerBackend:
    """Return the registered default backend or raise.

    Raises:
        NoBackendRegisteredError: No backend has been registered.
            Error message includes the canonical install hints so
            operators can self-resolve without docs.
    """
    if _DEFAULT_BACKEND is None:
        # Verified 2026-05-02: no default ML backend ships with
        # Sovyx because every candidate fails enterprise-grade
        # criteria (see module docstring). The hints below point to
        # paths that actually exist + work, NOT to speculative
        # extras. This message is the operator's first encounter
        # with the pluggable surface — keep it accurate.
        msg = (
            "No wake-word training backend registered. Sovyx ships "
            "the orchestrator + CLI + dashboard surface; the ML "
            "training step is pluggable BY DESIGN — see "
            "``sovyx.voice.wake_word_training`` package docstring "
            "for the verified rationale. Three operator paths:\n"
            "  1. **Train externally**, then drop the ``.onnx`` "
            "into ``<data_dir>/wake_word_models/pretrained/<id>.onnx``. "
            "Sovyx's ``PretrainedModelRegistry`` picks it up at boot; "
            "the dashboard's hot-reload endpoint "
            "(``POST /api/voice/training/jobs/<id>/cancel``-adjacent "
            "RPC ``wake_word.register_mind``) activates it without "
            "a daemon restart. Canonical external trainers: the "
            "OpenWakeWord Colab notebook (linked from "
            "github.com/dscripka/openWakeWord) OR the actively "
            "maintained MIT-licensed fork at "
            "github.com/lgpearson1771/openwakeword-trainer.\n"
            "  2. **Implement** ``TrainerBackend`` for your in-house "
            "ML platform + register at boot via "
            "``register_default_backend(MyBackend())``. The Protocol "
            "is 2 methods (``name`` + ``train``); see the source for "
            "the contract.\n"
            "  3. **Use STT fallback** (already shipped, T8.17 - T8.19) "
            "if per-keyword ONNX training is more friction than the "
            "operator wants — the Moonshine STT path matches wake "
            "words from transcriptions with ~500 ms latency vs "
            "~80 ms for ONNX, no training required."
        )
        raise NoBackendRegisteredError(msg)
    return _DEFAULT_BACKEND


def _reset_default_backend_for_tests() -> None:
    """Test-only helper to clear the registered backend.

    Production code MUST NOT call this. The leading underscore +
    ``_for_tests`` suffix signals the contract.
    """
    global _DEFAULT_BACKEND  # noqa: PLW0603
    _DEFAULT_BACKEND = None


__all__ = [
    "NoBackendRegisteredError",
    "TrainerBackend",
    "TrainingCancelledError",
    "register_default_backend",
    "resolve_default_backend",
]
