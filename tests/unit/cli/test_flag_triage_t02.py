"""T02 mission test — CLI flag triage (Mission pre-wake-word-hardening-2026-05-02).

After T02:
* ``sovyx start --foreground`` is REMOVED (semantically redundant; ``start``
  already blocks in ``run_forever``). Typer must reject the flag.
* ``sovyx init --quick`` is REMOVED (init is non-interactive; no prompts to
  skip). Typer must reject the flag.
* ``sovyx plugin install --yes`` semantics documented in docstring: skips
  permission prompt for local-dir; no-op for pip/git (matching apt/pip/brew
  industry pattern). The flag itself stays accepted on all 3 paths.

This file pins the contract so future refactors don't reintroduce dead flags.

**ANSI-aware contract** — Rich (via Typer) emits ANSI escape codes on
non-Windows TTYs in CI (Linux + macOS runners). Naive
``'--foreground' in stdout`` substring matches break because the
hyphen + flag-name get split by colour codes
(``'\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-foreground\\x1b[0m'``). Tests
strip ANSI escape sequences before substring assertion.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from sovyx.cli.main import app

runner = CliRunner()


# Strip ANSI escape sequences from CLI output before substring matching.
# Pattern matches CSI (Control Sequence Introducer) sequences:
# ESC [ <params> <intermediate> <final-byte 0x40-0x7E>.
# Captures every Rich/click colour + style sequence that splits flag
# names mid-token in CI Linux/macOS Rich output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _output(result: object) -> str:
    """Return ANSI-stripped output from a CliRunner Result."""
    return _strip_ansi(getattr(result, "stdout", "") or getattr(result, "output", "") or "")


class TestStartForegroundRemoved:
    """``sovyx start --foreground`` was removed 2026-05-02 (T02)."""

    def test_foreground_flag_rejected(self) -> None:
        result = runner.invoke(app, ["start", "--foreground"])
        # Typer returns exit-code 2 with "no such option" when an unknown
        # flag is passed.
        assert result.exit_code == 2, (
            f"--foreground should be rejected as unknown option; "
            f"got exit_code={result.exit_code} stdout={result.stdout!r}"
        )
        assert "--foreground" in _output(result)

    def test_short_f_flag_rejected(self) -> None:
        result = runner.invoke(app, ["start", "-f"])
        assert result.exit_code == 2, (
            f"-f short alias should be rejected; "
            f"got exit_code={result.exit_code} stdout={result.stdout!r}"
        )


class TestInitQuickRemoved:
    """``sovyx init --quick`` was removed 2026-05-02 (T02)."""

    def test_quick_flag_rejected(self) -> None:
        result = runner.invoke(app, ["init", "TestMind", "--quick"])
        assert result.exit_code == 2, (
            f"--quick should be rejected as unknown option; "
            f"got exit_code={result.exit_code} stdout={result.stdout!r}"
        )
        assert "--quick" in _output(result)

    def test_short_q_flag_rejected(self) -> None:
        result = runner.invoke(app, ["init", "TestMind", "-q"])
        assert result.exit_code == 2, (
            f"-q short alias should be rejected; "
            f"got exit_code={result.exit_code} stdout={result.stdout!r}"
        )


class TestPluginInstallYesAccepted:
    """``sovyx plugin install --yes`` stays accepted on all 3 paths.

    The flag's semantics are asymmetric by design (matching apt/pip/brew
    industry pattern):
    * local-dir: skips the permission confirmation prompt
    * pip / git: no-op (no prompt to skip — operator's package/URL choice
      is the trust gate)
    """

    def test_yes_flag_accepted_in_help(self) -> None:
        result = runner.invoke(app, ["plugin", "install", "--help"])
        assert result.exit_code == 0
        # Verify the flag is documented in --help output (ANSI-stripped
        # so Rich's colour escapes don't split flag names mid-token).
        out = _output(result)
        assert "--yes" in out
        assert "-y" in out

    def test_yes_help_text_documents_asymmetry(self) -> None:
        """The flag's help text must surface the local-dir vs pip/git
        asymmetry so operators don't expect uniform behaviour."""
        result = runner.invoke(app, ["plugin", "install", "--help"])
        assert result.exit_code == 0
        out = _output(result).lower()
        # Either "no-op for pip" or equivalent disambiguation must be present.
        assert "no-op" in out or "trust the source" in out or "industry pattern" in out, (
            f"--yes help text must explain asymmetry; got {out!r}"
        )
