#!/usr/bin/env python3
"""voice_diag_triage — back-compat wrapper around sovyx.voice.diagnostics.triage.

The triage analyzer's implementation moved to
:mod:`sovyx.voice.diagnostics.triage` in v0.30.14 (T1.3 of
``MISSION-voice-self-calibrating-system-2026-05-05.md``). This wrapper
preserves the legacy analyst CLI invocation::

    python tools/voice_diag_triage.py <tarball_path>
    python tools/voice_diag_triage.py --extract-dir <directory>

Equivalent invocations once Sovyx is installed:

    python -m sovyx.voice.diagnostics.triage <tarball_path>
    sovyx doctor voice --full-diag        # full diag + triage in-process

The wrapper is intentionally minimal so future improvements land in the
package module without further analyst-side changes.
"""

from __future__ import annotations

import sys

from sovyx.voice.diagnostics.triage import _cli_main

if __name__ == "__main__":
    sys.exit(_cli_main())
