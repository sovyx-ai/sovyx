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


# ════════════════════════════════════════════════════════════════════
# rc.6 (Agent 2 C.3) — entrypoint validates --only layer letters.
# Pre-rc.6 a typo like ``--only A,J,Z`` produced a successful-looking
# run with an empty tarball (no layer matched). Operator wasted minutes
# with no error indication. The entrypoint now rejects unknown letters
# with an actionable error message.
# ════════════════════════════════════════════════════════════════════


class TestOnlyFlagEntrypointValidation:
    """``sovyx-voice-diag.sh --only <list>`` rejects unknown layer letters."""

    @staticmethod
    def _run_diag(*args: str) -> subprocess.CompletedProcess[str]:
        """Invoke the diag entrypoint script directly with the given args."""
        assert _BASH_BIN is not None
        diag_script = _BASH_LIB.parent.parent / "sovyx-voice-diag.sh"
        diag_script_posix = _to_bash_path(diag_script)
        return subprocess.run(  # noqa: S603 — controlled bash invocation
            [_BASH_BIN, diag_script_posix, *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_only_with_unknown_letter_rejected_with_actionable_error(self) -> None:
        result = self._run_diag("--only", "A,J,Z")
        # Exit code 2 = bash usage error per the entrypoint convention.
        assert result.returncode == 2, (
            f"unknown --only letter must yield exit 2; got {result.returncode}"
        )
        # Operator-readable stderr: cite the bad letter + suggest the fix.
        assert "Z" in result.stderr
        assert "Valid layers: A,B,C,D,E,F,G,H,I,J,K" in result.stderr
        assert "case-sensitive" in result.stderr

    def test_only_with_only_unknown_letter_also_rejected(self) -> None:
        """A single bad letter (no valid letter mixed in) MUST also reject
        — pre-rc.6, ``--only Z`` would silently no-op every layer.
        """
        result = self._run_diag("--only", "Z")
        assert result.returncode == 2
        assert "Z" in result.stderr

    def test_only_with_all_valid_letters_passes_validation(self) -> None:
        """``--only A,C,D,E,J`` passes the letter-validation gate.

        On Windows test hosts the diag still exits non-zero at the
        Linux-only gate AFTER validation, but the validation itself
        does NOT reject. We assert the validation error is absent.
        """
        result = self._run_diag("--only", "A,C,D,E,J")
        # Either exits 2 (Linux-only on Windows test host) or proceeds —
        # what matters is the validation error message is NOT in stderr.
        assert "Valid layers: A,B,C,D,E,F,G,H,I,J,K" not in result.stderr, (
            f"valid letters MUST pass the validation gate; got stderr: {result.stderr!r}"
        )

    def test_only_with_whitespace_in_list_handled(self) -> None:
        """Operator typing `--only "A, C, D"` (with spaces) MUST still work."""
        result = self._run_diag("--only", "A, C, D")
        # Validation should accept whitespace; "Valid layers" rejection
        # must NOT appear.
        assert "Valid layers: A,B,C,D,E,F,G,H,I,J,K" not in result.stderr


# ════════════════════════════════════════════════════════════════════
# rc.3 (Agent 2 #8) — trap-EXIT cleans up /tmp/.sovyx_prompts_err.<pid>
# ════════════════════════════════════════════════════════════════════


class TestPromptsErrFileCleanup:
    """``_cleanup`` removes the per-pid prompts-err capture file.

    QA-FIX-3 (v0.31.0-rc.2) added stderr-capture for
    ``prompt_emit_structured`` with the path
    ``/tmp/.sovyx_prompts_err.$$``. The inline ``rm -f`` after each
    call covers the happy path, but a SIGTERM/SIGINT/SIGHUP between
    the echo and the rm leaks the file.

    rc.3 (Agent 2 #8) extends ``_cleanup`` (registered via trap on
    EXIT/INT/TERM/HUP) to mop up the file. SIGKILL inherently leaks
    one such file per process death (no userspace handler can run);
    the file is ≤ 4 KB so the leak is bounded.
    """

    def test_cleanup_line_present_in_common_sh(self) -> None:
        """Regression-grep: the cleanup line MUST be inside ``_cleanup``."""
        text = _BASH_LIB.read_text(encoding="utf-8")
        # The cleanup line lives between the trap-EXIT body and the
        # final ``exit "$exit_code"``.
        assert 'rm -f "/tmp/.sovyx_prompts_err.$$"' in text, (
            "rc.3 regression: _cleanup must remove the per-pid prompts-err "
            "capture file so SIGTERM/INT/HUP exits don't leak /tmp files"
        )

    def test_cleanup_removes_prompts_err_file(self, tmp_path: Path) -> None:
        """Functional: pre-create the file + call ``_cleanup`` + assert gone.

        We invoke ``_cleanup`` in a subshell so its ``exit`` doesn't kill
        the parent bash. The test bash records its own ``$$`` BEFORE the
        subshell so we can verify the file removal AFTER ``_cleanup``
        completed (the subshell inherits parent's ``$$`` via
        ``BASH_SUBSHELL``-aware ``$BASHPID`` not ``$$``; bash 4+ docs).
        """
        # The file path uses parent shell's PID. We capture it BEFORE
        # invoking _cleanup in a subshell. Touch the file, invoke
        # _cleanup in a () subshell (so its ``exit`` exits only the
        # subshell), then check the file is gone.
        snippet = (
            'errfile="/tmp/.sovyx_prompts_err.$$"; '
            'touch "$errfile"; '
            '[[ -f "$errfile" ]] && echo BEFORE_EXISTS || echo BEFORE_MISSING; '
            # Set the run-completed sentinel so _cleanup's "partial"
            # branch doesn't trigger; mute its log spam.
            "SOVYX_DIAG_RUN_COMPLETED=1; "
            'SOVYX_DIAG_OUTDIR=""; '
            # Run _cleanup in a subshell so its exit doesn't kill us.
            "(_cleanup) >/dev/null 2>&1; "
            '[[ -f "$errfile" ]] && echo AFTER_EXISTS || echo AFTER_REMOVED'
        )
        result = _bash(snippet)
        lines = [line for line in result.stdout.strip().splitlines() if line]
        assert "BEFORE_EXISTS" in lines, (
            f"setup failed: file not created. stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "AFTER_REMOVED" in lines, (
            f"_cleanup did not remove /tmp/.sovyx_prompts_err.<pid>; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
