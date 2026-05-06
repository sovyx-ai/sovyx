"""Tests for the P3 capture-prompt protocol (v0.30.31).

The bash diag writes one JSONL line per operator-facing prompt to
``<job_dir>/prompts.jsonl``; the wizard orchestrator's
``_tail_prompts_file`` task polls that file every 500ms and surfaces
each prompt via:

* ``state.extras["current_prompt"]`` so the dashboard's
  ``<CapturePrompt>`` component renders it in real time.
* ``voice.calibration.wizard.capture_prompt`` telemetry per parsed line.

Coverage:
* tail loop reads sample lines + emits per-line telemetry
* malformed JSON skipped (logged DEBUG, no crash)
* unknown prompt_type does NOT emit telemetry (closed-enum guard)
* state holder mutation propagates extras to subsequent emissions
* OSError on read suppressed (transient — retried next poll)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.calibration import _wizard_orchestrator as wo
from sovyx.voice.calibration._wizard_progress import WizardProgressTracker
from sovyx.voice.calibration._wizard_state import WizardJobState, WizardStatus


def _state(job_id: str = "testjob", mind_id: str = "default") -> WizardJobState:
    return WizardJobState(
        job_id=job_id,
        mind_id=mind_id,
        status=WizardStatus.SLOW_PATH_DIAG,
        progress=0.10,
        current_stage_message="running diag",
        created_at_utc="2026-05-06T18:00:00Z",
        updated_at_utc="2026-05-06T18:00:01Z",
    )


def _capture_logger() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    events: list[tuple[str, dict[str, Any]]] = []

    class _Cap:
        def info(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

        def warning(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

        def debug(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

    original = wo.logger
    wo.logger = _Cap()  # type: ignore[assignment]
    return events, original


def _restore_logger(original: Any) -> None:
    wo.logger = original  # type: ignore[assignment]


@pytest.mark.asyncio()
class TestTailPromptsFile:
    async def test_tails_prompts_and_emits_telemetry(self, tmp_path: Path) -> None:
        prompts_file = tmp_path / "prompts.jsonl"
        prompts_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "speak",
                            "phrase": "Sovyx, me ouça",
                            "seconds": None,
                            "emitted_at_utc": "2026-05-06T18:00:01Z",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "silence",
                            "phrase": None,
                            "seconds": 3,
                            "emitted_at_utc": "2026-05-06T18:00:05Z",
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        orch = wo.WizardOrchestrator(data_dir=tmp_path)
        tracker = WizardProgressTracker(tmp_path / "progress.jsonl")
        state_holder = {"state": _state()}

        events, original = _capture_logger()
        try:
            with patch.object(wo, "_PROMPTS_POLL_INTERVAL_S", 0.01):
                task = asyncio.create_task(
                    orch._tail_prompts_file(
                        prompts_file=prompts_file,
                        state_holder=state_holder,
                        tracker=tracker,
                    )
                )
                # Yield enough event-loop ticks for the tail loop to
                # read both prompts. _PROMPTS_POLL_INTERVAL_S=0.01 so
                # 50ms is ~5 polls — comfortably enough for the read
                # path on the first iteration.
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            _restore_logger(original)

        capture_events = [e for e in events if e[0] == "voice.calibration.wizard.capture_prompt"]
        assert len(capture_events) == 2
        types = [e[1]["prompt_type"] for e in capture_events]
        assert types == ["speak", "silence"]
        # state_holder picked up the LAST prompt.
        assert state_holder["state"].extras["current_prompt"]["type"] == "silence"
        assert state_holder["state"].extras["current_prompt"]["seconds"] == 3

    async def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        prompts_file = tmp_path / "prompts.jsonl"
        prompts_file.write_text(
            "\n".join(
                [
                    "{not json",
                    json.dumps({"type": "speak", "phrase": "ok"}),
                    "{still: invalid",
                ]
            ),
            encoding="utf-8",
        )

        orch = wo.WizardOrchestrator(data_dir=tmp_path)
        tracker = WizardProgressTracker(tmp_path / "progress.jsonl")
        state_holder = {"state": _state()}

        events, original = _capture_logger()
        try:
            with patch.object(wo, "_PROMPTS_POLL_INTERVAL_S", 0.01):
                task = asyncio.create_task(
                    orch._tail_prompts_file(
                        prompts_file=prompts_file,
                        state_holder=state_holder,
                        tracker=tracker,
                    )
                )
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            _restore_logger(original)

        capture_events = [e for e in events if e[0] == "voice.calibration.wizard.capture_prompt"]
        # Only the well-formed line emits; malformed are skipped.
        assert len(capture_events) == 1
        assert capture_events[0][1]["prompt_type"] == "speak"

    async def test_unknown_type_does_not_emit_telemetry(self, tmp_path: Path) -> None:
        # Closed-enum guard: only "speak" + "silence" emit; future bash
        # versions adding new types should require a corresponding code
        # update on the Python side.
        prompts_file = tmp_path / "prompts.jsonl"
        prompts_file.write_text(
            json.dumps({"type": "future_unknown", "phrase": "..."}),
            encoding="utf-8",
        )

        orch = wo.WizardOrchestrator(data_dir=tmp_path)
        tracker = WizardProgressTracker(tmp_path / "progress.jsonl")
        state_holder = {"state": _state()}

        events, original = _capture_logger()
        try:
            with patch.object(wo, "_PROMPTS_POLL_INTERVAL_S", 0.01):
                task = asyncio.create_task(
                    orch._tail_prompts_file(
                        prompts_file=prompts_file,
                        state_holder=state_holder,
                        tracker=tracker,
                    )
                )
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            _restore_logger(original)

        capture_events = [e for e in events if e[0] == "voice.calibration.wizard.capture_prompt"]
        # Closed-enum guard: unknown type does not emit, but extras
        # still updates (so dashboards see WHAT bash emitted even if
        # we lack a renderer for it).
        assert capture_events == []
        assert state_holder["state"].extras["current_prompt"]["type"] == "future_unknown"

    async def test_missing_file_no_op(self, tmp_path: Path) -> None:
        prompts_file = tmp_path / "never_created.jsonl"
        # Don't write the file at all -- tail loop should poll harmlessly.
        orch = wo.WizardOrchestrator(data_dir=tmp_path)
        tracker = WizardProgressTracker(tmp_path / "progress.jsonl")
        state_holder = {"state": _state()}

        events, original = _capture_logger()
        try:
            with patch.object(wo, "_PROMPTS_POLL_INTERVAL_S", 0.01):
                task = asyncio.create_task(
                    orch._tail_prompts_file(
                        prompts_file=prompts_file,
                        state_holder=state_holder,
                        tracker=tracker,
                    )
                )
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            _restore_logger(original)

        capture_events = [e for e in events if e[0] == "voice.calibration.wizard.capture_prompt"]
        assert capture_events == []
        # state.extras is unchanged — no current_prompt key inserted.
        assert "current_prompt" not in state_holder["state"].extras

    async def test_appended_lines_picked_up_on_next_poll(self, tmp_path: Path) -> None:
        prompts_file = tmp_path / "prompts.jsonl"
        prompts_file.write_text(
            json.dumps({"type": "speak", "phrase": "first"}) + "\n",
            encoding="utf-8",
        )

        orch = wo.WizardOrchestrator(data_dir=tmp_path)
        tracker = WizardProgressTracker(tmp_path / "progress.jsonl")
        state_holder = {"state": _state()}

        events, original = _capture_logger()
        try:
            with patch.object(wo, "_PROMPTS_POLL_INTERVAL_S", 0.01):
                task = asyncio.create_task(
                    orch._tail_prompts_file(
                        prompts_file=prompts_file,
                        state_holder=state_holder,
                        tracker=tracker,
                    )
                )
                await asyncio.sleep(0.03)
                # Append a second line mid-flight.
                with prompts_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "speak", "phrase": "second"}) + "\n")
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            _restore_logger(original)

        capture_events = [e for e in events if e[0] == "voice.calibration.wizard.capture_prompt"]
        phrases = [e[1]["phrase"] for e in capture_events]
        assert phrases == ["first", "second"]


class TestRunFullDiagAsyncEnvOverrides:
    """env_overrides param injected into the subprocess env when set."""

    @pytest.mark.asyncio()
    async def test_env_overrides_passed_to_subprocess(self, tmp_path: Path) -> None:
        from sovyx.voice.diagnostics import _runner

        captured_kwargs: list[dict[str, Any]] = []

        async def _factory(*_args: Any, **kwargs: Any) -> Any:
            captured_kwargs.append(kwargs)
            proc = MagicMock()
            proc.pid = 1
            proc.returncode = 0

            async def _wait() -> int:
                return 0

            proc.wait = _wait
            return proc

        # Materialize a fake "extracted" dir + tarball so the runner
        # post-flight step succeeds.
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "sovyx-voice-diag.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        output_root = tmp_path / "home"
        output_root.mkdir()
        diag_dir = output_root / "sovyx-diag-x"
        diag_dir.mkdir()
        (diag_dir / "sovyx-voice-diag_x.tar.gz").write_bytes(b"\x1f\x8b\x08\x00")

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(_runner, "_extract_bash_to_temp", return_value=extracted),
            patch.object(_runner.asyncio, "create_subprocess_exec", side_effect=_factory),
        ):
            await _runner.run_full_diag_async(
                output_root=output_root,
                env_overrides={"SOVYX_DIAG_PROMPTS_FILE": "/tmp/prompts.jsonl"},
            )

        assert len(captured_kwargs) == 1
        env = captured_kwargs[0].get("env")
        assert env is not None
        assert env["SOVYX_DIAG_PROMPTS_FILE"] == "/tmp/prompts.jsonl"

    @pytest.mark.asyncio()
    async def test_no_env_overrides_inherits_parent_env(self, tmp_path: Path) -> None:
        from sovyx.voice.diagnostics import _runner

        captured_kwargs: list[dict[str, Any]] = []

        async def _factory(*_args: Any, **kwargs: Any) -> Any:
            captured_kwargs.append(kwargs)
            proc = MagicMock()
            proc.pid = 1
            proc.returncode = 0

            async def _wait() -> int:
                return 0

            proc.wait = _wait
            return proc

        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "sovyx-voice-diag.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        output_root = tmp_path / "home"
        output_root.mkdir()
        diag_dir = output_root / "sovyx-diag-x"
        diag_dir.mkdir()
        (diag_dir / "sovyx-voice-diag_x.tar.gz").write_bytes(b"\x1f\x8b\x08\x00")

        with (
            patch.object(_runner, "_check_prerequisites"),
            patch.object(_runner, "_extract_bash_to_temp", return_value=extracted),
            patch.object(_runner.asyncio, "create_subprocess_exec", side_effect=_factory),
        ):
            await _runner.run_full_diag_async(output_root=output_root)

        # When env_overrides is None, the runner does NOT pass an
        # explicit env kwarg -- the subprocess inherits the parent's
        # env via asyncio's default behavior.
        assert "env" not in captured_kwargs[0]
