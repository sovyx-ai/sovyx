"""Unit tests for sovyx.dashboard._integrity (Mission C5 §T1.5)."""

from __future__ import annotations

import shutil
from pathlib import Path

from sovyx.dashboard._integrity import (
    BundleVerdict,
    scan_bundle_integrity,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HEAD_STATIC_DIR = REPO_ROOT / "src" / "sovyx" / "dashboard" / "static"


def _copy_head_bundle_to(tmp: Path) -> Path:
    """Materialize a snapshot of the committed head bundle under tmp."""
    fixture = tmp / "static"
    shutil.copytree(HEAD_STATIC_DIR, fixture)
    return fixture


def _write_index_html(static_dir: Path, body: str) -> None:
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text(body, encoding="utf-8")


class TestVerdictResolution:
    """Verdict precedence cases per the module docstring."""

    def test_fully_present_at_head(self, tmp_path: Path) -> None:
        fixture = _copy_head_bundle_to(tmp_path)
        report = scan_bundle_integrity(fixture)
        assert report.verdict is BundleVerdict.FULLY_PRESENT
        assert report.missing_assets == ()
        assert len(report.referenced_assets) >= 30  # the HEAD bundle preloads ~40 chunks

    def test_static_dir_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "nonexistent"
        report = scan_bundle_integrity(target)
        assert report.verdict is BundleVerdict.STATIC_DIR_MISSING
        assert report.referenced_assets == ()
        assert report.missing_assets == ()

    def test_static_dir_is_file_not_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "actually_a_file"
        target.write_text("not a dir", encoding="utf-8")
        report = scan_bundle_integrity(target)
        assert report.verdict is BundleVerdict.STATIC_DIR_MISSING

    def test_index_html_missing(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "assets").mkdir()
        report = scan_bundle_integrity(static_dir)
        assert report.verdict is BundleVerdict.INDEX_HTML_MISSING
        assert report.referenced_assets == ()

    def test_legacy_index_html_no_assets_dir(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><script src="/assets/main.js"></script></head></html>',
        )
        # No assets/ directory exists.
        report = scan_bundle_integrity(static_dir)
        assert report.verdict is BundleVerdict.LEGACY_INDEX_HTML_NO_ASSETS
        assert "assets/main.js" in report.missing_assets

    def test_legacy_assets_dir_empty(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><link rel="modulepreload" href="/assets/x.js"></head></html>',
        )
        (static_dir / "assets").mkdir()
        report = scan_bundle_integrity(static_dir)
        assert report.verdict is BundleVerdict.LEGACY_INDEX_HTML_NO_ASSETS

    def test_partial_one_chunk_missing(self, tmp_path: Path) -> None:
        fixture = _copy_head_bundle_to(tmp_path)
        # Find a chunk that index.html ACTUALLY references (some assets
        # on disk are stale orphans from prior builds — deleting them
        # would not cause PARTIAL). Scan first to obtain the real list.
        baseline = scan_bundle_integrity(fixture)
        assert baseline.verdict is BundleVerdict.FULLY_PRESENT
        assert baseline.referenced_assets, "fixture sanity"
        target = baseline.referenced_assets[0]
        (fixture / target).unlink()
        report = scan_bundle_integrity(fixture)
        assert report.verdict is BundleVerdict.PARTIAL
        assert target in report.missing_assets

    def test_partial_all_chunks_missing(self, tmp_path: Path) -> None:
        fixture = _copy_head_bundle_to(tmp_path)
        for asset in (fixture / "assets").iterdir():
            if asset.is_file():
                asset.unlink()
        # assets/ still exists as an empty dir; index.html references
        # chunks under it. Empty assets/ → LEGACY_INDEX_HTML_NO_ASSETS
        # per precedence (assets_dir empty AND ≥1 referenced asset).
        report = scan_bundle_integrity(fixture)
        assert report.verdict is BundleVerdict.LEGACY_INDEX_HTML_NO_ASSETS


class TestReferenceExtraction:
    """The HTML parser walks edge cases produced by real bundlers."""

    def test_script_src_extracted(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            "<html><head>"
            '<script type="module" crossorigin src="/assets/x.js"></script>'
            "</head></html>",
        )
        (static_dir / "assets").mkdir()
        (static_dir / "assets" / "x.js").write_text("// ok", encoding="utf-8")
        report = scan_bundle_integrity(static_dir)
        assert "assets/x.js" in report.referenced_assets
        assert report.verdict is BundleVerdict.FULLY_PRESENT

    def test_modulepreload_extracted(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            "<html><head>"
            '<link rel="modulepreload" crossorigin href="/assets/m.js" />'
            "</head></html>",
        )
        (static_dir / "assets").mkdir()
        (static_dir / "assets" / "m.js").write_text("// ok", encoding="utf-8")
        report = scan_bundle_integrity(static_dir)
        assert "assets/m.js" in report.referenced_assets

    def test_stylesheet_extracted(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><link rel="stylesheet" crossorigin href="/assets/s.css" /></head></html>',
        )
        (static_dir / "assets").mkdir()
        (static_dir / "assets" / "s.css").write_text("/* ok */", encoding="utf-8")
        report = scan_bundle_integrity(static_dir)
        assert "assets/s.css" in report.referenced_assets

    def test_preload_extracted(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><link rel="preload" as="font" href="/assets/font.woff2" /></head></html>',
        )
        (static_dir / "assets").mkdir()
        (static_dir / "assets" / "font.woff2").write_bytes(b"font-bytes")
        report = scan_bundle_integrity(static_dir)
        assert "assets/font.woff2" in report.referenced_assets

    def test_icon_link_skipped(self, tmp_path: Path) -> None:
        """rel='icon' is informational — not tracked, missing favicon
        does NOT trigger PARTIAL verdict."""
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><link rel="icon" type="image/svg+xml" href="/icon.svg" /></head></html>',
        )
        # No icon.svg on disk — but we don't track icon refs.
        report = scan_bundle_integrity(static_dir)
        assert report.verdict is BundleVerdict.FULLY_PRESENT
        assert "icon.svg" not in report.referenced_assets

    def test_meta_content_skipped(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><meta property="og:image" content="/og.png" /></head></html>',
        )
        report = scan_bundle_integrity(static_dir)
        assert report.verdict is BundleVerdict.FULLY_PRESENT

    def test_external_links_ignored(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            "<html><head>"
            '<script src="https://cdn.example.com/lib.js"></script>'
            '<link rel="stylesheet" href="http://cdn.example.com/x.css">'
            "</head></html>",
        )
        report = scan_bundle_integrity(static_dir)
        assert report.verdict is BundleVerdict.FULLY_PRESENT
        assert report.referenced_assets == ()

    def test_data_uri_refs_ignored(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><link rel="preload" href="data:image/svg+xml;base64,XYZ"></head></html>',
        )
        report = scan_bundle_integrity(static_dir)
        assert report.verdict is BundleVerdict.FULLY_PRESENT

    def test_preserves_query_strings(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><link rel="modulepreload" href="/assets/x.js?v=1"></head></html>',
        )
        (static_dir / "assets").mkdir()
        (static_dir / "assets" / "x.js").write_text("// ok", encoding="utf-8")
        report = scan_bundle_integrity(static_dir)
        assert "assets/x.js" in report.referenced_assets
        assert report.verdict is BundleVerdict.FULLY_PRESENT

    def test_preserves_fragments(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><link rel="modulepreload" href="/assets/x.js#section"></head></html>',
        )
        (static_dir / "assets").mkdir()
        (static_dir / "assets" / "x.js").write_text("// ok", encoding="utf-8")
        report = scan_bundle_integrity(static_dir)
        assert "assets/x.js" in report.referenced_assets
        assert report.verdict is BundleVerdict.FULLY_PRESENT

    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        """Refs that resolve outside static_dir are counted as missing,
        never admitted as legitimate."""
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><script src="/../etc/passwd"></script></head></html>',
        )
        (static_dir / "assets").mkdir()
        report = scan_bundle_integrity(static_dir)
        # The ref resolves outside static_dir; classified as missing.
        assert "../etc/passwd" in report.missing_assets


class TestOrphanDetection:
    def test_orphan_assets_informational(self, tmp_path: Path) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            '<html><head><script src="/assets/main.js"></script></head></html>',
        )
        (static_dir / "assets").mkdir()
        (static_dir / "assets" / "main.js").write_text("// ok", encoding="utf-8")
        # Orphan: a stale chunk left from a prior build.
        (static_dir / "assets" / "stale-CHUNK.js").write_text("// stale", encoding="utf-8")
        report = scan_bundle_integrity(static_dir)
        assert report.verdict is BundleVerdict.FULLY_PRESENT
        assert "assets/stale-CHUNK.js" in report.orphan_assets


class TestBoundedDuration:
    def test_scan_duration_bounded(self, tmp_path: Path) -> None:
        fixture = _copy_head_bundle_to(tmp_path)
        report = scan_bundle_integrity(fixture)
        # 100ms ceiling per spec invariant; allow a comfortable headroom
        # so the test is not flaky on heavily-loaded CI cells.
        assert report.scan_duration_ms < 500.0, (
            f"scan took {report.scan_duration_ms:.2f}ms (> 500ms ceiling)"
        )

    def test_deterministic_ordering(self, tmp_path: Path) -> None:
        fixture = _copy_head_bundle_to(tmp_path)
        r1 = scan_bundle_integrity(fixture)
        r2 = scan_bundle_integrity(fixture)
        assert r1.verdict == r2.verdict
        assert r1.referenced_assets == r2.referenced_assets
        assert r1.missing_assets == r2.missing_assets


class TestRefNormalization:
    """Mission C5 §9.7 — coverage closure for ``_normalize_ref`` edge cases."""

    def test_whitespace_only_ref_yields_none(self, tmp_path: Path) -> None:
        """``<script src="  ">`` (whitespace only) is rejected by the
        normaliser — verified by absence from the report's referenced
        list.
        """
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            "<html><head>"
            '<script src="  "></script>'
            '<script src="/assets/x.js"></script>'
            "</head></html>",
        )
        (static_dir / "assets").mkdir()
        (static_dir / "assets" / "x.js").write_text("// ok", encoding="utf-8")
        report = scan_bundle_integrity(static_dir)
        # Only the real chunk is referenced; the whitespace-only src is
        # filtered before reaching the disk-existence check.
        assert report.referenced_assets == ("assets/x.js",)
        assert report.verdict is BundleVerdict.FULLY_PRESENT

    def test_pure_query_string_ref_yields_none(self, tmp_path: Path) -> None:
        """``href="?just-query"`` strips to empty and is dropped — covers
        the second ``if not value: return None`` in ``_normalize_ref``."""
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            "<html><head>"
            '<link rel="modulepreload" href="?q=1">'
            '<link rel="modulepreload" href="#fragment-only">'
            "</head></html>",
        )
        report = scan_bundle_integrity(static_dir)
        # Both refs strip to empty and are dropped.
        assert report.referenced_assets == ()
        assert report.verdict is BundleVerdict.FULLY_PRESENT

    def test_script_without_src_ignored(self, tmp_path: Path) -> None:
        """Inline ``<script>`` without ``src=`` is skipped without adding
        a ref — covers the early-exit branch in ``handle_starttag``."""
        static_dir = tmp_path / "static"
        _write_index_html(
            static_dir,
            "<html><head>"
            '<script>console.log("inline");</script>'
            '<script src="/assets/x.js"></script>'
            "</head></html>",
        )
        (static_dir / "assets").mkdir()
        (static_dir / "assets" / "x.js").write_text("// ok", encoding="utf-8")
        report = scan_bundle_integrity(static_dir)
        assert report.referenced_assets == ("assets/x.js",)
        assert report.verdict is BundleVerdict.FULLY_PRESENT


class TestReadTextOSError:
    """Mission C5 §9.7 — coverage for the ``OSError`` branch in
    :func:`scan_bundle_integrity` when ``Path.is_file()`` returns True
    but ``read_text`` fails (race / permission denied mid-stat-and-read).
    """

    def test_read_text_oserror_treats_as_index_html_missing(
        self,
        tmp_path: Path,
        monkeypatch: __import__("pytest").MonkeyPatch,
    ) -> None:
        static_dir = tmp_path / "static"
        _write_index_html(static_dir, "<html></html>")
        (static_dir / "assets").mkdir()

        # Patch Path.read_text to raise OSError on the index.html path
        # ONLY — leave other reads intact (none happen during scan).
        original_read_text = Path.read_text
        index_html_path = (static_dir / "index.html").resolve()

        def _raising_read_text(
            self: Path,
            *args: object,
            **kwargs: object,
        ) -> str:
            if self.resolve() == index_html_path:
                raise OSError("simulated read failure (race / permission)")
            return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", _raising_read_text)
        report = scan_bundle_integrity(static_dir)
        # OSError branch → treat as INDEX_HTML_MISSING per the spec
        # (the operator's surface is identical for both cases).
        assert report.verdict is BundleVerdict.INDEX_HTML_MISSING


class TestReportSerialization:
    """The frozen dataclass returns stable references consumers can rely on."""

    def test_report_is_frozen(self, tmp_path: Path) -> None:
        fixture = _copy_head_bundle_to(tmp_path)
        report = scan_bundle_integrity(fixture)
        # Frozen dataclass — assignment raises.
        import dataclasses

        assert dataclasses.is_dataclass(report)
        try:
            report.verdict = BundleVerdict.PARTIAL  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            return
        raise AssertionError("BundleIntegrityReport must be frozen")

    def test_report_type_is_immutable_tuples(self, tmp_path: Path) -> None:
        fixture = _copy_head_bundle_to(tmp_path)
        report = scan_bundle_integrity(fixture)
        assert isinstance(report.referenced_assets, tuple)
        assert isinstance(report.missing_assets, tuple)
        assert isinstance(report.orphan_assets, tuple)
