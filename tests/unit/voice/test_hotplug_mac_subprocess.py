"""Tests for MA3 — macOS hotplug subprocess fallback (Step 6.a).

Covers: parse, diff, async watchdog lifecycle, callback isolation.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 6.a.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from sovyx.voice._hotplug_mac_subprocess import (
    AudioDeviceSnapshot,
    HotplugEvent,
    MacosHotplugSubprocessWatchdog,
    PollOutcome,
    _diff_snapshots,
    _parse_devices,
    poll_once,
)


def _make_sp_json(devices: list[dict]) -> str:
    """Build a synthetic system_profiler JSON payload for tests."""
    return json.dumps(
        {
            "SPAudioDataType": [
                {
                    "_name": "Audio",
                    "_items": devices,
                },
            ],
        },
    )


class TestParseDevices:
    def test_parses_input_only_device(self) -> None:
        stdout = _make_sp_json(
            [
                {
                    "_name": "Mic",
                    "coreaudio_device_id": "uid-mic",
                    "coreaudio_input_source": "internal",
                },
            ],
        )
        snapshots, notes = _parse_devices(stdout)
        assert len(snapshots) == 1
        assert snapshots[0].unique_id == "uid-mic"
        assert snapshots[0].name == "Mic"
        assert snapshots[0].is_input is True
        assert snapshots[0].is_output is False
        assert notes == []

    def test_parses_output_only_device(self) -> None:
        stdout = _make_sp_json(
            [
                {
                    "_name": "Speakers",
                    "coreaudio_device_id": "uid-spk",
                    "coreaudio_device_output": "stereo",
                },
            ],
        )
        snapshots, _notes = _parse_devices(stdout)
        assert len(snapshots) == 1
        assert snapshots[0].is_input is False
        assert snapshots[0].is_output is True

    def test_sorts_devices_by_unique_id(self) -> None:
        stdout = _make_sp_json(
            [
                {"_name": "Z-device", "coreaudio_device_id": "z"},
                {"_name": "A-device", "coreaudio_device_id": "a"},
                {"_name": "M-device", "coreaudio_device_id": "m"},
            ],
        )
        snapshots, _notes = _parse_devices(stdout)
        assert [s.unique_id for s in snapshots] == ["a", "m", "z"]

    def test_skips_entries_missing_identity(self) -> None:
        stdout = _make_sp_json(
            [
                {"_name": ""},  # No identity
                {"_name": "Valid", "coreaudio_device_id": "uid"},
            ],
        )
        snapshots, _notes = _parse_devices(stdout)
        assert len(snapshots) == 1
        assert snapshots[0].name == "Valid"

    def test_handles_malformed_json(self) -> None:
        snapshots, notes = _parse_devices("{not-json")
        assert snapshots == []
        assert any("JSON parse failed" in n for n in notes)

    def test_handles_empty_input(self) -> None:
        snapshots, notes = _parse_devices("")
        assert snapshots == []
        assert notes == []


class TestDiffSnapshots:
    def test_added_device_yields_added_event(self) -> None:
        prev: tuple[AudioDeviceSnapshot, ...] = ()
        curr = (AudioDeviceSnapshot(unique_id="a", name="A", is_input=True, is_output=False),)
        events = _diff_snapshots(prev, curr)
        assert len(events) == 1
        assert events[0].kind == "added"
        assert events[0].device.unique_id == "a"

    def test_removed_device_yields_removed_event(self) -> None:
        prev = (AudioDeviceSnapshot(unique_id="a", name="A", is_input=True, is_output=False),)
        curr: tuple[AudioDeviceSnapshot, ...] = ()
        events = _diff_snapshots(prev, curr)
        assert len(events) == 1
        assert events[0].kind == "removed"

    def test_rename_only_yields_no_event(self) -> None:
        """A device whose name changed but unique_id stayed must NOT
        trigger a hotplug event (rename != plug change)."""
        prev = (AudioDeviceSnapshot(unique_id="a", name="Old", is_input=True, is_output=False),)
        curr = (AudioDeviceSnapshot(unique_id="a", name="New", is_input=True, is_output=False),)
        assert _diff_snapshots(prev, curr) == ()

    def test_no_changes_yields_no_events(self) -> None:
        snap = (AudioDeviceSnapshot(unique_id="a", name="A", is_input=True, is_output=False),)
        assert _diff_snapshots(snap, snap) == ()


class TestPollOnce:
    def test_first_poll_has_no_events(self) -> None:
        with patch(
            "sovyx.voice._hotplug_mac_subprocess._run_system_profiler",
            return_value=(_make_sp_json([{"_name": "Mic", "coreaudio_device_id": "u"}]), []),
        ):
            outcome = poll_once(())
        assert len(outcome.snapshot) == 1
        assert outcome.events == ()

    def test_second_poll_with_added_device_emits_added_event(self) -> None:
        baseline = (AudioDeviceSnapshot(unique_id="a", name="A", is_input=True, is_output=False),)
        with patch(
            "sovyx.voice._hotplug_mac_subprocess._run_system_profiler",
            return_value=(
                _make_sp_json(
                    [
                        {"_name": "A", "coreaudio_device_id": "a", "coreaudio_input_source": "x"},
                        {"_name": "B", "coreaudio_device_id": "b", "coreaudio_input_source": "y"},
                    ],
                ),
                [],
            ),
        ):
            outcome = poll_once(baseline)
        assert len(outcome.events) == 1
        assert outcome.events[0].kind == "added"
        assert outcome.events[0].device.unique_id == "b"


class TestWatchdogLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop_idempotent(self) -> None:
        watchdog = MacosHotplugSubprocessWatchdog(interval_s=5.0)
        with patch(
            "sovyx.voice._hotplug_mac_subprocess._run_system_profiler",
            return_value=("", []),
        ):
            await watchdog.start()
            assert watchdog.is_running
            # Calling start again is a no-op.
            await watchdog.start()
            assert watchdog.is_running
            await watchdog.stop()
            assert not watchdog.is_running
            # Calling stop again is a no-op.
            await watchdog.stop()
            assert not watchdog.is_running

    @pytest.mark.asyncio
    async def test_callback_isolation_when_callback_raises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A buggy ``on_event`` callback must not crash the polling
        loop or propagate to the caller."""
        events_seen: list[HotplugEvent] = []

        def bad_callback(event: HotplugEvent) -> None:
            events_seen.append(event)
            raise RuntimeError("synthetic callback failure")

        watchdog = MacosHotplugSubprocessWatchdog(interval_s=5.0, on_event=bad_callback)
        # Inject a poll outcome that emits an event on the very first
        # poll (avoids waiting for a second iteration of the 5 s loop).
        synthetic_event = HotplugEvent(
            kind="added",
            device=AudioDeviceSnapshot(
                unique_id="u",
                name="N",
                is_input=True,
                is_output=False,
            ),
        )
        synthetic_outcome = PollOutcome(
            snapshot=(
                AudioDeviceSnapshot(
                    unique_id="u",
                    name="N",
                    is_input=True,
                    is_output=False,
                ),
            ),
            events=(synthetic_event,),
        )

        with (
            patch(
                "sovyx.voice._hotplug_mac_subprocess.poll_once",
                return_value=synthetic_outcome,
            ),
            caplog.at_level("WARNING", logger="sovyx.voice._hotplug_mac_subprocess"),
        ):
            await watchdog.start()
            # First poll runs immediately; the loop then sleeps interval_s.
            # 0.2 s gives the first iteration time to dispatch the
            # callback before stop() cancels the task.
            await asyncio.sleep(0.2)
            await watchdog.stop()

        # The callback was invoked AND the watchdog logged the failure
        # but didn't propagate it.
        assert len(events_seen) >= 1
        cb_warns = [r for r in caplog.records if "hotplug_callback_failed" in str(r.msg)]
        assert len(cb_warns) >= 1
