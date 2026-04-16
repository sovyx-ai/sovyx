"""Tests for ConfigEditor — atomic YAML updates with comment preservation."""

from __future__ import annotations

from pathlib import Path

import pytest

from sovyx.engine.config_editor import ConfigEditor


@pytest.fixture
def editor() -> ConfigEditor:
    return ConfigEditor()


class TestUpdateSection:
    """ConfigEditor.update_section writes atomically and preserves structure."""

    @pytest.mark.asyncio()
    async def test_creates_file_if_missing(self, editor: ConfigEditor, tmp_path: Path) -> None:
        yaml_path = tmp_path / "mind.yaml"
        await editor.update_section(
            yaml_path, "plugins_config.caldav.config", {"base_url": "https://x"}
        )
        assert yaml_path.exists()
        content = yaml_path.read_text()
        assert "base_url" in content
        assert "https://x" in content

    @pytest.mark.asyncio()
    async def test_preserves_existing_content(self, editor: ConfigEditor, tmp_path: Path) -> None:
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text("name: my-mind\nlanguage: en\n")
        await editor.update_section(
            yaml_path, "plugins_config.caldav.config", {"base_url": "https://x"}
        )
        content = yaml_path.read_text()
        assert "name: my-mind" in content
        assert "language: en" in content
        assert "base_url" in content

    @pytest.mark.asyncio()
    async def test_updates_existing_section(self, editor: ConfigEditor, tmp_path: Path) -> None:
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text("plugins_config:\n  caldav:\n    config:\n      base_url: old\n")
        await editor.update_section(yaml_path, "plugins_config.caldav.config", {"base_url": "new"})
        content = yaml_path.read_text()
        assert "new" in content
        assert "old" not in content

    @pytest.mark.asyncio()
    async def test_preserves_comments(self, editor: ConfigEditor, tmp_path: Path) -> None:
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text("# Main config\nname: my-mind  # keep this\n")
        await editor.update_section(yaml_path, "plugins_config.ha.config", {"token": "abc"})
        content = yaml_path.read_text()
        assert "# Main config" in content
        assert "# keep this" in content


class TestReadSection:
    """ConfigEditor.read_section reads dotted sections."""

    @pytest.mark.asyncio()
    async def test_reads_existing_section(self, editor: ConfigEditor, tmp_path: Path) -> None:
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text(
            "plugins_config:\n  caldav:\n    config:\n      base_url: https://x\n"
        )
        result = await editor.read_section(yaml_path, "plugins_config.caldav.config")
        assert result["base_url"] == "https://x"

    @pytest.mark.asyncio()
    async def test_missing_section_returns_empty(
        self, editor: ConfigEditor, tmp_path: Path
    ) -> None:
        yaml_path = tmp_path / "mind.yaml"
        yaml_path.write_text("name: test\n")
        result = await editor.read_section(yaml_path, "plugins_config.caldav.config")
        assert result == {}

    @pytest.mark.asyncio()
    async def test_missing_file_returns_empty(self, editor: ConfigEditor, tmp_path: Path) -> None:
        result = await editor.read_section(tmp_path / "nope.yaml", "any.section")
        assert result == {}
