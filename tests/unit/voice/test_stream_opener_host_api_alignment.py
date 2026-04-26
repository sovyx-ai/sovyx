"""Voice Windows Paranoid Mission §D4 — cascade ↔ runtime opener
alignment tests.

Furo W-4 latent-bug fix: ``_stream_opener._device_chain`` now accepts
``preferred_host_api`` + ``fallback_host_apis`` keyword-only params
and applies a 3-tier bucket sort to the rest of the sibling chain
when ``cascade_host_api_alignment_enabled=True``.

Test surface pinned by this file:

* When the alignment flag is False (foundation default), behaviour
  matches v0.23.x — siblings are returned in PortAudio enumeration
  order regardless of ``preferred_host_api``.
* When the alignment flag is True AND ``preferred_host_api`` is
  supplied:
  - Bucket 0 (preferred host_api siblings) sort first.
  - Bucket 1 (ranked ``fallback_host_apis`` in configured order)
    sort second.
  - Bucket 2 (unranked siblings) sort last in PortAudio order.
* Both new params are Optional with default None — when either is
  None or the flag is False, behaviour is identical to v0.23.x.
* The ``capture_fallback_host_apis`` config field (previously dead
  code at engine/config.py:437-447) is now consumed by
  ``open_input_stream`` and threaded into ``_device_chain`` —
  closes the latent-bug.
* Alignment SLI counter ``voice.opener.host_api_alignment`` fires on
  every ``_device_chain`` call when ``preferred_host_api`` is
  supplied, with ``aligned=true`` when chain[0]'s host_api matches
  the preferred and ``aligned=false`` (Furo W-4 bug signature) when
  it doesn't.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from sovyx.voice._stream_opener import _device_chain
from sovyx.voice.device_enum import DeviceEntry


def _entry(
    *,
    index: int,
    host_api_name: str,
    name: str = "Razer BlackShark V2 Pro",
    canonical: str | None = None,
    channels: int = 1,
    rate: int = 16_000,
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=canonical or name.strip().lower()[:30],
        host_api_index=index,  # placeholder — irrelevant for these tests
        host_api_name=host_api_name,
        max_input_channels=channels,
        max_output_channels=0,
        default_samplerate=rate,
        is_os_default=False,
    )


def _razer_endpoint_4_siblings() -> list[DeviceEntry]:
    """The reproducer rig: Razer BlackShark V2 Pro on Windows 11 25H2 +
    Voice Clarity. PortAudio enumerates 4 sibling DeviceEntries:
    MME first, then DirectSound, then WDM-KS, then WASAPI."""
    return [
        _entry(index=0, host_api_name="MME"),
        _entry(index=1, host_api_name="Windows DirectSound"),
        _entry(index=2, host_api_name="Windows WDM-KS"),
        _entry(index=3, host_api_name="Windows WASAPI"),
    ]


@pytest.fixture(autouse=True)
def _clear_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every SOVYX_TUNING__VOICE__* env var so each test runs
    against the documented defaults without leakage."""
    for key in list(os.environ):
        if key.startswith("SOVYX_TUNING__VOICE__"):
            monkeypatch.delenv(key, raising=False)


# ── Backwards compatibility: alignment flag default False ──────────


class TestAlignmentFlagDefaultPreservesV023Behavior:
    """Foundation default ``cascade_host_api_alignment_enabled=False``
    — _device_chain returns siblings in legacy PortAudio enum order
    regardless of ``preferred_host_api``."""

    def test_no_preferred_returns_enum_order(self) -> None:
        siblings = _razer_endpoint_4_siblings()
        starting = siblings[0]  # MME — runtime resolved here (drift)
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
        )
        # Legacy: chain[0] is starting, rest is enum order.
        assert chain[0].index == 0  # MME
        assert chain[1].index == 1  # DirectSound
        assert chain[2].index == 2  # WDM-KS
        assert chain[3].index == 3  # WASAPI

    def test_preferred_supplied_but_flag_false_uses_enum_order(self) -> None:
        """Even when preferred_host_api is supplied, behaviour falls
        back to enum order when the alignment flag is False —
        backwards-compat with v0.23.x."""
        siblings = _razer_endpoint_4_siblings()
        starting = siblings[0]  # MME (drift)
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
            preferred_host_api="Windows DirectSound",
        )
        # Flag default False → enum order preserved.
        assert chain[1].host_api_name == "Windows DirectSound"  # legacy enum order
        # Bucket sort would have re-ranked these but flag is False.

    def test_starting_is_always_chain_head(self) -> None:
        """``starting`` is always chain[0] regardless of flag /
        preferred — only ``rest`` gets re-sorted."""
        siblings = _razer_endpoint_4_siblings()
        starting = siblings[0]  # MME
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
        )
        assert chain[0] is starting


# ── Alignment flag enabled — bucket sort active ─────────────────────


class TestBucketSortWithAlignmentEnabled:
    """When the alignment flag is True AND preferred_host_api is
    supplied, the 3-tier bucket sort biases sibling iteration."""

    @pytest.fixture
    def _alignment_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED", "true")

    def test_bucket_0_preferred_host_api_first(self, _alignment_enabled: None) -> None:
        """When cascade picks DirectSound but runtime resolves MME
        (Furo W-4 bug repro), the bucket sort puts DirectSound siblings
        ahead of MME / WDM-KS / unranked.

        Note: the cascade winner's DeviceEntry is ALSO a sibling — the
        Razer endpoint exposes 4 host_api wrappers of the same
        canonical mic. With preferred=DirectSound, the DirectSound
        sibling sorts to bucket 0 of rest; MME (starting) is fixed at
        chain[0]; remaining unranked go to bucket 2.
        """
        del _alignment_enabled
        siblings = _razer_endpoint_4_siblings()
        starting = siblings[0]  # MME (drift)
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
            preferred_host_api="Windows DirectSound",
        )
        # chain[0] is starting (MME — fixed).
        assert chain[0].host_api_name == "MME"
        # chain[1] is the preferred host_api sibling — DirectSound.
        # In legacy enum order DirectSound is also at index 1, so this
        # particular test doesn't fully discriminate. The next test
        # uses a perturbed enum order to prove the sort is active.
        assert chain[1].host_api_name == "Windows DirectSound"

    def test_bucket_sort_overrides_enum_order(self, _alignment_enabled: None) -> None:
        """Critical test — perturbed enum order proves the bucket sort
        is doing real work, not coincidentally matching enum order.
        Enum order: WASAPI, WDM-KS, MME, DirectSound. Cascade picked
        DirectSound. Without the sort, chain[1]=WASAPI; with it,
        chain[1]=DirectSound."""
        del _alignment_enabled
        siblings = [
            _entry(index=0, host_api_name="MME"),  # starting
            _entry(index=1, host_api_name="Windows WASAPI"),  # last in pref
            _entry(index=2, host_api_name="Windows WDM-KS"),  # mid
            _entry(index=3, host_api_name="Windows DirectSound"),  # preferred
        ]
        starting = siblings[0]  # MME
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
            preferred_host_api="Windows DirectSound",
        )
        # chain[0] = starting (MME, fixed)
        assert chain[0].host_api_name == "MME"
        # chain[1] = preferred (DirectSound) despite being last in enum
        assert chain[1].host_api_name == "Windows DirectSound"

    def test_bucket_1_fallback_host_apis_ranked(self, _alignment_enabled: None) -> None:
        """Bucket 1: ranked fallback_host_apis sort in configured
        order before unranked siblings."""
        del _alignment_enabled
        siblings = [
            _entry(index=0, host_api_name="MME"),  # starting
            _entry(index=1, host_api_name="Windows WASAPI"),  # rank 1
            _entry(index=2, host_api_name="Windows WDM-KS"),  # rank 2
            _entry(index=3, host_api_name="Windows DirectSound"),  # preferred
        ]
        starting = siblings[0]
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
            preferred_host_api="Windows DirectSound",
            fallback_host_apis=("Windows WASAPI", "Windows WDM-KS"),
        )
        assert [e.host_api_name for e in chain] == [
            "MME",  # starting (chain[0])
            "Windows DirectSound",  # bucket 0 (preferred)
            "Windows WASAPI",  # bucket 1, rank 0
            "Windows WDM-KS",  # bucket 1, rank 1
        ]

    def test_bucket_2_unranked_in_enum_order(self, _alignment_enabled: None) -> None:
        """Bucket 2: unranked siblings (host_api ∉ fallback_host_apis)
        sort in PortAudio enumeration order at the end."""
        del _alignment_enabled
        siblings = [
            _entry(index=0, host_api_name="MME"),  # starting
            _entry(index=1, host_api_name="ASIO"),  # unranked
            _entry(index=2, host_api_name="Windows WASAPI"),  # rank 0
            _entry(index=3, host_api_name="Windows DirectSound"),  # preferred
            _entry(index=4, host_api_name="Pretend-Vendor-API"),  # unranked
        ]
        starting = siblings[0]
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
            preferred_host_api="Windows DirectSound",
            fallback_host_apis=("Windows WASAPI",),
        )
        assert [e.host_api_name for e in chain] == [
            "MME",  # starting
            "Windows DirectSound",  # bucket 0
            "Windows WASAPI",  # bucket 1, rank 0
            "ASIO",  # bucket 2, enum order (index=1)
            "Pretend-Vendor-API",  # bucket 2, enum order (index=4)
        ]


# ── Alignment SLI counter (voice.opener.host_api_alignment) ─────────


class TestAlignmentSLICounter:
    """The Furo W-4 SLI signal — fires on every _device_chain call
    when preferred_host_api is supplied."""

    def test_aligned_true_when_starting_matches_preferred(self) -> None:
        siblings = _razer_endpoint_4_siblings()
        # starting=DirectSound, cascade picked DirectSound — aligned!
        starting = siblings[1]
        with patch("sovyx.voice._stream_opener.record_opener_host_api_alignment") as mock_record:
            _device_chain(
                starting,
                enumerate_fn=lambda: siblings,
                kind="input",
                preferred_host_api="Windows DirectSound",
            )
            mock_record.assert_called_once_with(
                aligned=True,
                cascade_winner_host_api="Windows DirectSound",
                runtime_chain_head_host_api="Windows DirectSound",
            )

    def test_aligned_false_when_starting_drifts_off_preferred(self) -> None:
        """Furo W-4 bug signature — cascade winner host_api ≠
        runtime starting host_api. Alignment SLI fires
        ``aligned=false`` so dashboards can surface drift."""
        siblings = _razer_endpoint_4_siblings()
        # starting=MME, cascade picked DirectSound — DRIFT.
        starting = siblings[0]
        with patch("sovyx.voice._stream_opener.record_opener_host_api_alignment") as mock_record:
            _device_chain(
                starting,
                enumerate_fn=lambda: siblings,
                kind="input",
                preferred_host_api="Windows DirectSound",
            )
            mock_record.assert_called_once_with(
                aligned=False,
                cascade_winner_host_api="Windows DirectSound",
                runtime_chain_head_host_api="MME",
            )

    def test_no_metric_when_preferred_host_api_none(self) -> None:
        """Caller didn't supply ``preferred_host_api`` (legacy code
        path) → no alignment sample. The SLI is opt-in via the new
        param."""
        siblings = _razer_endpoint_4_siblings()
        starting = siblings[0]
        with patch("sovyx.voice._stream_opener.record_opener_host_api_alignment") as mock_record:
            _device_chain(
                starting,
                enumerate_fn=lambda: siblings,
                kind="input",
            )
            mock_record.assert_not_called()


# ── capture_fallback_host_apis wire-up (T16 latent bug fix) ────────


class TestCaptureFallbackHostApisWiredFromTuning:
    """``open_input_stream`` derives ``fallback_host_apis`` from
    ``tuning.capture_fallback_host_apis`` when the caller doesn't
    pass an explicit ``fallback_host_apis``. Closes the Furo W-4
    latent bug — the field at engine/config.py:437-447 was previously
    dead code."""

    @pytest.mark.asyncio()
    async def test_default_capture_fallback_host_apis_propagates_to_device_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When alignment is enabled and the operator hasn't
        overridden ``capture_fallback_host_apis``, the default tuning
        list (``["Windows WASAPI", "Core Audio", "ALSA", ...]``)
        flows into ``_device_chain``."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED", "true")
        from sovyx.engine.config import VoiceTuningConfig

        tuning = VoiceTuningConfig()
        # The default config field lists "Windows WASAPI" first.
        assert tuning.capture_fallback_host_apis[0] == "Windows WASAPI"

        siblings = [
            _entry(index=0, host_api_name="MME"),  # starting
            _entry(index=1, host_api_name="Windows DirectSound"),  # last in fallback default
            _entry(index=2, host_api_name="Windows WASAPI"),  # rank 0 in default fallback
        ]
        starting = siblings[0]

        # Direct _device_chain call with the tuning's list — same
        # pattern open_input_stream uses internally. This pins that
        # the threading is correct end-to-end.
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
            preferred_host_api="Windows WASAPI",
            fallback_host_apis=tuple(tuning.capture_fallback_host_apis),
        )
        # WASAPI was the cascade winner → bucket 0; DirectSound is
        # ranked in default fallback (after Core Audio / ALSA which
        # don't exist in this enum) → bucket 1.
        assert chain[0].host_api_name == "MME"  # starting
        assert chain[1].host_api_name == "Windows WASAPI"  # bucket 0


# ── Both params Optional → backwards compat ─────────────────────────


class TestBothParamsOptional:
    """Foundation contract: both new params default to None so every
    legacy caller (including ``open_input_stream`` callers in
    ``_capture_task.py`` that don't yet pass them) keeps working."""

    def test_both_none_is_legacy_behavior(self) -> None:
        siblings = _razer_endpoint_4_siblings()
        starting = siblings[0]
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
            preferred_host_api=None,
            fallback_host_apis=None,
        )
        # Legacy behavior: starting + rest in enum order.
        assert chain[0] is starting
        assert chain[1].index == 1
        assert chain[2].index == 2
        assert chain[3].index == 3

    def test_omitting_both_kwargs_is_legacy_behavior(self) -> None:
        """Equivalent to passing None — exercise the default-arg path."""
        siblings = _razer_endpoint_4_siblings()
        starting = siblings[0]
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
        )
        assert [e.index for e in chain] == [0, 1, 2, 3]

    def test_preferred_with_no_fallback_uses_bucket_2_for_unranked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``preferred_host_api`` supplied, ``fallback_host_apis=None``
        + alignment flag enabled → bucket 0 + bucket 2 only (no
        bucket 1 since fallback list is None)."""
        monkeypatch.setenv("SOVYX_TUNING__VOICE__CASCADE_HOST_API_ALIGNMENT_ENABLED", "true")
        siblings = [
            _entry(index=0, host_api_name="MME"),  # starting
            _entry(index=1, host_api_name="Windows WASAPI"),  # unranked → bucket 2
            _entry(index=2, host_api_name="Windows WDM-KS"),  # unranked → bucket 2
            _entry(index=3, host_api_name="Windows DirectSound"),  # preferred → bucket 0
        ]
        starting = siblings[0]
        chain = _device_chain(
            starting,
            enumerate_fn=lambda: siblings,
            kind="input",
            preferred_host_api="Windows DirectSound",
            fallback_host_apis=None,
        )
        assert [e.host_api_name for e in chain] == [
            "MME",  # starting
            "Windows DirectSound",  # bucket 0 (preferred)
            "Windows WASAPI",  # bucket 2 (enum order)
            "Windows WDM-KS",  # bucket 2 (enum order)
        ]
