"""YAML → :class:`MixerKBProfile` loader — L2.5 Phase F1.C.

Reads profile YAML files, validates against :class:`KBProfileModel`
(pydantic v2), and materialises them as frozen runtime dataclasses
(:class:`~sovyx.voice.health.contract.MixerKBProfile`).

Failure policy: malformed, unreadable, or schema-invalid files are
**skipped** with a structured WARN log rather than aborting the load.
Rationale — invariant P6 (fail honest, fail fast) applies at the
profile level, not the cohort level: one bad YAML should not sink
every other KB entry. The orchestrator can still match against the
valid profiles that loaded, and the dashboard surfaces the skip list.

F2 wire-up: the loader now accepts an optional
:class:`KBSignatureVerifier`. When a verifier is supplied, every
profile is signature-checked before its frozen runtime form is
materialised. Mode.LENIENT (the default verifier mode) emits
``voice.kb.signature.invalid`` WARN events but lets unsigned /
badly-signed profiles through; Mode.STRICT skips them entirely.
Passing ``verifier=None`` (the default) keeps the legacy F1
behaviour — no verification at all — for backward compat with
call sites that haven't been migrated yet.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from sovyx.observability.logging import get_logger
from sovyx.voice.health._mixer_kb._signing import (
    KBSignatureError,
    KBSignatureVerifier,
)
from sovyx.voice.health._mixer_kb.schema import KBProfileModel

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.voice.health.contract import MixerKBProfile

logger = get_logger(__name__)


def _normalise_for_match(text: str) -> str:
    """Unicode-fold + casefold for filename/profile_id comparison.

    Paranoid-QA R2 LOW #2/#3: casefold handles German ß/ss + most
    Unicode case pairs, but composed vs decomposed accents (café
    as NFC U+00E9 vs NFD U+0065 U+0301) still miscompare. NFKC
    first, then casefold, closes both gaps.
    """
    return unicodedata.normalize("NFKC", text).casefold()


_PROFILE_INDEX_PREFIX = "_"
"""Prefix marking reserved loader-metadata files (e.g. ``_index.yaml``).

Files whose stem starts with ``_`` are skipped by
:func:`load_profiles_from_directory` so the loader never tries to
parse index / signature / changelog YAMLs as profiles.
"""


def load_profile_file(
    path: Path,
    *,
    verifier: KBSignatureVerifier | None = None,
) -> MixerKBProfile:
    """Load and validate one profile YAML file.

    Args:
        path: Profile YAML file to load.
        verifier: Optional :class:`KBSignatureVerifier`. When
            supplied, the loaded profile is signature-checked
            against the verifier's trusted public key. Behaviour
            depends on the verifier's :class:`Mode` —
            ``Mode.LENIENT`` emits a structured WARN and returns
            the profile anyway; ``Mode.STRICT`` raises
            :class:`KBSignatureError`. ``None`` (default) skips
            verification entirely (legacy F1 behaviour).

    Raises:
        FileNotFoundError: ``path`` does not exist.
        OSError: Read failed (permission, encoding, …).
        yaml.YAMLError: YAML is structurally malformed.
        ValidationError: Schema validation failed (per-field detail
            in the ``.errors()`` list).
        ValueError: ``profile_id`` stem disagrees with the YAML body
            — the loader enforces filename-as-id to prevent silent
            profile renames on disk.
        KBSignatureError: Only when ``verifier.mode is Mode.STRICT``
            and the signature does not verify.
    """
    raw_text = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw_text)
    if not isinstance(parsed, dict):
        msg = (
            f"profile file {path.name!r} must contain a YAML mapping at "
            f"the top level (got {type(parsed).__name__})"
        )
        raise ValueError(msg)
    model = KBProfileModel.model_validate(parsed)
    # Paranoid-QA HIGH #18 + R2 LOW #2/#3: filename-stem
    # comparison must be case-insensitive AND Unicode-form-
    # insensitive. ``casefold()`` alone handles German ß/ss and
    # Turkish-I-like cases correctly, but composed vs decomposed
    # accents (café as NFC "café" vs NFD "café") still miscompare.
    # NFKC normalise both sides first so "café.yaml" matches
    # ``profile_id: café`` regardless of which encoder wrote the
    # filename. ``casefold + NFKC`` together cover every real-world
    # filename/profile_id drift we've observed.
    if _normalise_for_match(model.profile_id) != _normalise_for_match(path.stem):
        msg = (
            f"profile_id={model.profile_id!r} in {path.name!r} disagrees "
            f"with filename stem {path.stem!r} (case-folded); they must "
            f"match so directory listings are authoritative"
        )
        raise ValueError(msg)
    if verifier is not None:
        # F2 wire-up: feed the parsed YAML mapping (NOT the pydantic
        # model) into the verifier so the canonical-payload bytes
        # reflect the on-disk content faithfully. The verifier emits
        # its own structured WARN on rejection in LENIENT mode and
        # raises KBSignatureError in STRICT mode.
        verifier.verify(parsed)
    elif model.signature is not None:
        logger.debug(
            "mixer_kb_profile_signature_present_no_verifier_configured",
            profile_id=model.profile_id,
            path=str(path),
        )
    return model.to_profile()


def load_profiles_from_directory(
    directory: Path,
    *,
    verifier: KBSignatureVerifier | None = None,
) -> list[MixerKBProfile]:
    """Load every ``*.yaml`` in ``directory`` (non-recursive).

    Files starting with :data:`_PROFILE_INDEX_PREFIX` are skipped.
    Invalid files are logged and skipped; valid files are returned
    in filename sort order (stable across OS/filesystem differences).

    Args:
        directory: Directory containing profile YAMLs. Must exist;
            missing directory is logged at DEBUG and returns an
            empty list (common case on fresh installs before F1.H
            populates ``profiles/``).

    Returns:
        List of successfully-loaded profiles in deterministic order.
    """
    if not directory.exists():
        logger.debug(
            "mixer_kb_directory_missing",
            directory=str(directory),
        )
        return []
    if not directory.is_dir():
        logger.warning(
            "mixer_kb_directory_not_a_directory",
            path=str(directory),
        )
        return []

    profiles: list[MixerKBProfile] = []
    skipped: list[tuple[str, str]] = []
    # Paranoid-QA R4 HIGH-6: dedupe within the pool. On a case-
    # sensitive filesystem, two files named ``Café.yaml`` (NFC) and
    # ``CAFÉ.yaml`` (cased NFD) can both carry ``profile_id: café``
    # and both pass the loader's filename-stem check (via
    # ``_normalise_for_match``). The resulting KBLookup has two
    # profiles sharing a ``profile_id`` — ``match()`` scores them
    # identically, the ambiguity-window check trips, and L2.5 DEFERS
    # on hardware the KB explicitly targets. Dedupe here keeps the
    # first-seen winner (sorted filename order for determinism) and
    # logs the rejected twins so KB authors can clean up.
    seen_profile_ids: dict[str, str] = {}

    for path in sorted(directory.glob("*.yaml")):
        if path.name.startswith(_PROFILE_INDEX_PREFIX):
            continue
        try:
            profile = load_profile_file(path, verifier=verifier)
        except ValidationError as exc:
            logger.warning(
                "mixer_kb_profile_schema_invalid",
                path=str(path),
                error_count=len(exc.errors()),
                first_error=exc.errors()[0] if exc.errors() else None,
            )
            skipped.append((path.name, "schema_invalid"))
            continue
        except KBSignatureError as exc:
            # F2: only reachable when verifier mode is STRICT — the
            # verifier itself emits the structured WARN before
            # raising. Per the cohort-level failure policy, one bad
            # signature should not sink the rest of the pool.
            logger.warning(
                "mixer_kb_profile_signature_rejected",
                path=str(path),
                profile_id=exc.profile_id,
                verdict=exc.result.value,
            )
            skipped.append((path.name, f"signature_{exc.result.value}"))
            continue
        except yaml.YAMLError as exc:
            logger.warning(
                "mixer_kb_profile_yaml_malformed",
                path=str(path),
                detail=str(exc)[:200],
            )
            skipped.append((path.name, "yaml_malformed"))
            continue
        except (OSError, ValueError) as exc:
            logger.warning(
                "mixer_kb_profile_load_failed",
                path=str(path),
                detail=str(exc)[:200],
            )
            skipped.append((path.name, "load_failed"))
            continue
        if profile.profile_id in seen_profile_ids:
            logger.warning(
                "mixer_kb_profile_id_duplicate_in_pool",
                profile_id=profile.profile_id,
                first_path=seen_profile_ids[profile.profile_id],
                duplicate_path=str(path),
                note=(
                    "two files in the same pool carry the same "
                    "profile_id; keeping the first (sorted order), "
                    "dropping the second to avoid ambiguity-window "
                    "deferrals in MixerKBLookup.match"
                ),
            )
            skipped.append((path.name, "duplicate_profile_id"))
            continue
        seen_profile_ids[profile.profile_id] = str(path)
        profiles.append(profile)

    if skipped:
        logger.info(
            "mixer_kb_profiles_loaded",
            loaded=len(profiles),
            skipped=len(skipped),
            skipped_detail=skipped,
        )
    else:
        logger.debug(
            "mixer_kb_profiles_loaded",
            loaded=len(profiles),
            skipped=0,
        )

    return profiles


__all__ = [
    "load_profile_file",
    "load_profiles_from_directory",
]
