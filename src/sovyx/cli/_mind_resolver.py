"""Shared mind-id resolver for the Sovyx CLI.

Per-mind config lives at ``<data_dir>/<mind_id>/mind.yaml``. CLI commands
that operate on a specific mind expose an optional ``--mind-id`` flag;
this module is the single source of truth for resolving that flag value
to a concrete, existing mind on disk.

Resolution contract:

* ``cli_arg`` is a non-empty string:
    - ``<data_dir>/<cli_arg>/mind.yaml`` exists → ``MindId(cli_arg)``.
    - Else → ``typer.BadParameter`` listing the available minds.
* ``cli_arg`` is ``None`` (flag omitted):
    - 0 minds available → ``typer.BadParameter`` pointing at ``sovyx init``.
    - 1 mind available → that mind (structured INFO log ``cli.mind_auto_detected``).
    - 2+ minds available → ``typer.BadParameter`` asking for explicit ``--mind-id``.

Empty / whitespace-only string is rejected explicitly so callers cannot
silently fall through to the auto-detect branch (anti-pattern #35 closure).

History: introduced 2026-05-13 to close the 6th surface of anti-pattern
#35 (cross-layer sentinel defaults) in the Sovyx voice/CLI flow. Mission:
``docs-internal/archive/missions-completed/MISSION-voice-config-calibrate-enterprise-2026-05-13.md``
§4 Phase 1 (T1.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from sovyx.engine.types import MindId
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


def enumerate_minds(data_dir: Path) -> list[MindId]:
    """List every mind that exists on disk under ``data_dir``.

    A mind exists when ``<data_dir>/<name>/mind.yaml`` is a regular file.
    Returns mind ids sorted alphabetically for deterministic CLI output.
    A non-existent or non-directory ``data_dir`` returns an empty list —
    a fresh install has no minds, and that is a valid state, not an
    error.
    """
    if not data_dir.exists() or not data_dir.is_dir():
        return []
    result: list[MindId] = []
    for child in sorted(data_dir.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "mind.yaml").is_file():
            continue
        result.append(MindId(child.name))
    return result


def resolve_mind_id(cli_arg: str | None, data_dir: Path) -> MindId:
    """Resolve a CLI ``--mind-id`` flag value to a concrete, existing mind.

    Args:
        cli_arg: The raw value of ``--mind-id`` from Typer. ``None`` when
            the flag was omitted (CLI commands MUST default the flag to
            ``None``, never to a literal string sentinel like
            ``"default"``).
        data_dir: The Sovyx data directory (typically
            ``Path.home() / ".sovyx"`` or
            ``EngineConfig().database.data_dir``).

    Returns:
        The resolved :class:`~sovyx.engine.types.MindId` whose
        ``<data_dir>/<mind_id>/mind.yaml`` file exists on disk.

    Raises:
        typer.BadParameter: On any unresolvable input, with an
            operator-readable, actionable message. Typer turns this into
            a non-zero exit code with the message rendered on stderr.
    """
    if cli_arg is not None:
        normalized = cli_arg.strip()
        if not normalized:
            raise typer.BadParameter(
                "--mind-id cannot be empty.",
                param_hint="--mind-id",
            )
        candidate = data_dir / normalized / "mind.yaml"
        if candidate.is_file():
            return MindId(normalized)
        available = enumerate_minds(data_dir)
        raise typer.BadParameter(
            _format_missing_mind_message(normalized, available),
            param_hint="--mind-id",
        )

    available = enumerate_minds(data_dir)
    if not available:
        raise typer.BadParameter(
            f"No mind configured under {data_dir}. Run `sovyx init` first to create one.",
            param_hint="--mind-id",
        )
    if len(available) == 1:
        resolved = available[0]
        logger.info(
            "cli.mind_auto_detected",
            mind_id=str(resolved),
            data_dir=str(data_dir),
        )
        return resolved

    raise typer.BadParameter(
        f"Multiple minds found ({_format_mind_list(available)}). Pass --mind-id to disambiguate.",
        param_hint="--mind-id",
    )


def _format_missing_mind_message(requested: str, available: list[MindId]) -> str:
    """Build the ``--mind-id <X>`` not-found error body."""
    if not available:
        return (
            f"Mind {requested!r} not found, and no minds are configured. "
            f"Run `sovyx init` first to create one."
        )
    return f"Mind {requested!r} not found. Available: {_format_mind_list(available)}."


def _format_mind_list(minds: list[MindId]) -> str:
    """Comma-join mind ids alphabetically for operator-readable error output."""
    return ", ".join(sorted(str(m) for m in minds))
