"""Pluggable trainer backend protocol — Phase 8 / T8.13.

Sovyx ships the **training pipeline foundation** without bundling a
specific wake-word training backend. Operators register a backend
that implements :class:`TrainerBackend` and the orchestrator drives
it via the Protocol contract.

Why pluggable + no default:

* OpenWakeWord training requires a feature-extractor model + a
  custom training script (Jupyter notebook in their docs). Bundling
  that path adds ~500 MB of training assets and a heavy ML dep tree.
* Sherpa-ONNX (alternative) has different sample format requirements.
* Custom enterprise deployments may want their own trainer (e.g.
  integrated with internal ML platforms).

Bundling one default would force every Sovyx install to carry the
ML dep tree even when not training. The Protocol approach keeps the
default install lean + lets operators opt in via a single
``register_default_backend`` call from their bootstrap code.

Reference architecture:
* ``register_default_backend`` is the single registration point
  (one process, one default backend).
* The CLI / dashboard / orchestrator query
  ``resolve_default_backend`` and raise
  :class:`NoBackendRegisteredError` with installation hints when
  none is registered.
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
        msg = (
            "No wake-word training backend registered. "
            "T8.13-T8.15 ships the orchestrator + state machine; "
            "the actual training backend is pluggable. Operator "
            "options:\n"
            "  1. Install OpenWakeWord training extras: "
            "``pip install openwakeword[training]`` then call "
            "``register_default_backend(OpenWakeWordBackend(...))`` "
            "from your bootstrap code.\n"
            "  2. Implement ``TrainerBackend`` for your custom ML "
            "platform and register it the same way.\n"
            "  3. Wait for the v0.32+ default OpenWakeWord backend "
            "ratification (mini-mission tracked in "
            "``OPERATOR-DEBT-MASTER-2026-05-01.md``)."
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
