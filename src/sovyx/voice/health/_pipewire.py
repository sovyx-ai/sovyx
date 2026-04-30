"""PipeWire / WirePlumber detection + module-echo-cancel routing (F3).

Layer 1 of the F1-spec'd 4-layer Linux mixer cascade. Pre-F3 the
cascade short-circuits to layers 2-4 (UCM, KB profiles, AGC2) even
when PipeWire's own echo-cancel module would solve the problem
cleanly without any user-space DSP. ~70% of 2026 Linux desktops
ship PipeWire by default (Ubuntu 24.04+, Fedora 36+, Arch); this
module is the load-bearing first defense for that majority.

Capabilities:

* :func:`detect_pipewire` — verdict + raw evidence (socket, pactl info).
* :func:`enumerate_pipewire_modules` — list of loaded modules via
  ``pactl list short modules``.
* :func:`is_echo_cancel_loaded` — convenience predicate keyed on the
  module list.
* :func:`load_echo_cancel_module` — opt-in helper that runs ``pactl
  load-module module-echo-cancel`` against a named source. Returns the
  module ID on success, raises :class:`PipeWireRoutingError` on
  failure with structured ``stderr`` + ``returncode`` for telemetry.

Design contract:

* **Never raises from detection**. Subprocess / parsing failures
  collapse into ``UNKNOWN`` with structured ``notes`` so the cascade
  can keep advancing through the remaining layers.
* **Loading is explicit**. Detection alone NEVER loads a module —
  layer 1 reports the verdict; the operator (or a future opt-in
  factory wire-up) decides whether to actually load echo-cancel.
* **No PipeWire Python bindings required**. Uses ``pactl`` (always
  shipped alongside PipeWire on PipeWire-only systems via the
  ``pipewire-pulse`` compat layer). Avoids a hard build-time
  dependency on the C bindings.

Reference: F1 inventory mission tasks F3; PipeWire docs on
``module-echo-cancel`` (``man pipewire`` § ``echo-cancel``).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted pactl binary
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Bounds + tunables ─────────────────────────────────────────────


_PACTL_TIMEOUT_S = 3.0
"""Wall-clock budget for any single ``pactl`` invocation. PipeWire
queries return in <50 ms on a healthy desktop; 3 s is generous
enough that a momentarily-busy daemon doesn't false-fail while
short enough that a wedged daemon doesn't stall preflight."""


_LOAD_MODULE_TIMEOUT_S = 5.0
"""Wall-clock budget for ``pactl load-module`` — slightly larger
than detection because the daemon has to spawn the module thread
and open the source. Still bounded so a wedged load doesn't hang
boot indefinitely."""


# ── Public types ──────────────────────────────────────────────────


class PipeWireStatus(StrEnum):
    """Closed-set verdict of :func:`detect_pipewire`.

    Anti-pattern #9: StrEnum value-based comparison stays xdist-safe
    and serialises verbatim into the structured-event ``status`` field
    that dashboards key on."""

    ABSENT = "absent"
    """No PipeWire daemon detected. Likely a pure ALSA / PulseAudio
    system; cascade should advance to layers 2-4."""

    RUNNING = "running"
    """PipeWire daemon is alive and responsive. Layer 1 is viable;
    operator may invoke :func:`load_echo_cancel_module` to engage."""

    RUNNING_WITH_ECHO_CANCEL = "running_with_echo_cancel"
    """PipeWire is alive AND ``module-echo-cancel`` is already loaded.
    Layer 1 is fully active — no further intervention needed; the
    cascade should treat this as a success and skip layers 2-4."""

    UNKNOWN = "unknown"
    """Detection failed (pactl missing, subprocess error, parse
    failure). Cascade should treat as ABSENT for routing decisions
    but surface UNKNOWN for telemetry attribution."""


@dataclass(frozen=True, slots=True)
class PipeWireReport:
    """Structured detection outcome.

    Carries enough detail for the cascade's verdict AND for the
    dashboard's ``GET /api/voice/status`` to surface the per-evidence
    trace (socket present? pactl OK? echo-cancel module loaded?)."""

    status: PipeWireStatus
    """Aggregated verdict."""

    socket_present: bool = False
    """``/run/user/$UID/pipewire-0`` exists. Cheapest indicator —
    confirms a PipeWire daemon was started for this user session."""

    pactl_available: bool = False
    """``pactl`` binary resolvable on PATH. Required for module
    enumeration + load."""

    pactl_info_ok: bool = False
    """``pactl info`` returned 0 + parseable. Distinguishes "PipeWire
    started but jammed" from "PipeWire healthy"."""

    server_name: str | None = None
    """Value of the ``Server Name:`` line in ``pactl info`` output
    (e.g. ``"PulseAudio (on PipeWire 1.0.5)"``). Operators key on
    this to confirm PipeWire vs. classic PulseAudio."""

    modules_loaded: tuple[str, ...] = field(default_factory=tuple)
    """All module names listed by ``pactl list short modules``
    (e.g. ``"module-echo-cancel"``, ``"module-rnnoise"``). Empty
    tuple when enumeration failed or no modules loaded."""

    echo_cancel_loaded: bool = False
    """``"module-echo-cancel"`` appears in :attr:`modules_loaded`.
    Convenience predicate so the dashboard doesn't need to re-parse."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes (e.g. parse failures, OS errors)
    that don't change the verdict but help operators trace the probe."""

    pipewire_version: str | None = None
    """Phase 5 / T5.31 — full PipeWire version string parsed
    from :attr:`server_name` (e.g. ``"1.0.5"``, ``"0.3.65"``).
    ``None`` when the version couldn't be extracted (older
    PipeWire builds OR a non-PW PulseAudio daemon)."""

    pipewire_major_version: int | None = None
    """Phase 5 / T5.31 — major version digit. ``0`` for the
    legacy PW 0.3.x line (Mint 21, older Ubuntu LTS), ``1`` for
    the PW 1.0+ line (Mint 22, current Fedora/Arch). Used by
    operator-facing surfaces to gate on schema-incompatible
    features without re-parsing the version string."""

    hybrid_pulseaudio_conflict: bool = False
    """Phase 5 / T5.34 — True when BOTH a real ``pulseaudio``
    daemon process AND PipeWire are detected on the same
    session. The combination produces unpredictable echo-cancel
    + module routing because both stacks try to manage the
    audio graph; the operator must pick ONE
    (``systemctl --user mask pulseaudio.service`` is the
    canonical fix on modern distros)."""


class PipeWireRoutingError(Exception):
    """Raised when an explicit routing operation
    (:func:`load_echo_cancel_module`) fails. Carries structured
    detail for telemetry — never silently swallowed."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str = "",
        command: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr
        self.command = command


# ── Detection ─────────────────────────────────────────────────────


def detect_pipewire(*, runtime_dir: Path | None = None) -> PipeWireReport:
    """Synchronous Layer-1 detection probe.

    Args:
        runtime_dir: Override for ``$XDG_RUNTIME_DIR``. Test-only;
            production callers rely on the env var.

    Returns:
        :class:`PipeWireReport` with verdict + per-step evidence.
        Never raises — subprocess / parse failures collapse into
        UNKNOWN with structured ``notes``.
    """
    if sys.platform != "linux":
        return PipeWireReport(
            status=PipeWireStatus.ABSENT,
            notes=(f"non-linux platform: {sys.platform}",),
        )

    notes: list[str] = []

    # ── Step 1: socket presence (cheapest possible signal) ─────────
    socket_path = _resolve_pipewire_socket(runtime_dir, notes)
    socket_present = socket_path is not None and socket_path.exists()

    # ── Step 2: pactl available ────────────────────────────────────
    pactl_path = shutil.which("pactl")
    if pactl_path is None:
        notes.append("pactl binary not found on PATH")
        # Without pactl we can't enumerate or load modules. If the
        # socket exists, PipeWire is running but routing layer is
        # unusable — treat as RUNNING (foundational signal preserved)
        # with a note so the dashboard can suggest installing
        # pipewire-pulse.
        return PipeWireReport(
            status=PipeWireStatus.RUNNING if socket_present else PipeWireStatus.ABSENT,
            socket_present=socket_present,
            pactl_available=False,
            notes=tuple(notes),
        )
    # mypy narrowing — pactl_path is now str (not None) for the rest
    # of the function. _query_pactl_info + _enumerate_modules both
    # require str.

    # ── Step 3: pactl info ─────────────────────────────────────────
    info_ok, server_name = _query_pactl_info(pactl_path, notes)
    if not info_ok and not socket_present:
        return PipeWireReport(
            status=PipeWireStatus.ABSENT,
            socket_present=False,
            pactl_available=True,
            notes=tuple(notes),
        )

    # ── Step 4: module enumeration ─────────────────────────────────
    modules = _enumerate_modules(pactl_path, notes)
    echo_cancel = "module-echo-cancel" in modules

    if echo_cancel:
        verdict = PipeWireStatus.RUNNING_WITH_ECHO_CANCEL
    elif info_ok or socket_present:
        verdict = PipeWireStatus.RUNNING
    else:
        verdict = PipeWireStatus.UNKNOWN

    # Phase 5 / T5.31 — version extraction from the server
    # name string. Format: "PulseAudio (on PipeWire 1.0.5)".
    pw_version, pw_major = _extract_pipewire_version(server_name)

    # Phase 5 / T5.34 — hybrid PA + PW conflict detection.
    # Best-effort + read-only; failure collapses to False so a
    # broken /proc never blocks boot.
    hybrid_conflict = _detect_hybrid_pulseaudio_conflict(notes)

    return PipeWireReport(
        status=verdict,
        socket_present=socket_present,
        pactl_available=True,
        pactl_info_ok=info_ok,
        server_name=server_name,
        modules_loaded=tuple(sorted(modules)),
        echo_cancel_loaded=echo_cancel,
        notes=tuple(notes),
        pipewire_version=pw_version,
        pipewire_major_version=pw_major,
        hybrid_pulseaudio_conflict=hybrid_conflict,
    )


def _extract_pipewire_version(server_name: str | None) -> tuple[str | None, int | None]:
    """Extract ``(full_version, major)`` from the server-name string.

    PipeWire emits ``"PulseAudio (on PipeWire 1.0.5)"`` via the
    pulseaudio compat layer. Older installs may emit just
    ``"PulseAudio"`` (real PA, no PW) — return ``(None, None)``.

    Args:
        server_name: ``Server Name:`` value from ``pactl info``.

    Returns:
        Tuple of (version-string-or-None, major-int-or-None).
    """
    if not server_name:
        return None, None
    # Match the literal "PipeWire X.Y.Z" substring; tolerate
    # parens / extra qualifiers around it.
    import re

    match = re.search(r"PipeWire\s+(\d+)\.(\d+)\.(\d+)", server_name)
    if match is None:
        # Some PipeWire builds emit "PipeWire X.Y" (no patch).
        match = re.search(r"PipeWire\s+(\d+)\.(\d+)", server_name)
        if match is None:
            return None, None
        version = f"{match.group(1)}.{match.group(2)}"
        return version, int(match.group(1))
    version = f"{match.group(1)}.{match.group(2)}.{match.group(3)}"
    return version, int(match.group(1))


def _detect_hybrid_pulseaudio_conflict(notes: list[str]) -> bool:
    """Detect a real ``pulseaudio`` daemon running alongside PipeWire.

    The pulseaudio compat layer (``pipewire-pulse``) is NOT a
    "real" pulseaudio process — it's the PipeWire daemon
    impersonating PA's IPC. This function looks for a
    ``pulseaudio`` process whose ``cmdline`` does NOT contain
    ``pipewire`` (the compat layer's argv mentions PipeWire so
    we can distinguish them).

    Best-effort + read-only; subprocess / parse failures collapse
    to ``False`` so a broken ``/proc`` walk never blocks boot.

    Args:
        notes: Mutable note buffer for diagnostic attribution.

    Returns:
        True iff a non-PipeWire pulseaudio process is running.
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv to trusted pgrep
            ["pgrep", "-a", "-x", "pulseaudio"],
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append(f"hybrid_conflict_probe_failed: {exc}")
        return False
    if result.returncode != 0:
        # pgrep returns 1 when no matches found — that's the
        # healthy "no real PA running" path, NOT a probe failure.
        return False
    for line in result.stdout.splitlines():
        # pgrep -a output: "PID command-line"; we want the cmdline.
        parts = line.split(maxsplit=1)
        if len(parts) < 2:  # noqa: PLR2004
            continue
        cmdline = parts[1].lower()
        if "pipewire" in cmdline:
            # This is the compat layer — not a real PA daemon.
            continue
        # Real PA found alongside PW.
        notes.append(f"real_pulseaudio_detected: {parts[0]}")
        return True
    return False


def enumerate_pipewire_modules() -> tuple[str, ...]:
    """Standalone module enumeration helper.

    Convenience for callers that have already detected PipeWire and
    only need the current module list (e.g. a dashboard refresh).
    Returns an empty tuple on any failure — never raises."""
    pactl = shutil.which("pactl")
    if pactl is None:
        return ()
    return tuple(sorted(_enumerate_modules(pactl, [])))


def is_echo_cancel_loaded() -> bool:
    """``True`` iff ``pactl list short modules`` output contains
    ``module-echo-cancel``. Convenience predicate; callers that need
    the full report should use :func:`detect_pipewire`."""
    return "module-echo-cancel" in enumerate_pipewire_modules()


# ── Routing ───────────────────────────────────────────────────────


async def load_echo_cancel_module(
    *,
    source_name: str = "auto_null",
    sink_name: str = "auto_null",
    aec_method: str = "webrtc",
) -> int:
    """Load PipeWire's ``module-echo-cancel`` against the configured
    source/sink, returning the new module ID.

    Args:
        source_name: ``source_name=`` argument to the module — the
            virtual mic source name created by the AEC. Default
            ``"auto_null"`` keeps PipeWire's auto-naming.
        sink_name: ``sink_name=`` argument — the loopback sink the
            AEC subtracts. Default ``"auto_null"``.
        aec_method: ``aec_method=`` argument — algorithm. Defaults
            to ``"webrtc"`` (Google WebRTC AEC, ships with most
            distros). Alternative: ``"speex"`` (older, lower CPU).

    Returns:
        The module ID emitted on stdout by ``pactl load-module``.

    Raises:
        PipeWireRoutingError: ``pactl`` missing, subprocess timeout,
            non-zero exit, or unparseable output. The exception
            carries structured ``returncode`` + ``stderr`` for
            telemetry — never silently swallowed.
    """
    pactl = shutil.which("pactl")
    if pactl is None:
        msg = "pactl binary not found on PATH; cannot load module-echo-cancel"
        raise PipeWireRoutingError(msg)

    args = (
        pactl,
        "load-module",
        "module-echo-cancel",
        f"aec_method={aec_method}",
        f"source_name={source_name}",
        f"sink_name={sink_name}",
    )
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            args,
            capture_output=True,
            text=True,
            timeout=_LOAD_MODULE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"pactl load-module exceeded {_LOAD_MODULE_TIMEOUT_S} s budget"
        raise PipeWireRoutingError(
            msg,
            command=args,
            stderr=str(exc),
        ) from exc
    if result.returncode != 0:
        msg = f"pactl load-module exited {result.returncode}"
        raise PipeWireRoutingError(
            msg,
            returncode=result.returncode,
            stderr=result.stderr.strip(),
            command=args,
        )
    module_id_str = result.stdout.strip()
    try:
        return int(module_id_str)
    except ValueError as exc:
        msg = f"pactl load-module returned non-integer module ID: {module_id_str!r}"
        raise PipeWireRoutingError(
            msg,
            returncode=0,
            stderr=result.stderr.strip(),
            command=args,
        ) from exc


# ── Internal helpers ──────────────────────────────────────────────


def _resolve_pipewire_socket(
    runtime_dir: Path | None,
    notes: list[str],
) -> Path | None:
    """Resolve ``$XDG_RUNTIME_DIR/pipewire-0``; return None when the
    runtime dir is unset (rare on systemd-init systems but valid in
    minimal containers)."""
    base = runtime_dir or _xdg_runtime_dir()
    if base is None:
        notes.append("XDG_RUNTIME_DIR unset")
        return None
    return base / "pipewire-0"


def _xdg_runtime_dir() -> Path | None:
    raw = os.environ.get("XDG_RUNTIME_DIR")
    if not raw:
        # systemd default: /run/user/<uid>
        try:
            # POSIX-only attribute; mypy on Linux sees it (no ignore
            # needed) but on Windows / cross-platform builds the
            # attribute is absent.
            uid = os.getuid()  # type: ignore[attr-defined,unused-ignore]
        except AttributeError:
            return None  # pragma: no cover — non-POSIX (impossible after sys.platform check)
        candidate = Path(f"/run/user/{uid}")
        if candidate.exists():
            return candidate
        return None
    return Path(raw)


def _query_pactl_info(pactl_path: str, notes: list[str]) -> tuple[bool, str | None]:
    """Run ``pactl info`` with the standard timeout. Returns
    ``(ok, server_name)``."""
    try:
        result = subprocess.run(
            (pactl_path, "info"),
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        notes.append("pactl info timed out")
        return False, None
    except OSError as exc:
        notes.append(f"pactl info failed to spawn: {exc!r}")
        return False, None
    if result.returncode != 0:
        notes.append(f"pactl info exited {result.returncode}")
        return False, None
    server_name = _parse_server_name(result.stdout)
    return True, server_name


def _parse_server_name(stdout: str) -> str | None:
    """Extract the ``Server Name:`` line value from ``pactl info``
    output. PipeWire emits ``"PulseAudio (on PipeWire X.Y.Z)"`` so
    operators can disambiguate vs. classic PulseAudio."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Server Name:"):
            return stripped.split(":", 1)[1].strip() or None
    return None


def _enumerate_modules(pactl_path: str, notes: list[str]) -> set[str]:
    """Return the set of module names from ``pactl list short modules``.

    Output format::

        <id>\t<module-name>\t<arg1=val1 arg2=val2 ...>

    We only need column 2 (module name). Returns an empty set on
    subprocess / parse failure — never raises."""
    try:
        result = subprocess.run(
            (pactl_path, "list", "short", "modules"),
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        notes.append("pactl list modules timed out")
        return set()
    except OSError as exc:
        notes.append(f"pactl list modules spawn failed: {exc!r}")
        return set()
    if result.returncode != 0:
        notes.append(f"pactl list modules exited {result.returncode}")
        return set()
    modules: set[str] = set()
    for line in result.stdout.splitlines():
        # tab-separated; module name is the second column.
        parts = line.split("\t")
        if len(parts) < 2:  # noqa: PLR2004
            continue
        name = parts[1].strip()
        if name:
            modules.add(name)
    return modules


__all__ = [
    "PipeWireReport",
    "PipeWireRoutingError",
    "PipeWireStatus",
    "detect_pipewire",
    "enumerate_pipewire_modules",
    "is_echo_cancel_loaded",
    "load_echo_cancel_module",
]
