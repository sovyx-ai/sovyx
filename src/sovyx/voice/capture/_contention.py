"""Linux session-manager contention pattern helpers.

Extracted from ``voice/_capture_task.py`` (lines 236-307 pre-split)
per master mission Phase 1 / T1.4 step 3. Pure functions + a frozen
constant — no class state coupling, no I/O.

Why a separate module: the contention-pattern classifier is the
single place that turns the raw PortAudio + opener error stream
into the structured ``CaptureDeviceContendedError`` payload the
dashboard renders as actionable chips. Keeping it isolated makes
it trivially unit-testable (the existing
``test_capture_device_contended_error.py`` suite imports the two
public helpers directly) and decouples future heuristic changes
from the bulk of the capture task.

Legacy import surface preserved: ``voice/_capture_task.py``
re-exports every name in ``__all__`` so the existing import
``from sovyx.voice._capture_task import
_is_session_manager_contention_pattern`` keeps working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.voice._stream_opener import OpenAttempt


__all__ = [
    "_SESSION_MANAGER_CONTENTION_ERROR_CODES",
    "_is_session_manager_contention_pattern",
    "_suggest_session_manager_alternatives",
]


_SESSION_MANAGER_CONTENTION_ERROR_CODES: frozenset[str] = frozenset(
    {
        "device_busy",
        "device_disappeared",
        "device_not_found",
    }
)
"""ErrorCode values interpreted as "another client holds the device".

PortAudio on Linux returns ``-9985 Device unavailable`` for the common
"PipeWire grabbed hw:X,Y" pathology. The opener classifies that as
``ErrorCode.DEVICE_BUSY``. ``DEVICE_DISAPPEARED`` covers the related
``-9988 Device disappeared`` and ``DEVICE_NOT_FOUND`` is included
because some kernel-invalidated states surface as ``-9996 Invalid
device`` when a session manager yanks the exclusive lock mid-open.
"""


def _is_session_manager_contention_pattern(
    *,
    platform: str,
    open_attempts: Sequence[OpenAttempt],
) -> bool:
    """Return ``True`` iff the attempt list matches "session manager holds hw".

    The rule is intentionally narrow — false positives would only
    swap a generic ``RuntimeError`` message for a slightly more useful
    one (no regression risk), but we still constrain the heuristic to
    (a) Linux only, (b) at least one attempt made, (c) every attempt
    falls in the contention-class :data:`_SESSION_MANAGER_CONTENTION_ERROR_CODES`.

    The ``attempts_tried_hw_and_virtual`` half of the ADR rule is
    handled upstream by the candidate-set: when this function fires,
    the opener already iterated the opener-side pyramid on the *current*
    candidate, and the cascade-level loop in
    :func:`~sovyx.voice.health.cascade.run_cascade_for_candidates` has
    exhausted every candidate (hardware + virtual). Re-checking here
    would require access to the cascade history, which the capture
    task legitimately does not have. Keeping the check at open-level
    is sound because the cascade only reaches ``start()`` on a device
    it already considered "best bet remaining" — a device-busy cluster
    at this stage implies every earlier candidate also failed.
    """
    if platform != "linux":
        return False
    if not open_attempts:
        return False
    return all(
        attempt.error_code is not None
        and attempt.error_code.value in _SESSION_MANAGER_CONTENTION_ERROR_CODES
        for attempt in open_attempts
    )


def _suggest_session_manager_alternatives() -> list[str]:
    """Return the UI-facing action tokens for a session-manager grab.

    Order: preferred alternative first. The dashboard maps each token
    to an i18n key + an action (chip click dispatches the corresponding
    fallback request). Currently static — future revisions may query
    enumeration to elide tokens for devices that don't exist on the
    host, but doing so here would introduce a sync ``sounddevice`` call
    on the error path.
    """
    return [
        "select_device:pipewire",
        "select_device:default",
        "select_device:pulse",
        "stop_process:pipewire",
    ]
