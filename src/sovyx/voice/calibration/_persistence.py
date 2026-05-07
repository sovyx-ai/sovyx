"""Persist + load CalibrationProfile to/from ``<data_dir>/<mind_id>/calibration.json``.

JSON serialization with explicit schema-version gates: the loader
raises :class:`CalibrationProfileLoadError` on unknown schema versions
rather than silently coercing or falling back, per anti-pattern #35
(sentinel defaults must surface as errors). Migrations are explicit
code changes, not runtime behaviour.

Signing model (v0.30.15-17 staged adoption):

* :data:`Mode.LENIENT` (default in v0.30.15-16): unsigned profiles
  are accepted with a structured WARN
  (``voice.calibration.profile.signature_missing``); profiles WITH a
  signature are verified, and rejection emits
  ``voice.calibration.profile.signature_invalid`` but does not raise.
* :data:`Mode.STRICT` (default flip in v0.30.17): unsigned profiles
  raise :class:`CalibrationProfileLoadError`; verification failures
  also raise. The flip lands after one minor cycle of
  telemetry-validated lenient operation per the master mission's
  staged-adoption discipline.

Atomicity: ``save_calibration_profile`` writes to a sibling
``.calibration.json.tmp`` then ``os.replace`` to the final path so
partial writes never corrupt the persisted state. The ``calibration.json``
under ``<data_dir>/<mind_id>/`` is the canonical artifact; the
:class:`CalibrationApplier` (T2.8) reads it on next mind start to
replay applicable decisions.

History: introduced in v0.30.15 as T2.7 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_pem_public_key,
)

from sovyx.observability.logging import get_logger
from sovyx.observability.privacy import short_hash as _short_hash
from sovyx.voice.calibration._signing import (
    VerifyResult,
    canonical_calibration_payload,
)
from sovyx.voice.calibration.schema import (
    CALIBRATION_PROFILE_SCHEMA_VERSION,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    ProvenanceTrace,
)

logger = get_logger(__name__)

_PROFILE_FILENAME = "calibration.json"
_TMP_SUFFIX = ".tmp"

# Pre-rc.12 single-slot backup suffix. Kept as a constant for the
# legacy-migration code path: any operator with an existing
# ``calibration.json.bak`` (from v0.30.x..v0.31.0-rc.11) needs that
# file moved into the new generational chain on first save/rollback
# AFTER the rc.12 upgrade.
_LEGACY_BAK_SUFFIX = ".bak"

# rc.12 multi-generation backup chain. Operator can roll back up to
# 3 calibrations in a row before exhausting the chain. Generation 1
# is the most-recent prior profile (one step back); generation 3 is
# the oldest. This addresses the operator-debt item flagged in the
# rc.11 final-audit ("operator calibrates twice ruim, original
# state is lost") without unbounded disk growth.
_BAK_SUFFIX_TEMPLATE = ".bak.{generation}"
_MAX_BACKUP_GENERATIONS = 3


class SaveProfileResult(NamedTuple):
    """Result of :func:`save_calibration_profile`.

    rc.7 (Agent 2 NEW.2/NEW.3): the renderer at
    :func:`sovyx.cli.commands.doctor._render_calibration_verdict`
    needs to know whether the persisted profile was signed so the
    operator who passed ``--signing-key`` can verify signing actually
    succeeded. Pre-rc.7 the function returned only ``Path`` and the
    renderer read ``profile.signature`` (which is always None on the
    in-memory profile because :class:`CalibrationProfile` is frozen
    and the signature is injected into a serialized dict copy at
    :func:`save_calibration_profile`). Result: every clean
    ``--calibrate`` run rendered "Profile is unsigned" even when
    signing succeeded — defeating the operator-validation gate Step 6
    contract.

    Attributes:
        path: Where the profile was persisted.
        signed: True when the profile was signed before write
            (``--signing-key`` resolved + key loaded + signature
            computed); False otherwise (no key passed, or key
            load/sign raised + we logged ``signing_failed`` /
            ``signing_skipped`` and fell through to unsigned).
    """

    path: Path
    signed: bool


# Trust store: shipped public key is loaded once at first verify call,
# then cached at module level. Layered v2.pub support (multi-key trust
# during rotation) is deferred to v0.31.0+ per the brief §14.
_TRUSTED_PUBKEY_PATH = Path(__file__).parent / "_trusted_keys" / "v1.pub"
_TRUSTED_PUBKEY: Ed25519PublicKey | None = None
_TRUSTED_PUBKEY_LOADED = False


def _load_trusted_calibration_key() -> Ed25519PublicKey | None:
    """Load + cache the calibration trust-store public key (v1.pub).

    Returns ``None`` when the trust-store file is absent (legitimate
    during operator-side adoption — operators on hosts without the
    bundled key MUST regenerate the trust store from the matching
    private key, or operate in LENIENT mode where the verifier surfaces
    ``REJECTED_NO_TRUSTED_KEY`` instead of refusing to load).

    Raises :class:`RuntimeError` if the file exists but is NOT an
    Ed25519 public key — that's a packaging bug, not an operator
    misconfiguration, and silently returning ``None`` would mask it.
    """
    global _TRUSTED_PUBKEY, _TRUSTED_PUBKEY_LOADED
    if _TRUSTED_PUBKEY_LOADED:
        return _TRUSTED_PUBKEY
    _TRUSTED_PUBKEY_LOADED = True
    if not _TRUSTED_PUBKEY_PATH.is_file():
        _TRUSTED_PUBKEY = None
        return None
    try:
        pem = _TRUSTED_PUBKEY_PATH.read_bytes()
    except OSError:
        _TRUSTED_PUBKEY = None
        return None
    try:
        key = load_pem_public_key(pem)
    except (ValueError, TypeError) as exc:
        msg = (
            f"trusted calibration key at {_TRUSTED_PUBKEY_PATH} is unparseable: "
            f"{exc}. The shipped wheel is corrupt; reinstall Sovyx."
        )
        raise RuntimeError(msg) from exc
    if not isinstance(key, Ed25519PublicKey):
        msg = (
            f"trusted calibration key at {_TRUSTED_PUBKEY_PATH} is not Ed25519 "
            f"({type(key).__name__}). The shipped wheel is corrupt; reinstall Sovyx."
        )
        raise RuntimeError(msg)
    _TRUSTED_PUBKEY = key
    return key


def _verify_calibration_signature(
    profile: CalibrationProfile,
) -> VerifyResult:
    """Real Ed25519 verification against the calibration trust store.

    Replaces the v0.30.15-31 theater check ("is signature field
    present?") with cryptographic verification using the shipped
    ``_trusted_keys/v1.pub`` Ed25519 public key. Returns one of five
    closed-enum verdicts:

    * :data:`VerifyResult.ACCEPTED` — signature verified.
    * :data:`VerifyResult.REJECTED_NO_SIGNATURE` — profile carries
      ``signature is None``; legitimate during the v0.30.x staged
      adoption window.
    * :data:`VerifyResult.REJECTED_NO_TRUSTED_KEY` — trust-store file
      missing or unloadable; LENIENT logs + accepts, STRICT raises.
    * :data:`VerifyResult.REJECTED_MALFORMED_SIGNATURE` — signature
      field present but not 64 bytes of valid base64.
    * :data:`VerifyResult.REJECTED_BAD_SIGNATURE` — bytes valid but
      Ed25519 ``verify()`` raised :class:`InvalidSignature` (payload
      tampered OR signed by a different private key).

    The verifier is pure (no logging, no telemetry); the load path
    branches on the verdict + emits the right structured event.
    """
    pubkey = _load_trusted_calibration_key()
    if pubkey is None:
        return VerifyResult.REJECTED_NO_TRUSTED_KEY

    if profile.signature is None:
        return VerifyResult.REJECTED_NO_SIGNATURE
    if not isinstance(profile.signature, str):
        return VerifyResult.REJECTED_MALFORMED_SIGNATURE

    try:
        sig_bytes = base64.b64decode(profile.signature, validate=True)
    except (binascii.Error, ValueError):
        return VerifyResult.REJECTED_MALFORMED_SIGNATURE
    # Ed25519 signatures are exactly 64 bytes; any other length means
    # the signature was truncated, padded, or generated by a different
    # algorithm — reject as malformed rather than dispatching to the
    # cryptographic verify (which would raise InvalidSignature with a
    # less informative diagnostic).
    if len(sig_bytes) != 64:
        return VerifyResult.REJECTED_MALFORMED_SIGNATURE

    payload = canonical_calibration_payload(profile.canonical_signing_payload())
    try:
        pubkey.verify(sig_bytes, payload)
    except InvalidSignature:
        return VerifyResult.REJECTED_BAD_SIGNATURE
    return VerifyResult.ACCEPTED


def _load_private_signing_key(path: Path) -> Ed25519PrivateKey:
    """Load an unencrypted Ed25519 private key from PEM at ``path``.

    Raises :class:`RuntimeError` for any failure mode (unreadable,
    unparseable, wrong algorithm) so callers signing the persistence
    boundary can degrade to "save unsigned + log warning" without
    masking the underlying cause.
    """
    try:
        pem = path.read_bytes()
    except OSError as exc:
        msg = f"signing key at {path} unreadable: {exc}"
        raise RuntimeError(msg) from exc
    try:
        key = load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        msg = f"signing key at {path} is unparseable PEM: {exc}"
        raise RuntimeError(msg) from exc
    if not isinstance(key, Ed25519PrivateKey):
        msg = (
            f"signing key at {path} is not Ed25519 "
            f"({type(key).__name__}); regenerate via "
            f"`scripts/dev/generate_calibration_signing_key.py`."
        )
        raise RuntimeError(msg)
    return key


class CalibrationProfileRollbackError(Exception):
    """Raised when ``rollback_calibration_profile`` cannot complete.

    Causes:
        * No backup file exists (nothing to roll back to).
        * The backup file is malformed and cannot be loaded.
        * Filesystem error during the atomic rename.
    """


class CalibrationProfileLoadError(Exception):
    """Raised when a calibration profile cannot be loaded.

    Causes:
        * File is missing (FileNotFoundError on ``open``).
        * File is not valid JSON.
        * ``schema_version`` is missing, not an int, or not in the
          set of known versions.
        * Required fields are missing.
        * In :data:`Mode.STRICT`, signature is missing/invalid.
    """


def profile_path(*, data_dir: Path, mind_id: str) -> Path:
    """Return the canonical path for a mind's persisted calibration profile.

    Args:
        data_dir: The Sovyx data directory (e.g. ``~/.sovyx``).
        mind_id: The mind whose profile to address.

    Returns:
        ``<data_dir>/<mind_id>/calibration.json``.
    """
    return data_dir / mind_id / _PROFILE_FILENAME


def profile_backup_path(
    *,
    data_dir: Path,
    mind_id: str,
    generation: int = 1,
) -> Path:
    """Return the path for generation N of a mind's calibration backup chain.

    rc.12: replaces the single-slot ``.bak`` model with a 3-generation
    rotating chain. Generation 1 = most recent prior profile (one
    rollback step away from current); generation 3 = oldest. Caller
    MUST pass ``generation in (1, 2, 3)`` -- values outside the valid
    range raise ``ValueError`` so a typo doesn't silently address the
    wrong file.

    Args:
        data_dir: The Sovyx data directory.
        mind_id: The mind whose backup to address.
        generation: Which generation to address (1 = newest, default).

    Returns:
        ``<data_dir>/<mind_id>/calibration.json.bak.<generation>``.

    Raises:
        ValueError: when ``generation`` is outside [1, MAX_BACKUP_GENERATIONS].
    """
    if not 1 <= generation <= _MAX_BACKUP_GENERATIONS:
        raise ValueError(
            f"generation must be in [1, {_MAX_BACKUP_GENERATIONS}], got {generation}",
        )
    suffix = _BAK_SUFFIX_TEMPLATE.format(generation=generation)
    return data_dir / mind_id / (_PROFILE_FILENAME + suffix)


def _legacy_backup_path(*, data_dir: Path, mind_id: str) -> Path:
    """rc.12: pre-rc.12 single-slot backup at ``calibration.json.bak``.

    Kept for migration only — the upgrade path moves the legacy file
    into ``.bak.1`` on first ``save_calibration_profile`` /
    ``rollback_calibration_profile`` after the rc.12 upgrade.
    """
    return data_dir / mind_id / (_PROFILE_FILENAME + _LEGACY_BAK_SUFFIX)


def _migrate_legacy_backup_if_present(*, data_dir: Path, mind_id: str) -> None:
    """Move ``calibration.json.bak`` (rc.11 and earlier) into ``.bak.1``.

    Idempotent + no-op when no legacy file exists OR when ``.bak.1``
    already exists (operator already rolled into the new chain). Logged
    once at INFO so operators reading logs see the one-time migration.
    """
    legacy = _legacy_backup_path(data_dir=data_dir, mind_id=mind_id)
    if not legacy.is_file():
        return
    gen1 = profile_backup_path(data_dir=data_dir, mind_id=mind_id, generation=1)
    if gen1.is_file():
        # Operator has already saved at least once after rc.12 upgrade;
        # drop the now-redundant legacy file.
        legacy.unlink(missing_ok=True)
        return
    os.replace(legacy, gen1)
    logger.info(
        "voice.calibration.profile.legacy_backup_migrated",
        mind_id_hash=_short_hash(mind_id),
        from_path_suffix=_LEGACY_BAK_SUFFIX,
        to_generation=1,
    )


def list_calibration_backups(
    *,
    data_dir: Path,
    mind_id: str,
) -> list[tuple[int, Path]]:
    """Enumerate available backup generations for a mind, ordered newest-first.

    rc.12 dashboard surface: the operator's Settings → Voice card
    needs to know how many rollback steps are still available so the
    Rollback button can be disabled when the chain is empty + show
    "N backups available" instead of guessing.

    Returns:
        List of ``(generation, path)`` tuples for every existing
        backup file, ordered by generation ascending (1 = newest
        first). Empty list when no backups exist.
    """
    _migrate_legacy_backup_if_present(data_dir=data_dir, mind_id=mind_id)
    found: list[tuple[int, Path]] = []
    for generation in range(1, _MAX_BACKUP_GENERATIONS + 1):
        path = profile_backup_path(
            data_dir=data_dir,
            mind_id=mind_id,
            generation=generation,
        )
        if path.is_file():
            found.append((generation, path))
    return found


def save_calibration_profile(
    profile: CalibrationProfile,
    *,
    data_dir: Path,
    signing_key_path: Path | None = None,
) -> SaveProfileResult:
    """Persist a calibration profile atomically; optionally sign first.

    Writes to a sibling ``.calibration.json.tmp`` first, then
    :func:`os.replace`s into the final path so partial writes never
    corrupt the persisted state.

    Signing (P4 v0.30.32): when ``signing_key_path`` is provided AND the
    file exists + parses as a valid Ed25519 PEM private key, this
    function signs the canonical payload (sort_keys=True, separators=
    (",",":")) and rewrites the in-memory ``signature`` field as a
    base64 string before serialization. Failure to load the signing key
    or sign the payload logs a structured WARN
    (``voice.calibration.profile.signing_failed``) but does NOT raise —
    the profile lands on disk unsigned, which the load path treats as
    ``REJECTED_NO_SIGNATURE`` (LENIENT-accepted).

    Args:
        profile: The profile to persist. Its ``mind_id`` field
            determines the target directory.
        data_dir: Sovyx data directory; the per-mind subdirectory is
            created if missing.
        signing_key_path: Path to an unencrypted PEM Ed25519 private
            key; when supplied, the profile is signed before write.
            ``None`` (default) writes unsigned profiles, mirroring
            the v0.30.x staged-adoption window.

    Returns:
        :class:`SaveProfileResult` carrying the persisted path AND a
        ``signed`` boolean so callers (the renderer in particular) can
        accurately tell the operator whether the profile landed signed
        or unsigned. rc.7 (Agent 2 NEW.2/NEW.3): pre-rc.7 returned
        only ``Path`` and the renderer read ``profile.signature`` which
        was always ``None`` on the in-memory frozen profile.
    """
    target = profile_path(data_dir=data_dir, mind_id=profile.mind_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + _TMP_SUFFIX)

    # rc.12 multi-generation backup chain. Pre-rc.12 the current
    # profile rotated into a single ``.bak`` slot, overwriting any
    # prior backup — so an operator who calibrated twice in a row
    # with a bad result lost their original good state forever. Now:
    # rotate .bak.2 → .bak.3 (drop oldest), .bak.1 → .bak.2, current
    # → .bak.1. Operator can roll back up to MAX_BACKUP_GENERATIONS
    # times before the chain exhausts. Legacy ``.bak`` (single-slot,
    # pre-rc.12) is auto-migrated into ``.bak.1`` before rotation.
    _migrate_legacy_backup_if_present(data_dir=data_dir, mind_id=profile.mind_id)
    _rotate_backup_chain(data_dir=data_dir, mind_id=profile.mind_id)
    backup_target = profile_backup_path(
        data_dir=data_dir,
        mind_id=profile.mind_id,
        generation=1,
    )
    if target.is_file():
        os.replace(target, backup_target)

    serialized = _profile_to_dict(profile)

    # Optional signing at persistence boundary. The signer is best-
    # effort: any failure (missing key, bad PEM, wrong algorithm)
    # logs + falls through to the unsigned write path so the operator
    # always gets a persisted profile they can re-sign later.
    #
    # QA-FIX-2 (v0.31.0-rc.2): the pre-rc.2 ``is_file()`` short-circuit
    # silently degraded when the operator passed a non-existent path
    # (e.g. ``--signing-key /tmp/nope``); the profile landed unsigned
    # with zero observability. Now: when a path is supplied but the
    # file is missing, emit ``signing_skipped{reason="key_path_missing"}``
    # so operators see WHY the profile they expected to be signed
    # wasn't.
    was_signed = False
    if signing_key_path is not None:
        if not signing_key_path.is_file():
            logger.warning(
                "voice.calibration.profile.signing_skipped",
                mind_id_hash=_short_hash(profile.mind_id),
                profile_id_hash=_short_hash(profile.profile_id),
                reason="key_path_missing",
                # Closed-enum reason field; raw operator-set path is
                # NOT logged (see privacy contract in P0/P1).
            )
        else:
            try:
                private_key = _load_private_signing_key(signing_key_path)
                sig_payload = canonical_calibration_payload(profile.canonical_signing_payload())
                sig_bytes = private_key.sign(sig_payload)
                serialized["signature"] = base64.b64encode(sig_bytes).decode("ascii")
                was_signed = True
            except (RuntimeError, ValueError) as exc:
                logger.warning(
                    "voice.calibration.profile.signing_failed",
                    mind_id_hash=_short_hash(profile.mind_id),
                    profile_id_hash=_short_hash(profile.profile_id),
                    reason=str(exc)[:200],
                )

    payload = json.dumps(
        serialized,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, target)

    backup_generations = len(list_calibration_backups(data_dir=data_dir, mind_id=profile.mind_id))
    logger.info(
        "voice.calibration.profile.persisted",
        mind_id_hash=_short_hash(profile.mind_id),
        profile_id_hash=_short_hash(profile.profile_id),
        signed=was_signed,
        backup_present=backup_target.is_file(),
        backup_generations=backup_generations,
    )
    return SaveProfileResult(path=target, signed=was_signed)


def _rotate_backup_chain(*, data_dir: Path, mind_id: str) -> None:
    """Shift the .bak.N chain up by one generation; oldest is dropped.

    rc.12: called BEFORE ``save_calibration_profile`` rotates the
    current profile into ``.bak.1``. Walking from oldest to newest
    so each rename target is empty when reached:

    - ``.bak.3`` deleted (drop oldest if present).
    - ``.bak.2`` → ``.bak.3``.
    - ``.bak.1`` → ``.bak.2``.

    Now ``.bak.1`` is empty + ready to receive the about-to-be-
    rotated current profile. No-op when no backups exist (fresh
    install). Idempotent across crashes (each rename is a single
    ``os.replace``, atomic on POSIX + Windows when on the same
    volume).
    """
    # Walk newest-target first (delete .bak.3) so renames don't
    # collide. The loop walks from MAX down to 1; each iteration
    # promotes generation N to generation N+1.
    oldest = profile_backup_path(
        data_dir=data_dir,
        mind_id=mind_id,
        generation=_MAX_BACKUP_GENERATIONS,
    )
    if oldest.is_file():
        oldest.unlink(missing_ok=True)
    for generation in range(_MAX_BACKUP_GENERATIONS - 1, 0, -1):
        src = profile_backup_path(
            data_dir=data_dir,
            mind_id=mind_id,
            generation=generation,
        )
        if not src.is_file():
            continue
        dst = profile_backup_path(
            data_dir=data_dir,
            mind_id=mind_id,
            generation=generation + 1,
        )
        os.replace(src, dst)


def rollback_calibration_profile(
    *,
    data_dir: Path,
    mind_id: str,
) -> Path:
    """Restore generation 1 of the backup chain as the current profile.

    rc.12: walks the .bak.1..N chain. Restores .bak.1 as the current
    profile (consumed by ``os.replace``), then shifts the remaining
    backups down by one generation (.bak.2 → .bak.1, .bak.3 →
    .bak.2). Pre-rc.12 was single-step (one .bak slot, consumed-on-
    rollback); rc.12 lets the operator roll back up to
    MAX_BACKUP_GENERATIONS times before the chain is exhausted.

    Args:
        data_dir: Sovyx data directory.
        mind_id: The mind whose profile to roll back.

    Returns:
        The absolute path the restored profile now lives at
        (always ``<data_dir>/<mind_id>/calibration.json``).

    Raises:
        CalibrationProfileRollbackError: when no backup exists at
            generation 1 (chain exhausted OR fresh install with no
            backups), or the .bak.1 cannot be loaded as a valid
            CalibrationProfile (refuse to roll back to corrupt state).
    """
    _migrate_legacy_backup_if_present(data_dir=data_dir, mind_id=mind_id)
    target = profile_path(data_dir=data_dir, mind_id=mind_id)
    backup = profile_backup_path(data_dir=data_dir, mind_id=mind_id, generation=1)

    if not backup.is_file():
        raise CalibrationProfileRollbackError(
            f"no calibration backup at {backup} -- nothing to roll back. "
            f"The backup chain is exhausted (or never populated). "
            f"Re-run `sovyx doctor voice --calibrate` to regenerate."
        )

    # Best-effort: if the rollback can't load the backup as a valid
    # profile we surface the error early so the operator doesn't end
    # up with an unloadable canonical file. The validated profile
    # also gives us its ``profile_id`` for the rolled_back telemetry
    # event's spec §8.3 ``profile_id_hash`` field.
    try:
        raw = json.loads(backup.read_text(encoding="utf-8"))
        backup_profile = _profile_from_dict(raw)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise CalibrationProfileRollbackError(
            f"backup at {backup} is unreadable: {exc}. Refusing to roll back to a corrupt state."
        ) from exc

    # Atomic swap: write the backup over the canonical (which gets
    # discarded -- single-step contract). os.replace is atomic on
    # POSIX + Windows when source and target are on the same volume.
    os.replace(backup, target)

    # rc.12: shift remaining backups down one generation so the
    # operator can roll back AGAIN. .bak.2 → .bak.1, .bak.3 → .bak.2.
    # Walking newest-shifted-target last so each rename target is
    # empty (the just-consumed .bak.1, then the just-shifted .bak.2).
    for generation in range(2, _MAX_BACKUP_GENERATIONS + 1):
        src = profile_backup_path(
            data_dir=data_dir,
            mind_id=mind_id,
            generation=generation,
        )
        if not src.is_file():
            continue
        dst = profile_backup_path(
            data_dir=data_dir,
            mind_id=mind_id,
            generation=generation - 1,
        )
        os.replace(src, dst)

    remaining = len(list_calibration_backups(data_dir=data_dir, mind_id=mind_id))
    logger.info(
        "voice.calibration.applier.rolled_back",
        profile_id_hash=_short_hash(backup_profile.profile_id),
        mind_id_hash=_short_hash(mind_id),
        rollback_reason="operator_initiated",
        backup_generations_remaining=remaining,
    )
    return target


class _LoadMode(StrEnum):
    """Local alias for :class:`sovyx.voice.calibration._signing.Mode`.

    Re-defining here avoids a circular import (``_signing`` imports
    nothing from this module, but we keep the persistence layer
    independent of the signing module so tests can exercise the
    JSON round-trip without crypto dependencies).
    """

    LENIENT = "lenient"
    STRICT = "strict"


def _read_and_migrate_profile_dict(
    *,
    data_dir: Path,
    mind_id: str,
) -> tuple[dict[str, Any], Path]:
    """Read JSON + migrate to current schema; return (raw_dict, path).

    Shared spine of :func:`load_calibration_profile` and
    :func:`inspect_migrated_profile_dict`: both need the
    file-existence check, JSON parse, and migration walk; only the
    post-migration step (construct frozen profile + signature gate
    vs. return the dict) differs.
    """
    # Local import: _migrations imports from this module (the error
    # type), so the cycle is broken by deferring the registry import
    # to call time.
    from sovyx.voice.calibration._migrations import (  # noqa: PLC0415
        CalibrationProfileMigrationError,
        migrate_to_current,
    )

    path = profile_path(data_dir=data_dir, mind_id=mind_id)
    if not path.is_file():
        raise CalibrationProfileLoadError(
            f"calibration profile not found at {path} (mind_id={mind_id!r})"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalibrationProfileLoadError(
            f"calibration profile at {path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise CalibrationProfileLoadError(
            f"calibration profile at {path} must be a JSON object, got {type(raw).__name__}"
        )

    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int):
        raise CalibrationProfileLoadError(
            f"calibration profile at {path} has missing or non-int schema_version "
            f"(got {schema_version!r}); explicit migration required"
        )
    # P5 (v0.30.33): walk the migration chain when the profile's
    # schema_version is older than the runtime's. The walker is a
    # no-op when versions match. CalibrationProfileMigrationError
    # subclasses CalibrationProfileLoadError, so existing operator
    # surfaces (CLI exit codes, dashboard error rendering) treat it
    # uniformly with other load failures.
    try:
        raw = migrate_to_current(
            raw,
            target_version=CALIBRATION_PROFILE_SCHEMA_VERSION,
            path=path,
        )
    except CalibrationProfileMigrationError as exc:
        logger.error(
            "voice.calibration.profile.migration_failed",
            mind_id_hash=_short_hash(mind_id),
            from_version=exc.source_version,
            to_version=exc.target_version,
            step=exc.step_failed or "unknown",
        )
        raise
    return raw, path


def inspect_migrated_profile_dict(
    *,
    data_dir: Path,
    mind_id: str,
) -> dict[str, Any]:
    """Return the migrated raw dict WITHOUT constructing a profile.

    Operator inspection mode (P5 v0.30.33+): triage "what does my
    pre-migration profile look like after the v1→v2 reshape?" without
    committing to the new shape or running the signature gate.

    Skips ``_profile_from_dict`` + signature verification; the dict is
    suitable for re-serialising into a JSON file the operator can
    diff against the original on disk. The caller is responsible for
    not mistaking the inspection result for a fully-validated profile.
    """
    raw, _path = _read_and_migrate_profile_dict(data_dir=data_dir, mind_id=mind_id)
    return raw


def load_calibration_profile(
    *,
    data_dir: Path,
    mind_id: str,
    mode: _LoadMode = _LoadMode.LENIENT,
) -> CalibrationProfile:
    """Load a persisted calibration profile.

    Args:
        data_dir: Sovyx data directory.
        mind_id: The mind whose profile to load.
        mode: ``LENIENT`` (default in v0.30.15-32) accepts unsigned
            profiles with a WARN; ``STRICT`` raises on unsigned or
            invalid signatures.

    Returns:
        The frozen :class:`CalibrationProfile`.

    Raises:
        CalibrationProfileLoadError: file missing, malformed JSON,
            missing required fields, or (in STRICT) missing/invalid
            signature.
        CalibrationProfileMigrationError: profile claims a
            schema_version the migration registry can't bridge to
            the runtime's :data:`CALIBRATION_PROFILE_SCHEMA_VERSION`
            (subclass of CalibrationProfileLoadError).
    """
    raw, path = _read_and_migrate_profile_dict(data_dir=data_dir, mind_id=mind_id)

    try:
        profile = _profile_from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationProfileLoadError(
            f"calibration profile at {path} is malformed: {exc}"
        ) from exc

    # Signature gate (P4 v0.30.32 — REAL verification).
    #
    # v0.30.15-31 ran a "is signature field present?" theater check;
    # v0.30.32 wires :func:`_verify_calibration_signature` against the
    # bundled Ed25519 trust store (``_trusted_keys/v1.pub``). The
    # 5-way verdict drives ``signature_status`` + the matching
    # structured event:
    #
    # * ACCEPTED               -> "accepted"  (DEBUG log; no warning)
    # * REJECTED_NO_SIGNATURE  -> "missing"   (WARN; STRICT raises)
    # * REJECTED_BAD_SIGNATURE -> "invalid"   (WARN; STRICT raises)
    # * REJECTED_MALFORMED_*   -> "invalid"   (WARN; STRICT raises)
    # * REJECTED_NO_TRUSTED_KEY -> "invalid"  (WARN; STRICT raises)
    #
    # STRICT default flip stays deferred to v0.31.0 per
    # ``feedback_staged_adoption``.
    profile_hash = _short_hash(profile.profile_id)
    mind_hash = _short_hash(mind_id)
    verdict = _verify_calibration_signature(profile)
    if verdict == VerifyResult.ACCEPTED:
        signature_status = "accepted"
        logger.debug(
            "voice.calibration.profile.signature.accepted",
            mind_id_hash=mind_hash,
            profile_id_hash=profile_hash,
            mode=mode.value,
        )
    elif verdict == VerifyResult.REJECTED_NO_SIGNATURE:
        if mode is _LoadMode.STRICT:
            raise CalibrationProfileLoadError(
                f"calibration profile at {path} is unsigned; STRICT mode requires "
                f"a signature. Regenerate via `sovyx doctor voice --calibrate "
                f"--signing-key <path>`."
            )
        signature_status = "missing"
        logger.warning(
            "voice.calibration.profile.signature_missing",
            mind_id_hash=mind_hash,
            profile_id_hash=profile_hash,
            mode=mode.value,
        )
    else:
        # REJECTED_BAD_SIGNATURE / REJECTED_MALFORMED_SIGNATURE /
        # REJECTED_NO_TRUSTED_KEY — all surface as
        # ``signature.invalid`` with the verdict in the closed-enum
        # ``verdict`` field so dashboards can distinguish without
        # parsing free-form messages.
        signature_status = "invalid"
        logger.warning(
            "voice.calibration.profile.signature.invalid",
            mind_id_hash=mind_hash,
            profile_id_hash=profile_hash,
            verdict=verdict.value,
            mode=mode.value,
        )
        if mode is _LoadMode.STRICT:
            raise CalibrationProfileLoadError(
                f"calibration profile at {path} failed signature verification: "
                f"{verdict.value}. STRICT mode requires a valid signature; "
                f"regenerate via `sovyx doctor voice --calibrate --signing-key <path>`."
            )

    logger.info(
        "voice.calibration.profile.loaded",
        mind_id_hash=mind_hash,
        profile_id_hash=profile_hash,
        signature_status=signature_status,
        mode=mode.value,
        schema_version=profile.schema_version,
    )

    return profile


# ====================================================================
# Internal: dict <-> dataclass conversion
# ====================================================================


def _profile_to_dict(profile: CalibrationProfile) -> dict[str, Any]:
    return {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "mind_id": profile.mind_id,
        "fingerprint": _fingerprint_to_dict(profile.fingerprint),
        "measurements": _measurements_to_dict(profile.measurements),
        "decisions": [_decision_to_dict(d) for d in profile.decisions],
        "provenance": [_provenance_to_dict(p) for p in profile.provenance],
        "generated_by_engine_version": profile.generated_by_engine_version,
        "generated_by_rule_set_version": profile.generated_by_rule_set_version,
        "generated_at_utc": profile.generated_at_utc,
        "signature": profile.signature,
    }


def _fingerprint_to_dict(fp: HardwareFingerprint) -> dict[str, Any]:
    return {
        "schema_version": fp.schema_version,
        "captured_at_utc": fp.captured_at_utc,
        "distro_id": fp.distro_id,
        "distro_id_like": fp.distro_id_like,
        "kernel_release": fp.kernel_release,
        "kernel_major_minor": fp.kernel_major_minor,
        "cpu_model": fp.cpu_model,
        "cpu_cores": fp.cpu_cores,
        "ram_mb": fp.ram_mb,
        "has_gpu": fp.has_gpu,
        "gpu_vram_mb": fp.gpu_vram_mb,
        "audio_stack": fp.audio_stack,
        "pipewire_version": fp.pipewire_version,
        "pulseaudio_version": fp.pulseaudio_version,
        "alsa_lib_version": fp.alsa_lib_version,
        "codec_id": fp.codec_id,
        "driver_family": fp.driver_family,
        "system_vendor": fp.system_vendor,
        "system_product": fp.system_product,
        "capture_card_count": fp.capture_card_count,
        "capture_devices": list(fp.capture_devices),
        "apo_active": fp.apo_active,
        "apo_name": fp.apo_name,
        "hal_interceptors": list(fp.hal_interceptors),
        "pulse_modules_destructive": list(fp.pulse_modules_destructive),
    }


def _measurements_to_dict(m: MeasurementSnapshot) -> dict[str, Any]:
    return {
        "schema_version": m.schema_version,
        "captured_at_utc": m.captured_at_utc,
        "duration_s": m.duration_s,
        "rms_dbfs_per_capture": list(m.rms_dbfs_per_capture),
        "vad_speech_probability_max": m.vad_speech_probability_max,
        "vad_speech_probability_p99": m.vad_speech_probability_p99,
        "noise_floor_dbfs_estimate": m.noise_floor_dbfs_estimate,
        "capture_callback_p99_ms": m.capture_callback_p99_ms,
        "capture_jitter_ms": m.capture_jitter_ms,
        "portaudio_latency_advertised_ms": m.portaudio_latency_advertised_ms,
        "mixer_card_index": m.mixer_card_index,
        "mixer_capture_pct": m.mixer_capture_pct,
        "mixer_boost_pct": m.mixer_boost_pct,
        "mixer_internal_mic_boost_pct": m.mixer_internal_mic_boost_pct,
        "mixer_attenuation_regime": m.mixer_attenuation_regime,
        "echo_correlation_db": m.echo_correlation_db,
        "triage_winner_hid": m.triage_winner_hid,
        "triage_winner_confidence": m.triage_winner_confidence,
    }


def _decision_to_dict(d: CalibrationDecision) -> dict[str, Any]:
    return {
        "target": d.target,
        "target_class": d.target_class,
        "operation": d.operation,
        "value": d.value,
        "rationale": d.rationale,
        "rule_id": d.rule_id,
        "rule_version": d.rule_version,
        "confidence": d.confidence.value,
    }


def _provenance_to_dict(p: ProvenanceTrace) -> dict[str, Any]:
    return {
        "rule_id": p.rule_id,
        "rule_version": p.rule_version,
        "fired_at_utc": p.fired_at_utc,
        "matched_conditions": list(p.matched_conditions),
        "produced_decisions": list(p.produced_decisions),
        "confidence": p.confidence.value,
    }


def _profile_from_dict(d: dict[str, Any]) -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=d["schema_version"],
        profile_id=d["profile_id"],
        mind_id=d["mind_id"],
        fingerprint=_fingerprint_from_dict(d["fingerprint"]),
        measurements=_measurements_from_dict(d["measurements"]),
        decisions=tuple(_decision_from_dict(x) for x in d["decisions"]),
        provenance=tuple(_provenance_from_dict(x) for x in d["provenance"]),
        generated_by_engine_version=d["generated_by_engine_version"],
        generated_by_rule_set_version=d["generated_by_rule_set_version"],
        generated_at_utc=d["generated_at_utc"],
        signature=d.get("signature"),
    )


def _fingerprint_from_dict(d: dict[str, Any]) -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=d["schema_version"],
        captured_at_utc=d["captured_at_utc"],
        distro_id=d["distro_id"],
        distro_id_like=d["distro_id_like"],
        kernel_release=d["kernel_release"],
        kernel_major_minor=d["kernel_major_minor"],
        cpu_model=d["cpu_model"],
        cpu_cores=d["cpu_cores"],
        ram_mb=d["ram_mb"],
        has_gpu=d["has_gpu"],
        gpu_vram_mb=d["gpu_vram_mb"],
        audio_stack=d["audio_stack"],
        pipewire_version=d["pipewire_version"],
        pulseaudio_version=d["pulseaudio_version"],
        alsa_lib_version=d["alsa_lib_version"],
        codec_id=d["codec_id"],
        driver_family=d["driver_family"],
        system_vendor=d["system_vendor"],
        system_product=d["system_product"],
        capture_card_count=d["capture_card_count"],
        capture_devices=tuple(d["capture_devices"]),
        apo_active=d["apo_active"],
        apo_name=d["apo_name"],
        hal_interceptors=tuple(d["hal_interceptors"]),
        pulse_modules_destructive=tuple(d["pulse_modules_destructive"]),
    )


def _measurements_from_dict(d: dict[str, Any]) -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=d["schema_version"],
        captured_at_utc=d["captured_at_utc"],
        duration_s=d["duration_s"],
        rms_dbfs_per_capture=tuple(d["rms_dbfs_per_capture"]),
        vad_speech_probability_max=d["vad_speech_probability_max"],
        vad_speech_probability_p99=d["vad_speech_probability_p99"],
        noise_floor_dbfs_estimate=d["noise_floor_dbfs_estimate"],
        capture_callback_p99_ms=d["capture_callback_p99_ms"],
        capture_jitter_ms=d["capture_jitter_ms"],
        portaudio_latency_advertised_ms=d["portaudio_latency_advertised_ms"],
        mixer_card_index=d["mixer_card_index"],
        mixer_capture_pct=d["mixer_capture_pct"],
        mixer_boost_pct=d["mixer_boost_pct"],
        mixer_internal_mic_boost_pct=d["mixer_internal_mic_boost_pct"],
        mixer_attenuation_regime=d["mixer_attenuation_regime"],
        echo_correlation_db=d["echo_correlation_db"],
        triage_winner_hid=d["triage_winner_hid"],
        triage_winner_confidence=d["triage_winner_confidence"],
    )


def _decision_from_dict(d: dict[str, Any]) -> CalibrationDecision:
    return CalibrationDecision(
        target=d["target"],
        target_class=d["target_class"],
        operation=d["operation"],
        value=d["value"],
        rationale=d["rationale"],
        rule_id=d["rule_id"],
        rule_version=d["rule_version"],
        confidence=CalibrationConfidence(d["confidence"]),
    )


def _provenance_from_dict(d: dict[str, Any]) -> ProvenanceTrace:
    return ProvenanceTrace(
        rule_id=d["rule_id"],
        rule_version=d["rule_version"],
        fired_at_utc=d["fired_at_utc"],
        matched_conditions=tuple(d["matched_conditions"]),
        produced_decisions=tuple(d["produced_decisions"]),
        confidence=CalibrationConfidence(d["confidence"]),
    )
