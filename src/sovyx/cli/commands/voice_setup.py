"""``sovyx voice setup`` — interactive or flag-driven mic configuration.

Phase 2.T2.1 — the headless / non-dashboard path to commit a capture
device choice to a per-mind ``mind.yaml``. Before this command, the
ONLY production write site for ``voice_input_device_name`` was the
dashboard voice-enable endpoint (``routes/voice.py:2802-2813``);
operators on server-only installs had to hand-edit ``mind.yaml``.

The shared :func:`run_voice_setup` function is callable from three
entry points:

1. The ``sovyx voice setup`` CLI command (this module, decorated
   below).
2. The ``sovyx init`` flow (Phase 2.T2.2) — invokes setup inline
   when stdin is a TTY and ``--skip-voice-setup`` was not passed.
3. The ``sovyx doctor voice --calibrate`` prereq gate
   (Phase 4 LENIENT / Phase 5 STRICT) — invokes setup before step 1
   of the calibration pipeline when ``voice_input_device_name`` is
   empty.

All three callers go through the same picker / validation /
persistence path. Persistence delegates to
:func:`sovyx.voice.calibration._persist_device.persist_voice_input_device`
(Phase 2.T2.3).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from sovyx.cli._mind_resolver import resolve_mind_id
from sovyx.cli.commands.voice import voice_app
from sovyx.observability.logging import get_logger
from sovyx.voice.calibration._persist_device import persist_voice_input_device

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.engine.types import MindId

console = Console()
logger = get_logger(__name__)


class VoiceSetupError(RuntimeError):
    """Voice setup failed (no devices, invalid selection, persist failure)."""


class VoiceSetupRequiredError(VoiceSetupError):
    """``sovyx voice setup`` invoked non-interactively without ``--input-device``.

    Distinct subtype so callers can render an actionable message that
    points the operator at the interactive flow or the explicit flag.
    """


@dataclass(frozen=True)
class CaptureDevice:
    """Operator-readable capture device row from the PortAudio enumeration."""

    index: int
    name: str
    host_api: str
    input_channels: int
    default_samplerate: float


@dataclass(frozen=True)
class VoiceSetupResult:
    """Outcome of a successful :func:`run_voice_setup` call."""

    mind_id: str
    device_name: str
    host_api: str


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(name: str) -> str:
    """Lower + collapse-whitespace + strip; matches the calibration resolver
    semantics in :mod:`sovyx.voice.calibration._active_mic` so the operator's
    ``--input-device 'Razer'`` matches the full enumerated name regardless
    of casing or extra spaces."""
    return _WHITESPACE_RE.sub(" ", name).strip().lower()


def list_capture_devices() -> list[CaptureDevice]:
    """Enumerate PortAudio devices with ``max_input_channels > 0``.

    Returns the list in enumeration order (PortAudio assigns deterministic
    indices). Raises :class:`VoiceSetupError` when sounddevice / PortAudio
    is unavailable (CI hosts without ALSA libs, headless containers
    without ``libportaudio2``) with an actionable install hint.
    """
    try:
        import sounddevice as sd  # noqa: PLC0415 — heavy optional dep
    except (ImportError, OSError) as exc:
        msg = (
            "PortAudio / sounddevice is not available on this host. "
            "Install the platform package (Linux: `libportaudio2`; "
            "macOS: `brew install portaudio`; Windows: bundled with the "
            "Python wheel) and the `sounddevice` Python module."
        )
        raise VoiceSetupError(msg) from exc

    try:
        hostapis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception as exc:  # noqa: BLE001 — PortAudio raises various
        msg = f"PortAudio device enumeration failed: {exc}"
        raise VoiceSetupError(msg) from exc

    result: list[CaptureDevice] = []
    for idx, dev in enumerate(devices):
        if not isinstance(dev, dict):
            continue
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        host_api_index = int(dev.get("hostapi", -1))
        host_api_name = ""
        if 0 <= host_api_index < len(hostapis):
            host_api_dict = hostapis[host_api_index]
            if isinstance(host_api_dict, dict):
                host_api_name = str(host_api_dict.get("name", ""))
        result.append(
            CaptureDevice(
                index=idx,
                name=str(dev.get("name", "")),
                host_api=host_api_name,
                input_channels=int(dev.get("max_input_channels", 0)),
                default_samplerate=float(dev.get("default_samplerate", 0.0)),
            )
        )
    return result


def find_matching_device(
    devices: list[CaptureDevice],
    requested: str,
) -> CaptureDevice:
    """Resolve ``--input-device <requested>`` to a single :class:`CaptureDevice`.

    Match precedence (first non-empty match wins):

    1. Integer index — operator passed the enumeration index from the picker.
    2. Exact case-sensitive name match.
    3. Case-insensitive normalised substring match (single hit).

    Raises :class:`VoiceSetupError` when:

    * No device matches.
    * The substring match is ambiguous (>1 hit) — operator must pass a
      more specific value or use the interactive picker.
    """
    requested_stripped = requested.strip()
    if not requested_stripped:
        msg = "Empty device specifier — pass a device name or enumeration index."
        raise VoiceSetupError(msg)

    if requested_stripped.isdigit():
        idx = int(requested_stripped)
        for dev in devices:
            if dev.index == idx:
                return dev
        msg = (
            f"No capture device at index {idx}. "
            f"Available: {', '.join(str(d.index) for d in devices)}."
        )
        raise VoiceSetupError(msg)

    exact = [d for d in devices if d.name == requested_stripped]
    if len(exact) == 1:
        return exact[0]

    needle = _normalize(requested_stripped)
    substring_hits = [d for d in devices if needle in _normalize(d.name)]
    if len(substring_hits) == 1:
        return substring_hits[0]
    if len(substring_hits) > 1:
        names = ", ".join(repr(d.name) for d in substring_hits)
        msg = (
            f"Ambiguous --input-device {requested_stripped!r}: matched "
            f"{len(substring_hits)} devices ({names}). Pass a more "
            f"specific name or the enumeration index."
        )
        raise VoiceSetupError(msg)

    available_names = ", ".join(repr(d.name) for d in devices)
    msg = f"No capture device matches {requested_stripped!r}. Available: {available_names}."
    raise VoiceSetupError(msg)


def _render_devices_table(devices: list[CaptureDevice]) -> None:
    """Print an operator-readable table of capture devices to the console."""
    table = Table(title="Capture devices", show_lines=False)
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Host API", style="dim")
    table.add_column("Channels", justify="right")
    table.add_column("Sample rate (Hz)", justify="right", style="dim")
    for dev in devices:
        table.add_row(
            str(dev.index),
            dev.name,
            dev.host_api or "—",
            str(dev.input_channels),
            f"{dev.default_samplerate:.0f}",
        )
    console.print(table)


def _interactive_pick(devices: list[CaptureDevice]) -> CaptureDevice:
    """Show the device table + prompt the operator for a selection.

    Accepts either an enumeration index or the device name; the input
    is fed back through :func:`find_matching_device` so the same
    validation rules apply.
    """
    _render_devices_table(devices)
    while True:
        try:
            raw = console.input("[bold]Enter device # or name[/bold] (or 'q' to abort): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            msg = "Voice setup aborted by operator."
            raise VoiceSetupError(msg) from exc
        if raw.lower() in {"q", "quit", "exit"}:
            msg = "Voice setup aborted by operator."
            raise VoiceSetupError(msg)
        if not raw:
            console.print("[yellow]Please enter a value, or 'q' to abort.[/yellow]")
            continue
        try:
            return find_matching_device(devices, raw)
        except VoiceSetupError as exc:
            console.print(f"[red]{exc}[/red]")


async def run_voice_setup(
    *,
    mind_id: MindId,
    data_dir: Path,
    input_device: str | None = None,
    non_interactive: bool = False,
) -> VoiceSetupResult:
    """Configure the per-mind mic + persist to mind.yaml.

    Args:
        mind_id: The mind to configure (already resolved via
            :func:`sovyx.cli._mind_resolver.resolve_mind_id`).
        data_dir: Sovyx data directory (e.g. ``EngineConfig().data_dir``).
        input_device: Optional device specifier (name substring OR
            enumeration index). When ``None``: interactive picker
            (requires TTY) or ``VoiceSetupRequiredError`` non-interactively.
        non_interactive: When True, refuse to prompt — caller must
            supply ``input_device``. Use in CI / systemd / cron contexts.

    Returns:
        :class:`VoiceSetupResult` with the resolved mind id + persisted
        device name + host API name.

    Raises:
        VoiceSetupRequiredError: ``non_interactive=True`` and
            ``input_device`` is None.
        VoiceSetupError: device enumeration failed, no devices found,
            ``input_device`` matched zero or >1 devices, or persistence
            failed.
        FileNotFoundError: ``<data_dir>/<mind_id>/mind.yaml`` does not
            exist (operator must run ``sovyx init`` first).
    """
    devices = list_capture_devices()
    if not devices:
        msg = (
            "No capture devices enumerated by PortAudio. Plug in a "
            "microphone or check OS-level audio permissions."
        )
        raise VoiceSetupError(msg)

    if input_device is not None:
        chosen = find_matching_device(devices, input_device)
    elif non_interactive:
        available_names = ", ".join(repr(d.name) for d in devices)
        msg = (
            f"voice setup requires an explicit --input-device when "
            f"running non-interactively. Available: {available_names}."
        )
        raise VoiceSetupRequiredError(msg)
    else:
        console.print(f"\n[bold cyan]Voice setup[/bold cyan] for mind [bold]{mind_id}[/bold]\n")
        chosen = _interactive_pick(devices)

    mind_yaml_path = data_dir / str(mind_id) / "mind.yaml"
    await persist_voice_input_device(
        mind_yaml_path=mind_yaml_path,
        device_name=chosen.name,
        host_api=chosen.host_api or None,
    )

    logger.info(
        "voice.setup.completed",
        mind_id=str(mind_id),
        device_index=chosen.index,
        host_api=chosen.host_api or "<unset>",
    )
    return VoiceSetupResult(
        mind_id=str(mind_id),
        device_name=chosen.name,
        host_api=chosen.host_api,
    )


def _resolve_data_dir() -> Path:
    """Match the data_dir resolution used by other voice subcommands."""
    try:
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415

        return EngineConfig().data_dir
    except Exception:  # noqa: BLE001 — fall back gracefully
        from pathlib import Path as _Path  # noqa: PLC0415

        return _Path.home() / ".sovyx"


@voice_app.command("setup")
def voice_setup_cmd(
    mind_id: str | None = typer.Option(
        None,
        "--mind-id",
        help=(
            "Mind to configure. Default: auto-detected when exactly one "
            "mind exists at <data_dir>/<mind>/mind.yaml; required when "
            "multiple minds exist."
        ),
    ),
    input_device: str | None = typer.Option(
        None,
        "--input-device",
        help=(
            "Capture device name (substring matched, case-insensitive) "
            "OR enumeration index. Required when --non-interactive. "
            "When omitted on a TTY, opens an interactive picker."
        ),
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help=(
            "Refuse to prompt for input. REQUIRED when running under "
            "CI / systemd / cron / any non-TTY shell. Errors with the "
            "available-device list when --input-device is omitted."
        ),
    ),
) -> None:
    """Configure the per-mind capture device (mic).

    Persists ``voice_input_device_name`` and ``voice_input_device_host_api``
    to ``<data_dir>/<mind_id>/mind.yaml``. The same fields are written by
    the dashboard voice-enable endpoint; both writers share
    :func:`sovyx.voice.calibration._persist_device.persist_voice_input_device`.

    Common flows:

    * First-time setup (TTY): ``sovyx voice setup`` → interactive picker.
    * Scripted install: ``sovyx voice setup --non-interactive --input-device 'Razer'``.
    * Multi-mind operator: pass ``--mind-id <name>`` explicitly.
    """
    data_dir = _resolve_data_dir()
    resolved_mind = resolve_mind_id(mind_id, data_dir)
    try:
        result = asyncio.run(
            run_voice_setup(
                mind_id=resolved_mind,
                data_dir=data_dir,
                input_device=input_device,
                non_interactive=non_interactive,
            )
        )
    except VoiceSetupRequiredError as exc:
        console.print(f"\n[red]{exc}[/red]\n")
        raise typer.Exit(code=2) from None
    except VoiceSetupError as exc:
        console.print(f"\n[red]{exc}[/red]\n")
        raise typer.Exit(code=1) from None
    except FileNotFoundError as exc:
        console.print(f"\n[red]{exc}[/red]\n")
        raise typer.Exit(code=2) from None

    console.print(
        f"\n[green]✓[/green] Mic configured for mind "
        f"[bold]{result.mind_id}[/bold]:\n"
        f"  device:    [cyan]{result.device_name}[/cyan]\n"
        f"  host API:  [dim]{result.host_api or '—'}[/dim]\n"
    )
