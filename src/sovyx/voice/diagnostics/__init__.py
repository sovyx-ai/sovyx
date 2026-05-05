"""Voice diagnostics: full forensic diag toolkit + triage analyzer.

This package owns the in-process voice diagnostic surface that ships
with Sovyx. The bash diag toolkit (``sovyx-voice-diag.sh`` + ``lib/``)
is bundled as wheel data under :mod:`sovyx.voice.diagnostics._bash`
and exposed at runtime via :mod:`importlib.resources`. The triage
analyzer ports the standalone ``tools/voice_diag_triage.py`` into a
typed Python module (:mod:`sovyx.voice.diagnostics.triage`).

Public surface (post-Layer 1, mission
``MISSION-voice-self-calibrating-system-2026-05-05.md``):

* :class:`TriageResult` -- structured triage verdict
* :class:`HypothesisVerdict` -- one ranked hypothesis with confidence
* :class:`HypothesisId` -- closed enum of supported hypotheses
* :func:`triage_tarball` -- analyze a diag tarball, return TriageResult
* :func:`render_markdown` -- render TriageResult as operator markdown
* :func:`run_full_diag` -- orchestrate end-to-end diag run + triage
* :class:`DiagRunResult` -- result of a successful diag run
* :class:`DiagRunError` -- raised on selftest fail or non-zero exit

The bash toolkit lives under :mod:`sovyx.voice.diagnostics._bash` as
package data. Its standalone Python helpers under ``_bash/lib/py/``
are *not* part of the Sovyx import surface; they are executed by the
bash orchestrator via ``python3`` subprocess invocation and excluded
from project linters via ``[tool.ruff]`` and ``[tool.mypy]``
configuration.
"""

from __future__ import annotations
