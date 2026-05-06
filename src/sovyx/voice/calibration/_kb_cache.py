"""Local KB cache: per-fingerprint cached CalibrationProfiles.

When the wizard orchestrator successfully completes a slow-path
calibration (terminal DONE), the resulting CalibrationProfile is
also written to a cache keyed by ``fingerprint_hash`` at
``<data_dir>/voice_calibration/_kb/<hash>.json``. On subsequent runs
with the same hardware, the wizard takes the FAST_PATH:

  PROBING -> FAST_PATH_LOOKUP (cache hit) -> FAST_PATH_APPLY ->
  FAST_PATH_VALIDATE -> DONE   (~5s instead of ~10 min)

Without a cache hit, the wizard falls through to SLOW_PATH as before.

The cache is intentionally local-only (single host) for v0.30.18.
The L4 community KB (mission §7) uploads aggregated profiles to a
shared service; the local cache feeds it but does NOT yet pull from
it. v0.32+ wires the community KB pull side.

Cache invalidation:
* The cache key (fingerprint_hash) bakes in distro + kernel +
  audio-stack version + codec_id + driver_family etc. Any change to
  hardware identity invalidates the lookup automatically (cache
  miss + slow_path).
* Cache entries do NOT expire on time. They expire when the
  fingerprint changes (i.e. when the operator updates kernel /
  switches audio stack / swaps hardware), which produces a new
  fingerprint_hash and an automatic miss.
* Operator can force re-calibration by deleting the cache file or
  the entire `<data_dir>/voice_calibration/_kb/` directory.

History: introduced in v0.30.18 as C3 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 3.
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.privacy import short_hash
from sovyx.voice.calibration._persistence import (
    _profile_from_dict,
    _profile_to_dict,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.voice.calibration.schema import CalibrationProfile

logger = get_logger(__name__)

_KB_DIR_NAME = "_kb"
_KB_FILE_SUFFIX = ".json"
_KB_FILE_TMP_SUFFIX = ".tmp"


def kb_dir(data_dir: Path) -> Path:
    """Return ``<data_dir>/voice_calibration/_kb/``."""
    return data_dir / "voice_calibration" / _KB_DIR_NAME


def cache_path(data_dir: Path, fingerprint_hash: str) -> Path:
    """Return ``<data_dir>/voice_calibration/_kb/<hash>.json``.

    Args:
        data_dir: The Sovyx data directory.
        fingerprint_hash: The 64-char hex SHA256 from
            :attr:`HardwareFingerprint.fingerprint_hash`.
    """
    return kb_dir(data_dir) / f"{fingerprint_hash}{_KB_FILE_SUFFIX}"


def store_profile(profile: CalibrationProfile, *, data_dir: Path) -> Path:
    """Persist a profile in the KB cache for fast-path replay.

    Atomic write (.tmp + os.replace) so partial writes never corrupt
    the cache. The cache key is derived from the profile's
    fingerprint, so callers don't need to compute it.

    Args:
        profile: The CalibrationProfile to cache. Its fingerprint's
            ``fingerprint_hash`` is the lookup key.
        data_dir: Sovyx data directory; the ``_kb/`` subdirectory is
            created on demand.

    Returns:
        The absolute path the cache entry was written to.
    """
    fingerprint_hash = profile.fingerprint.fingerprint_hash
    target = cache_path(data_dir, fingerprint_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + _KB_FILE_TMP_SUFFIX)

    payload = json.dumps(
        _profile_to_dict(profile),
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, target)

    logger.info(
        "voice.calibration.kb.stored",
        fingerprint_hash=fingerprint_hash,
        mind_id_hash=short_hash(profile.mind_id),
        # Deprecated raw fields (removal in v0.30.29 per
        # MISSION-voice-calibration-extreme-audit-2026-05-06 §4.2):
        mind_id=profile.mind_id,
        path=str(target),
    )
    return target


def lookup_profile(*, data_dir: Path, fingerprint_hash: str) -> CalibrationProfile | None:
    """Return the cached profile for ``fingerprint_hash``, or None on miss.

    Returns None on:
    * file doesn't exist (cache miss);
    * file exists but is malformed (best-effort: log a warning + miss);
    * schema_version mismatch (best-effort: log a warning + miss).

    The "log + miss" semantics are chosen so a corrupt cache file
    NEVER blocks a calibration run; the worst case is the operator
    falls through to SLOW_PATH and re-populates the cache on success.
    """
    path = cache_path(data_dir, fingerprint_hash)
    if not path.is_file():
        return None
    raw_text = ""
    with contextlib.suppress(OSError):
        raw_text = path.read_text(encoding="utf-8")
    if not raw_text:
        return None
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "voice.calibration.kb.malformed_json",
            fingerprint_hash=fingerprint_hash,
            reason=str(exc),
            # Deprecated raw filesystem path (removal in v0.30.29):
            path=str(path),
        )
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "voice.calibration.kb.not_an_object",
            fingerprint_hash=fingerprint_hash,
            # Deprecated raw filesystem path (removal in v0.30.29):
            path=str(path),
        )
        return None
    try:
        profile = _profile_from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "voice.calibration.kb.schema_mismatch",
            fingerprint_hash=fingerprint_hash,
            reason=str(exc),
            # Deprecated raw filesystem path (removal in v0.30.29):
            path=str(path),
        )
        return None
    logger.info(
        "voice.calibration.kb.hit",
        fingerprint_hash=fingerprint_hash,
        cached_mind_id_hash=short_hash(profile.mind_id),
        # Deprecated raw field (removal in v0.30.29):
        cached_mind_id=profile.mind_id,
    )
    return profile


def has_match(*, data_dir: Path, fingerprint_hash: str) -> bool:
    """Return True if a cache entry exists for the given fingerprint hash.

    Cheap presence check (no JSON parse / schema validation) for the
    preview-fingerprint endpoint to decide ``fast_path`` vs
    ``slow_path`` recommendation. The full lookup happens in
    :func:`lookup_profile` which DOES validate.
    """
    return cache_path(data_dir, fingerprint_hash).is_file()
