"""SamplingProcessor — keep-every-N rate-limiting for high-frequency events.

Voice and VAD pipelines emit thousands of frames per second. Logging
every one would saturate the file handler within minutes and bury the
signals that actually matter (state transitions, errors, anomalies).

This processor sits in the structlog chain between the envelope and
the renderer. For each event name registered in
:data:`_SAMPLED_EVENTS`, it keeps one in every ``N`` records (where
``N`` is sourced from
:class:`sovyx.engine.config.ObservabilitySamplingConfig`) and drops
the rest by raising :class:`structlog.DropEvent`. Drops are tallied
per-event in :attr:`_dropped_counts` so a periodic emitter can publish
``logging.sampled`` summaries (the one event that is itself never
sampled — meta-observability, see plan §22.0).

All other events pass through untouched: sampling is opt-in, not
opt-out, so a new event accidentally added to a hot path is logged at
full rate until someone explicitly registers it here.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §7 Task 1.5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from sovyx.engine.config import ObservabilitySamplingConfig


_SAMPLED_EVENTS: Final[dict[str, str]] = {
    "audio.frame": "audio_frame_rate",
    "voice.vad.frame": "vad_frame_rate",
}
"""Mapping of event name → ``ObservabilitySamplingConfig`` attribute.

Events not listed here are never sampled. Adding a new event requires
both a config field and an entry in this map; the indirection keeps
runtime tweaks (env vars) decoupled from the processor code.
"""


class SamplingProcessor:
    """Structlog processor that keeps one in every N records of hot events.

    Construct once with the daemon's
    :class:`ObservabilitySamplingConfig`; the rate for each registered
    event is snapshotted at construction so the hot-path emit does not
    re-read the config (Pydantic attribute access is fast but not
    free, and this processor runs on every voice frame).

    The first occurrence of any sampled event is always kept (counter
    starts at 0, ``0 % rate == 0``), so a cold-start trace shows the
    initial frame. Subsequent records are dropped until the counter
    wraps back around to a multiple of the rate.

    A non-positive rate (``≤ 0``) disables sampling for that event —
    every record is kept. This is the documented escape hatch for
    debugging: ``SOVYX_OBSERVABILITY__SAMPLING__AUDIO_FRAME_RATE=0``.
    """

    __slots__ = ("_counters", "_dropped_counts", "_rates")

    def __init__(self, config: ObservabilitySamplingConfig) -> None:
        self._rates: dict[str, int] = {
            event: getattr(config, attr) for event, attr in _SAMPLED_EVENTS.items()
        }
        self._counters: dict[str, int] = dict.fromkeys(_SAMPLED_EVENTS, 0)
        self._dropped_counts: dict[str, int] = dict.fromkeys(_SAMPLED_EVENTS, 0)

    def __call__(
        self,
        logger: Any,  # noqa: ANN401 — opaque structlog logger reference.
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Decide whether to keep or drop *event_dict* based on its event name.

        Raises:
            structlog.DropEvent: when the event is registered for
                sampling and the current counter value falls outside
                the keep-every-N window.
        """
        event_name = event_dict.get("event")
        if not isinstance(event_name, str):
            return event_dict
        rate = self._rates.get(event_name)
        if rate is None or rate <= 1:
            return event_dict
        counter = self._counters[event_name]
        self._counters[event_name] = counter + 1
        if counter % rate == 0:
            return event_dict
        self._dropped_counts[event_name] += 1
        raise structlog.DropEvent

    def flush_dropped(self) -> dict[str, int]:
        """Return per-event drop counts since the last flush, then reset.

        Intended to be called by a periodic emitter that publishes
        ``logging.sampled`` summaries. Returning a copy (not a live
        view) means the caller can iterate safely while the next
        sampled record increments the fresh counters.
        """
        snapshot = dict(self._dropped_counts)
        self._dropped_counts = dict.fromkeys(_SAMPLED_EVENTS, 0)
        return snapshot


__all__ = ["SamplingProcessor"]
