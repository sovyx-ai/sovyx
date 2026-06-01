#!/usr/bin/env python3
"""Quality Gate 19 — name-lock integrity (Mission Ω-3, anti-pattern #68 DRAFT).

Scans ``src/sovyx`` for path-form references to ``docs-internal/*`` and asserts
each target resolves to an existing file (or is a prefix of one, to tolerate
references wrapped across two comment lines). The gate enforces **developer
working-tree reference integrity**: a docstring that says "see
``docs-internal/X``" must resolve in a fresh checkout. Past archive moves
silently rotted 34 such links (targets relocated to ``archive/`` or never
committed) — this gate makes that rot impossible to reintroduce.

(Separately, the docstring TEXT also ships inside the ``.py`` to PyPI, where
``docs-internal/`` is absent entirely — that broader limitation is why CLAUDE.md
prefers a public ``docs/`` target for consumer-facing references. Gate 19 does
not fix that; it guarantees the LOCAL path is at least correct.)

Bare spec-ID citations that carry NO ``docs-internal/`` prefix (e.g.
``IMPL-OBSERVABILITY-001 §7 Task 1.6``) are historical provenance, not path
links, and are deliberately NOT flagged.

Staged adoption (CLAUDE.md North Star §3): LENIENT v0.49.x — STRICT at v0.52.0.

Exit 0 = no dead path links. Exit 1 = at least one dead link (the verify_gates.sh
wrapper treats this as a LENIENT warn until the STRICT flip).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "sovyx"

# Path-form reference: must carry the docs-internal/ prefix + a path body.
_REF_RE = re.compile(r"docs-internal/[A-Za-z0-9_./-]+")

# File types whose comments/docstrings can carry doc references.
_EXTS = {".py", ".pyi", ".yaml", ".yml", ".sh", ".md", ".txt", ".cfg", ".toml"}


def _normalize(token: str) -> str:
    """Strip trailing chars that can never end a real docs-internal path.

    A valid target ends in an alphanumeric (``...md``) — trailing ``.`` (sentence
    period), ``/``, or ``-`` (line-wrap break) are not part of the filename.
    """
    return token.rstrip("./-")


def _resolves(rel: str) -> bool:
    """True if ``rel`` is an existing file OR the prefix of one (wrapped ref)."""
    target = ROOT / rel
    if target.exists():
        return True
    parent = target.parent
    stem = target.name
    if parent.is_dir():
        return any(child.name.startswith(stem) for child in parent.iterdir())
    return False


def main() -> int:
    violations: list[tuple[str, int, str]] = []
    for path in SRC.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _EXTS:
            continue
        if "__pycache__" in path.parts:
            continue
        # Signed KB profiles (_mixer_kb/profiles/*.yaml) are cryptographically
        # immutable — editing a comment breaks the Ed25519 signature
        # (anti-pattern #26). Their doc references can't be repaired in place
        # (would require re-signing with the private key), so they are out of
        # scope for this docstring-link gate. The referenced docs still exist
        # in archive/ — only the in-file path string is stale-by-archive-move.
        if (
            path.suffix.lower() in {".yaml", ".yml"}
            and "_mixer_kb" in path.parts
            and "profiles" in path.parts
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _REF_RE.findall(line):
                rel = _normalize(match)
                if not _resolves(rel):
                    violations.append((str(path.relative_to(ROOT)), lineno, rel))

    if violations:
        print(f"Quality Gate 19 — name-lock integrity: {len(violations)} violation(s)")
        for rel, lineno, ref in violations:
            print(f"  {rel}:{lineno} -> {ref} (target missing)")
        return 1

    print(
        "Quality Gate 19 — name-lock integrity: PASS "
        "(every docs-internal path reference in src/sovyx resolves)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
