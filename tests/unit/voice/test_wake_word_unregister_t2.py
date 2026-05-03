"""T2/T4 mission tests — wake-word unregister wire-up.

Mission: ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T2 (orchestrator
method on VoicePipeline) and §T4 (``wake_word.unregister_mind`` RPC
handler in :mod:`sovyx.engine._rpc_handlers`).

These tests cover the symmetric inverse of the ``wake_word.register_mind``
chain: the dashboard's per-mind wake-word toggle endpoint (T3) calls the
RPC, which delegates to :meth:`VoicePipeline.unregister_mind_wake_word`,
which delegates to :meth:`WakeWordRouter.unregister_mind`. The bool
return distinguishes "actually disabled" from "already disabled".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from sovyx.engine.errors import VoiceError
from sovyx.engine.types import MindId
from sovyx.voice._wake_word_router import WakeWordRouter

# ── T2: VoicePipeline.unregister_mind_wake_word orchestrator method ──


def _mock_onnx_session() -> MagicMock:
    session = MagicMock()
    inputs_meta = MagicMock()
    inputs_meta.name = "input"
    session.get_inputs.return_value = [inputs_meta]
    session.run.side_effect = lambda *_a, **_kw: [np.array([[0.1]], dtype=np.float32)]
    return session


def _patch_onnxruntime() -> object:
    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
    mock_ort.InferenceSession.return_value = _mock_onnx_session()
    return patch.dict("sys.modules", {"onnxruntime": mock_ort})


class TestVoicePipelineUnregisterMindWakeWord:
    """T2 — :meth:`VoicePipeline.unregister_mind_wake_word`.

    Tested via a stand-in (a tiny shim around the live router) so we
    don't need to spin a full pipeline. The orchestrator's contract is
    a 3-line method: raise VoiceError when router is None, else delegate
    to ``self._wake_word_router.unregister_mind(mind_id)`` and return
    its bool. Pinning that contract here protects the wire from T3's
    dashboard endpoint.
    """

    def _make_pipeline_stub(self, router: WakeWordRouter | None) -> object:
        """Build the smallest object that exposes the same method
        :meth:`VoicePipeline.unregister_mind_wake_word` calls. The real
        method's body is a pure delegate, so a duck-typed stub matches
        the contract — and keeps this test off the heavy pipeline
        constructor."""
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        stub = MagicMock(spec=VoicePipeline)
        stub._wake_word_router = router  # noqa: SLF001 — mirrors orch field
        # Bind the real method to the stub so the method body runs against
        # our stub-controlled ``_wake_word_router``.
        stub.unregister_mind_wake_word = lambda mind_id: VoicePipeline.unregister_mind_wake_word(
            stub, mind_id
        )
        return stub

    def test_router_none_raises_voice_error(self) -> None:
        stub = self._make_pipeline_stub(router=None)
        with pytest.raises(VoiceError, match="router not configured"):
            stub.unregister_mind_wake_word(MindId("aria"))

    def test_returns_true_when_mind_was_registered(self) -> None:
        router = WakeWordRouter()
        with _patch_onnxruntime():
            router.register_mind(
                MindId("aria"),
                model_path=Path("/fake/aria.onnx"),
            )
        stub = self._make_pipeline_stub(router=router)

        assert stub.unregister_mind_wake_word(MindId("aria")) is True
        assert len(router) == 0

    def test_returns_false_for_unknown_mind(self) -> None:
        router = WakeWordRouter()  # empty router
        stub = self._make_pipeline_stub(router=router)
        assert stub.unregister_mind_wake_word(MindId("never-existed")) is False

    def test_idempotent_on_repeated_calls(self) -> None:
        router = WakeWordRouter()
        with _patch_onnxruntime():
            router.register_mind(
                MindId("aria"),
                model_path=Path("/fake/aria.onnx"),
            )
        stub = self._make_pipeline_stub(router=router)
        assert stub.unregister_mind_wake_word(MindId("aria")) is True
        assert stub.unregister_mind_wake_word(MindId("aria")) is False
        assert stub.unregister_mind_wake_word(MindId("aria")) is False


# ── T4: wake_word.unregister_mind RPC handler ────────────────────────


@pytest.mark.asyncio
class TestWakeWordUnregisterMindRpc:
    """T4 — ``wake_word.unregister_mind`` RPC handler.

    Validates: (1) empty mind_id rejected, (2) voice subsystem must be
    registered, (3) delegation to
    :meth:`VoicePipeline.unregister_mind_wake_word`, (4) bool from the
    pipeline surfaces in the response.
    """

    async def test_happy_path_returns_unregistered_true(self) -> None:
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline.unregister_mind_wake_word = MagicMock(return_value=True)

        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=True)
        registry.resolve = AsyncMock(return_value=pipeline)

        rpc = DaemonRPCServer()
        register_cli_handlers(rpc, registry)

        result = await rpc._methods["wake_word.unregister_mind"](  # noqa: SLF001
            mind_id="lucia",
        )

        assert result == {"mind_id": "lucia", "unregistered": True}
        pipeline.unregister_mind_wake_word.assert_called_once()
        # Confirm the kwarg/arg contract: positional MindId.
        call_args = pipeline.unregister_mind_wake_word.call_args
        assert str(call_args.args[0]) == "lucia"

    async def test_returns_unregistered_false_when_idempotent_noop(self) -> None:
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline.unregister_mind_wake_word = MagicMock(return_value=False)

        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=True)
        registry.resolve = AsyncMock(return_value=pipeline)

        rpc = DaemonRPCServer()
        register_cli_handlers(rpc, registry)

        result = await rpc._methods["wake_word.unregister_mind"](  # noqa: SLF001
            mind_id="lucia",
        )

        assert result == {"mind_id": "lucia", "unregistered": False}

    async def test_empty_mind_id_rejected(self) -> None:
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer

        rpc = DaemonRPCServer()
        registry = MagicMock()
        register_cli_handlers(rpc, registry)

        with pytest.raises(ValueError, match="non-empty"):
            await rpc._methods["wake_word.unregister_mind"](  # noqa: SLF001
                mind_id="",
            )

    async def test_whitespace_only_mind_id_rejected(self) -> None:
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer

        rpc = DaemonRPCServer()
        registry = MagicMock()
        register_cli_handlers(rpc, registry)

        with pytest.raises(ValueError, match="non-empty"):
            await rpc._methods["wake_word.unregister_mind"](  # noqa: SLF001
                mind_id="   ",
            )

    async def test_voice_subsystem_not_registered_raises_voice_error(self) -> None:
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer

        rpc = DaemonRPCServer()
        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=False)
        register_cli_handlers(rpc, registry)

        with pytest.raises(VoiceError, match="voice subsystem not enabled"):
            await rpc._methods["wake_word.unregister_mind"](  # noqa: SLF001
                mind_id="lucia",
            )

    async def test_pipeline_voice_error_propagates(self) -> None:
        """When the pipeline rejects (single-mind mode), the RPC
        surfaces the VoiceError so the dashboard can render the
        operator-facing remediation message."""
        from sovyx.engine._rpc_handlers import register_cli_handlers
        from sovyx.engine.rpc_server import DaemonRPCServer
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        pipeline = MagicMock(spec=VoicePipeline)
        pipeline.unregister_mind_wake_word = MagicMock(
            side_effect=VoiceError("router not configured (single-mind mode)"),
        )

        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=True)
        registry.resolve = AsyncMock(return_value=pipeline)

        rpc = DaemonRPCServer()
        register_cli_handlers(rpc, registry)

        with pytest.raises(VoiceError, match="single-mind mode"):
            await rpc._methods["wake_word.unregister_mind"](  # noqa: SLF001
                mind_id="lucia",
            )
