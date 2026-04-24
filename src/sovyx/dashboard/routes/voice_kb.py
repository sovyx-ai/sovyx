"""L7 Voice Mixer-KB REST surface — read-only, Bearer-token protected.

Endpoints (all under ``/api/voice/health/kb``):

* ``GET  /profiles``              — list shipped + user-pool profiles.
* ``GET  /profiles/{profile_id}`` — one profile's fields.
* ``POST /validate``              — validate a YAML body against the
  shipping loader. Used by the dashboard contribution-review UI so a
  reviewer can paste a candidate profile and see the same errors the
  contributor would see from ``sovyx kb validate``.

Design notes:

* **Read-only.** Nothing here writes to shipped profiles, the user
  pool, or fixture data. Contribution flows a PR through the repo;
  the dashboard's role is inspection + pre-flight validation only.
* **No PortAudio / ALSA dependency.** Endpoints import the KB loader
  and schema only — a Linux desktop reviewer can hit the API from a
  macOS client without the capture stack installed.
* **Session-wide auth.** All routes share the ``verify_token``
  dependency from :mod:`sovyx.dashboard.routes._deps`; behaviour
  mirrors every other voice-health endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from starlette.status import (
    HTTP_200_OK,
    HTTP_404_NOT_FOUND,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger
from sovyx.voice.health._mixer_kb import _SHIPPED_PROFILES_DIR
from sovyx.voice.health._mixer_kb.loader import (
    load_profiles_from_directory,
)
from sovyx.voice.health._mixer_kb.schema import KBProfileModel

if TYPE_CHECKING:
    from sovyx.voice.health.contract import MixerKBProfile

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/voice/health/kb",
    tags=["voice-health-kb"],
    dependencies=[Depends(verify_token)],
)


class ProfileSummary(BaseModel):
    """Compact profile identity shown in list responses."""

    pool: str = Field(..., description="'shipped' or 'user'.")
    profile_id: str
    profile_version: int
    schema_version: int
    driver_family: str
    codec_id_glob: str
    match_threshold: float
    factory_regime: str
    contributed_by: str


class ProfileDetail(ProfileSummary):
    """Full profile surface — superset of :class:`ProfileSummary`."""

    system_vendor_glob: str | None = None
    system_product_glob: str | None = None
    distro_family: str | None = None
    audio_stack: str | None = None
    kernel_major_minor_glob: str | None = None
    factory_signature_roles: list[str] = Field(default_factory=list)
    verified_on_count: int = 0


class ListResponse(BaseModel):
    profiles: list[ProfileSummary]
    shipped_count: int
    user_count: int


class ValidateRequest(BaseModel):
    """Validation request.

    Attributes:
        yaml_body: Raw YAML text. The server runs :meth:`KBProfileModel.model_validate`
            — the same path the daemon uses at boot.
        filename_stem: Optional filename stem to cross-check against
            ``profile_id`` (the loader's filename-is-authoritative
            invariant). When omitted, the filename check is skipped;
            the schema still runs.
    """

    yaml_body: str = Field(..., min_length=1, max_length=1_000_000)
    filename_stem: str | None = Field(default=None, max_length=512)


class ValidationIssue(BaseModel):
    loc: str
    msg: str


class ValidateResponse(BaseModel):
    ok: bool
    profile_id: str | None = None
    profile_version: int | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)


def _to_summary(pool: str, profile: MixerKBProfile) -> ProfileSummary:
    return ProfileSummary(
        pool=pool,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        schema_version=profile.schema_version,
        driver_family=profile.driver_family,
        codec_id_glob=profile.codec_id_glob,
        match_threshold=profile.match_threshold,
        factory_regime=profile.factory_regime,
        contributed_by=profile.contributed_by,
    )


def _to_detail(pool: str, profile: MixerKBProfile) -> ProfileDetail:
    return ProfileDetail(
        pool=pool,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        schema_version=profile.schema_version,
        driver_family=profile.driver_family,
        codec_id_glob=profile.codec_id_glob,
        match_threshold=profile.match_threshold,
        factory_regime=profile.factory_regime,
        contributed_by=profile.contributed_by,
        system_vendor_glob=profile.system_vendor_glob,
        system_product_glob=profile.system_product_glob,
        distro_family=profile.distro_family,
        audio_stack=profile.audio_stack,
        kernel_major_minor_glob=profile.kernel_major_minor_glob,
        factory_signature_roles=sorted(role.name for role in profile.factory_signature),
        verified_on_count=len(profile.verified_on),
    )


def _load_all_profiles() -> tuple[list[MixerKBProfile], list[MixerKBProfile]]:
    """Return ``(shipped, user)`` profile lists.

    User pool is derived from the daemon's ``data_dir`` when
    available (app-state wired by ``create_app``); falls back to the
    home-directory default so the endpoint behaves sanely when the
    daemon hasn't populated ``data_dir``.
    """
    shipped = load_profiles_from_directory(_SHIPPED_PROFILES_DIR)
    # Note: the user-pool resolver is deliberately home-dir-only for
    # the dashboard. An operator reviewing a contribution should
    # review the repo PR, not the running daemon's cache — so mixing
    # daemon-state paths here would hide which copy the dashboard
    # reflects.
    from pathlib import Path

    user_dir = Path.home() / ".sovyx" / "mixer_kb" / "user"
    user = load_profiles_from_directory(user_dir) if user_dir.exists() else []
    return shipped, user


# ── GET /profiles ───────────────────────────────────────────────────


@router.get("/profiles", response_model=ListResponse, status_code=HTTP_200_OK)
async def list_profiles() -> ListResponse:
    """List every shipped + user-pool profile."""
    shipped, user = _load_all_profiles()
    return ListResponse(
        profiles=[_to_summary("shipped", p) for p in shipped]
        + [_to_summary("user", p) for p in user],
        shipped_count=len(shipped),
        user_count=len(user),
    )


# ── GET /profiles/{profile_id} ──────────────────────────────────────


@router.get(
    "/profiles/{profile_id}",
    response_model=ProfileDetail,
    status_code=HTTP_200_OK,
)
async def get_profile(profile_id: str) -> ProfileDetail:
    """Return one profile's full fields.

    404 when no matching profile exists in either pool.
    """
    shipped, user = _load_all_profiles()
    for pool, pool_profiles in (("shipped", shipped), ("user", user)):
        for profile in pool_profiles:
            if profile.profile_id == profile_id:
                return _to_detail(pool, profile)
    raise HTTPException(
        status_code=HTTP_404_NOT_FOUND,
        detail=f"profile_id={profile_id!r} not found",
    )


# ── POST /validate ──────────────────────────────────────────────────


@router.post(
    "/validate",
    response_model=ValidateResponse,
    status_code=HTTP_200_OK,
)
async def validate_profile(payload: ValidateRequest) -> ValidateResponse:
    """Validate a candidate YAML against the shipping schema.

    Returns ``ok=True`` when the YAML parses, matches the schema, and
    (if ``filename_stem`` is provided) the stem lines up with
    ``profile_id``. Otherwise returns ``ok=False`` with a flat
    ``issues`` list — the same shape pydantic uses so the frontend can
    render either shape with a single component.
    """
    try:
        parsed = yaml.safe_load(payload.yaml_body)
    except yaml.YAMLError as exc:
        return ValidateResponse(
            ok=False,
            issues=[ValidationIssue(loc="<yaml>", msg=str(exc)[:200])],
        )

    if not isinstance(parsed, dict):
        return ValidateResponse(
            ok=False,
            issues=[
                ValidationIssue(
                    loc="<yaml>",
                    msg=(
                        "profile YAML must contain a mapping at the top "
                        f"level (got {type(parsed).__name__})"
                    ),
                ),
            ],
        )

    try:
        model = KBProfileModel.model_validate(parsed)
    except ValidationError as exc:
        return ValidateResponse(
            ok=False,
            issues=[
                ValidationIssue(
                    loc=".".join(str(p) for p in err.get("loc", ())),
                    msg=str(err.get("msg", "")),
                )
                for err in exc.errors()
            ],
        )

    # Post-schema: filename-stem invariant. Loader enforces this at
    # boot; the endpoint reports it so contributors catch the drift
    # before PR.
    if payload.filename_stem and model.profile_id != payload.filename_stem:
        return ValidateResponse(
            ok=False,
            profile_id=model.profile_id,
            profile_version=model.profile_version,
            issues=[
                ValidationIssue(
                    loc="profile_id",
                    msg=(
                        f"profile_id={model.profile_id!r} disagrees with "
                        f"filename stem {payload.filename_stem!r}"
                    ),
                ),
            ],
        )

    # Pydantic was happy; re-run the runtime-dataclass validation path
    # so __post_init__ rules (non-empty verified_on, role whitelist,
    # threshold range) are enforced the same way as the CLI/boot path.
    try:
        model.to_profile()
    except ValueError as exc:
        return ValidateResponse(
            ok=False,
            profile_id=model.profile_id,
            profile_version=model.profile_version,
            issues=[ValidationIssue(loc="<semantic>", msg=str(exc))],
        )
    return ValidateResponse(
        ok=True,
        profile_id=model.profile_id,
        profile_version=model.profile_version,
    )


# Sentinel re-export used by tests that want to stub the validator
# error class without reaching into the third-party namespace.
_ValidationError = ValidationError


__all__ = ["router"]
