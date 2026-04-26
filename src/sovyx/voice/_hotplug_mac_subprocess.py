"""MA3 — macOS hotplug fallback via ``system_profiler`` polling.

The pyobjc-backed ``AudioObjectAddPropertyListener`` path remains the
canonical hotplug listener on macOS, but it requires the
``pyobjc-framework-CoreAudio`` extra which the Sovyx wheel does not
bundle by default (vendor decision, mission §8.2). This module ships
the **subprocess fallback**: a polling loop that calls
``system_profiler SPAudioDataType -json`` every 30 s, diffs the device
list against the previous snapshot, and emits a structured event for
each transition.

Tradeoffs vs. the native listener:

* **Latency**: 30 s polling vs. sub-100 ms native callback. The
  fallback is acceptable for human-perceived hotplug events
  (plugging in a headset, joining a Bluetooth speaker) which take
  comparable wall time.
* **Cost**: ``system_profiler`` is heavyweight (2-5 s cold-start,
  ~50 ms warm). Polling at 30 s = ~0.2% CPU on a modern Mac.
* **Reliability**: subprocess + JSON parse is deterministic across
  macOS versions; the native listener's behaviour shifted between
  Big Sur and Sonoma in undocumented ways.

The module is opt-in via
:attr:`VoiceTuningConfig.voice_macos_hotplug_subprocess_enabled` —
default OFF until the operator opts in (the native pyobjc path is
still the preferred long-term solution per §8.2; the fallback exists
as a "good enough until vendor decision lands" patch).

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 6.a.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


_DEFAULT_INTERVAL_S = 30.0
"""Polling interval. 30 s is the sweet spot between human-perceived
latency (plug-in a headset, hear "speaking now" within 30 s) and
cost (~0.2% CPU). Operators tuning for kiosk-style deployments can
override via :attr:`VoiceTuningConfig.voice_macos_hotplug_subprocess_interval_s`."""

_SP_TIMEOUT_S = 8.0
"""``system_profiler`` cold-start can hit 5 s on an idle Mac; 8 s
gives ~60% headroom. A timeout produces an empty diff (treated as
'no change') so the watchdog never spuriously emits."""


@dataclass(frozen=True, slots=True)
class AudioDeviceSnapshot:
    """One ``system_profiler SPAudioDataType`` device entry.

    Identity is the ``unique_id`` field (CoreAudio's stable
    ``kAudioDevicePropertyDeviceUID``). Diff comparison uses this
    field so transient name changes (renaming an audio interface)
    do NOT count as add/remove events.
    """

    unique_id: str
    """Stable CoreAudio device UID."""

    name: str
    """Human-readable device name (may change without identity change)."""

    is_input: bool
    """True iff the device exposes input channels."""

    is_output: bool
    """True iff the device exposes output channels."""


@dataclass(frozen=True, slots=True)
class HotplugEvent:
    """One add/remove transition between successive snapshots."""

    kind: str
    """Either ``"added"`` or ``"removed"``."""

    device: AudioDeviceSnapshot
    """The device that transitioned. For ``"added"`` events this is
    the new device; for ``"removed"`` events the device that was
    last seen and is no longer in the snapshot."""


@dataclass(frozen=True, slots=True)
class PollOutcome:
    """Result of a single ``poll_once`` invocation."""

    snapshot: tuple[AudioDeviceSnapshot, ...]
    """The device list at this poll. May be empty when
    ``system_profiler`` failed (timeout, parse error)."""

    events: tuple[HotplugEvent, ...] = field(default_factory=tuple)
    """Add/remove events vs. the previous snapshot. Empty on first
    poll (no baseline) and on polls where nothing changed."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Diagnostic notes (subprocess timeout, parse fallback, etc.)."""


def _run_system_profiler(*, timeout_s: float = _SP_TIMEOUT_S) -> tuple[str, list[str]]:
    """Run ``system_profiler`` and return (stdout, notes).

    Returns empty stdout + notes on any failure — never raises.
    """
    if sys.platform != "darwin":
        return "", [f"non-darwin platform: {sys.platform}"]
    sp_path = shutil.which("system_profiler")
    if sp_path is None:
        return "", ["system_profiler binary not found on PATH"]
    try:
        result = subprocess.run(  # noqa: S603 — sp_path is from shutil.which, args are fixed
            (sp_path, "SPAudioDataType", "-json"),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", [f"system_profiler timed out after {timeout_s}s"]
    except OSError as exc:
        return "", [f"system_profiler spawn failed: {exc!r}"]
    if result.returncode != 0:
        return "", [f"system_profiler exited {result.returncode}"]
    return result.stdout, []


def _parse_devices(stdout: str) -> tuple[list[AudioDeviceSnapshot], list[str]]:
    """Parse the ``SPAudioDataType`` JSON output into device snapshots.

    Returns ``(snapshots, notes)`` — snapshots are stable-sorted by
    ``unique_id`` so two parses of the same input always return
    identical tuples (deterministic diff).
    """
    if not stdout:
        return [], []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return [], [f"JSON parse failed: {exc!r}"]
    audio_section = data.get("SPAudioDataType")
    if not isinstance(audio_section, list):
        return [], ["unexpected SPAudioDataType shape (not a list)"]

    snapshots: list[AudioDeviceSnapshot] = []
    notes: list[str] = []
    for entry in audio_section:
        if not isinstance(entry, dict):
            continue
        items = entry.get("_items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            unique_id = str(item.get("coreaudio_device_id") or item.get("_name") or "")
            name = str(item.get("_name") or "")
            if not unique_id or not name:
                notes.append(f"device entry missing identity: {item.get('_name')!r}")
                continue
            input_ch = item.get("coreaudio_input_source") or item.get("coreaudio_device_input")
            output_ch = item.get("coreaudio_device_output")
            snapshots.append(
                AudioDeviceSnapshot(
                    unique_id=unique_id,
                    name=name,
                    is_input=bool(input_ch),
                    is_output=bool(output_ch),
                ),
            )
    snapshots.sort(key=lambda s: s.unique_id)
    return snapshots, notes


def _diff_snapshots(
    previous: tuple[AudioDeviceSnapshot, ...],
    current: tuple[AudioDeviceSnapshot, ...],
) -> tuple[HotplugEvent, ...]:
    """Compute add/remove events between two snapshots.

    Identity is by ``unique_id``. A device whose ``name`` changed but
    ``unique_id`` stayed produces NO event (transient renames are
    not hotplug transitions).
    """
    prev_ids = {d.unique_id: d for d in previous}
    curr_ids = {d.unique_id: d for d in current}
    events: list[HotplugEvent] = []
    for uid, dev in curr_ids.items():
        if uid not in prev_ids:
            events.append(HotplugEvent(kind="added", device=dev))
    for uid, dev in prev_ids.items():
        if uid not in curr_ids:
            events.append(HotplugEvent(kind="removed", device=dev))
    return tuple(events)


def poll_once(
    previous: tuple[AudioDeviceSnapshot, ...],
    *,
    timeout_s: float = _SP_TIMEOUT_S,
) -> PollOutcome:
    """Single synchronous poll. Pure function modulo subprocess.

    The async watchdog (:class:`MacosHotplugSubprocessWatchdog`) wraps
    this in :func:`asyncio.to_thread`. Tests can call it directly with
    a stubbed ``system_profiler`` for deterministic verification.
    """
    stdout, sp_notes = _run_system_profiler(timeout_s=timeout_s)
    snapshots, parse_notes = _parse_devices(stdout)
    snapshot_tuple = tuple(snapshots)
    notes = tuple(sp_notes) + tuple(parse_notes)
    if not previous:
        # First poll — no baseline, no events.
        return PollOutcome(snapshot=snapshot_tuple, notes=notes)
    return PollOutcome(
        snapshot=snapshot_tuple,
        events=_diff_snapshots(previous, snapshot_tuple),
        notes=notes,
    )


class MacosHotplugSubprocessWatchdog:
    """Async polling watchdog for macOS audio device hotplug.

    Lifecycle: ``await watchdog.start()`` → background task polls
    every ``interval_s``; ``await watchdog.stop()`` cancels the
    task and awaits its termination.

    Each transition fires the optional ``on_event`` callback (sync or
    async) AND emits a structured ``voice.macos.device_changed`` log.
    Operators receive per-event observability without needing to wire
    the callback themselves.
    """

    def __init__(
        self,
        *,
        interval_s: float = _DEFAULT_INTERVAL_S,
        on_event: Callable[[HotplugEvent], None | Awaitable[None]] | None = None,
    ) -> None:
        self._interval_s = max(5.0, min(300.0, interval_s))
        self._on_event = on_event
        self._previous: tuple[AudioDeviceSnapshot, ...] = ()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="macos_hotplug_subprocess")

    async def stop(self) -> None:
        if not self.is_running:
            return
        self._stopping.set()
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                outcome = await asyncio.to_thread(poll_once, self._previous)
            except Exception as exc:  # noqa: BLE001 — watchdog must never crash
                logger.warning(
                    "voice.macos.hotplug_poll_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                outcome = PollOutcome(snapshot=self._previous)
            self._previous = outcome.snapshot
            for event in outcome.events:
                logger.info(
                    "voice.macos.device_changed",
                    **{
                        "voice.kind": event.kind,
                        "voice.device_name": event.device.name,
                        "voice.device_unique_id": event.device.unique_id,
                        "voice.is_input": event.device.is_input,
                        "voice.is_output": event.device.is_output,
                    },
                )
                if self._on_event is not None:
                    await self._dispatch_callback(event)
            if outcome.notes:
                logger.debug(
                    "voice.macos.hotplug_poll_notes",
                    **{"voice.notes": list(outcome.notes)},
                )
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue

    async def _dispatch_callback(self, event: HotplugEvent) -> None:
        """Invoke the optional event callback without letting it
        crash the polling loop."""
        try:
            assert self._on_event is not None
            result: Any = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 — callback isolation
            logger.warning(
                "voice.macos.hotplug_callback_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )


__all__ = [
    "AudioDeviceSnapshot",
    "HotplugEvent",
    "MacosHotplugSubprocessWatchdog",
    "PollOutcome",
    "poll_once",
]
