"""Hypothesis property tests for sovyx.dashboard._integrity (Mission C5 §T1.6).

Invariants:

1. For any synthetic HTML with N tracked refs, the report's
   ``len(referenced_assets) == N`` (modulo external-URL filtering).
2. For any synthetic HTML where K refs map to existing files (K ≤ N),
   ``len(missing_assets) == N - K``.
3. Verdict is deterministic in input (idempotent on re-scan).
4. ``scan_duration_ms >= 0.0`` always.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.dashboard._integrity import (
    BundleVerdict,
    scan_bundle_integrity,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# Constrain the alphabet to filesystem-safe characters; vite chunk names
# are [A-Za-z0-9_-] with a content-hash suffix.
_CHUNK_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
_chunk_name = st.text(
    alphabet=_CHUNK_ALPHABET,
    min_size=1,
    max_size=24,
).map(lambda s: f"{s}.js")


def _write_synthetic(static_dir: Path, refs: list[str], assets_present: list[str]) -> None:
    """Write index.html with the given refs and create the listed assets."""
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "assets").mkdir(exist_ok=True)
    for asset in assets_present:
        (static_dir / "assets" / asset).write_text("// ok", encoding="utf-8")
    parts = ["<html><head>"]
    for ref in refs:
        parts.append(f'<link rel="modulepreload" crossorigin href="/assets/{ref}">')
    parts.append("</head><body></body></html>")
    (static_dir / "index.html").write_text("\n".join(parts), encoding="utf-8")


@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(refs=st.lists(_chunk_name, min_size=1, max_size=8, unique_by=lambda s: s.lower()))
def test_referenced_count_matches_input(
    tmp_path_factory: pytest.TempPathFactory, refs: list[str]
) -> None:
    tmp = tmp_path_factory.mktemp("synthetic")
    _write_synthetic(tmp, refs=refs, assets_present=refs)
    report = scan_bundle_integrity(tmp)
    assert len(report.referenced_assets) == len(refs)
    assert report.verdict is BundleVerdict.FULLY_PRESENT


@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(data=st.data())
def test_missing_count_is_complement(
    tmp_path_factory: pytest.TempPathFactory,
    data: st.DataObject,
) -> None:
    refs = data.draw(st.lists(_chunk_name, min_size=2, max_size=8, unique_by=lambda s: s.lower()))
    keep_count = data.draw(st.integers(min_value=0, max_value=len(refs) - 1))
    present = refs[:keep_count]
    tmp = tmp_path_factory.mktemp("partial")
    _write_synthetic(tmp, refs=refs, assets_present=present)
    report = scan_bundle_integrity(tmp)
    expected_missing = len(refs) - keep_count
    assert len(report.missing_assets) == expected_missing
    if expected_missing == 0:
        assert report.verdict is BundleVerdict.FULLY_PRESENT
    else:
        # Could be LEGACY_INDEX_HTML_NO_ASSETS if ALL refs miss AND
        # the assets dir is empty — guard with the assets-present count.
        if keep_count > 0:
            assert report.verdict is BundleVerdict.PARTIAL


@settings(
    max_examples=50,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(refs=st.lists(_chunk_name, min_size=1, max_size=6, unique_by=lambda s: s.lower()))
def test_verdict_idempotent(
    tmp_path_factory: pytest.TempPathFactory,
    refs: list[str],
) -> None:
    tmp = tmp_path_factory.mktemp("idempotent")
    _write_synthetic(tmp, refs=refs, assets_present=refs)
    r1 = scan_bundle_integrity(tmp)
    r2 = scan_bundle_integrity(tmp)
    assert r1.verdict == r2.verdict
    assert r1.referenced_assets == r2.referenced_assets
    assert r1.missing_assets == r2.missing_assets


@settings(
    max_examples=50,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(refs=st.lists(_chunk_name, min_size=0, max_size=10, unique_by=lambda s: s.lower()))
def test_scan_duration_non_negative(
    tmp_path_factory: pytest.TempPathFactory,
    refs: list[str],
) -> None:
    tmp = tmp_path_factory.mktemp("duration")
    _write_synthetic(tmp, refs=refs, assets_present=refs)
    report = scan_bundle_integrity(tmp)
    assert report.scan_duration_ms >= 0.0
    assert report.scanned_at_monotonic >= 0.0
