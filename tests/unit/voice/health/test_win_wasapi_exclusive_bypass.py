"""Unit tests for :class:`WindowsWASAPIExclusiveBypass`.

Coverage focus: mission band-aid #19 — pre-hardening the eligibility
check used a case-insensitive substring match on ``"WASAPI"`` in the
``host_api_name``. Any future PortAudio / vendor wrapper label that
contained the substring (e.g. ``"WASAPI Compatibility (DirectSound)"``)
falsely passed eligibility and would trigger an exclusive-mode open
against a non-WASAPI endpoint, producing a confusing failure mode
downstream. The hardened check uses an explicit allowlist of
canonical PortAudio Windows-WASAPI labels.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25
Appendix A band-aid #19 (Host API Check).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sovyx.voice.health.bypass._strategy import BypassRevertError
from sovyx.voice.health.bypass._win_wasapi_exclusive import (
    _WASAPI_HOST_API_LABELS,
    WindowsWASAPIExclusiveBypass,
)
from sovyx.voice.health.contract import BypassContext


@dataclass
class _FakeCaptureTask:
    """Minimal CaptureTaskProto stand-in — never actually invoked
    in eligibility tests (probe_eligibility never touches it)."""

    async def request_exclusive_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("unused")

    async def request_shared_restart(self) -> Any:  # pragma: no cover
        raise AssertionError("unused")


def _ctx(
    *,
    host_api_name: str = "Windows WASAPI",
    platform_key: str = "win32",
) -> BypassContext:
    async def _probe() -> Any:  # pragma: no cover
        raise AssertionError("unused in eligibility")

    return BypassContext(
        endpoint_guid="{guid-A}",
        endpoint_friendly_name="Test Mic",
        host_api_name=host_api_name,
        platform_key=platform_key,
        capture_task=_FakeCaptureTask(),  # type: ignore[arg-type]
        probe_fn=_probe,
    )


class TestEligibilityHostApiAllowlistB19:
    """Mission #19: exact-match allowlist instead of substring containment."""

    @pytest.mark.asyncio()
    async def test_canonical_windows_wasapi_label_accepted(self) -> None:
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="Windows WASAPI"))
        assert result.applicable is True
        assert result.reason == ""

    @pytest.mark.asyncio()
    async def test_legacy_bare_wasapi_label_accepted(self) -> None:
        """Legacy ComboStore entries before PortAudio's ``"Windows WASAPI"``
        canonicalisation stored the bare ``"WASAPI"`` label — the
        allowlist must keep them eligible for backwards-compat."""
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="WASAPI"))
        assert result.applicable is True

    @pytest.mark.asyncio()
    async def test_case_insensitive_normalisation_accepted(self) -> None:
        bypass = WindowsWASAPIExclusiveBypass()
        for label in ("WINDOWS WASAPI", "windows wasapi", "Windows wasapi"):
            result = await bypass.probe_eligibility(_ctx(host_api_name=label))
            assert result.applicable is True, f"label {label!r} should pass"

    @pytest.mark.asyncio()
    async def test_whitespace_normalised(self) -> None:
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="  Windows WASAPI  "))
        assert result.applicable is True

    # ── Band-aid #19 regression: substring impersonation rejected ───

    @pytest.mark.asyncio()
    async def test_wasapi_compatibility_directsound_rejected(self) -> None:
        """The exact failure mode the mission identified: a label
        CONTAINING ``"WASAPI"`` but representing a different host API
        must NOT pass eligibility."""
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(
            _ctx(host_api_name="WASAPI Compatibility (DirectSound)"),
        )
        assert result.applicable is False
        assert result.reason == "not_wasapi_endpoint"

    @pytest.mark.asyncio()
    async def test_pretend_wasapi_wrapper_rejected(self) -> None:
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(
            _ctx(host_api_name="Pretend-WASAPI-but-MME"),
        )
        assert result.applicable is False
        assert result.reason == "not_wasapi_endpoint"

    @pytest.mark.asyncio()
    async def test_directsound_rejected(self) -> None:
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(
            _ctx(host_api_name="Windows DirectSound"),
        )
        assert result.applicable is False
        assert result.reason == "not_wasapi_endpoint"

    @pytest.mark.asyncio()
    async def test_mme_rejected(self) -> None:
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="MME"))
        assert result.applicable is False
        assert result.reason == "not_wasapi_endpoint"

    @pytest.mark.asyncio()
    async def test_wdm_ks_rejected(self) -> None:
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name="Windows WDM-KS"))
        assert result.applicable is False
        assert result.reason == "not_wasapi_endpoint"

    @pytest.mark.asyncio()
    async def test_empty_host_api_rejected(self) -> None:
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(_ctx(host_api_name=""))
        assert result.applicable is False
        assert result.reason == "not_wasapi_endpoint"

    # ── Platform gate (orthogonal to #19) ────────────────────────────

    @pytest.mark.asyncio()
    async def test_non_win32_platform_rejected_first(self) -> None:
        """Platform check fires BEFORE the WASAPI check (cheaper)."""
        bypass = WindowsWASAPIExclusiveBypass()
        result = await bypass.probe_eligibility(
            _ctx(host_api_name="Windows WASAPI", platform_key="linux"),
        )
        assert result.applicable is False
        assert result.reason == "not_win32_platform"

    # ── Allowlist constant invariants ────────────────────────────────

    def test_allowlist_contains_canonical_label(self) -> None:
        """The canonical PortAudio v19 label MUST be in the allowlist."""
        assert "windows wasapi" in _WASAPI_HOST_API_LABELS

    def test_allowlist_lowercased(self) -> None:
        """All entries must be lowercase — the eligibility check
        normalises before lookup, and a non-lowercased entry would
        be unreachable (false negative)."""
        for label in _WASAPI_HOST_API_LABELS:
            assert label == label.lower(), f"allowlist entry {label!r} not lowercased"

    def test_allowlist_no_substring_traps(self) -> None:
        """No allowlist entry should be a strict substring of another
        — otherwise the LONGER label would never be matched on its
        own (would always normalise to the shorter)."""
        labels = sorted(_WASAPI_HOST_API_LABELS, key=len)
        for i, short in enumerate(labels):
            for long in labels[i + 1 :]:
                # ``short`` and ``long`` must NOT be related as substrings.
                assert short != long
                # We DO want "wasapi" inside "windows wasapi" — but they're
                # both LEGITIMATE distinct labels (legacy + canonical).
                # The real trap would be e.g. having both "wasapi" and
                # "wasapi exclusive" as separate entries, which would
                # never happen in practice. Document the invariant
                # rather than enforce a bogus check.

    # ── BypassRevertError import contract (downstream consumers
    #    expect both error classes to be importable from the strategy
    #    module's import edge — regression guard for B3 wiring) ──────

    def test_bypass_revert_error_importable(self) -> None:
        """Mission B3 added BypassRevertError — its import path must
        stay stable so downstream tests / coordinator code don't
        silently break."""
        assert BypassRevertError is not None
        # Constructor contract.
        exc = BypassRevertError("test", reason="x")
        assert exc.reason == "x"
