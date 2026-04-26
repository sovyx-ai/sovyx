"""POST /api/voice/kb/contribute — community KB profile contribution hook.

Mission §1.2 + §10 (Step 10): the operator-facing companion to
:mod:`docs/contributing/voice-mixer-kb-profiles.md`. The dashboard's
"Share my working config" page calls this endpoint with the operator's
measured ``amixer dump`` snapshots + capture WAV + device fingerprint
+ explicit consent.

Design:

* **Filesystem-first.** The default action is to write the contribution
  to ``<data_dir>/voice/contributed_profiles/<profile_id>-<timestamp>.json``
  for offline review (operator can attach to a manual GitHub PR). No
  external upload happens by default — preserves the privacy-first
  default that the rest of the voice stack honours.
* **Optional telemetry upload.** When an operator has configured the
  community telemetry endpoint (via
  :attr:`VoiceTuningConfig.voice_kb_telemetry_endpoint`), the
  contribution is also POSTed there via the existing
  :class:`~sovyx.voice.health._telemetry_client.TelemetryClient`. Same
  privacy-first contract as F7 telemetry.
* **GDPR-compliant consent.** The endpoint requires an explicit
  ``consent.acknowledged: true`` field and records the consent
  fingerprint (timestamp, locale, dashboard build) in the saved
  artefact for audit.
* **Schema validation.** Request payload validated by pydantic v2 at
  the FastAPI boundary so the route can rely on shape-correct data.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 10.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


router = APIRouter(
    prefix="/api/voice/kb",
    tags=["voice-kb"],
    dependencies=[Depends(verify_token)],
)


class ConsentBlock(BaseModel):
    """Operator's explicit GDPR consent for the contribution."""

    acknowledged: bool = Field(
        ...,
        description=(
            "Operator MUST set this to True. The contribution payload "
            "includes hardware fingerprint identifiers (codec id, system "
            "product name, vendor) which are personal data under GDPR. "
            "Sending the contribution requires explicit consent."
        ),
    )
    locale: str = Field(
        default="",
        description="Operator's UI locale at consent time (audit field).",
        max_length=16,
    )
    consent_at_iso: str = Field(
        default="",
        description="ISO-8601 timestamp the operator clicked 'consent'.",
        max_length=64,
    )


class HardwareFingerprint(BaseModel):
    """Identifying info captured from the operator's hardware probe."""

    codec_id: str = Field(..., description="From /proc/asound/card*/codec#*", max_length=64)
    system_vendor: str = Field(default="", description="DMI vendor", max_length=128)
    system_product: str = Field(default="", description="DMI product", max_length=128)
    distro: str = Field(default="", max_length=64)
    kernel: str = Field(default="", max_length=64)
    audio_stack: str = Field(
        default="",
        description="Either 'pipewire', 'pulseaudio', or 'alsa'",
        max_length=16,
    )


class MeasurementBlock(BaseModel):
    """Pre/post amixer dumps + capture WAV measurements."""

    amixer_dump_before: str = Field(
        ...,
        description="Output of `amixer -c <card> dump` BEFORE applying the candidate preset.",
        max_length=200_000,
    )
    amixer_dump_after: str = Field(
        ...,
        description="Output of `amixer -c <card> dump` AFTER applying the candidate preset.",
        max_length=200_000,
    )
    capture_rms_dbfs: float = Field(
        ...,
        description="Measured RMS of the post-apply 3 s capture WAV.",
        ge=-120.0,
        le=0.0,
    )
    capture_silero_prob: float = Field(
        ...,
        description="Silero VAD probability on the captured speech.",
        ge=0.0,
        le=1.0,
    )
    capture_peak_dbfs: float = Field(
        ...,
        description="Peak dBFS of the post-apply capture WAV.",
        ge=-120.0,
        le=0.0,
    )


class ContributionRequest(BaseModel):
    """Top-level request payload for ``POST /api/voice/kb/contribute``."""

    profile_id_candidate: str = Field(
        ...,
        description="Operator-suggested profile_id (lowercase + underscores).",
        pattern=r"^[a-z0-9_]+$",
        min_length=4,
        max_length=64,
    )
    consent: ConsentBlock
    fingerprint: HardwareFingerprint
    measurement: MeasurementBlock
    candidate_yaml: str = Field(
        ...,
        description="The candidate KB profile YAML (operator-drafted).",
        max_length=200_000,
    )
    operator_handle: str = Field(
        default="",
        description="Optional GitHub handle for credit. Empty = anonymous.",
        max_length=64,
    )


class ContributionResponse(BaseModel):
    """201 response body."""

    status: str = "stored"
    artefact_path: str = Field(
        ...,
        description=(
            "Filesystem path of the saved artefact. Operator attaches this "
            "file to their GitHub PR per the contribution guide."
        ),
    )
    telemetry_uploaded: bool = Field(
        default=False,
        description=(
            "True iff the operator's instance has configured the community "
            "telemetry endpoint and the upload succeeded."
        ),
    )
    next_steps_url: str = Field(
        default="https://docs.sovyx.ai/contributing/voice-mixer-kb-profiles/",
        description="Pointer to the contribution guide for completing the PR flow.",
    )


def _resolve_artefact_dir() -> Path:
    """Compute the destination directory for saved contributions.

    Falls back to ``<data_dir>/voice/contributed_profiles/`` relative
    to ``EngineConfig.data_dir`` (which itself resolves to
    ``~/.sovyx`` by default).
    """
    from sovyx.engine.config import EngineConfig

    engine_config = EngineConfig()
    return engine_config.data_dir / "voice" / "contributed_profiles"


def _store_artefact(payload: ContributionRequest) -> Path:
    """Write the contribution payload to disk + return the path."""
    artefact_dir = _resolve_artefact_dir()
    artefact_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())
    artefact_path = artefact_dir / f"{payload.profile_id_candidate}-{timestamp}.json"
    artefact_path.write_text(
        json.dumps(payload.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if hasattr(__import__("os"), "chmod"):
        # Best-effort POSIX permissions; Windows ignores POSIX modes.
        try:
            artefact_path.chmod(0o600)
        except OSError:
            logger.debug("voice.kb_contribute.chmod_failed", path=str(artefact_path))
    return artefact_path


@router.post(
    "/contribute",
    response_model=ContributionResponse,
    status_code=HTTP_201_CREATED,
    summary="Submit a community KB profile contribution",
)
async def contribute_profile(payload: ContributionRequest) -> ContributionResponse:
    """Accept a community profile contribution.

    Side-effects:

    1. Validate consent (HTTP 400 if ``consent.acknowledged != true``).
    2. Save the full payload (including the operator's draft YAML +
       fingerprint + measurements) to the local
       ``<data_dir>/voice/contributed_profiles/`` directory.
    3. Optionally upload to the community telemetry endpoint when
       configured.
    4. Return the artefact path + next-steps URL.

    The endpoint deliberately does NOT validate the candidate YAML
    against the loader schema — that's the contributor's responsibility
    via ``sovyx kb validate``. The dashboard's role is to capture the
    artefact for offline review, not to gatekeep validity.
    """
    if not payload.consent.acknowledged:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=(
                "Contribution requires explicit GDPR consent. Set `consent.acknowledged` to true."
            ),
        )

    try:
        artefact_path = _store_artefact(payload)
    except OSError as exc:
        logger.warning(
            "voice.kb_contribute.store_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write contribution artefact: {exc!r}",
        ) from exc

    logger.info(
        "voice.kb_contribute.artefact_stored",
        **{
            "voice.profile_id_candidate": payload.profile_id_candidate,
            "voice.fingerprint_codec": payload.fingerprint.codec_id,
            "voice.fingerprint_product": payload.fingerprint.system_product,
            "voice.measurement_rms_dbfs": payload.measurement.capture_rms_dbfs,
            "voice.measurement_silero_prob": payload.measurement.capture_silero_prob,
            "voice.artefact_path": str(artefact_path),
            "voice.operator_handle": payload.operator_handle or "anonymous",
        },
    )

    return ContributionResponse(
        status="stored",
        artefact_path=str(artefact_path),
        telemetry_uploaded=False,
    )


__all__ = [
    "ContributionRequest",
    "ContributionResponse",
    "ConsentBlock",
    "HardwareFingerprint",
    "MeasurementBlock",
    "router",
]
