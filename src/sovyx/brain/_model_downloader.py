"""Brain-specific model URL table + re-export of the shared downloader.

The generic :class:`~sovyx.engine._model_downloader.ModelDownloader` lives
under :mod:`sovyx.engine` because both brain and voice consume it. This
module keeps the brain-facing surface stable:

- Module-level constants (``MODEL_URL``, ``MODEL_URLS``, ``MODEL_SHA256``, …)
  are still importable from here.
- Cooldown helpers (``_is_in_cooldown``, ``_write_cooldown``, …) are
  re-exported so legacy test patches continue to resolve.
- ``ModelDownloader`` is re-exported verbatim. Brain callers that want the
  brain-tier cooldown should construct it with
  ``ModelDownloader(..., cooldown_seconds=_COOLDOWN_SECONDS)``.

This module carries no runtime logic of its own — it is a naming layer
so the pre-split ``from sovyx.brain._model_downloader import ...`` paths
keep working.
"""

from __future__ import annotations

from sovyx.engine._model_downloader import (
    DownloadAttempt,
    ModelDownloader,
    ModelDownloadError,
    _clear_cooldown,
    _cooldown_path,
    _is_in_cooldown,
    _is_permanent,
    _is_transient,
    _write_cooldown,
)
from sovyx.engine.config import BrainTuningConfig as _BrainTuning
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Model constants ─────────────────────────────────────────────────────────

MODEL_FILENAME = "e5-small-v2.onnx"
MODEL_SHA256 = "4b8205be2a3c5fc53c6534d76a2012064f7309c162b806f2889c6ec8ec4fdcba"
TOKENIZER_FILENAME = "tokenizer.json"
TOKENIZER_SHA256 = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"
MODEL_DIMENSIONS = 384
MAX_TOKENS = 512

# Primary + mirror URLs for resilience.
# Order: HuggingFace (canonical) → GitHub Releases (mirror).
MODEL_URLS: tuple[str, ...] = (
    "https://huggingface.co/intfloat/e5-small-v2/resolve/main/model.onnx",
    "https://github.com/sovyx-ai/sovyx/releases/download/models-v1/e5-small-v2.onnx",
)
TOKENIZER_URLS: tuple[str, ...] = (
    "https://huggingface.co/intfloat/e5-small-v2/resolve/main/tokenizer.json",
    "https://github.com/sovyx-ai/sovyx/releases/download/models-v1/tokenizer.json",
)

# Backward compatibility aliases (single URL).
MODEL_URL = MODEL_URLS[0]
TOKENIZER_URL = TOKENIZER_URLS[0]


# ── Brain-tier cooldown default ─────────────────────────────────────────────
#
# Sourced from :class:`BrainTuningConfig.model_download_cooldown_seconds`;
# overridable via ``SOVYX_TUNING__BRAIN__MODEL_DOWNLOAD_COOLDOWN_SECONDS``.
_COOLDOWN_SECONDS = _BrainTuning().model_download_cooldown_seconds


__all__ = [
    "MAX_TOKENS",
    "MODEL_DIMENSIONS",
    "MODEL_FILENAME",
    "MODEL_SHA256",
    "MODEL_URL",
    "MODEL_URLS",
    "TOKENIZER_FILENAME",
    "TOKENIZER_SHA256",
    "TOKENIZER_URL",
    "TOKENIZER_URLS",
    "DownloadAttempt",
    "ModelDownloadError",
    "ModelDownloader",
    "_COOLDOWN_SECONDS",
    "_clear_cooldown",
    "_cooldown_path",
    "_is_in_cooldown",
    "_is_permanent",
    "_is_transient",
    "_write_cooldown",
]
