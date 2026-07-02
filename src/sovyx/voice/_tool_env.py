"""Locale-pinned environment for external audio-tool subprocesses.

The Python voice stack string-matches the stdout/stderr of ``pactl`` /
``wpctl`` / ``amixer`` / ``alsaucm`` (labels like ``Name:`` / ``Mute:`` /
``Server Name:`` and error strings like ``Connection refused``). Those
strings are gettext-translated on non-English desktops, so parsing them
without pinning the locale silently degrades detection (mute/name/server
classification collapses to "not broken" on a pt_BR/de/fr session).

The project's Linux bash toolkit already codified the fix — its runner
exports ``LC_ALL=C`` / ``LANG=C`` "so every external tool ... emits
English messages that our parsers/regex can match". This module is the
Python-side twin of that discipline: every ``subprocess`` invocation of
a locale-sensitive audio tool passes ``env=linux_tool_env()``.

Audit anchor: MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02
finding LINUX-6.
"""

from __future__ import annotations

import os

__all__ = ["linux_tool_env"]


def linux_tool_env() -> dict[str, str]:
    """Return a copy of the process environment with the locale pinned to C.

    ``LC_ALL`` wins over every ``LC_*`` category and ``LANG``; ``LANG``
    is pinned too so a tool that (incorrectly) consults it directly
    still sees C. The rest of the environment is preserved — pactl and
    wpctl need ``XDG_RUNTIME_DIR`` / ``PULSE_*`` to find the session
    daemon.
    """
    return {**os.environ, "LC_ALL": "C", "LANG": "C"}
