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
    * :func:`run_full_diag_async` -- async-native primary; cancellable
      mid-run via ``asyncio.CancelledError`` propagation
    * :func:`run_full_diag` -- thin sync wrapper around the async
      primary (``asyncio.run``) for CLI callers
    * :class:`DiagRunResult` -- frozen dataclass with tarball + duration
    * :class:`DiagRunError` -- raised on selftest fail / non-zero exit
    * :class:`DiagPrerequisiteError` -- raised on non-Linux / missing bash

Cancellation contract (P2 v0.30.30):

    The async path uses :func:`asyncio.create_subprocess_exec` with
    ``start_new_session=True`` so the bash diag becomes a process
    group leader; when the awaiting task is cancelled, the runner
    sends ``SIGTERM`` to the entire process group (all children:
    arecord, pactl, pw-record, sox, etc.), waits up to 10s for the
    bash trap-EXIT cleanup to run, then escalates to ``SIGKILL`` if
    the group is still alive. Windows uses :func:`Process.terminate`
    (per-process; group support deferred to a future minor).

    The 10s grace period is critical: the bash ``_cleanup`` function
    in ``common.sh`` restores Sovyx daemon state (un-disables the
    voice pipeline). A naive SIGKILL leaves the daemon disabled.

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

import asyncio
import contextlib
import importlib.resources
import os
import shutil
import signal
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
# Bash reality (verified via ``finalize.sh::_build_tarball`` line 467):
# the tarball lands at ``$parent/${base}${suffix}.tar.gz`` where ``base``
# is the basename of ``SOVYX_DIAG_OUTDIR`` (= ``sovyx-diag-${host}-${ts}-
# ${uuid}``) and ``suffix`` is empty on success or ``_PARTIAL`` on
# interrupted runs. So the tarball is a SIBLING of the work dir, not
# inside it. Pre-rc.16 the constant + ``_find_latest_result_tarball``
# encoded the misleading help-text claim ``<outdir>/sovyx-voice-diag_
# <hostname>_<ts>_<uuid>.tar.gz`` — but no production bash code ever
# emitted that path. The CI Voice-Bash-Diag-Smoke gate had been
# ``Skipped`` since rc.10 (because upstream gates were failing) so the
# bug surfaced only when the gate finally ran post-conftest enterprise
# fixes.
_RESULT_TARBALL_GLOB = "sovyx-diag-*.tar.gz"
_BASH_VERSION_CMD = ("bash", "-c", "echo $BASH_VERSINFO")
_BASH_VERSION_TIMEOUT_S = 5.0

# P2 cancellation tuning constants. Kept module-level rather than in
# EngineConfig.tuning because they're invariants of the bash trap
# contract (anti-pattern #17 calls for tuning knobs only when the
# operator might legitimately need to adjust them; this 10s grace
# matches the bash trap's empirical worst-case ≈1-2s + safety margin).
_CANCEL_GRACE_PERIOD_S = 10.0
_CANCEL_SIGKILL_WAIT_S = 5.0

# rc.12 (operator-debt P2): defense-in-depth slow-path watchdog. The
# bash diag's design budget is 8-12 minutes on healthy hardware; a
# value of 30 minutes covers a 2.5× safety multiplier for slow disk
# I/O / paged-out swap scenarios and still kills a hung diag long
# before the operator gives up. Caller can override via
# ``total_deadline_s`` parameter (None disables the watchdog -- the
# pre-rc.12 behaviour, kept available for CLI operators who explicitly
# want to wait indefinitely). Watchdog fires SIGTERM → grace → SIGKILL
# via the existing cancellation path so all the cleanup invariants
# (trap-EXIT, process-group teardown) still hold.
_DEFAULT_TOTAL_DEADLINE_S: float = 30 * 60.0  # 30 minutes


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


# Closed-enum spec §8.3: who triggered the diag run. ``cli`` = direct
# operator invocation via ``sovyx doctor voice --full-diag/--calibrate``.
# ``wizard`` = dashboard onboarding flow's slow-path orchestrator.
# Bounded cardinality at 2 values; future trigger sources (e.g. cron,
# RPC) extend this enum + the spec.
_TRIGGER_VALUES: tuple[str, ...] = ("cli", "wizard")


async def run_full_diag_async(
    *,
    extra_args: tuple[str, ...] = (),
    output_root: Path | None = None,
    trigger: str = "cli",
    env_overrides: dict[str, str] | None = None,
    total_deadline_s: float | None = _DEFAULT_TOTAL_DEADLINE_S,
) -> DiagRunResult:
    """Async-native version of :func:`run_full_diag`.

    Identical semantics + return shape, but the underlying subprocess
    is spawned via :func:`asyncio.create_subprocess_exec` so the
    awaiting task can be cancelled mid-run; on
    :class:`asyncio.CancelledError` the runner signals the bash
    process group with SIGTERM (POSIX) or terminates the process
    (Windows), waits up to :data:`_CANCEL_GRACE_PERIOD_S` for the
    bash trap-EXIT cleanup to run, then escalates to SIGKILL if the
    group is still alive. The CancelledError is re-raised once the
    subprocess has been terminated so callers see the cancellation
    propagate cleanly.

    Args:
        extra_args: Same as :func:`run_full_diag`.
        output_root: Same as :func:`run_full_diag`.
        trigger: Same as :func:`run_full_diag`.
        env_overrides: Optional mapping of environment variables to
            inject into the bash subprocess. Used by the wizard
            orchestrator to set ``SOVYX_DIAG_PROMPTS_FILE`` so the
            bash side emits structured prompts to a JSONL file the
            orchestrator tails (P3 capture-prompt protocol). When
            ``None`` (default), the subprocess inherits the parent's
            full environment via ``os.environ.copy()``.
        total_deadline_s: rc.12 defense-in-depth watchdog. Maximum
            wall-clock time the bash diag is allowed to run before
            the runner cancels it via the same SIGTERM-grace-SIGKILL
            path the operator-cancellation flow uses. Defaults to
            :data:`_DEFAULT_TOTAL_DEADLINE_S` (30 minutes — 2.5× the
            12-minute design budget). Pass ``None`` to disable the
            watchdog (pre-rc.12 behaviour, kept for CLI operators
            who explicitly want to wait indefinitely on slow hosts).
            Operator-cancellation still works regardless of this
            field.

    Returns:
        :class:`DiagRunResult` on successful completion.

    Raises:
        DiagPrerequisiteError: same pre-flight contract as the sync
            entry point.
        DiagRunError: same post-run contract.
        asyncio.CancelledError: re-raised after best-effort process
            termination if the awaiting task was cancelled. Also
            raised when the watchdog fires (caller cannot tell the
            two apart from the exception alone — the
            ``voice.diagnostics.full_diag_watchdog_fired`` log event
            distinguishes them).
    """
    _check_prerequisites()

    if trigger not in _TRIGGER_VALUES:
        # Defensive: spec §8.3 closed enum. If a caller passes an
        # unknown trigger we coerce to "cli" rather than poisoning
        # OTel cardinality with arbitrary strings.
        trigger = "cli"

    mode = _classify_diag_mode(extra_args)
    logger.info(
        "voice.diagnostics.full_diag_started",
        trigger=trigger,
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
        # ``start_new_session=True`` makes bash the process group
        # leader on POSIX; on Windows it's silently a no-op
        # (asyncio.create_subprocess_exec doesn't expose creationflags).
        # The kwarg works on both platforms even if the effect differs.
        spawn_kwargs: dict[str, object] = {
            "stdin": sys.stdin,
            "stdout": sys.stdout,
            "stderr": sys.stderr,
        }
        if sys.platform != "win32":
            spawn_kwargs["start_new_session"] = True
        # Inject env overrides (P3 capture-prompt protocol uses this to
        # tell bash where to write prompts.jsonl). Default behaviour:
        # subprocess inherits full parent env via os.environ.copy.
        if env_overrides is not None:
            env = os.environ.copy()
            env.update(env_overrides)
            spawn_kwargs["env"] = env
        proc = await asyncio.create_subprocess_exec(*cmd, **spawn_kwargs)  # type: ignore[arg-type]
        try:
            # rc.12: wrap the bash wait in a watchdog timeout so a
            # hung diag (driver bug, blocked syscall, paged-out swap)
            # gets force-killed instead of hanging the wizard
            # forever. Operator-cancellation still flows through the
            # outer CancelledError handler. When ``total_deadline_s``
            # is None, fall through to the bare ``proc.wait()`` —
            # preserves the pre-rc.12 unbounded-wait contract for
            # CLI operators who explicitly opt out.
            if total_deadline_s is None:
                return_code = await proc.wait()
            else:
                try:
                    return_code = await asyncio.wait_for(
                        proc.wait(),
                        timeout=total_deadline_s,
                    )
                except TimeoutError:
                    logger.warning(
                        "voice.diagnostics.full_diag_watchdog_fired",
                        mode=mode,
                        deadline_s=total_deadline_s,
                        elapsed_s=round(time.monotonic() - start, 3),
                    )
                    await _cancel_process_tree(proc, grace_period_s=_CANCEL_GRACE_PERIOD_S)
                    raise DiagRunError(
                        f"diag exceeded the {total_deadline_s:.0f}s watchdog "
                        f"deadline (design budget 8-12 min); SIGTERM-grace-"
                        f"SIGKILL escalation completed. Re-run on a less-"
                        f"loaded host or pass --no-deadline if the diag is "
                        f"genuinely slow on this hardware.",
                        exit_code=-1,
                    ) from None
        except asyncio.CancelledError:
            await _cancel_process_tree(proc, grace_period_s=_CANCEL_GRACE_PERIOD_S)
            raise
        duration_s = time.monotonic() - start

        if return_code != 0:
            logger.warning(
                "voice.diagnostics.full_diag_failed",
                mode=mode,
                exit_code=return_code,
                duration_s=round(duration_s, 3),
                failure_reason=("selftest_failed" if return_code == 3 else "non_zero_exit"),
            )
            raise DiagRunError(
                f"diag exited with code {return_code} "
                f"(rc=3 typically means analyzer selftest failed; see stderr above)",
                exit_code=return_code,
                partial_output_dir=_find_latest_result_dir(output_root),
            )

        tarball = _find_latest_result_tarball(output_root)
        if tarball is None:
            logger.warning(
                "voice.diagnostics.full_diag_failed",
                mode=mode,
                exit_code=return_code,
                duration_s=round(duration_s, 3),
                failure_reason="tarball_missing",
            )
            raise DiagRunError(
                "diag exited cleanly but no result tarball found under "
                f"{output_root or Path.home()} matching {_RESULT_DIR_GLOB}/"
                f"{_RESULT_TARBALL_GLOB} — packaging step likely failed",
                exit_code=return_code,
                partial_output_dir=_find_latest_result_dir(output_root),
            )

        logger.info(
            "voice.diagnostics.full_diag_completed",
            mode=mode,
            duration_s=round(duration_s, 3),
            exit_code=return_code,
            tarball_size_bytes=tarball.stat().st_size,
            # Spec §8.3 prescribes hypothesis_winner here. The runner
            # has zero knowledge of triage (which runs AFTER the diag
            # exits + reads the tarball); we emit empty string from
            # this layer + downstream callers (CLI _run_voice_calibrate,
            # wizard orchestrator's slow-path) emit triage-aware events
            # via voice.calibration.engine.run_completed which carries
            # triage_winner_hid. Keeping the field in this event with
            # an explicit empty value preserves the spec field set
            # without falsely claiming knowledge the runner doesn't have.
            hypothesis_winner="",
        )

        return DiagRunResult(
            tarball_path=tarball,
            duration_s=duration_s,
            exit_code=return_code,
        )
    finally:
        shutil.rmtree(extracted, ignore_errors=True)


def run_full_diag(
    *,
    extra_args: tuple[str, ...] = (),
    output_root: Path | None = None,
    trigger: str = "cli",
) -> DiagRunResult:
    """Materialize the bundled bash diag, run it interactively, return the tarball path.

    The function blocks until the diag exits (typically 8-12 minutes
    for a default run with ``--yes``). The operator's stdin/stdout/stderr
    are attached so the interactive capture prompts ("speak now") reach
    the terminal and the operator's keyboard reaches the script.

    Sync convenience wrapper around :func:`run_full_diag_async`; CLI
    callers (``sovyx doctor voice --full-diag``) use this. Async
    callers (the wizard orchestrator) call
    :func:`run_full_diag_async` directly so cancellation propagates.

    Args:
        extra_args: Additional flags appended after ``--yes`` when
            invoking the script. Used by the calibration engine
            (L2.T2.3) to pass scope-narrowing flags such as
            ``--only A,C,D,E,J``. Empty by default.
        output_root: Override the directory under which the result
            tarball is searched. Defaults to ``Path.home()``. Provided
            for testability and for operators with non-default
            ``$HOME`` setups.
        trigger: Closed enum (``"cli"`` | ``"wizard"``) that propagates
            into the ``voice.diagnostics.full_diag_started{trigger=...}``
            telemetry field per spec §8.3. Defaults to ``"cli"`` so
            direct CLI callers don't need to override; the wizard
            orchestrator passes ``"wizard"``.

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
    return asyncio.run(
        run_full_diag_async(
            extra_args=extra_args,
            output_root=output_root,
            trigger=trigger,
        )
    )


# ====================================================================
# Cancellation
# ====================================================================


async def _cancel_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    grace_period_s: float = _CANCEL_GRACE_PERIOD_S,
) -> None:
    """Best-effort: signal the subprocess (+ its group on POSIX) to exit.

    POSIX path:
        1. ``os.killpg(proc.pid, SIGTERM)`` signals the entire process
           group spawned with ``start_new_session=True`` (bash + every
           child it forked: arecord, pactl, etc.).
        2. Wait ``grace_period_s`` for the bash trap-EXIT handler to
           run (restores Sovyx daemon state).
        3. If the process is still alive, ``os.killpg(proc.pid,
           SIGKILL)`` and wait :data:`_CANCEL_SIGKILL_WAIT_S` for the
           kernel to reap.

    Windows path:
        1. ``proc.terminate()`` (TerminateProcess) on the bash process.
           Does NOT propagate to children — that's accepted v1
           limitation; CTRL_BREAK_EVENT-style group support is deferred
           to a future minor.
        2. Same grace + escalation pattern via ``proc.kill()``.

    Telemetry:
        * ``voice.diagnostics.cancel_grace_expired`` fires when the
          grace period elapses without exit (escalation triggered).
        * ``voice.diagnostics.cancel_completed`` fires after the final
          wait, with ``escalated_to_sigkill=True/False``.

    All ``ProcessLookupError`` from already-exited processes are
    suppressed; this method NEVER raises (callers re-raise the
    original ``CancelledError`` after this returns).
    """
    started_mono = time.monotonic()
    escalated_to_sigkill = False

    # Initial signal: SIGTERM (POSIX) or terminate (Windows).
    if sys.platform == "win32":
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_period_s)
    except TimeoutError:
        # Grace period expired; escalate.
        escalated_to_sigkill = True
        logger.warning(
            "voice.diagnostics.cancel_grace_expired",
            grace_period_s=grace_period_s,
        )
        if sys.platform == "win32":
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

        # Best-effort wait so the asyncio loop sees the child reaped
        # before _cancel_process_tree returns. CPython issue #119710:
        # without this, the asyncio.run() event-loop close path can
        # itself hang on a zombie subprocess.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_CANCEL_SIGKILL_WAIT_S)

    duration_s = round(time.monotonic() - started_mono, 3)
    logger.info(
        "voice.diagnostics.cancel_completed",
        duration_s=duration_s,
        escalated_to_sigkill=escalated_to_sigkill,
    )


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
    """Return the newest ``sovyx-diag-*.tar.gz`` directly under ``output_root``.

    The tarball is a SIBLING of the work dir (both share the
    ``sovyx-diag-${host}-${ts}-${uuid}`` prefix; the dir has no suffix,
    the tarball has ``.tar.gz``). Search at the top level only —
    descending into the dir would never find the tarball. Filters out
    sub-directories that would otherwise match the glob (e.g. a stray
    ``sovyx-diag-name.tar.gz`` that's actually a directory).
    """
    root = output_root if output_root is not None else Path.home()
    candidates = [p for p in root.glob(_RESULT_TARBALL_GLOB) if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
