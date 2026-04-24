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

Ed25519 signature field on profiles is accepted but not enforced in
F1 (stub per task T1.C.2). ``signature`` presence is logged at DEBUG
so F2's verifier has a visible transition path.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from sovyx.observability.logging import get_logger
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


def load_profile_file(path: Path) -> MixerKBProfile:
    """Load and validate one profile YAML file.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        OSError: Read failed (permission, encoding, …).
        yaml.YAMLError: YAML is structurally malformed.
        ValidationError: Schema validation failed (per-field detail
            in the ``.errors()`` list).
        ValueError: ``profile_id`` stem disagrees with the YAML body
            — the loader enforces filename-as-id to prevent silent
            profile renames on disk.
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
    if model.signature is not None:
        logger.debug(
            "mixer_kb_profile_signature_present_not_enforced_in_f1",
            profile_id=model.profile_id,
            path=str(path),
        )
    return model.to_profile()


def load_profiles_from_directory(directory: Path) -> list[MixerKBProfile]:
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

    for path in sorted(directory.glob("*.yaml")):
        if path.name.startswith(_PROFILE_INDEX_PREFIX):
            continue
        try:
            profiles.append(load_profile_file(path))
        except ValidationError as exc:
            logger.warning(
                "mixer_kb_profile_schema_invalid",
                path=str(path),
                error_count=len(exc.errors()),
                first_error=exc.errors()[0] if exc.errors() else None,
            )
            skipped.append((path.name, "schema_invalid"))
        except yaml.YAMLError as exc:
            logger.warning(
                "mixer_kb_profile_yaml_malformed",
                path=str(path),
                detail=str(exc)[:200],
            )
            skipped.append((path.name, "yaml_malformed"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "mixer_kb_profile_load_failed",
                path=str(path),
                detail=str(exc)[:200],
            )
            skipped.append((path.name, "load_failed"))

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
