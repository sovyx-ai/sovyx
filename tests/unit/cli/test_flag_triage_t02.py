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
"""

from __future__ import annotations

from typer.testing import CliRunner

from sovyx.cli.main import app

runner = CliRunner()


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
        assert "--foreground" in result.stdout or "--foreground" in result.output

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
        assert "--quick" in result.stdout or "--quick" in result.output

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
        # Verify the flag is documented in --help output
        assert "--yes" in result.stdout or "--yes" in result.output
        assert "-y" in result.stdout or "-y" in result.output

    def test_yes_help_text_documents_asymmetry(self) -> None:
        """The flag's help text must surface the local-dir vs pip/git
        asymmetry so operators don't expect uniform behaviour."""
        result = runner.invoke(app, ["plugin", "install", "--help"])
        assert result.exit_code == 0
        out = result.stdout if result.stdout else result.output
        # Either "no-op for pip" or equivalent disambiguation must be present.
        assert (
            "no-op" in out.lower()
            or "trust the source" in out.lower()
            or "industry pattern" in out.lower()
        ), f"--yes help text must explain asymmetry; got {out!r}"
