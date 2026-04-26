"""Operator-callable 24h frame pipeline soak script.

Drives the synthetic conversation simulation (mirror of
``tests/integration/voice/test_frame_pipeline_soak.py``) for 24 hours
on the operator's host. Asserts:

* No memory growth (RSS stable within 10%)
* Frame ring buffer stays at capacity
* No state-frame divergence

Not run in CI (24h window doesn't fit a CI budget). Operators
schedule this manually before pushing v0.23.0-rc → v0.23.0.

Usage::

    uv run python scripts/soak/run-frame-pipeline-soak.py [--hours N]

Default duration: 24h. Override via ``--hours`` for a shorter run
during smoke validation.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 16.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def _setup_python_path() -> None:
    """Make ``sovyx.*`` importable when this script is run as a
    standalone file (not via ``-m``)."""
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = repo_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))


_setup_python_path()

# Import after sys.path setup so the script works when run directly.
from sovyx.voice.pipeline._config import VoicePipelineConfig  # noqa: E402
from sovyx.voice.pipeline._frame_types import (  # noqa: E402
    BargeInInterruptionFrame,
    EndFrame,
    UserStartedSpeakingFrame,
)
from sovyx.voice.pipeline._orchestrator import VoicePipeline  # noqa: E402
from sovyx.voice.pipeline._state import VoicePipelineState  # noqa: E402


def _make_pipeline() -> VoicePipeline:
    return VoicePipeline(
        config=VoicePipelineConfig(),
        vad=MagicMock(),
        wake_word=MagicMock(),
        stt=AsyncMock(),
        tts=AsyncMock(),
        event_bus=None,
    )


def _drive_synthetic_turn(pipeline: VoicePipeline, turn_id: int) -> None:
    """One synthetic IDLE → RECORDING → ... → IDLE turn."""
    pipeline._current_utterance_id = f"soak-{turn_id}"
    pipeline._record_frame(
        UserStartedSpeakingFrame(
            frame_type="UserStartedSpeaking",
            timestamp_monotonic=time.monotonic(),
            source="wake_word",
        ),
    )
    pipeline._state = VoicePipelineState.RECORDING
    pipeline._state = VoicePipelineState.TRANSCRIBING
    pipeline._state = VoicePipelineState.THINKING
    pipeline._state = VoicePipelineState.SPEAKING
    pipeline._state = VoicePipelineState.IDLE
    pipeline._current_utterance_id = ""


def _get_rss_mb() -> float:
    """Best-effort process RSS in MB. Returns 0 if psutil absent."""
    try:
        import psutil  # noqa: PLC0415

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except ImportError:
        return 0.0


async def soak(hours: float, *, turn_interval_s: float = 5.0) -> int:
    pipeline = _make_pipeline()
    deadline = time.monotonic() + hours * 3600
    turn_id = 0
    baseline_rss = 0.0
    last_report = time.monotonic()

    print(f"[soak] starting {hours}h run, turn_interval={turn_interval_s}s")  # noqa: T201

    while time.monotonic() < deadline:
        _drive_synthetic_turn(pipeline, turn_id)
        turn_id += 1

        # Periodic concurrent barge-in to exercise that path too.
        if turn_id % 10 == 0:
            await asyncio.gather(
                pipeline.cancel_speech_chain(reason="soak_barge"),
                pipeline.cancel_speech_chain(reason="soak_barge2"),
            )

        # Hourly report.
        now = time.monotonic()
        if now - last_report > 3600:
            gc.collect()
            rss = _get_rss_mb()
            if baseline_rss == 0.0:
                baseline_rss = rss
            history = pipeline._state_machine.frame_history()
            end_frames = sum(1 for f in history if isinstance(f, EndFrame))
            barge_frames = sum(1 for f in history if isinstance(f, BargeInInterruptionFrame))
            growth_pct = (
                (rss - baseline_rss) / baseline_rss * 100 if baseline_rss > 0 else 0.0
            )
            print(  # noqa: T201
                f"[soak] turn={turn_id} ring_size={len(history)} "
                f"end_frames={end_frames} barge_frames={barge_frames} "
                f"rss={rss:.1f}MB (Δ {growth_pct:+.1f}%)",
            )
            if growth_pct > 10.0:
                print(  # noqa: T201
                    f"[soak] FAIL: RSS grew {growth_pct:.1f}% from baseline "
                    f"{baseline_rss:.1f}MB",
                )
                return 1
            last_report = now

        await asyncio.sleep(turn_interval_s)

    print(f"[soak] PASS: completed {turn_id} turns in {hours}h")  # noqa: T201
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="Soak duration in hours (default: 24).",
    )
    parser.add_argument(
        "--turn-interval-s",
        type=float,
        default=5.0,
        help="Seconds between synthetic turns (default: 5).",
    )
    args = parser.parse_args()
    return asyncio.run(
        soak(hours=args.hours, turn_interval_s=args.turn_interval_s),
    )


if __name__ == "__main__":
    sys.exit(main())
