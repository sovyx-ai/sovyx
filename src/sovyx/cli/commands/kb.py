"""``sovyx kb`` — inspect and validate the mixer-profile Knowledge Base.

Four subcommands cover the contribution workflow end-to-end without
needing a live PortAudio / ALSA environment:

* ``sovyx kb list``           — enumerate shipped + user-pool profiles.
* ``sovyx kb inspect <id>``   — pretty-print a single profile's fields.
* ``sovyx kb validate <path>``— schema + sanity check a contributor's
  YAML before they open a PR. Same loader the daemon uses at boot, so a
  local ``validate`` pass is the authoritative answer to "will this
  parse?".
* ``sovyx kb fixtures <id>``  — verify that the HIL attestation
  fixtures (before / after amixer dumps + capture WAV) referenced by
  the profile actually exist at the expected paths. CI uses the same
  logic via the mirror test ``test_kb_fixture_consistency.py``; this
  CLI path is the authoritative local check for contributors.

Design notes:

* Read-only — every command is a pure inspection/validation step. No
  writes to shipped profiles, no subprocess side effects, no outbound
  network.
* No PortAudio / ALSA dependency — the commands import only the KB
  loader/schema, not the mixer apply layer. That means contributors on
  Windows / macOS (reviewing a Linux-only contribution) can still run
  ``sovyx kb validate`` against the YAML.
* Deterministic exit codes — ``0`` success, ``1`` validation/lookup
  failure, ``2`` filesystem or argument error. CI gates can tell the
  difference without stdout-scraping.

See V2 Master Plan Part F.3 and ADR-voice-mixer-sanity-l2.5-bidirectional.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from sovyx.voice.health._mixer_kb import _SHIPPED_PROFILES_DIR
from sovyx.voice.health._mixer_kb.loader import (
    load_profile_file,
    load_profiles_from_directory,
)

if TYPE_CHECKING:
    from sovyx.voice.health.contract import MixerKBProfile

console = Console()

kb_app = typer.Typer(
    name="kb",
    help="Inspect and validate the mixer-profile Knowledge Base.",
    no_args_is_help=True,
)


_EXIT_OK = 0
_EXIT_VALIDATION_FAILED = 1
_EXIT_USAGE_ERROR = 2


# The fixture layout documented in V2 Master Plan §F.3: every KB
# profile references three HIL fixtures — a pre-heal amixer dump, a
# post-heal amixer dump, and a validation capture WAV. They live
# under ``tests/fixtures/voice/mixer/`` keyed by profile_id.
_FIXTURES_ROOT_DEFAULT = Path("tests/fixtures/voice/mixer")
_FIXTURE_SUFFIXES: tuple[str, ...] = ("_before.txt", "_after.txt", "_capture.wav")


def _default_user_dir() -> Path:
    """User-pool directory — ``~/.sovyx/mixer_kb/user/``."""
    return Path.home() / ".sovyx" / "mixer_kb" / "user"


# ── sovyx kb list ──────────────────────────────────────────────────


@kb_app.command("list")
def kb_list(
    *,
    user_dir: Annotated[
        Path | None,
        typer.Option(
            "--user-dir",
            help=("Override the user-pool directory. Defaults to ~/.sovyx/mixer_kb/user/."),
        ),
    ] = None,
    shipped_only: Annotated[
        bool,
        typer.Option(
            "--shipped-only",
            help="Skip the user pool and list only bundled profiles.",
        ),
    ] = False,
) -> None:
    """List every profile with identity + match-scope + provenance."""
    shipped_profiles = load_profiles_from_directory(_SHIPPED_PROFILES_DIR)
    user_profiles: list[MixerKBProfile] = []
    if not shipped_only:
        target_user_dir = user_dir if user_dir is not None else _default_user_dir()
        if target_user_dir.exists():
            user_profiles = load_profiles_from_directory(target_user_dir)

    if not shipped_profiles and not user_profiles:
        console.print("[dim]No mixer-KB profiles loaded.[/dim]")
        if not shipped_profiles:
            console.print(
                f"  [dim]shipped dir:[/dim] {_SHIPPED_PROFILES_DIR}  [dim](empty)[/dim]",
            )
        if not shipped_only and user_profiles == []:
            user_hint = user_dir if user_dir is not None else _default_user_dir()
            console.print(f"  [dim]user dir:[/dim]    {user_hint}  [dim](empty or missing)[/dim]")
        raise typer.Exit(code=_EXIT_OK)

    table = Table(title="Mixer KB Profiles")
    table.add_column("Pool", style="yellow", no_wrap=True)
    table.add_column("Profile ID", style="cyan")
    table.add_column("Ver", justify="right", style="green")
    table.add_column("Driver", style="blue")
    table.add_column("Codec", style="magenta")
    table.add_column("Threshold", justify="right")
    table.add_column("Contributor")

    for profile in shipped_profiles:
        _add_row(table, "shipped", profile)
    for profile in user_profiles:
        _add_row(table, "user", profile)

    console.print(table)
    console.print(
        f"[dim]{len(shipped_profiles)} shipped, {len(user_profiles)} user[/dim]",
    )


def _add_row(table: Table, pool: str, profile: MixerKBProfile) -> None:
    table.add_row(
        pool,
        profile.profile_id,
        str(profile.profile_version),
        profile.driver_family,
        profile.codec_id_glob,
        f"{profile.match_threshold:.2f}",
        profile.contributed_by,
    )


# ── sovyx kb inspect ───────────────────────────────────────────────


@kb_app.command("inspect")
def kb_inspect(
    profile_id: Annotated[
        str,
        typer.Argument(help="profile_id to inspect (as it appears in `sovyx kb list`)."),
    ],
    *,
    user_dir: Annotated[
        Path | None,
        typer.Option(
            "--user-dir",
            help="Override the user-pool directory.",
        ),
    ] = None,
) -> None:
    """Print a single profile's fields in human-readable form."""
    target_user_dir = user_dir if user_dir is not None else _default_user_dir()
    all_profiles: list[tuple[str, MixerKBProfile]] = []
    for profile in load_profiles_from_directory(_SHIPPED_PROFILES_DIR):
        all_profiles.append(("shipped", profile))
    if target_user_dir.exists():
        for profile in load_profiles_from_directory(target_user_dir):
            all_profiles.append(("user", profile))

    matches = [(pool, p) for pool, p in all_profiles if p.profile_id == profile_id]
    if not matches:
        console.print(f"[red]No profile with id {profile_id!r} found.[/red]")
        raise typer.Exit(code=_EXIT_VALIDATION_FAILED)

    for pool, profile in matches:
        _print_profile(pool, profile)


def _print_profile(pool: str, profile: MixerKBProfile) -> None:
    console.print(f"[bold cyan]{profile.profile_id}[/bold cyan] [dim]({pool} pool)[/dim]")
    rows = (
        ("profile_version", str(profile.profile_version)),
        ("schema_version", str(profile.schema_version)),
        ("driver_family", profile.driver_family),
        ("codec_id_glob", profile.codec_id_glob),
        ("system_vendor_glob", profile.system_vendor_glob or "—"),
        ("system_product_glob", profile.system_product_glob or "—"),
        ("distro_family", profile.distro_family or "—"),
        ("audio_stack", profile.audio_stack or "—"),
        ("kernel_major_minor_glob", profile.kernel_major_minor_glob or "—"),
        ("match_threshold", f"{profile.match_threshold:.3f}"),
        ("factory_regime", profile.factory_regime),
        ("factory_signature_roles", ", ".join(r.name for r in profile.factory_signature)),
        ("verified_on_count", str(len(profile.verified_on))),
        ("contributed_by", profile.contributed_by),
    )
    for key, value in rows:
        console.print(f"  [yellow]{key}[/yellow]: {value}")


# ── sovyx kb validate ──────────────────────────────────────────────


@kb_app.command("validate")
def kb_validate(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path to a candidate profile YAML. Validated against the "
                "shipping loader, which is the authoritative schema the "
                "daemon uses at boot."
            ),
            exists=False,
            dir_okay=False,
        ),
    ],
) -> None:
    """Validate a candidate YAML against the KB schema.

    Exit codes:

    * ``0`` — YAML parsed, schema accepted, ``profile_id`` matches
      filename stem, semantic rules upheld.
    * ``1`` — validation failure; the detailed error is printed.
    * ``2`` — filesystem / argument problem (file missing, not a
      file, not readable).
    """
    if not path.exists():
        console.print(f"[red]File not found:[/red] {path}")
        raise typer.Exit(code=_EXIT_USAGE_ERROR)
    if not path.is_file():
        console.print(f"[red]Not a file:[/red] {path}")
        raise typer.Exit(code=_EXIT_USAGE_ERROR)

    try:
        profile = load_profile_file(path)
    except ValidationError as exc:
        console.print(f"[red]Schema validation failed[/red] for {path.name}:")
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            console.print(f"  [yellow]{loc}[/yellow]: {err.get('msg')}")
        raise typer.Exit(code=_EXIT_VALIDATION_FAILED) from exc
    except yaml.YAMLError as exc:
        console.print(f"[red]YAML malformed[/red] in {path.name}: {exc}")
        raise typer.Exit(code=_EXIT_VALIDATION_FAILED) from exc
    except ValueError as exc:
        console.print(f"[red]Validation error[/red] in {path.name}: {exc}")
        raise typer.Exit(code=_EXIT_VALIDATION_FAILED) from exc
    except OSError as exc:
        console.print(f"[red]Read failed[/red] for {path}: {exc}")
        raise typer.Exit(code=_EXIT_USAGE_ERROR) from exc

    console.print(f"[green]OK[/green] — {profile.profile_id} v{profile.profile_version}")
    console.print(f"  driver_family: {profile.driver_family}")
    console.print(f"  codec_id_glob: {profile.codec_id_glob}")
    console.print(
        f"  verified_on:   {len(profile.verified_on)} attestation"
        f"{'s' if len(profile.verified_on) != 1 else ''}",
    )


# ── sovyx kb fixtures ──────────────────────────────────────────────


@kb_app.command("fixtures")
def kb_fixtures(
    profile_id: Annotated[
        str,
        typer.Argument(
            help=(
                "profile_id whose HIL fixtures to verify. Use 'all' to "
                "check every shipped profile at once (CI-friendly)."
            ),
        ),
    ],
    *,
    fixtures_root: Annotated[
        Path,
        typer.Option(
            "--fixtures-root",
            help=(
                "Root directory holding the HIL fixtures. Defaults to "
                "tests/fixtures/voice/mixer relative to the repo root."
            ),
        ),
    ] = _FIXTURES_ROOT_DEFAULT,
) -> None:
    """Verify HIL fixture files exist for a profile (or all).

    Each KB profile in the shipping pool must have three companion
    fixtures so reviewers can HIL-replay the contribution:

    * ``<profile_id>_before.txt`` — pre-heal ``amixer -c N contents`` dump
    * ``<profile_id>_after.txt``  — post-heal dump
    * ``<profile_id>_capture.wav``— 3 s validation capture

    Exit codes:

    * ``0`` — every required fixture exists.
    * ``1`` — one or more fixtures missing; the list is printed.
    * ``2`` — argument / filesystem error (fixtures_root missing,
      profile_id not found).
    """
    if not fixtures_root.exists() or not fixtures_root.is_dir():
        console.print(f"[red]Fixtures root missing or not a directory:[/red] {fixtures_root}")
        raise typer.Exit(code=_EXIT_USAGE_ERROR)

    shipped_profiles = load_profiles_from_directory(_SHIPPED_PROFILES_DIR)

    if profile_id == "all":
        if not shipped_profiles:
            console.print("[dim]No shipped profiles to check.[/dim]")
            raise typer.Exit(code=_EXIT_OK)
        profiles_to_check: list[MixerKBProfile] = list(shipped_profiles)
    else:
        match = next((p for p in shipped_profiles if p.profile_id == profile_id), None)
        if match is None:
            console.print(
                f"[red]profile_id {profile_id!r} not found in shipped pool[/red]",
            )
            console.print(
                f"  [dim]shipped dir:[/dim] {_SHIPPED_PROFILES_DIR}",
            )
            raise typer.Exit(code=_EXIT_USAGE_ERROR)
        profiles_to_check = [match]

    total_missing: list[tuple[str, str]] = []
    for profile in profiles_to_check:
        missing = _missing_fixtures(profile.profile_id, fixtures_root)
        if missing:
            for name in missing:
                console.print(
                    f"[red]MISSING[/red] {profile.profile_id}: {fixtures_root / name}",
                )
                total_missing.append((profile.profile_id, name))
        else:
            console.print(f"[green]OK[/green]      {profile.profile_id}")

    if total_missing:
        console.print(
            f"\n[red]{len(total_missing)} fixture"
            f"{'s' if len(total_missing) != 1 else ''} missing across "
            f"{len({pid for pid, _ in total_missing})} profile"
            f"{'s' if len({pid for pid, _ in total_missing}) != 1 else ''}.[/red]",
        )
        raise typer.Exit(code=_EXIT_VALIDATION_FAILED)
    console.print(
        f"\n[green]{len(profiles_to_check)} profile"
        f"{'s' if len(profiles_to_check) != 1 else ''} fixture-complete.[/green]",
    )


def _missing_fixtures(profile_id: str, fixtures_root: Path) -> list[str]:
    """Return the fixture filenames missing from ``fixtures_root``.

    Pure helper so the CI test can reuse it without touching typer.
    """
    missing: list[str] = []
    for suffix in _FIXTURE_SUFFIXES:
        name = f"{profile_id}{suffix}"
        if not (fixtures_root / name).is_file():
            missing.append(name)
    return missing


__all__ = ["kb_app"]
