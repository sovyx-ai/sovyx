"""Tests for :mod:`sovyx.voice.health._telemetry_client` (F7)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.health._telemetry_client import (
    CommunityTelemetryClient,
    CommunityTelemetryPayload,
    build_anonymised_payload,
    maybe_submit_community_telemetry,
)

# ── Defaults pin (privacy-first regression guard) ─────────────────


class TestPrivacyDefaults:
    """The privacy-first contract: NO TELEMETRY leaves the machine
    by default. Lock these defaults so a future config refactor
    can't silently start exfiltrating data."""

    def test_telemetry_default_disabled(self) -> None:
        assert VoiceTuningConfig().voice_community_telemetry_enabled is False

    def test_telemetry_endpoint_default_empty(self) -> None:
        assert VoiceTuningConfig().voice_community_telemetry_endpoint == ""


# ── PII anonymisation ─────────────────────────────────────────────


class TestPayloadAnonymisation:
    def test_raw_endpoint_never_appears_in_payload(self) -> None:
        raw_guid = "{ENDPOINT-AAA-VERY-IDENTIFYING}"
        payload = build_anonymised_payload(
            platform="linux",
            os_release="6.5.0",
            audio_subsystem_name="PipeWire",
            host_api_used="ALSA",
            sample_rate=48000,
            channels=2,
            sample_format="int16",
            device_class="external_usb",
            capture_healthy=True,
            raw_endpoint_id=raw_guid,
        )
        # The fingerprint must NOT contain the raw GUID.
        assert raw_guid not in payload.endpoint_fingerprint
        # And it must be a hex string of expected length (12 default).
        assert len(payload.endpoint_fingerprint) == 12  # noqa: PLR2004
        all(c in "0123456789abcdef" for c in payload.endpoint_fingerprint)

    def test_raw_device_name_never_appears(self) -> None:
        raw_name = "Microfone (Razer BlackShark V2 Pro)"
        payload = build_anonymised_payload(
            platform="win32",
            os_release="10.0.26100",
            audio_subsystem_name="WASAPI",
            host_api_used="Windows WASAPI",
            sample_rate=48000,
            channels=1,
            sample_format="int16",
            device_class="external_usb",
            capture_healthy=True,
            raw_device_name=raw_name,
        )
        assert raw_name not in payload.device_name_fingerprint
        assert "BlackShark" not in payload.device_name_fingerprint
        assert "Razer" not in payload.device_name_fingerprint

    def test_empty_raw_inputs_produce_empty_fingerprints(self) -> None:
        # Empty inputs collapse to empty fingerprints — the contract
        # of M1's hash_pii. Lets the receiver distinguish "no data"
        # from "data was hashed".
        payload = build_anonymised_payload(
            platform="linux",
            os_release="",
            audio_subsystem_name="ALSA",
            host_api_used="ALSA",
            sample_rate=44100,
            channels=2,
            sample_format="int16",
            device_class="other",
            capture_healthy=False,
        )
        assert payload.endpoint_fingerprint == ""
        assert payload.device_name_fingerprint == ""

    def test_different_endpoints_produce_different_fingerprints(self) -> None:
        a = build_anonymised_payload(
            platform="linux",
            os_release="",
            audio_subsystem_name="A",
            host_api_used="A",
            sample_rate=48000,
            channels=2,
            sample_format="int16",
            device_class="o",
            capture_healthy=True,
            raw_endpoint_id="{AAA}",
        )
        b = build_anonymised_payload(
            platform="linux",
            os_release="",
            audio_subsystem_name="A",
            host_api_used="A",
            sample_rate=48000,
            channels=2,
            sample_format="int16",
            device_class="o",
            capture_healthy=True,
            raw_endpoint_id="{BBB}",
        )
        assert a.endpoint_fingerprint != b.endpoint_fingerprint

    def test_endpoint_and_device_name_use_different_salts(self) -> None:
        # Same raw value used for endpoint AND device_name produces
        # DIFFERENT fingerprints — proves cross-field correlation
        # is impossible.
        same_raw = "{SAME-VALUE-IN-BOTH-FIELDS}"
        payload = build_anonymised_payload(
            platform="linux",
            os_release="",
            audio_subsystem_name="A",
            host_api_used="A",
            sample_rate=48000,
            channels=2,
            sample_format="int16",
            device_class="o",
            capture_healthy=True,
            raw_endpoint_id=same_raw,
            raw_device_name=same_raw,
        )
        assert payload.endpoint_fingerprint != payload.device_name_fingerprint


# ── Client construction ───────────────────────────────────────────


class TestClientConstructionBounds:
    def test_empty_endpoint_rejected(self) -> None:
        with pytest.raises(ValueError, match="endpoint_url must be non-empty"):
            CommunityTelemetryClient(endpoint_url="")

    def test_zero_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="timeout_s must be > 0"):
            CommunityTelemetryClient(endpoint_url="http://x", timeout_s=0)

    def test_negative_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="timeout_s must be > 0"):
            CommunityTelemetryClient(endpoint_url="http://x", timeout_s=-1.0)

    def test_negative_max_retries_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_retries must be >= 0"):
            CommunityTelemetryClient(endpoint_url="http://x", max_retries=-1)


# ── Client submit() behaviour ─────────────────────────────────────


def _make_payload() -> CommunityTelemetryPayload:
    return build_anonymised_payload(
        platform="linux",
        os_release="6.5.0",
        audio_subsystem_name="PipeWire",
        host_api_used="ALSA",
        sample_rate=48000,
        channels=2,
        sample_format="int16",
        device_class="external_usb",
        capture_healthy=True,
        raw_endpoint_id="{TEST}",
        raw_device_name="Test Mic",
    )


def _build_fake_http_factory(
    *,
    response_status: int = 200,
    raise_on_post: type[BaseException] | None = None,
) -> Any:
    """Build a factory that returns a fake httpx-like async client."""

    def _factory() -> Any:
        client = MagicMock()

        async def _post(*_args: Any, **_kwargs: Any) -> Any:
            if raise_on_post is not None:
                raise raise_on_post("simulated http failure")
            response = MagicMock()
            response.status_code = response_status
            return response

        client.post = _post
        client.aclose = AsyncMock(return_value=None)
        return client

    return _factory


class TestClientSubmit:
    @pytest.mark.asyncio
    async def test_2xx_returns_true(self) -> None:
        client = CommunityTelemetryClient(
            endpoint_url="http://example.test/telemetry",
            http_client_factory=_build_fake_http_factory(response_status=204),
        )
        ok = await client.submit(_make_payload())
        assert ok is True

    @pytest.mark.asyncio
    async def test_4xx_returns_false_no_retry(self) -> None:
        # 400/401/422 etc. — payload is wrong; retrying won't help.
        attempts: list[int] = []

        def _factory() -> Any:
            attempts.append(1)
            client = MagicMock()

            async def _post(*_args: Any, **_kwargs: Any) -> Any:
                response = MagicMock()
                response.status_code = 422
                return response

            client.post = _post
            client.aclose = AsyncMock(return_value=None)
            return client

        client = CommunityTelemetryClient(
            endpoint_url="http://example.test/telemetry",
            max_retries=3,  # Plenty of retries available — but 4xx skips them.
            http_client_factory=_factory,
        )
        ok = await client.submit(_make_payload())
        assert ok is False
        assert len(attempts) == 1  # No retries on 4xx.

    @pytest.mark.asyncio
    async def test_5xx_retries_then_returns_false(self) -> None:
        attempts: list[int] = []

        def _factory() -> Any:
            attempts.append(1)
            client = MagicMock()

            async def _post(*_args: Any, **_kwargs: Any) -> Any:
                response = MagicMock()
                response.status_code = 503
                return response

            client.post = _post
            client.aclose = AsyncMock(return_value=None)
            return client

        client = CommunityTelemetryClient(
            endpoint_url="http://example.test/telemetry",
            max_retries=2,
            http_client_factory=_factory,
        )
        ok = await client.submit(_make_payload())
        assert ok is False
        # 1 + max_retries(2) = 3 total attempts.
        assert len(attempts) == 3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_http_error_retried(self) -> None:
        attempts: list[int] = []

        def _factory() -> Any:
            attempts.append(1)
            client = MagicMock()

            async def _post(*_args: Any, **_kwargs: Any) -> Any:
                raise httpx.ConnectError("simulated connect failure")

            client.post = _post
            client.aclose = AsyncMock(return_value=None)
            return client

        client = CommunityTelemetryClient(
            endpoint_url="http://example.test/telemetry",
            max_retries=2,
            http_client_factory=_factory,
        )
        ok = await client.submit(_make_payload())
        assert ok is False
        assert len(attempts) == 3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_false_no_retry(self) -> None:
        # A non-HTTP exception (programming bug) should NOT trigger
        # the retry loop — that would just amplify the bug.
        attempts: list[int] = []

        def _factory() -> Any:
            attempts.append(1)
            client = MagicMock()

            async def _post(*_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("programming bug")

            client.post = _post
            client.aclose = AsyncMock(return_value=None)
            return client

        client = CommunityTelemetryClient(
            endpoint_url="http://example.test/telemetry",
            max_retries=5,
            http_client_factory=_factory,
        )
        ok = await client.submit(_make_payload())
        assert ok is False
        assert len(attempts) == 1  # Bailed without retry.

    @pytest.mark.asyncio
    async def test_asyncio_timeout_retried(self) -> None:
        attempts: list[int] = []

        def _factory() -> Any:
            attempts.append(1)
            client = MagicMock()

            async def _post(*_args: Any, **_kwargs: Any) -> Any:
                raise TimeoutError

            client.post = _post
            client.aclose = AsyncMock(return_value=None)
            return client

        client = CommunityTelemetryClient(
            endpoint_url="http://example.test/telemetry",
            max_retries=1,
            http_client_factory=_factory,
        )
        ok = await client.submit(_make_payload())
        assert ok is False
        assert len(attempts) == 2  # noqa: PLR2004 — 1 + max_retries(1)


# ── Top-level guard (maybe_submit_community_telemetry) ────────────


class TestTopLevelGuard:
    @pytest.mark.asyncio
    async def test_default_disabled_does_not_submit(self) -> None:
        # Using real config — defaults are off.
        called: list[int] = []

        def _factory() -> CommunityTelemetryPayload:
            called.append(1)
            return _make_payload()

        ok = await maybe_submit_community_telemetry(_factory)
        assert ok is False
        # Payload factory NOT called — short-circuited before any work.
        assert called == []

    @pytest.mark.asyncio
    async def test_enabled_but_empty_endpoint_does_not_submit(self) -> None:
        called: list[int] = []

        def _factory() -> CommunityTelemetryPayload:
            called.append(1)
            return _make_payload()

        ok = await maybe_submit_community_telemetry(
            _factory,
            config_overrides={"enabled": True, "endpoint": ""},
        )
        assert ok is False
        assert called == []

    @pytest.mark.asyncio
    async def test_payload_factory_crash_returns_false(self) -> None:
        def _factory() -> CommunityTelemetryPayload:
            msg = "intentional"
            raise RuntimeError(msg)

        ok = await maybe_submit_community_telemetry(
            _factory,
            config_overrides={"enabled": True, "endpoint": "http://x"},
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_invalid_payload_type_returns_false(self) -> None:
        # Factory returned the wrong type — guard rejects.
        def _factory() -> object:
            return {"not": "a payload"}

        ok = await maybe_submit_community_telemetry(
            _factory,  # type: ignore[arg-type]
            config_overrides={"enabled": True, "endpoint": "http://x"},
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_enabled_with_valid_endpoint_calls_submit(self) -> None:
        # Patch the client itself to verify the guard reaches it.
        with patch(
            "sovyx.voice.health._telemetry_client.CommunityTelemetryClient",
        ) as mock_cls:
            instance = mock_cls.return_value
            instance.submit = AsyncMock(return_value=True)
            ok = await maybe_submit_community_telemetry(
                _make_payload,
                config_overrides={
                    "enabled": True,
                    "endpoint": "http://community.example.test/telemetry",
                },
            )
            assert ok is True
            mock_cls.assert_called_once_with(
                endpoint_url="http://community.example.test/telemetry"
            )
            instance.submit.assert_awaited_once()


pytestmark = pytest.mark.timeout(15)
