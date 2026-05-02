"""Wake-word training pipeline foundation — Phase 8 / T8.13.

Layered design (per CLAUDE.md anti-pattern #16 "god files"):

* :mod:`._state` — pure types (``TrainingStatus`` StrEnum +
  ``TrainingJobState`` frozen dataclass). No I/O.
* :mod:`._progress` — JSONL progress writer / reader. Survives
  daemon restarts; the orchestrator resumes by replaying the file.
* :mod:`._trainer_protocol` — pluggable trainer interface. Default
  is "no backend registered" → clear error; operators wire
  OpenWakeWord / Sherpa / custom backends via the Protocol.
* :mod:`._orchestrator` — state machine + cancel/resume +
  hot-reload coordination. (Pending — landing in a follow-up
  focused mini-mission once a default backend is ratified.)

The hot-reload primitive is **already shipped**: the
:class:`~sovyx.voice._wake_word_router.WakeWordRouter.register_mind`
method is idempotent, so re-registering a mind_id with a
new ``model_path`` swaps the detector with no daemon restart.
T8.15's only remaining work is calling that method from the
training-completion hook.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.13 + T8.14 + T8.15. ``OPERATOR-DEBT-MASTER-2026-05-01.md``
notes T8.13-T8.15 as the next focused mini-mission.
"""

from __future__ import annotations

from sovyx.voice.wake_word_training._progress import (
    ProgressEvent,
    ProgressTracker,
)
from sovyx.voice.wake_word_training._state import (
    TrainingJobState,
    TrainingStatus,
)
from sovyx.voice.wake_word_training._synthesizer import (
    KokoroSampleSynthesizer,
    SynthesisRequest,
    SynthesisResult,
)
from sovyx.voice.wake_word_training._trainer_protocol import (
    NoBackendRegisteredError,
    TrainerBackend,
    TrainingCancelledError,
    register_default_backend,
    resolve_default_backend,
)

__all__ = [
    "KokoroSampleSynthesizer",
    "NoBackendRegisteredError",
    "ProgressEvent",
    "ProgressTracker",
    "SynthesisRequest",
    "SynthesisResult",
    "TrainerBackend",
    "TrainingCancelledError",
    "TrainingJobState",
    "TrainingStatus",
    "register_default_backend",
    "resolve_default_backend",
]
