"""F7 — Layer 4 community telemetry client (opt-in default-off).

Privacy-first HTTP client that POSTs anonymised capture-diagnostics
payloads to a configurable community-knowledge endpoint. The
purpose: aggregate hardware combinations + their working capture
configurations so the F10 KB-profile catalogue can grow organically
beyond the first 5 hand-curated profiles — without any user being
able to identify which payload came from which deployment.

Privacy contract (load-bearing):

* **Default OFF**. No telemetry leaves the user's machine unless they
  explicitly opt in via
  ``VoiceTuningConfig.voice_community_telemetry_enabled = True`` AND
  set ``voice_community_telemetry_endpoint`` to a non-empty URL.
* **PII redacted**. Every string field that COULD contain user data
  (endpoint GUID, device friendly name, host name) is hashed via
  M1's :func:`~sovyx.voice._observability_pii.hash_pii` with a
  per-namespace salt. The raw values NEVER leave the process.
* **Bounded payload**. Per-hardware-fingerprint dedupe so a single
  deployment doesn't dominate the corpus, and bounded retry / timeout
  so a hung endpoint doesn't degrade Sovyx's startup latency.
* **Append-only intent**. The client only POSTs; it never reads back
  community data. The KB catalogue ingests separately via the
  signed-profile flow (F2).

This module is the FOUNDATION. The actual community endpoint is
deliberately empty by default — operators (or Sovyx maintainers)
populate it via env var when the community-knowledge service exists
and the privacy review has cleared per-deployment opt-in.

Reference: F1 inventory mission task F7; ADR-combo-store-schema §6
(KB-profile catalogue); M1 PII hashing primitive.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import httpx

from sovyx.observability.logging import get_logger
from sovyx.voice._observability_pii import hash_pii

if TYPE_CHECKING:
    from collections.abc import Mapping


logger = get_logger(__name__)


# ── Bounds ─────────────────────────────────────────────────────────


_DEFAULT_TIMEOUT_S = 5.0
"""Per-call HTTP timeout. Community endpoint may be on a CDN with
variable latency; 5 s is generous enough to absorb transient
slowness while short enough that a hung endpoint doesn't degrade
Sovyx startup. Bounded ``[1, 60]`` at the config layer."""


_DEFAULT_MAX_RETRIES = 2
"""Max retries on transient HTTP failures (timeout / 5xx). Total
attempts = 1 + max_retries. 2 retries = 3 total attempts; matches
the typical CDN warm-up window without becoming a retry storm."""


_TELEMETRY_NAMESPACE_SALT = "voice.community_telemetry"
"""Stable salt for hash_pii calls inside this module. Different from
every other namespace so the same raw GUID hashes differently in
the community telemetry payload vs. the local dashboard event."""


# ── Public types ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CommunityTelemetryPayload:
    """Anonymised capture-diagnostics payload sent to the community
    endpoint.

    EVERY string field that COULD identify the user or their
    hardware in detail is hashed via M1 ``hash_pii``. The retained
    fields are:

    * ``platform`` — ``"linux" / "win32" / "darwin"`` (not user-
      identifying).
    * ``os_release`` — kernel / build version (e.g. ``"6.5.0"``,
      ``"10.0.26100"``). Not user-identifying.
    * ``audio_subsystem_name`` — ``"PipeWire" / "PulseAudio" /
      "ALSA-only" / "WASAPI" / "CoreAudio"``. Not user-identifying.
    * ``host_api_used`` — the PortAudio host API the cascade landed
      on. Not user-identifying.
    * ``sample_rate``, ``channels``, ``sample_format`` — the
      negotiated capture format. Aggregate signal across users.
    * ``device_class`` — categorical (``"internal_mic" /
      "external_usb" / "bluetooth" / "other"``). Not user-identifying.
    * ``capture_healthy`` — boolean: did the capture cascade succeed.
    * ``endpoint_fingerprint`` — HASHED endpoint GUID
      (NOT the raw GUID).
    * ``device_name_fingerprint`` — HASHED device name (NOT raw).
    * ``known_apos`` — friendly names of detected APOs (these ARE
      already non-PII, just product names like "Windows Voice
      Clarity"; useful for the KB to know which APO chains are in
      the wild).
    """

    platform: str
    os_release: str
    audio_subsystem_name: str
    host_api_used: str
    sample_rate: int
    channels: int
    sample_format: str
    device_class: str
    capture_healthy: bool
    endpoint_fingerprint: str = ""
    device_name_fingerprint: str = ""
    known_apos: tuple[str, ...] = field(default_factory=tuple)


# ── Construction helpers ──────────────────────────────────────────


def build_anonymised_payload(
    *,
    platform: str,
    os_release: str,
    audio_subsystem_name: str,
    host_api_used: str,
    sample_rate: int,
    channels: int,
    sample_format: str,
    device_class: str,
    capture_healthy: bool,
    raw_endpoint_id: str = "",
    raw_device_name: str = "",
    known_apos: tuple[str, ...] = (),
) -> CommunityTelemetryPayload:
    """Build a payload with PII-bearing inputs hashed via M1.

    Callers pass RAW values (endpoint GUID, device name); this
    function hashes them with the per-module salt before they enter
    the payload. Centralising the hashing here means there's exactly
    one place to audit for "did we forget to hash a field" — the
    ``CommunityTelemetryPayload`` dataclass itself only carries
    pre-hashed values."""
    return CommunityTelemetryPayload(
        platform=platform,
        os_release=os_release,
        audio_subsystem_name=audio_subsystem_name,
        host_api_used=host_api_used,
        sample_rate=sample_rate,
        channels=channels,
        sample_format=sample_format,
        device_class=device_class,
        capture_healthy=capture_healthy,
        endpoint_fingerprint=hash_pii(
            raw_endpoint_id,
            salt=_TELEMETRY_NAMESPACE_SALT + ":endpoint",
        ),
        device_name_fingerprint=hash_pii(
            raw_device_name,
            salt=_TELEMETRY_NAMESPACE_SALT + ":device_name",
        ),
        known_apos=known_apos,
    )


# ── HTTP client ───────────────────────────────────────────────────


class CommunityTelemetryClient:
    """Async HTTP client that POSTs payloads to the community
    endpoint.

    Lifecycle: construct → ``await submit(payload)`` per
    capture-enable success. The client owns no global state; every
    call is independent.

    Failure semantics: NEVER raises. HTTP / serialisation / timeout
    failures log structured WARN and return False so callers don't
    have to wrap submit() in try/except — the contract is "best
    effort, no observable side effect on Sovyx itself"."""

    def __init__(
        self,
        endpoint_url: str,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        http_client_factory: object | None = None,
    ) -> None:
        if not endpoint_url:
            msg = "endpoint_url must be non-empty"
            raise ValueError(msg)
        if timeout_s <= 0:
            msg = f"timeout_s must be > 0, got {timeout_s}"
            raise ValueError(msg)
        if max_retries < 0:
            msg = f"max_retries must be >= 0, got {max_retries}"
            raise ValueError(msg)
        self._endpoint_url = endpoint_url
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        # Factory for tests to inject a fake client. Default uses
        # a real httpx.AsyncClient instantiated per submit() call so
        # there's no shared state across calls.
        self._http_client_factory = http_client_factory

    @property
    def endpoint_url(self) -> str:
        return self._endpoint_url

    async def submit(self, payload: CommunityTelemetryPayload) -> bool:
        """POST the payload to the configured endpoint.

        Returns True on 2xx response, False on any failure (timeout,
        non-2xx, network error, JSON serialisation issue). The False
        return path is paired with a structured WARN log so operators
        can see attempted-but-failed submissions in the dashboard."""
        try:
            body_json = json.dumps(asdict(payload), sort_keys=True)
        except (TypeError, ValueError) as exc:  # pragma: no cover — dataclass values are JSON-safe
            logger.warning(
                "voice.community_telemetry.serialise_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

        attempts_made = 0
        last_exc: BaseException | None = None
        while attempts_made <= self._max_retries:
            attempts_made += 1
            try:
                ok, status = await self._post_once(body_json)
            except (TimeoutError, httpx.HTTPError) as exc:
                last_exc = exc
                # Transient — fall through to retry (or exhaustion).
                continue
            except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
                # Unexpected — log + bail (don't retry on programming bugs).
                logger.warning(
                    "voice.community_telemetry.unexpected_failure",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    attempts=attempts_made,
                )
                return False
            if ok:
                logger.info(
                    "voice.community_telemetry.submitted",
                    status=status,
                    attempts=attempts_made,
                    endpoint=self._endpoint_url,
                )
                return True
            # Non-2xx — retry only on 5xx (transient server). 4xx
            # means our payload is wrong; retrying doesn't help.
            if 400 <= status < 500:  # noqa: PLR2004
                logger.warning(
                    "voice.community_telemetry.client_error",
                    status=status,
                    attempts=attempts_made,
                )
                return False
            # 5xx — fall through to retry.

        logger.warning(
            "voice.community_telemetry.exhausted_retries",
            max_retries=self._max_retries,
            last_error=str(last_exc) if last_exc else "non-2xx",
            last_error_type=type(last_exc).__name__ if last_exc else "HttpError",
        )
        return False

    async def _post_once(self, body_json: str) -> tuple[bool, int]:
        """One HTTP POST attempt. Returns ``(ok, status)``."""
        if self._http_client_factory is not None:
            client = self._http_client_factory()  # type: ignore[operator]
        else:
            client = httpx.AsyncClient(timeout=self._timeout_s)
        try:
            response = await client.post(
                self._endpoint_url,
                content=body_json,
                headers={"Content-Type": "application/json"},
            )
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                close_result = close()
                if asyncio.iscoroutine(close_result):
                    await close_result
        ok = 200 <= response.status_code < 300  # noqa: PLR2004
        return ok, response.status_code


# ── High-level entrypoint ─────────────────────────────────────────


async def maybe_submit_community_telemetry(
    payload_factory: object,
    *,
    config_overrides: Mapping[str, object] | None = None,
) -> bool:
    """Top-level guard: read tuning config, opt-in check, build
    payload, submit. Returns True iff payload was actually submitted.

    Args:
        payload_factory: Zero-arg callable returning a
            :class:`CommunityTelemetryPayload`. Lazy so the caller
            doesn't pay the construction cost when the gate is off.
        config_overrides: Test-only injection of tuning fields
            (avoids reading VoiceTuningConfig in unit tests).
    """
    if config_overrides is not None:
        enabled = bool(config_overrides.get("enabled", False))
        endpoint = str(config_overrides.get("endpoint", ""))
    else:
        try:
            from sovyx.engine.config import VoiceTuningConfig

            tuning = VoiceTuningConfig()
            enabled = tuning.voice_community_telemetry_enabled
            endpoint = tuning.voice_community_telemetry_endpoint
        except Exception as exc:  # noqa: BLE001 — gate isolation
            logger.warning(
                "voice.community_telemetry.config_read_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False
    if not enabled or not endpoint:
        return False
    try:
        payload = payload_factory()  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 — payload build isolation
        logger.warning(
            "voice.community_telemetry.payload_build_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False
    if not isinstance(payload, CommunityTelemetryPayload):
        logger.warning(
            "voice.community_telemetry.invalid_payload_type",
            type=type(payload).__name__,
        )
        return False
    client = CommunityTelemetryClient(endpoint_url=endpoint)
    return await client.submit(payload)


__all__ = [
    "CommunityTelemetryClient",
    "CommunityTelemetryPayload",
    "build_anonymised_payload",
    "maybe_submit_community_telemetry",
]
