"""Diag-toolkit orchestrator: extract → exec → locate result tarball.

Runs the bundled bash diag toolkit
(:mod:`sovyx.voice.diagnostics._bash`) end-to-end: materializes the
script tree to a tempdir via :mod:`importlib.resources`, executes
``sovyx-voice-diag.sh --yes`` with the operator's terminal attached
for interactive prompts, then locates the result tarball under
``$HOME/sovyx-diag-*/`` and returns a typed :class:`DiagRunResult`.

The runner is invoked by ``sovyx doctor voice --full-diag``
(:mod:`sovyx.cli.commands.doctor` -- T1.5 of
``MISSION-voice-self-calibrating-system-2026-05-05.md``). It is also
the foundation for the calibration engine's targeted-measurement mode
(L2.T2.3), which will pass ``--only A,C,D,E,J`` via ``extra_args``.

Public surface:
    * :func:`run_full_diag` -- orchestrate the run; returns DiagRunResult
    * :class:`DiagRunResult` -- frozen dataclass with tarball + duration
    * :class:`DiagRunError` -- raised on selftest fail / non-zero exit
    * :class:`DiagPrerequisiteError` -- raised on non-Linux / missing bash

Failure-mode contract:
    * Non-Linux platform -> ``DiagPrerequisiteError``
    * bash missing / version <4 -> ``DiagPrerequisiteError``
    * Diag exits non-zero -> ``DiagRunError`` (exit_code populated;
      partial output dir preserved at ``$HOME/sovyx-diag-...`` for
      forensics)
    * Diag exits 0 but no result tarball at the expected path ->
      ``DiagRunError`` (impossible-by-contract; surfaces an
      operator-actionable error if the tarball assembly itself failed)

The temp script extraction directory is always cleaned up in a
``try/finally`` regardless of the run outcome; only the diag's own
output tarball under ``$HOME`` is preserved across calls.
"""

from __future__ import annotations

import importlib.resources
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

_DIAG_SCRIPT_NAME = "sovyx-voice-diag.sh"
_RESULT_DIR_GLOB = "sovyx-diag-*"
_RESULT_TARBALL_GLOB = "sovyx-voice-diag_*.tar.gz"
_BASH_VERSION_CMD = ("bash", "-c", "echo $BASH_VERSINFO")
_BASH_VERSION_TIMEOUT_S = 5.0


# ====================================================================
# Public types
# ====================================================================


@dataclass(frozen=True, slots=True)
class DiagRunResult:
    """Result of a successful diag run."""

    tarball_path: Path
    duration_s: float
    exit_code: int


class DiagPrerequisiteError(RuntimeError):
    """Host does not meet the prerequisites for running the diag toolkit.

    Examples:
        * The platform is not Linux.
        * ``bash`` is not installed.
        * Installed ``bash`` is older than 4.0 (the toolkit relies on
          bash-4+ array semantics and ``shopt -s nullglob``).
    """


class DiagRunError(RuntimeError):
    """The diag toolkit ran but reported a failure (non-zero exit / selftest abort).

    Attributes:
        exit_code: The integer exit code returned by ``bash``. ``3``
            specifically means the analyzer selftest failed
            (:mod:`sovyx.voice.diagnostics._bash.lib.selftest`); other
            non-zero values are scenario-specific.
        partial_output_dir: If the diag created an output directory at
            ``$HOME/sovyx-diag-<host>-<ts>-<uuid>/`` before failing,
            its path is preserved here for forensic inspection.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        partial_output_dir: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.partial_output_dir = partial_output_dir


# ====================================================================
# Public API
# ====================================================================


def _classify_diag_mode(extra_args: tuple[str, ...]) -> str:
    """Closed-enum classification of the diag run mode for telemetry.

    Returns ``"full"`` (default), ``"skip_captures"`` (when --skip-captures
    is among extra_args), or ``"surgical"`` (when --only is present).
    Bounded cardinality so the
    ``voice.diagnostics.full_diag_started{mode=...}`` label stays
    OTel-friendly.
    """
    if "--only" in extra_args:
        return "surgical"
    if "--skip-captures" in extra_args:
        return "skip_captures"
    return "full"


def run_full_diag(
    *,
    extra_args: tuple[str, ...] = (),
    output_root: Path | None = None,
) -> DiagRunResult:
    """Materialize the bundled bash diag, run it interactively, return the tarball path.

    The function blocks until the diag exits (typically 8-12 minutes
    for a default run with ``--yes``). The operator's stdin/stdout/stderr
    are attached so the interactive capture prompts ("speak now") reach
    the terminal and the operator's keyboard reaches the script.

    Args:
        extra_args: Additional flags appended after ``--yes`` when
            invoking the script. Used by the calibration engine
            (L2.T2.3) to pass scope-narrowing flags such as
            ``--only A,C,D,E,J``. Empty by default.
        output_root: Override the directory under which the result
            tarball is searched. Defaults to ``Path.home()``. Provided
            for testability and for operators with non-default
            ``$HOME`` setups.

    Returns:
        A :class:`DiagRunResult` carrying the absolute path to the
        result tarball, the wall-clock duration in seconds, and the
        bash exit code (always ``0`` on a successful return).

    Raises:
        DiagPrerequisiteError: if the host is not Linux, ``bash`` is
            missing, or ``bash`` is older than 4.0.
        DiagRunError: if the diag exits with a non-zero code or exits
            cleanly but produces no result tarball.
    """
    _check_prerequisites()

    mode = _classify_diag_mode(extra_args)
    logger.info(
        "voice.diagnostics.full_diag_started",
        mode=mode,
        extra_arg_count=len(extra_args),
    )

    extracted = _extract_bash_to_temp()
    try:
        script = extracted / _DIAG_SCRIPT_NAME
        if not script.is_file():
            raise DiagRunError(
                f"bundled diag script not found at {script} after extraction "
                "(wheel package data layout regression — please report)",
                exit_code=-1,
            )
        script.chmod(0o755)

        cmd: list[str] = ["bash", str(script), "--yes", *extra_args]
        start = time.monotonic()
        completed = subprocess.run(
            cmd,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False,
        )
        duration_s = time.monotonic() - start

        if completed.returncode != 0:
            logger.warning(
                "voice.diagnostics.full_diag_failed",
                mode=mode,
                exit_code=completed.returncode,
                duration_s=round(duration_s, 3),
                failure_reason=(
                    "selftest_failed" if completed.returncode == 3 else "non_zero_exit"
                ),
            )
            raise DiagRunError(
                f"diag exited with code {completed.returncode} "
                f"(rc=3 typically means analyzer selftest failed; see stderr above)",
                exit_code=completed.returncode,
                partial_output_dir=_find_latest_result_dir(output_root),
            )

        tarball = _find_latest_result_tarball(output_root)
        if tarball is None:
            logger.warning(
                "voice.diagnostics.full_diag_failed",
                mode=mode,
                exit_code=completed.returncode,
                duration_s=round(duration_s, 3),
                failure_reason="tarball_missing",
            )
            raise DiagRunError(
                "diag exited cleanly but no result tarball found under "
                f"{output_root or Path.home()} matching {_RESULT_DIR_GLOB}/"
                f"{_RESULT_TARBALL_GLOB} — packaging step likely failed",
                exit_code=completed.returncode,
                partial_output_dir=_find_latest_result_dir(output_root),
            )

        logger.info(
            "voice.diagnostics.full_diag_completed",
            mode=mode,
            duration_s=round(duration_s, 3),
            exit_code=completed.returncode,
            tarball_size_bytes=tarball.stat().st_size,
        )

        return DiagRunResult(
            tarball_path=tarball,
            duration_s=duration_s,
            exit_code=completed.returncode,
        )
    finally:
        shutil.rmtree(extracted, ignore_errors=True)


# ====================================================================
# Internal helpers
# ====================================================================


def _check_prerequisites() -> None:
    """Raise :class:`DiagPrerequisiteError` if the host can't run the diag."""
    if sys.platform != "linux":
        raise DiagPrerequisiteError(
            f"voice diag toolkit is Linux-only; current platform is {sys.platform!r}. "
            "On macOS or Windows, use `sovyx doctor voice` (cross-platform health checks) "
            "instead."
        )
    bash_path = shutil.which("bash")
    if bash_path is None:
        raise DiagPrerequisiteError(
            "bash is not installed or not in PATH. "
            "Install it via your distro package manager (apt/dnf/pacman/zypper/apk)."
        )
    major = _read_bash_major_version(bash_path)
    if major < 4:
        raise DiagPrerequisiteError(
            f"bash 4+ required (the diag uses bash-4+ array semantics + "
            f"`shopt -s nullglob`); detected bash {major} at {bash_path}"
        )


def _read_bash_major_version(bash_path: str) -> int:
    """Return the integer major version of the installed bash, or 0 on failure."""
    try:
        completed = subprocess.run(
            [bash_path, "-c", _BASH_VERSION_CMD[2]],
            capture_output=True,
            text=True,
            timeout=_BASH_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if completed.returncode != 0:
        return 0
    parts = completed.stdout.strip().split()
    if not parts:
        return 0
    try:
        return int(parts[0])
    except ValueError:
        return 0


def _extract_bash_to_temp() -> Path:
    """Materialize the bundled bash diag toolkit to a fresh temp dir.

    Uses :func:`importlib.resources.files` + :func:`importlib.resources.as_file`
    so the resolution works for both regular wheel installs (where the
    package data is on disk) and installed-from-zip scenarios (where
    ``as_file`` materializes the resource to a real filesystem path).
    """
    target = Path(tempfile.mkdtemp(prefix="sovyx-voice-diag-"))
    bash_root_resource = importlib.resources.files("sovyx.voice.diagnostics") / "_bash"
    with importlib.resources.as_file(bash_root_resource) as bash_src:
        shutil.copytree(bash_src, target, dirs_exist_ok=True)
    return target


def _find_latest_result_dir(output_root: Path | None) -> Path | None:
    """Return the most recently modified ``sovyx-diag-*/`` dir, or ``None``."""
    root = output_root if output_root is not None else Path.home()
    candidates = [p for p in root.glob(_RESULT_DIR_GLOB) if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _find_latest_result_tarball(output_root: Path | None) -> Path | None:
    """Return the newest ``sovyx-voice-diag_*.tar.gz`` under any ``sovyx-diag-*/``."""
    root = output_root if output_root is not None else Path.home()
    candidates: list[Path] = []
    for diag_dir in root.glob(_RESULT_DIR_GLOB):
        if not diag_dir.is_dir():
            continue
        candidates.extend(diag_dir.glob(_RESULT_TARBALL_GLOB))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
