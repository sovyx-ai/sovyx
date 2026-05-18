"""True-subprocess integration tests — `sovyx llm setup` (Mission C6 §T3.5).

Companion to ``tests/unit/cli/test_llm_setup.py`` (CliRunner). These
tests verify ``--non-interactive`` exit-code parity against the real
typer dispatch via the installed `sovyx` entry-point. Interactive
flows are not exercised here (no pty) — the CliRunner suite covers them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _sovyx_invocation() -> list[str]:
    return [sys.executable, "-m", "sovyx.cli.main"]


def _wide_env() -> dict[str, str]:
    """Subprocess env that forces a wide terminal so typer's Rich help
    renderer doesn't wrap flag names mid-character. CI runners default to
    COLUMNS=80 which collapses ``--non-interactive`` and ``--provider``
    into wrapped lines, breaking the substring assertions.
    """
    env = os.environ.copy()
    env["COLUMNS"] = "200"
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    return env


class TestLLMSetupSubprocess:
    def test_setup_help_lists_flags(self) -> None:
        result = subprocess.run(
            [*_sovyx_invocation(), "llm", "setup", "--help"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(_REPO_ROOT),
            env=_wide_env(),
        )
        assert result.returncode == 0
        for flag in ("--provider", "--api-key", "--non-interactive", "--data-dir"):
            assert flag in result.stdout, f"missing --help flag: {flag}"

    def test_setup_invalid_provider_exits_two(self) -> None:
        result = subprocess.run(
            [
                *_sovyx_invocation(),
                "llm",
                "setup",
                "--non-interactive",
                "--provider",
                "nonexistent_provider",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(_REPO_ROOT),
            env=_wide_env(),
        )
        assert result.returncode == 2
        assert "Unknown provider" in (result.stdout + result.stderr)

    def test_setup_non_interactive_no_provider_exits_two(self) -> None:
        result = subprocess.run(
            [*_sovyx_invocation(), "llm", "setup", "--non-interactive"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(_REPO_ROOT),
            env=_wide_env(),
        )
        assert result.returncode == 2

    def test_setup_cloud_provider_missing_key_exits_two(self) -> None:
        result = subprocess.run(
            [
                *_sovyx_invocation(),
                "llm",
                "setup",
                "--non-interactive",
                "--provider",
                "anthropic",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(_REPO_ROOT),
            env=_wide_env(),
        )
        assert result.returncode == 2
        # The setup wizard prints the requirement message
        combined = result.stdout + result.stderr
        assert "requires an API key" in combined or "api-key" in combined.lower()
