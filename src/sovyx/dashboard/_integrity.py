"""Dashboard SPA bundle integrity scanner.

Mission anchor: ``docs-internal/missions/MISSION-c5-dashboard-distribution-
integrity-2026-05-17.md`` §T1.1.

Parses ``static/index.html``, extracts every ``<script src=>``,
``<link rel="modulepreload" href=>``, ``<link rel="stylesheet" href=>``,
``<link rel="preload" href=>`` reference, and verifies each referenced
asset exists on disk under ``static/``.

Pure-Python, stdlib-only (``html.parser`` + ``pathlib`` + ``time``).
No new pyproject.toml dependencies. Bounded scan time (≤ 100 ms typical
for a ~70-chunk vite bundle), single-pass, no recursion. Anti-pattern
compliance: #9 (StrEnum verdict), #15 (bounded report cardinality —
capped by index.html file size).

Verdict precedence (top-to-bottom; first match wins):

1. ``STATIC_DIR_MISSING``  — ``static_dir`` does not exist OR is not a directory.
2. ``INDEX_HTML_MISSING``  — ``static_dir / "index.html"`` does not exist.
3. ``LEGACY_INDEX_HTML_NO_ASSETS`` — ``static_dir / "assets"`` does not
   exist OR is empty AND ``index.html`` references ≥ 1 asset.
4. ``PARTIAL``             — at least one referenced asset is missing on disk.
5. ``FULLY_PRESENT``       — every referenced asset is present on disk.

Used by:

* :func:`sovyx.dashboard.server.create_app` boot-scan branch (Mission C5 §T2.1).
* :class:`sovyx.dashboard.server._IntegrityAwareStaticFiles` reactive on-404
  arm (§T2.2).
* :mod:`sovyx.cli.commands.dashboard` ``doctor`` subcommand (§T3.3).
* :mod:`sovyx.cli.commands.doctor` ``_render_dashboard_integrity_surface``
  (§T3.4).
* :mod:`scripts.dev.check_dashboard_bundle_integrity` Quality Gate 11 (§T1.2).
"""

from __future__ import annotations

import html.parser
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class BundleVerdict(StrEnum):
    """Five-state classifier for bundle integrity.

    Replaces the two-state ``STATIC_DIR.exists() and (STATIC_DIR /
    "index.html").exists()`` gate at ``dashboard/server.py:585`` with
    explicit, operator-actionable verdicts. Distinguishes the v0.43.1
    operator's case (``PARTIAL`` — index present, chunks missing) from
    the prior catch-all (``INDEX_HTML_MISSING``).

    ``StrEnum`` per anti-pattern #9 (xdist-safe value-based comparison).
    """

    FULLY_PRESENT = "fully_present"
    PARTIAL = "partial"
    INDEX_HTML_MISSING = "index_html_missing"
    STATIC_DIR_MISSING = "static_dir_missing"
    LEGACY_INDEX_HTML_NO_ASSETS = "legacy_index_html_no_assets"


@dataclass(frozen=True, slots=True)
class BundleIntegrityReport:
    """Immutable scanner report.

    Frozen + slotted so consumers cannot mutate canonical state.
    ``referenced_assets`` and ``missing_assets`` are tuples (not lists)
    to keep them hashable + comparable across scans; emitted in
    document order from the HTML parser callback for deterministic
    reproducibility.

    Attributes:
        verdict: Categorical classification per :class:`BundleVerdict`.
        static_dir: Absolute path of the scanned static directory.
        index_html_path: Absolute path of the ``index.html`` (may not
            exist when ``verdict`` is ``INDEX_HTML_MISSING`` /
            ``STATIC_DIR_MISSING``).
        referenced_assets: POSIX-style relative paths (from
            ``static_dir``) referenced by ``index.html``. Empty when
            index.html is unreadable. Document order.
        missing_assets: Subset of ``referenced_assets`` whose target
            file is absent on disk. Document order.
        orphan_assets: Files present in ``static_dir/assets/`` but NOT
            referenced by ``index.html``. Informational only (legitimate
            after a stale build where the bundler emitted a new chunk
            name and left the old one behind).
        scan_duration_ms: Wall-clock duration of the scan in
            milliseconds via :func:`time.perf_counter`. Bounded
            ≤ 100 ms typical for ~70-chunk bundles.
        scanned_at_monotonic: :func:`time.monotonic` at scan start —
            used by the reactive-arm debounce in
            :class:`_IntegrityAwareStaticFiles`.
    """

    verdict: BundleVerdict
    static_dir: Path
    index_html_path: Path
    referenced_assets: tuple[str, ...] = field(default_factory=tuple)
    missing_assets: tuple[str, ...] = field(default_factory=tuple)
    orphan_assets: tuple[str, ...] = field(default_factory=tuple)
    scan_duration_ms: float = 0.0
    scanned_at_monotonic: float = 0.0


# Allowlist of <link rel="..."> values whose href= attribute references a
# bundled asset on disk. Other rels (icon, apple-touch-icon, manifest,
# preconnect, dns-prefetch, alternate, canonical, …) are skipped — they
# either reference root-level files (favicons survive partial-bundle
# damage gracefully) or are pure-metadata attributes.
_TRACKED_LINK_RELS: frozenset[str] = frozenset(
    {
        "modulepreload",
        "stylesheet",
        "preload",
    }
)


class _RefExtractor(html.parser.HTMLParser):
    """Internal SAX-style HTML walker that emits asset references.

    Tracks ``<script src=...>`` (regardless of ``type`` attribute) and
    ``<link rel=R href=...>`` for ``R`` in :data:`_TRACKED_LINK_RELS`.
    Ignores schemed URLs (``http://``, ``https://``, ``data:``, etc.).
    Strips query strings + fragments per spec invariants.

    Permissive parser (``convert_charrefs=True``, default) — survives
    vite's mildly non-canonical attribute quoting without raising.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            for name, value in attrs:
                if name == "src" and value:
                    ref = _normalize_ref(value)
                    if ref is not None:
                        self.refs.append(ref)
                    return
        elif tag == "link":
            rel = None
            href = None
            for name, value in attrs:
                if name == "rel" and value is not None:
                    rel = value.strip().lower()
                elif name == "href" and value:
                    href = value
            if rel is not None and rel in _TRACKED_LINK_RELS and href:
                ref = _normalize_ref(href)
                if ref is not None:
                    self.refs.append(ref)

    # html.parser also fires handle_startendtag for `<link ... />`; route
    # both shapes through the same handler so vite's self-closing tags
    # are captured.
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _normalize_ref(raw: str) -> str | None:
    """Normalize an href/src attribute value into a relative POSIX path,
    or None when the ref points outside the bundle.

    Drops:

    * Schemed URLs (anything containing ``://``).
    * ``data:`` URIs.
    * Empty / whitespace-only refs.
    * Refs that resolve outside ``static_dir`` via ``..`` segments —
      they are returned as-is (caller's ``is_file()`` will yield
      False, classifying them as missing).

    Strips:

    * Leading ``/`` (root-relative refs).
    * Query strings (``?…``).
    * Fragments (``#…``).
    """
    value = raw.strip()
    if not value:
        return None
    # Drop schemed + data URIs.
    if "://" in value or value.startswith("data:"):
        return None
    # Strip query + fragment.
    for sep in ("?", "#"):
        idx = value.find(sep)
        if idx != -1:
            value = value[:idx]
    if not value:
        return None
    # Normalize leading slash.
    return value.lstrip("/")


def _list_assets_on_disk(assets_dir: Path) -> tuple[str, ...]:
    """Enumerate files under ``assets/`` (one level deep) as POSIX-style
    relative refs.

    Used to compute ``orphan_assets``. Returns an empty tuple if the
    directory does not exist OR is empty (the caller distinguishes the
    two cases via :func:`Path.is_dir`).
    """
    if not assets_dir.is_dir():
        return ()
    files: list[str] = []
    for child in sorted(assets_dir.iterdir()):
        if child.is_file():
            files.append(f"assets/{child.name}")
    return tuple(files)


def scan_bundle_integrity(static_dir: Path) -> BundleIntegrityReport:
    """Scan ``static_dir`` for SPA bundle integrity.

    Pure function: no global state, no side effects beyond the
    file-existence check via :meth:`pathlib.Path.is_file`. Bounded
    duration (single-pass HTML parse + N file-existence checks where
    N is the number of refs in index.html — typically ~70).

    Args:
        static_dir: Absolute or relative path to the static directory
            (typically ``src/sovyx/dashboard/static/`` for the
            checked-out tree, OR the runtime ``STATIC_DIR`` for the
            install-time probe).

    Returns:
        :class:`BundleIntegrityReport` with one of the five verdicts
        per the precedence rules in the module docstring.
    """
    started = time.perf_counter()
    started_monotonic = time.monotonic()
    static_dir_abs = static_dir.resolve() if static_dir.exists() else static_dir.absolute()
    index_html_path = static_dir_abs / "index.html"
    assets_dir = static_dir_abs / "assets"

    # Verdict 1: STATIC_DIR_MISSING.
    if not static_dir.exists() or not static_dir.is_dir():
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return BundleIntegrityReport(
            verdict=BundleVerdict.STATIC_DIR_MISSING,
            static_dir=static_dir_abs,
            index_html_path=index_html_path,
            scan_duration_ms=elapsed_ms,
            scanned_at_monotonic=started_monotonic,
        )

    # Verdict 2: INDEX_HTML_MISSING.
    if not index_html_path.is_file():
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return BundleIntegrityReport(
            verdict=BundleVerdict.INDEX_HTML_MISSING,
            static_dir=static_dir_abs,
            index_html_path=index_html_path,
            scan_duration_ms=elapsed_ms,
            scanned_at_monotonic=started_monotonic,
        )

    # Parse index.html — best-effort; permissive parser tolerates the
    # mild non-canonicality of vite's emitted HTML.
    try:
        index_text = index_html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Stat said is_file() but read failed (race / permission). Treat
        # as INDEX_HTML_MISSING — the operator's surface is identical.
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return BundleIntegrityReport(
            verdict=BundleVerdict.INDEX_HTML_MISSING,
            static_dir=static_dir_abs,
            index_html_path=index_html_path,
            scan_duration_ms=elapsed_ms,
            scanned_at_monotonic=started_monotonic,
        )

    extractor = _RefExtractor()
    extractor.feed(index_text)
    extractor.close()
    referenced = tuple(extractor.refs)

    # Verdict 3: LEGACY_INDEX_HTML_NO_ASSETS — index references ≥ 1
    # asset but the assets/ directory is missing OR empty. Distinct from
    # PARTIAL: PARTIAL means SOME refs resolve, this means NONE can.
    assets_under_dir = [r for r in referenced if r.startswith("assets/")]
    assets_dir_exists_and_nonempty = assets_dir.is_dir() and any(assets_dir.iterdir())
    if assets_under_dir and not assets_dir_exists_and_nonempty:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return BundleIntegrityReport(
            verdict=BundleVerdict.LEGACY_INDEX_HTML_NO_ASSETS,
            static_dir=static_dir_abs,
            index_html_path=index_html_path,
            referenced_assets=referenced,
            missing_assets=tuple(assets_under_dir),
            scan_duration_ms=elapsed_ms,
            scanned_at_monotonic=started_monotonic,
        )

    # Verdict 4 / 5: PARTIAL vs FULLY_PRESENT.
    missing: list[str] = []
    for ref in referenced:
        candidate = (static_dir_abs / ref).resolve()
        # Path-traversal defense: if the ref resolves outside static_dir,
        # treat as missing rather than admitting cross-tree files.
        try:
            candidate.relative_to(static_dir_abs)
        except ValueError:
            missing.append(ref)
            continue
        if not candidate.is_file():
            missing.append(ref)

    orphans = _list_assets_on_disk(assets_dir)
    referenced_set = set(referenced)
    orphan_assets = tuple(o for o in orphans if o not in referenced_set)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    verdict = BundleVerdict.PARTIAL if missing else BundleVerdict.FULLY_PRESENT
    return BundleIntegrityReport(
        verdict=verdict,
        static_dir=static_dir_abs,
        index_html_path=index_html_path,
        referenced_assets=referenced,
        missing_assets=tuple(missing),
        orphan_assets=orphan_assets,
        scan_duration_ms=elapsed_ms,
        scanned_at_monotonic=started_monotonic,
    )


__all__ = [
    "BundleIntegrityReport",
    "BundleVerdict",
    "scan_bundle_integrity",
]
