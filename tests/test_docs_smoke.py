"""Docs site smoke tests — V05-P09.

Verifies that MkDocs builds successfully and key pages exist.
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
        ["index.md", "quickstart.md", "api.md", "voice.md", "plugins.md"],
    )
    def test_key_pages_exist(self, page: str) -> None:
        """Key doc pages must exist in docs/."""
        assert (DOCS_DIR / page).exists(), f"docs/{page} not found"


class TestMkDocsBuild:
    """Verify MkDocs builds without errors."""

    def test_mkdocs_build_succeeds(self) -> None:
        """MkDocs must build with --strict (zero warnings)."""
        result = subprocess.run(
            ["mkdocs", "build", "--strict"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"mkdocs build failed:\nstdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )

    def test_site_index_generated(self) -> None:
        """Build must produce site/index.html."""
        assert (SITE_DIR / "index.html").exists(), "site/index.html not generated"

    @pytest.mark.parametrize(
        "page",
        ["quickstart", "api", "voice", "plugins"],
    )
    def test_key_pages_generated(self, page: str) -> None:
        """Each key page must produce a site HTML output."""
        page_path = SITE_DIR / page / "index.html"
        assert page_path.exists(), f"site/{page}/index.html not generated"
