"""Voice diagnostics: full forensic diag toolkit + triage analyzer.

This package owns the in-process voice diagnostic surface that ships
with Sovyx. The bash diag toolkit (``sovyx-voice-diag.sh`` + ``lib/``)
is bundled as wheel data under :mod:`sovyx.voice.diagnostics._bash`
and exposed at runtime via :mod:`importlib.resources`. The triage
analyzer is :mod:`sovyx.voice.diagnostics.triage`.

Public surface (post-Layer 1, mission
``MISSION-voice-self-calibrating-system-2026-05-05.md``):

* :class:`TriageResult` -- structured triage verdict
* :class:`HypothesisVerdict` -- one ranked hypothesis with confidence
* :class:`HypothesisId` -- closed enum of supported hypotheses
* :class:`SchemaValidation` -- schema validation outcome
* :class:`AlertsSummary` -- alerts severity breakdown
* :func:`triage_tarball` -- analyze a diag tarball, return TriageResult
* :func:`render_markdown` -- render TriageResult as operator markdown

T1.4 will add :func:`run_full_diag`, :class:`DiagRunResult`, and
:class:`DiagRunError` for end-to-end diag orchestration; T1.5 wires
``sovyx doctor voice --full-diag`` against this surface.

The bash toolkit lives under :mod:`sovyx.voice.diagnostics._bash` as
package data. Its standalone Python helpers under ``_bash/lib/py/``
are *not* part of the Sovyx import surface; they are executed by the
bash orchestrator via ``python3`` subprocess invocation and excluded
from project linters via ``[tool.ruff]`` and ``[tool.mypy]``
configuration.
"""

from __future__ import annotations

from sovyx.voice.diagnostics.triage import (
    AlertsSummary,
    HypothesisId,
    HypothesisVerdict,
    SchemaValidation,
    TriageResult,
    render_markdown,
    triage_tarball,
)

__all__ = [
    "AlertsSummary",
    "HypothesisId",
    "HypothesisVerdict",
    "SchemaValidation",
    "TriageResult",
    "render_markdown",
    "triage_tarball",
]
