"""Dual-emission helper for capture-integrity events (Mission H2 §T1.3).

Single canonical emit path for the 5 :class:`CaptureIntegrityEvent`
values. During the ADR-D14 staged-adoption window (v0.49.7..v0.50.x),
each call emits BOTH the neutral event name AND the legacy twin from
:data:`LEGACY_TWIN_MAP`. Phase 3 STRICT (v0.51.0) removes the legacy
emission block; consumers that grep for ``# h2-allowlist: dual-emission
per ADR-D14`` find every removal site.

The neutral emission carries three v2.0.0 schema metadata fields not
present on the legacy event:

* ``voice.platform: Literal["linux", "windows", "darwin", "other"]`` —
  auto-resolved from :func:`current_platform_token` once at import time.
* ``voice.bypass_family: str`` — auto-resolved from the strategy list
  via :func:`resolve_family_from_strategies` majority vote.
* ``voice.event_schema_version: "2.0.0"`` — explicit schema version so
  downstream consumers can branch on shape.

The legacy emission stays EXACTLY as-is for operator playbook
compatibility — every legacy attribute payload is preserved verbatim;
only the new metadata is the neutral-emission delta.

Anti-pattern compliance:

* #1 — module-level ``logger = get_logger(__name__)``.
* #9 — uses :class:`StrEnum` :class:`CaptureIntegrityEvent` for the
  neutral name lookup.
* #14 — synchronous helper; no I/O, no event-loop blocking.
* #17 — reads the kill-switch from :class:`VoiceTuningConfig` (no
  hardcoded constants).
* #20 — extracted into a dedicated module so test patches use
  :func:`patch.object` against this module path.
* #42 — H2 does NOT add a composite-store wire here; the quarantine
  path downstream already records ``axis="voice"`` for actionable
  failure surfaces (H3 territory).

Mission anchor:
``docs-internal/missions/MISSION-h2-platform-neutral-event-naming-2026-05-18.md``
§T1.3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from sovyx.observability.logging import get_logger
from sovyx.voice._event_names import (
    LEGACY_TWIN_MAP,
    CaptureIntegrityEvent,
)
from sovyx.voice._platform_metadata import (
    current_platform_token,
    is_mixed_platform_strategy_list,
    resolve_family_from_strategies,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)


LogLevel = Literal["info", "warning", "error"]
"""Restricted level set — capture-integrity events never emit at DEBUG
(volume control) or CRITICAL (reserved for daemon-fatal axes).
"""


SCHEMA_VERSION: Literal["2.0.0"] = "2.0.0"
"""Neutral-event schema version. Bumped to ``"3.0.0"`` if/when the
metadata field set evolves in a backwards-incompatible way.
"""


# Legacy events whose pre-mission payload used dotted-namespace
# (``voice.*``) attribute keys. The ``audio.apo.bypassed`` verdict-tagged
# emission AND the ``voice.apo.bypass`` OTel-semconv parent both followed
# this convention. The snake-case legacy twins (``voice_apo_bypass_*``)
# used bare attribute keys. Preserving the EXACT pre-mission shape is
# the load-bearing invariant of ADR-D14 dual-emission discipline —
# operator playbooks grepping for ``voice.verdict=success`` etc. continue
# to resolve.
_DOTTED_NAMESPACE_LEGACY_EVENTS: frozenset[CaptureIntegrityEvent] = frozenset(
    {
        CaptureIntegrityEvent.BYPASS,
        CaptureIntegrityEvent.BYPASSED,
    }
)


_ENV_KNOB = "SOVYX_TUNING__VOICE__CAPTURE_INTEGRITY_DUAL_EMIT_ENABLED"
"""Tuning-knob env var name. Reading the env var directly avoids the
~50 ms cost of instantiating :class:`EngineConfig` on every emit; the
:class:`VoiceTuningConfig` Pydantic field uses the SAME env-var prefix
+ name so operator-side configuration via env vars works identically
to the canonical config-driven path.
"""

_FALSEY = frozenset({"false", "0", "no", "off", ""})


def _is_dual_emit_enabled() -> bool:
    """Return True if the dual-emission kill switch is enabled.

    Reads the env var directly to keep the wrapper's per-emit overhead
    in the microsecond range; pydantic-settings instantiation costs
    ~50ms which is unacceptable on a hot-path observability emitter.

    Defaults to True per anti-pattern #34 inverse (observability defaults
    always-on); the operator must explicitly set the env var to a falsey
    value to disable dual-emission. Acceptable falsey values mirror
    pydantic-settings ``BaseSettings.parse_bool`` semantics.
    """
    import os

    raw = os.environ.get(_ENV_KNOB, "true").strip().lower()
    return raw not in _FALSEY


def _emit_at_level(level: LogLevel, event: str, **attrs: object) -> None:
    """Dispatch the structured emission at the requested level.

    Keeps the level→logger-method dispatch out of the hot path callers
    (the bypass-coordinator mixin) so the wrapper is one entry point.
    """
    if level == "info":
        logger.info(event, **attrs)
    elif level == "warning":
        logger.warning(event, **attrs)
    elif level == "error":
        logger.error(event, **attrs)


def emit_capture_integrity_event(
    event: CaptureIntegrityEvent,
    level: LogLevel,
    *,
    mind_id: str,
    strategies: Sequence[str] | None = None,
    voice_clarity_active: bool | None = None,
    verdict: str | None = None,
    **legacy_attrs: object,
) -> None:
    """Dual-emit the neutral :class:`CaptureIntegrityEvent` + its legacy twin.

    During the staged-adoption window both events fire with the same
    severity and the same legacy payload. The neutral event additionally
    carries ``voice.platform``, ``voice.bypass_family``,
    ``voice.event_schema_version``.

    The legacy event uses the pre-mission attribute shape — bare kwargs
    like ``voice_clarity_active=False`` are passed verbatim so the legacy
    log-schema continues to match. The neutral event uses dotted-namespace
    attribute keys (``voice.voice_clarity_active``) per OTel semconv
    discipline.

    Args:
        event: Which neutral :class:`CaptureIntegrityEvent` to emit. The
            legacy twin name is looked up from :data:`LEGACY_TWIN_MAP`.
        level: One of ``"info"``, ``"warning"``, ``"error"``.
        mind_id: The mind identifier (``voice.mind_id`` attribute).
        strategies: The strategy-name list driving the cascade — used to
            resolve ``voice.bypass_family`` via majority vote. ``None``
            and empty list both map to ``family=noop``.
        voice_clarity_active: Whether the Windows Voice Clarity APO is
            active on the current endpoint. ``None`` when the caller
            cannot determine (preserved for legacy compatibility).
        verdict: ``"success"`` / ``"failure"`` / ``"partial"`` —
            categorisation of the bypass outcome. Optional.
        **legacy_attrs: Any additional attributes the legacy event
            carried (``attempts``, ``outcomes``, ``error``, ``error_type``,
            ``strategy_name``, ``attempt_index``, ``reason``, etc.).
            Passed verbatim to the legacy emission AND included in the
            neutral emission under dotted-namespace keys via the
            ``voice.<name>`` rewrite below.

    The function is total — never raises on bad input. Mixed-platform
    strategy lists emit a structured WARN ``voice.capture_integrity.mixed_platform_strategies``
    before the main emission so observers can correlate.
    """
    legacy_name = LEGACY_TWIN_MAP[event]
    platform = current_platform_token()
    family = resolve_family_from_strategies(strategies or []).value

    if strategies and is_mixed_platform_strategy_list(strategies):
        logger.warning(
            "voice.capture_integrity.mixed_platform_strategies",
            **{
                "voice.mind_id": mind_id,
                "voice.platform": platform,
                "voice.strategies": list(strategies),
                "voice.action_required": (
                    "Bypass cascade contains strategies from multiple "
                    "platform families. Investigate the dispatch path — "
                    "this should never happen in production."
                ),
            },
        )

    # ── Neutral emission (always fires) ──
    neutral_attrs: dict[str, object] = {
        "voice.mind_id": mind_id,
        "voice.platform": platform,
        "voice.bypass_family": family,
        "voice.event_schema_version": SCHEMA_VERSION,
    }
    if voice_clarity_active is not None:
        neutral_attrs["voice.voice_clarity_active"] = voice_clarity_active
    if verdict is not None:
        neutral_attrs["voice.verdict"] = verdict
    if strategies is not None:
        neutral_attrs["voice.strategies"] = list(strategies)
    # Promote any extra legacy_attrs into dotted-namespace neutral attrs.
    for k, v in legacy_attrs.items():
        if k.startswith("voice."):
            neutral_attrs[k] = v
        else:
            neutral_attrs[f"voice.{k}"] = v
    _emit_at_level(level, str(event), **neutral_attrs)

    # ── Legacy emission (preserved through STRICT flip; skip if knob off) ──
    if not _is_dual_emit_enabled():
        return

    legacy_payload: dict[str, object]
    if event in _DOTTED_NAMESPACE_LEGACY_EVENTS:
        # ``audio.apo.bypassed`` + ``voice.apo.bypass`` pre-mission shape:
        # every attribute key carries the ``voice.`` dotted-namespace prefix.
        # Operator playbooks grepping ``voice.verdict=success`` continue
        # to resolve through the dual-emission window.
        legacy_payload = {"voice.mind_id": mind_id}
        if voice_clarity_active is not None:
            legacy_payload["voice.voice_clarity_active"] = voice_clarity_active
        if verdict is not None:
            legacy_payload["voice.verdict"] = verdict
        if strategies is not None:
            legacy_payload["voice.strategies"] = list(strategies)
        for k, v in legacy_attrs.items():
            key = k if k.startswith("voice.") else f"voice.{k}"
            legacy_payload[key] = v
    else:
        # ``voice_apo_bypass_*`` pre-mission shape: bare attribute keys.
        legacy_payload = {"mind_id": mind_id}
        if voice_clarity_active is not None:
            legacy_payload["voice_clarity_active"] = voice_clarity_active
        if verdict is not None:
            legacy_payload["verdict"] = verdict
        if strategies is not None:
            legacy_payload["strategies"] = list(strategies)
        legacy_payload.update(legacy_attrs)

    # h2-allowlist: dual-emission per ADR-D14
    _emit_at_level(level, legacy_name, **legacy_payload)


__all__ = [
    "LogLevel",
    "SCHEMA_VERSION",
    "emit_capture_integrity_event",
]
