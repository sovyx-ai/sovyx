"""L3 — Voice Capture Health Lifecycle probe.

Single entry point for the two probe modes described in
``docs-internal/ADR-voice-capture-health-lifecycle.md`` §4.3:

* :attr:`~sovyx.voice.health.contract.ProbeMode.COLD` — boot-time
  validation with no user speaking. Verifies that the stream opens
  cleanly and that PortAudio callbacks are firing. Cannot tell a silent
  room apart from a destroyed signal, so its diagnosis surface is
  deliberately coarse (``HEALTHY`` / ``NO_SIGNAL`` / open-error family).

* :attr:`~sovyx.voice.health.contract.ProbeMode.WARM` — wizard or
  first-interaction validation where the user is asked to speak. Runs
  the captured audio through :class:`~sovyx.voice._frame_normalizer.FrameNormalizer`
  and :class:`~sovyx.voice.vad.SileroVAD` so the probe can derive the
  full :class:`~sovyx.voice.health.contract.Diagnosis` surface (in
  particular :attr:`~sovyx.voice.health.contract.Diagnosis.APO_DEGRADED`,
  which requires signal *content* evidence — healthy RMS + dead VAD).

Subpackage layout (per anti-pattern #16, split from the v0.23.x
single-file ``probe.py``):

* ``_classifier.py`` — pure signal-processing helpers + threshold
  constants (``_RMS_DB_*``, ``_VAD_*``, ``_TARGET_PIPELINE_*``,
  ``_compute_rms_db``, ``_format_scale``, ``_warmup_samples``).
* ``_cold.py`` — open-error keyword sets, :func:`_classify_open_error`,
  the Voice Windows Paranoid Mission Furo W-1 flag
  (``_COLD_STRICT_VALIDATION_ENABLED``), and :func:`_diagnose_cold`.
* ``_warm.py`` — :func:`_analyse_rms` / :func:`_analyse_vad`,
  :func:`_default_frame_normalizer_factory`, :func:`_diagnose_warm`.
* ``_dispatch.py`` — top-level :func:`probe` + :func:`_run_probe` core
  loop + sounddevice / WASAPI stream lifecycle helpers.

Public API: only :func:`probe` (re-exported via ``__all__`` below).

Private re-exports: existing call sites in ``cascade.py``,
``_factory_integration.py``, and the test suite import several
underscore-prefixed names directly (e.g. ``_classify_open_error``,
``_diagnose_cold``, ``record_start_time_error``). These are re-bound
at this package level to preserve the v0.23.x import contract per
anti-pattern #20.

**Test patches that monkeypatch module-level constants must target the
underlying submodule path** (e.g. ``sovyx.voice.health.probe._cold``),
NOT this package — package-level rebinds are read once at import and
don't reflect later mutations to the submodule's namespace.
"""

from __future__ import annotations

# Backwards-compat re-exports — every name listed in ``__all__`` below
# was importable from the v0.23.x ``sovyx.voice.health.probe`` module.
# Keeping the public-by-history surface stable means existing call
# sites in cascade.py / _factory_integration.py / external tests
# continue to work post-split. New consumers should import directly
# from the submodule that owns each name (anti-pattern #20: package-
# level rebinds don't propagate monkeypatches to the submodule).
from sovyx.voice.health._metrics import record_start_time_error
from sovyx.voice.health.probe._classifier import (
    _RMS_DB_LOW_SIGNAL_CEILING,
    _RMS_DB_NO_SIGNAL_CEILING,
    _TARGET_PIPELINE_RATE,
    _TARGET_PIPELINE_WINDOW,
    _VAD_APO_DEGRADED_CEILING,
    _VAD_HEALTHY_FLOOR,
    _WARMUP_DISCARD_MS,
    _compute_rms_db,
    _format_scale,
    _linear_to_db,
    _warmup_samples,
)
from sovyx.voice.health.probe._cold import (
    _COLD_STRICT_VALIDATION_ENABLED,
    _DEVICE_BUSY_KEYWORDS,
    _FORMAT_MISMATCH_KEYWORDS,
    _KERNEL_INVALIDATED_KEYWORDS,
    _PERMISSION_KEYWORDS,
    _classify_open_error,
    _diagnose_cold,
)
from sovyx.voice.health.probe._dispatch import (
    _DEFAULT_COLD_DURATION_MS,
    _DEFAULT_WARM_DURATION_MS,
    _FORMAT_TO_SD_DTYPE,
    _HARD_TIMEOUT_S,
    InputStreamLike,
    SoundDeviceModule,
    _build_probe_wasapi_settings,
    _combo_tag,
    _load_sounddevice,
    _open_input_stream,
    _run_probe,
    probe,
)
from sovyx.voice.health.probe._warm import (
    _analyse_rms,
    _analyse_vad,
    _default_frame_normalizer_factory,
    _diagnose_warm,
)

# The only name promoted to the documented public surface is ``probe``.
# Every other entry below is a back-compat re-export (anti-pattern #20
# discipline — keep the v0.23.x import contract working).
__all__ = [
    "InputStreamLike",
    "SoundDeviceModule",
    "_COLD_STRICT_VALIDATION_ENABLED",
    "_DEFAULT_COLD_DURATION_MS",
    "_DEFAULT_WARM_DURATION_MS",
    "_DEVICE_BUSY_KEYWORDS",
    "_FORMAT_MISMATCH_KEYWORDS",
    "_FORMAT_TO_SD_DTYPE",
    "_HARD_TIMEOUT_S",
    "_KERNEL_INVALIDATED_KEYWORDS",
    "_PERMISSION_KEYWORDS",
    "_RMS_DB_LOW_SIGNAL_CEILING",
    "_RMS_DB_NO_SIGNAL_CEILING",
    "_TARGET_PIPELINE_RATE",
    "_TARGET_PIPELINE_WINDOW",
    "_VAD_APO_DEGRADED_CEILING",
    "_VAD_HEALTHY_FLOOR",
    "_WARMUP_DISCARD_MS",
    "_analyse_rms",
    "_analyse_vad",
    "_build_probe_wasapi_settings",
    "_classify_open_error",
    "_combo_tag",
    "_compute_rms_db",
    "_default_frame_normalizer_factory",
    "_diagnose_cold",
    "_diagnose_warm",
    "_format_scale",
    "_linear_to_db",
    "_load_sounddevice",
    "_open_input_stream",
    "_run_probe",
    "_warmup_samples",
    "probe",
    "record_start_time_error",
]
