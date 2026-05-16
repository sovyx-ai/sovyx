"""Shared helpers for typed-response boundary round-trip tests.

Phase 5.D (commits aee85844..f277ba19, v0.32.7) introduced
``Model.model_validate(helper_dict)`` at 14 dashboard voice route
boundaries; Mission C2 §T2.7 codifies the round-trip pattern as a
reusable helper so every NEW typed endpoint inherits drift coverage
for free.

The canonical pattern — DO write this in every new boundary test::

    response = assert_boundary_accepts(
        VoiceStatusResponse,
        helper_factory=lambda: get_voice_status_helper_output(),
        field_assertions={"capture.input_device": 7},
    )

The helper factory's job is to mirror the producer's RUNTIME-bound
output (not the constructor-time defaults). Forgetting that
distinction is the exact coverage gap that allowed C2 to ship:
pre-mission tests asserted on a mock with ``input_device=3`` (int)
at the helper layer but never round-tripped through the typed
boundary, so the str-only union escaped CI for ~7 months.

Mission anchor:
``docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md``
§T2.7 (shared fixture) + §T2.6 (cohort audit).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def assert_boundary_accepts(
    model_cls: type[T],
    helper_factory: Callable[[], dict[str, Any]],
    *,
    field_assertions: dict[str, Any] | None = None,
) -> T:
    """Validate that ``model_cls`` accepts the producer's actual shape.

    Args:
        model_cls: The typed Pydantic boundary model (e.g.
            :class:`VoiceStatusResponse`).
        helper_factory: Zero-arg callable returning the helper dict
            shape the producer would emit in prod. MUST mirror the
            real producer's runtime-bound output, not the constructor-
            time defaults — see file docstring for the C2 rationale.
        field_assertions: Optional per-field equality checks. Keys
            are dotted attribute paths against the validated instance
            (e.g. ``"capture.input_device"``); values are the expected
            terminal values. Useful when the field IS the regression
            target (C2 §T1.2 pattern).

    Returns:
        The validated model instance, for further inspection by the
        caller's specific assertions.

    Raises:
        pydantic.ValidationError: On failure, with a structured
            message identifying which field rejected the producer's
            shape — exactly the signal C2's pre-mission coverage gap
            missed for ``input_device=7``.
        AssertionError: When ``field_assertions`` carry an expected
            value that doesn't match the validated instance — flags
            drift between the producer's runtime shape and the
            boundary's stored shape.
    """
    shape = helper_factory()
    instance = model_cls.model_validate(shape)
    if field_assertions:
        for dotted_path, expected in field_assertions.items():
            actual: Any = instance
            for segment in dotted_path.split("."):
                actual = getattr(actual, segment)
            assert actual == expected, (
                f"boundary round-trip drift: {dotted_path}={actual!r} "
                f"!= expected {expected!r}"
            )
    return instance


__all__ = ["assert_boundary_accepts"]
