"""FailoverSTTEngine — primary STT with automatic failover to a secondary.

W2.1 / G-P1-4 foundation (MISSION-VOICE-DEEP-INVESTIGATION-2026-06-01).

The Moonshine S2 timeout taxonomy (``MoonshineSTT.timeout_count``) and the
CloudSTT secondary engine both already exist, but nothing wired them into a
chain — so sustained primary-STT degradation produced **permanent silence
with good telemetry but no recovery** (a user whose local STT keeps timing
out just gets nothing, repeatedly). This is the missing mechanism: a
transparent :class:`~sovyx.voice.stt.STTEngine` wrapper that *recovers* a
failed/timed-out primary transcription via a secondary engine.

Failover trigger (distinguishes a real failure from genuine silence so we
never spam a paid cloud secondary on every quiet moment):

* primary ``transcribe`` raises (``VoiceError`` / ``RuntimeError`` /
  ``OSError``) → fail over; OR
* primary's cumulative ``timeout_count`` INCREASED across the call (the S2
  signal that the transcription timed out rather than the user being
  silent) → fail over.

A primary result with empty text but NO timeout-delta is treated as genuine
silence and returned as-is — no failover.

Circuit breaker: after ``breaker_threshold`` consecutive failovers the
primary is considered down and is SKIPPED (we go straight to the secondary
rather than burning the timeout budget on a doomed call), with a half-open
probe every ``breaker_probe_interval`` calls to let the primary recover. A
single primary success resets the breaker.

Scope: failover applies to the batch ``transcribe`` path (the voice
pipeline's final STT). Streaming (``transcribe_streaming``) delegates to the
primary unchanged — streaming failover is out of scope and intentionally not
implemented here.

This module is the FOUNDATION; the factory wire-up shipped separately (W2.1):
``bootstrap`` builds the secondary CloudSTT when configured and the factory
wraps it via :class:`FailoverSTTEngine`, gated by the opt-in default-OFF
``SOVYX_TUNING__VOICE__STT_FAILOVER_ENABLED`` flag per the staged-adoption
rule.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from sovyx.engine.errors import VoiceError
from sovyx.observability.logging import get_logger
from sovyx.voice.stt import STTEngine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np

    from sovyx.voice.stt import PartialTranscription, TranscriptionResult

logger = get_logger(__name__)

_DEFAULT_BREAKER_THRESHOLD = 3
"""Consecutive failovers before the primary is skipped (treated as down)."""

_DEFAULT_BREAKER_PROBE_INTERVAL = 10
"""While the breaker is open, retry the primary once every N calls (half-open
probe) so a recovered primary is picked back up without an operator restart."""


class FailoverSTTEngine(STTEngine):
    """Wrap a primary STT engine with automatic failover to a secondary.

    Args:
        primary: The preferred engine (typically local ``MoonshineSTT``).
        secondary: The fallback engine (typically ``CloudSTT``). Initialized
            best-effort; a secondary that fails to initialize disables
            failover (the wrapper then behaves like the bare primary).
        breaker_threshold: Consecutive failovers before the primary is
            skipped as down.
        breaker_probe_interval: Half-open re-probe cadence while open.
    """

    def __init__(
        self,
        primary: STTEngine,
        secondary: STTEngine,
        *,
        breaker_threshold: int = _DEFAULT_BREAKER_THRESHOLD,
        breaker_probe_interval: int = _DEFAULT_BREAKER_PROBE_INTERVAL,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._breaker_threshold = max(1, breaker_threshold)
        self._breaker_probe_interval = max(1, breaker_probe_interval)
        self._secondary_available = False
        self._consecutive_failovers = 0
        self._calls_since_probe = 0

    @property
    def state(self) -> object:
        """Proxy the primary engine's lifecycle state.

        The factory's post-initialize READY guard reads ``stt.state``; proxying
        the primary's keeps that guard meaningful when the engine is wrapped
        (the failover wrapper is only as ready as its primary). ``None`` when
        the primary exposes no ``state`` (a non-Moonshine primary)."""
        return getattr(self._primary, "state", None)

    async def initialize(self) -> None:
        """Initialize the primary (required) + the secondary (best-effort)."""
        await self._primary.initialize()
        try:
            await self._secondary.initialize()
            self._secondary_available = True
        except (VoiceError, RuntimeError, OSError, ImportError) as exc:
            # A broken / unconfigured secondary (e.g. missing cloud key, no
            # openai package) must NOT block the primary path — it just
            # disables failover. Surfaced so operators can see why recovery
            # is unavailable.
            self._secondary_available = False
            logger.warning(
                "voice.stt.failover.secondary_unavailable",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16_000,
    ) -> TranscriptionResult:
        """Transcribe via the primary, failing over to the secondary on a
        primary failure (raise or S2 timeout) — never on genuine silence."""
        # Breaker OPEN: primary considered down. Skip it (save the timeout
        # budget) and use the secondary directly, except for a periodic
        # half-open probe that lets a recovered primary reset the breaker.
        if self._consecutive_failovers >= self._breaker_threshold and self._secondary_available:
            self._calls_since_probe += 1
            if self._calls_since_probe < self._breaker_probe_interval:
                return await self._transcribe_secondary(audio, sample_rate, reason="breaker_open")
            self._calls_since_probe = 0  # half-open: fall through to probe the primary

        timeouts_before = self._primary_timeout_count()
        try:
            result = await self._primary.transcribe(audio, sample_rate)
        except (VoiceError, RuntimeError, OSError) as exc:
            return await self._failover(
                audio, sample_rate, reason="primary_raised", error=str(exc)
            )

        # S2 timeout-delta — a timeout is a FAILURE (fail over); an empty
        # result with no timeout is genuine silence (return as-is).
        if self._primary_timeout_count() > timeouts_before:
            return await self._failover(audio, sample_rate, reason="primary_timeout")

        # Primary succeeded — reset the breaker.
        self._consecutive_failovers = 0
        self._calls_since_probe = 0
        return result

    async def _failover(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        reason: str,
        error: str | None = None,
    ) -> TranscriptionResult:
        """Attempt the secondary after a primary failure; honest fallthrough."""
        self._consecutive_failovers += 1
        if not self._secondary_available:
            # No recovery path — re-raise so the caller's STT error handling
            # (orchestrator / Wyoming) engages, rather than masking the
            # failure as empty silence.
            logger.warning(
                "voice.stt.failover.no_secondary",
                reason=reason,
                error=error,
                consecutive_failovers=self._consecutive_failovers,
            )
            msg = f"primary STT failed ({reason}) and no secondary is available"
            raise VoiceError(msg)
        return await self._transcribe_secondary(audio, sample_rate, reason=reason)

    async def _transcribe_secondary(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        reason: str,
    ) -> TranscriptionResult:
        try:
            result = await self._secondary.transcribe(audio, sample_rate)
        except (VoiceError, RuntimeError, OSError) as exc:
            logger.warning(
                "voice.stt.failover.secondary_failed",
                reason=reason,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            msg = f"primary STT failed ({reason}) and secondary also failed: {exc}"
            raise VoiceError(msg) from exc
        logger.info(
            "voice.stt.failover.recovered",
            reason=reason,
            consecutive_failovers=self._consecutive_failovers,
        )
        return result

    def transcribe_streaming(
        self,
        audio_stream: AsyncIterator[tuple[np.ndarray, int]],
    ) -> AsyncIterator[PartialTranscription]:
        """Delegate to the primary — streaming failover is out of scope."""
        return self._primary.transcribe_streaming(audio_stream)

    async def close(self) -> None:
        """Close both engines (best-effort; one failure cannot block the other)."""
        with contextlib.suppress(Exception):
            await self._primary.close()
        with contextlib.suppress(Exception):
            await self._secondary.close()

    def _primary_timeout_count(self) -> int:
        """Read the primary's cumulative S2 timeout counter, 0 if it has none."""
        value = getattr(self._primary, "timeout_count", 0)
        return value if isinstance(value, int) else 0


__all__ = ["FailoverSTTEngine"]
