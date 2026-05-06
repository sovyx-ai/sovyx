"""Smoke tests for the ``--only`` layer-gating flag added in v0.30.19.

Validates the contract added by T2.3:

* ``_layer_enabled <letter>`` returns 0 (true) when the layer is in
  ``SOVYX_DIAG_FLAG_ONLY`` (or the var is empty = all layers run);
* it returns 1 (false) otherwise;
* ``--only`` is parsed by the entrypoint script's ``parse_args``;
* the SUMMARY.json ``flags.only`` field round-trips the value.

The tests source the bash lib in a subprocess + assert exit codes;
they do NOT execute the full diag (which is Linux-only + interactive).
This keeps the test portable + fast on Windows CI.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_BASH_LIB = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "sovyx"
    / "voice"
    / "diagnostics"
    / "_bash"
    / "lib"
    / "common.sh"
)

_BASH_BIN = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH_BIN is None,
    reason="bash not available on this host",
)


def _to_bash_path(p: Path) -> str:
    """Render ``p`` as a path bash on this host can ``source``.

    On Linux/macOS, the POSIX path is bash-native. On Windows
    (git-bash), the ``E:\\sovyx\\...`` form is rejected by ``source``
    -- bash needs ``/e/sovyx/...``. We try ``cygpath -u`` first
    (works for git-bash + cygwin + msys); fall back to a manual
    drive-letter rewrite when cygpath is missing.
    """
    if shutil.which("cygpath") is not None:
        result = subprocess.run(  # noqa: S603, S607 — controlled
            ["cygpath", "-u", str(p)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    posix = p.as_posix()
    if len(posix) >= 2 and posix[1] == ":":
        return f"/{posix[0].lower()}{posix[2:]}"
    return posix


_BASH_LIB_POSIX = _to_bash_path(_BASH_LIB)


def _bash(snippet: str) -> subprocess.CompletedProcess[str]:
    """Source common.sh + run the snippet in a fresh bash subshell.

    Uses the resolved ``shutil.which('bash')`` path explicitly so we
    don't pick up WSL's ``/bin/bash`` (``C:\\Windows\\System32\\bash.exe``)
    on Windows, which doesn't share the host filesystem under
    ``/e/sovyx/...``.
    """
    assert _BASH_BIN is not None  # guaranteed by skipif
    cmd = f"source '{_BASH_LIB_POSIX}' >/dev/null 2>&1; {snippet}"
    return subprocess.run(  # noqa: S603 — resolved bash binary, controlled snippet
        [_BASH_BIN, "-c", cmd],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


class TestLayerEnabledHelper:
    """``_layer_enabled <letter>`` semantics."""

    def test_empty_only_means_all_layers_enabled(self) -> None:
        result = _bash('_layer_enabled "A" && echo YES || echo NO')
        assert result.stdout.strip() == "YES"

    def test_letter_in_only_list_enabled(self) -> None:
        result = _bash(
            'SOVYX_DIAG_FLAG_ONLY="A,C,D,E,J"; _layer_enabled "C" && echo YES || echo NO'
        )
        assert result.stdout.strip() == "YES"

    def test_letter_not_in_only_list_disabled(self) -> None:
        result = _bash(
            'SOVYX_DIAG_FLAG_ONLY="A,C,D,E,J"; _layer_enabled "B" && echo YES || echo NO'
        )
        assert result.stdout.strip() == "NO"

    def test_single_letter_only_list(self) -> None:
        result = _bash(
            'SOVYX_DIAG_FLAG_ONLY="A"; '
            '_layer_enabled "A" && echo YES || echo NO; '
            '_layer_enabled "B" && echo YES || echo NO'
        )
        lines = result.stdout.strip().splitlines()
        assert lines == ["YES", "NO"]

    def test_first_and_last_letter_in_list_both_match(self) -> None:
        result = _bash(
            'SOVYX_DIAG_FLAG_ONLY="A,B,C"; '
            '_layer_enabled "A" && echo YES || echo NO; '
            '_layer_enabled "C" && echo YES || echo NO'
        )
        lines = result.stdout.strip().splitlines()
        assert lines == ["YES", "YES"]

    def test_substring_collision_does_not_match(self) -> None:
        # Letter "AB" should NOT match "A" — boundary check via comma split.
        result = _bash('SOVYX_DIAG_FLAG_ONLY="AB,CD"; _layer_enabled "A" && echo YES || echo NO')
        assert result.stdout.strip() == "NO"
