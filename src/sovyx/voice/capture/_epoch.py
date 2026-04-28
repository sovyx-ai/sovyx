"""Ring-buffer epoch packing — :class:`EpochMixin`.

Extracted from ``voice/_capture_task.py`` (line 1702 pre-split)
per master mission Phase 1 / T1.4 step 6. First mixin landed; the
subsequent steps (`RingMixin`, `RestartMixin`, `LoopMixin`) follow
the same pattern.

The capture task packs ``(epoch, samples_written)`` into a single
``int`` attribute (``self._ring_state``) so that an external
consumer's single ``LOAD_ATTR`` observes both components atomically.
``EpochMixin`` exposes the decomposition via
:meth:`samples_written_mark`; the packing constants
(``_RING_EPOCH_SHIFT``, ``_RING_SAMPLES_MASK``) live in
``capture/_constants.py`` from T1.4 step 5.

Mixin contract: the host class must initialise ``self._ring_state``
in its ``__init__`` before any ``samples_written_mark`` call. The
attribute annotation below is mypy-strict-compatible — it declares
the attribute exists without instantiating it on the mixin itself.
"""

from __future__ import annotations

from sovyx.voice.capture._constants import _RING_EPOCH_SHIFT, _RING_SAMPLES_MASK

__all__ = ["EpochMixin"]


class EpochMixin:
    """Atomic decomposition of the packed ring-buffer state.

    Pure-method mixin — exposes :meth:`samples_written_mark` and
    relies on the host class to maintain ``self._ring_state``
    (typically ``AudioCaptureTask`` updates it from the audio
    callback's ``call_soon_threadsafe`` enqueue path).

    The mixin pattern (vs a free function on the host class)
    isolates the "atomic LOAD_ATTR" contract in one place — any
    future change to the packing layout lands here without
    touching the rest of the capture-task surface.
    """

    # Declared so mypy strict accepts ``self._ring_state`` access.
    # The host class (``AudioCaptureTask``) sets the actual value
    # in its ``__init__``.
    _ring_state: int

    def samples_written_mark(self) -> tuple[int, int]:
        """Return an opaque ``(epoch, samples_written)`` pair.

        Atomic decomposition of the packed :attr:`_ring_state` into the
        two logical components the coordinator needs:

        1. Single ``LOAD_ATTR`` of ``_ring_state`` copies both components
           into a local name in one bytecode step — no cross-loop race
           can split the epoch from the samples.
        2. The returned tuple is therefore guaranteed to reflect one
           consistent state generation, satisfying the
           :class:`~sovyx.voice.health.contract.CaptureTaskProto`
           contract.

        Callers treat the tuple as opaque. See the Protocol docstring
        for the contract's rationale.
        """
        state = self._ring_state  # single atomic LOAD_ATTR
        return (state >> _RING_EPOCH_SHIFT, state & _RING_SAMPLES_MASK)
