"""Docs site smoke tests — V05-P09.

Verifies that MkDocs builds successfully and key pages exist.

The build-output assertions run a fresh ``mkdocs build`` via a
class-scoped fixture instead of trusting whatever ``site/`` happens to
be on disk: ``site/`` is gitignored, so a stale local build can keep
fossil pages alive for months and make assertions pass against a layout
the current nav no longer produces (this happened — the suite asserted
``site/quickstart`` et al. long after those pages were removed).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SITE_DIR = REPO_ROOT / "site"
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"


class TestDocsStructure:
    """Verify docs source files exist."""

    def test_mkdocs_config_exists(self) -> None:
        """MkDocs config must be at repo root."""
        assert MKDOCS_YML.exists(), "mkdocs.yml not found at repo root"

    def test_docs_directory_exists(self) -> None:
        """Docs source directory must exist."""
        assert DOCS_DIR.is_dir(), "docs/ directory not found"

    @pytest.mark.parametrize(
        "page",
        [
            "index.md",
            "getting-started.md",
            "architecture.md",
            "api-reference.md",
            "configuration.md",
            "security.md",
        ],
    )
    def test_key_pages_exist(self, page: str) -> None:
        """Key doc pages must exist in docs/."""
        assert (DOCS_DIR / page).exists(), f"docs/{page} not found"


try:
    _has_mkdocs = subprocess.run(["mkdocs", "--version"], capture_output=True).returncode == 0  # noqa: S603, S607
except FileNotFoundError:
    _has_mkdocs = False


@pytest.mark.skipif(not _has_mkdocs, reason="mkdocs not installed")
class TestMkDocsBuild:
    """Verify MkDocs builds without errors and produces the real layout."""

    @pytest.fixture(scope="class", autouse=True)
    def _fresh_build(self) -> None:
        """Build the site once for this class so assertions never read a stale ``site/``."""
        result = subprocess.run(  # noqa: S603
            ["mkdocs", "build", "--strict"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"mkdocs build failed:\nstdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
        )

    def test_site_index_generated(self) -> None:
        """Build must produce site/index.html — without it the docs-site root 404s."""
        assert (SITE_DIR / "index.html").exists(), "site/index.html not generated"

    @pytest.mark.parametrize(
        "page",
        ["getting-started", "api-reference", "modules/voice", "modules/plugins"],
    )
    def test_key_pages_generated(self, page: str) -> None:
        """Each key nav page must produce a site HTML output."""
        page_path = SITE_DIR / page / "index.html"
        assert page_path.exists(), f"site/{page}/index.html not generated"
