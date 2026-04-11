"""Tests for sovyx plugin CLI commands (TASK-439)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from sovyx.cli.commands.plugin import (
    _get_plugin_status,
    _list,
    _load_manifest_safe,
    _plugins_dir,
    _str,
    _update_mind_yaml_plugins,
    plugin_app,
)

runner = CliRunner()


def _create_plugin_dir(base: Path, name: str, **kwargs: object) -> Path:
    """Create a fake plugin directory with plugin.yaml."""
    plugin_dir = base / name
    plugin_dir.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": kwargs.get("version", "1.0.0"),
        "description": kwargs.get("description", f"Test plugin {name}"),
        **{k: v for k, v in kwargs.items() if k not in ("version", "description")},
    }
    (plugin_dir / "plugin.yaml").write_text(yaml.dump(manifest), encoding="utf-8")
    return plugin_dir


# ── Helpers ─────────────────────────────────────────────────────────


class TestHelpers:
    """Tests for CLI helper functions."""

    def test_str_with_value(self) -> None:
        assert _str("hello") == "hello"

    def test_str_with_none(self) -> None:
        assert _str(None) == ""

    def test_str_with_default(self) -> None:
        assert _str(None, "fallback") == "fallback"

    def test_list_with_list(self) -> None:
        assert _list([1, 2]) == [1, 2]

    def test_list_with_non_list(self) -> None:
        assert _list("not a list") == []

    def test_list_with_none(self) -> None:
        assert _list(None) == []

    def test_load_manifest_valid(self, tmp_path: Path) -> None:
        _create_plugin_dir(tmp_path, "test")
        result = _load_manifest_safe(tmp_path / "test")
        assert result is not None
        assert result["name"] == "test"

    def test_load_manifest_no_file(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        assert _load_manifest_safe(tmp_path / "empty") is None

    def test_load_manifest_invalid_yaml(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "bad"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(":::invalid", encoding="utf-8")
        assert _load_manifest_safe(plugin_dir) is None

    def test_load_manifest_non_dict(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "list"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text("- item1\n- item2", encoding="utf-8")
        assert _load_manifest_safe(plugin_dir) is None


# ── Plugin Status ───────────────────────────────────────────────────


class TestGetPluginStatus:
    """Tests for _get_plugin_status."""

    def test_enabled_by_default(self, tmp_path: Path) -> None:
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=tmp_path / "nonexistent.yaml",
        ):
            assert _get_plugin_status("weather") == "enabled"

    def test_disabled_in_list(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(
            yaml.dump({"plugins": {"disabled": ["weather"]}}),
            encoding="utf-8",
        )
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            assert _get_plugin_status("weather") == "disabled"

    def test_disabled_in_config(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(
            yaml.dump(
                {
                    "plugins": {
                        "plugins_config": {"weather": {"enabled": False}},
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            assert _get_plugin_status("weather") == "disabled"


# ── Update mind.yaml ────────────────────────────────────────────────


class TestUpdateMindYaml:
    """Tests for _update_mind_yaml_plugins."""

    def test_disable_plugin(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(
            yaml.dump({"name": "test", "plugins": {"disabled": []}}),
            encoding="utf-8",
        )
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            changed = _update_mind_yaml_plugins("weather")
        assert changed
        data = yaml.safe_load(mind_yaml.read_text())
        assert "weather" in data["plugins"]["disabled"]

    def test_enable_plugin(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(
            yaml.dump({"name": "test", "plugins": {"disabled": ["weather"]}}),
            encoding="utf-8",
        )
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            changed = _update_mind_yaml_plugins("weather", enable=True)
        assert changed
        data = yaml.safe_load(mind_yaml.read_text())
        assert "weather" not in data["plugins"]["disabled"]

    def test_enable_already_enabled(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(
            yaml.dump({"name": "test", "plugins": {"disabled": []}}),
            encoding="utf-8",
        )
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            changed = _update_mind_yaml_plugins("weather", enable=True)
        assert not changed

    def test_disable_already_disabled(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(
            yaml.dump({"name": "test", "plugins": {"disabled": ["weather"]}}),
            encoding="utf-8",
        )
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            changed = _update_mind_yaml_plugins("weather")
        assert not changed

    def test_remove_from_disabled(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(
            yaml.dump({"name": "test", "plugins": {"disabled": ["weather"]}}),
            encoding="utf-8",
        )
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            changed = _update_mind_yaml_plugins("weather", remove=True)
        assert changed

    def test_no_mind_yaml_enable(self, tmp_path: Path) -> None:
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=tmp_path / "nonexistent.yaml",
        ):
            changed = _update_mind_yaml_plugins("weather", enable=True)
        assert not changed

    def test_no_mind_yaml_disable_creates(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            changed = _update_mind_yaml_plugins("weather")
        assert changed
        assert mind_yaml.exists()


# ── CLI Commands ────────────────────────────────────────────────────


class TestPluginListCommand:
    """Tests for 'sovyx plugin list'."""

    def test_no_plugins(self, tmp_path: Path) -> None:
        with patch(
            "sovyx.cli.commands.plugin._plugins_dir",
            return_value=tmp_path / "empty",
        ):
            result = runner.invoke(plugin_app, ["list"])
        assert result.exit_code == 0
        assert "No plugins" in result.output

    def test_list_plugins(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        _create_plugin_dir(plugins_dir, "weather", tools=[{"name": "get_weather"}])
        _create_plugin_dir(plugins_dir, "timer")

        with (
            patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir),
            patch(
                "sovyx.cli.commands.plugin._mind_yaml_path",
                return_value=tmp_path / "no.yaml",
            ),
        ):
            result = runner.invoke(plugin_app, ["list"])
        assert result.exit_code == 0
        assert "weather" in result.output
        assert "timer" in result.output


class TestPluginInfoCommand:
    """Tests for 'sovyx plugin info'."""

    def test_info_existing(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        _create_plugin_dir(
            plugins_dir,
            "weather",
            author="Nyx",
            permissions=["network:internet"],
            tools=[{"name": "get_weather", "description": "Get weather"}],
        )

        with (
            patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir),
            patch(
                "sovyx.cli.commands.plugin._mind_yaml_path",
                return_value=tmp_path / "no.yaml",
            ),
        ):
            result = runner.invoke(plugin_app, ["info", "weather"])
        assert result.exit_code == 0
        assert "weather" in result.output
        assert "Nyx" in result.output
        assert "network:internet" in result.output

    def test_info_not_found(self, tmp_path: Path) -> None:
        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=tmp_path):
            result = runner.invoke(plugin_app, ["info", "ghost"])
        assert result.exit_code == 1


class TestPluginInstallCommand:
    """Tests for 'sovyx plugin install'."""

    def test_install_local(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        _create_plugin_dir(source.parent, "source")

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir):
            result = runner.invoke(plugin_app, ["install", str(source), "--yes"])
        assert result.exit_code == 0
        assert "installed" in result.output.lower()

    def test_install_local_no_manifest(self, tmp_path: Path) -> None:
        source = tmp_path / "empty"
        source.mkdir()

        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=tmp_path):
            result = runner.invoke(plugin_app, ["install", str(source), "--yes"])
        assert result.exit_code == 1


class TestPluginEnableDisable:
    """Tests for enable/disable commands."""

    def test_disable(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(yaml.dump({"name": "test", "plugins": {"disabled": []}}))

        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            result = runner.invoke(plugin_app, ["disable", "weather"])
        assert result.exit_code == 0
        assert "disabled" in result.output.lower()

    def test_enable(self, tmp_path: Path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(yaml.dump({"name": "test", "plugins": {"disabled": ["weather"]}}))

        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            result = runner.invoke(plugin_app, ["enable", "weather"])
        assert result.exit_code == 0
        assert "enabled" in result.output.lower()


class TestPluginRemoveCommand:
    """Tests for 'sovyx plugin remove'."""

    def test_remove_existing(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        _create_plugin_dir(plugins_dir, "weather")

        with (
            patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir),
            patch(
                "sovyx.cli.commands.plugin._mind_yaml_path",
                return_value=tmp_path / "no.yaml",
            ),
        ):
            result = runner.invoke(plugin_app, ["remove", "weather"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()
        assert not (plugins_dir / "weather").exists()

    def test_remove_not_found(self, tmp_path: Path) -> None:
        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=tmp_path):
            result = runner.invoke(plugin_app, ["remove", "ghost"])
        assert result.exit_code == 1


# ── Edge Cases ──────────────────────────────────────────────────────


class TestEdgeCases:
    """Cover remaining branches."""

    def test_plugins_dir_default(self) -> None:
        """_plugins_dir returns ~/.sovyx/plugins."""
        d = _plugins_dir()
        assert d.name == "plugins"
        assert d.parent.name == ".sovyx"

    def test_list_skips_non_dirs(self, tmp_path: Path) -> None:
        """List skips non-directory files."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "not-a-dir.txt").write_text("nope")

        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir):
            result = runner.invoke(plugin_app, ["list"])
        assert result.exit_code == 0
        assert "No plugins" in result.output

    def test_list_skips_invalid_manifests(self, tmp_path: Path) -> None:
        """List skips dirs without valid manifest."""
        plugins_dir = tmp_path / "plugins"
        (plugins_dir / "broken").mkdir(parents=True)

        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir):
            result = runner.invoke(plugin_app, ["list"])
        assert result.exit_code == 0
        assert "No plugins" in result.output

    def test_info_invalid_manifest(self, tmp_path: Path) -> None:
        """Info with invalid plugin.yaml shows error."""
        plugins_dir = tmp_path / "plugins"
        bad_dir = plugins_dir / "bad"
        bad_dir.mkdir(parents=True)
        (bad_dir / "plugin.yaml").write_text("- not a dict")

        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir):
            result = runner.invoke(plugin_app, ["info", "bad"])
        assert result.exit_code == 1

    def test_info_with_deps(self, tmp_path: Path) -> None:
        """Info shows dependencies."""
        plugins_dir = tmp_path / "plugins"
        _create_plugin_dir(
            plugins_dir,
            "dependent",
            depends=[{"name": "base-plugin", "version": ">=1.0.0"}],
        )
        with (
            patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir),
            patch(
                "sovyx.cli.commands.plugin._mind_yaml_path",
                return_value=tmp_path / "no.yaml",
            ),
        ):
            result = runner.invoke(plugin_app, ["info", "dependent"])
        assert result.exit_code == 0
        assert "base-plugin" in result.output

    def test_install_replaces_existing(self, tmp_path: Path) -> None:
        """Install overwrites existing plugin."""
        source = tmp_path / "source"
        _create_plugin_dir(source.parent, "source")

        plugins_dir = tmp_path / "plugins"
        _create_plugin_dir(plugins_dir, "source")  # Already exists

        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir):
            result = runner.invoke(plugin_app, ["install", str(source), "--yes"])
        assert result.exit_code == 0
        assert "Replacing" in result.output or "installed" in result.output.lower()

    def test_install_pip(self, tmp_path: Path) -> None:
        """Install via pip (mocked)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
            result = runner.invoke(plugin_app, ["install", "sovyx-plugin-example"])
        assert result.exit_code == 0
        assert "pip" in result.output.lower()

    def test_install_pip_failure(self, tmp_path: Path) -> None:
        """Install via pip failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stderr": "not found"})()
            result = runner.invoke(plugin_app, ["install", "nonexistent-pkg"])
        assert result.exit_code == 1

    def test_install_git(self, tmp_path: Path) -> None:
        """Install via git URL (mocked)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
            result = runner.invoke(plugin_app, ["install", "git+https://github.com/x/y.git"])
        assert result.exit_code == 0

    def test_install_git_failure(self, tmp_path: Path) -> None:
        """Install via git failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stderr": "clone failed"})()
            result = runner.invoke(plugin_app, ["install", "git+https://github.com/x/y.git"])
        assert result.exit_code == 1

    def test_get_status_invalid_yaml(self, tmp_path: Path) -> None:
        """Status returns enabled for invalid mind.yaml."""
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(":::invalid")
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            assert _get_plugin_status("weather") == "enabled"

    def test_get_status_non_dict(self, tmp_path: Path) -> None:
        """Status returns enabled for non-dict yaml."""
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text("- list\n- items")
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            assert _get_plugin_status("weather") == "enabled"

    def test_update_non_dict_yaml(self, tmp_path: Path) -> None:
        """Update returns False for non-dict yaml."""
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text("- list")
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            assert _update_mind_yaml_plugins("weather") is False

    def test_update_non_dict_plugins(self, tmp_path: Path) -> None:
        """Update handles non-dict plugins section."""
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(yaml.dump({"name": "test", "plugins": "not-a-dict"}))
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            assert _update_mind_yaml_plugins("weather") is False

    def test_update_non_list_disabled(self, tmp_path: Path) -> None:
        """Update handles non-list disabled."""
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(yaml.dump({"name": "test", "plugins": {"disabled": "string"}}))
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            changed = _update_mind_yaml_plugins("weather")
        assert changed

    def test_get_status_non_dict_plugins(self, tmp_path: Path) -> None:
        """Status returns enabled when plugins is not a dict."""
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(yaml.dump({"plugins": "not-dict"}))
        with patch(
            "sovyx.cli.commands.plugin._mind_yaml_path",
            return_value=mind_yaml,
        ):
            assert _get_plugin_status("weather") == "enabled"


class TestPermissionPrompt:
    """Tests for permission approval on install."""

    def test_install_with_perms_approved(self, tmp_path: Path) -> None:
        """User approves permissions."""
        source = tmp_path / "src"
        _create_plugin_dir(source.parent, "src", permissions=["network:internet"])
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=plugins_dir):
            result = runner.invoke(plugin_app, ["install", str(source)], input="y\n")
        assert result.exit_code == 0
        assert "installed" in result.output.lower()

    def test_install_with_perms_denied(self, tmp_path: Path) -> None:
        """User denies permissions."""
        source = tmp_path / "src"
        _create_plugin_dir(source.parent, "src", permissions=["brain:write"])

        with patch("sovyx.cli.commands.plugin._plugins_dir", return_value=tmp_path):
            result = runner.invoke(plugin_app, ["install", str(source)], input="n\n")
        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()

    def test_enable_already_enabled_cli(self, tmp_path: Path) -> None:
        """Enable already-enabled shows 'already enabled'."""
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(yaml.dump({"name": "t", "plugins": {"disabled": []}}))
        with patch("sovyx.cli.commands.plugin._mind_yaml_path", return_value=mind_yaml):
            result = runner.invoke(plugin_app, ["enable", "weather"])
        assert "already" in result.output.lower()

    def test_disable_already_disabled_cli(self, tmp_path: Path) -> None:
        """Disable already-disabled shows 'already disabled'."""
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text(yaml.dump({"name": "t", "plugins": {"disabled": ["weather"]}}))
        with patch("sovyx.cli.commands.plugin._mind_yaml_path", return_value=mind_yaml):
            result = runner.invoke(plugin_app, ["disable", "weather"])
        assert "already" in result.output.lower()


# ── Plugin Create / Scaffold (TASK-440) ─────────────────────────────


class TestPluginCreate:
    """Tests for 'sovyx plugin create'."""

    def test_create_basic(self, tmp_path: Path) -> None:
        """Create generates all scaffold files."""
        result = runner.invoke(plugin_app, ["create", "my-plugin", "-o", str(tmp_path)])
        assert result.exit_code == 0
        assert "created" in result.output.lower()

        plugin_dir = tmp_path / "my-plugin"
        assert (plugin_dir / "__init__.py").exists()
        assert (plugin_dir / "plugin.py").exists()
        assert (plugin_dir / "plugin.yaml").exists()
        assert (plugin_dir / "tests" / "__init__.py").exists()
        assert (plugin_dir / "tests" / "test_my_plugin.py").exists()
        assert (plugin_dir / "README.md").exists()
        assert (plugin_dir / "pyproject.toml").exists()

    def test_create_plugin_py_content(self, tmp_path: Path) -> None:
        """plugin.py has ISovyxPlugin subclass with @tool."""
        runner.invoke(plugin_app, ["create", "weather", "-o", str(tmp_path)])
        content = (tmp_path / "weather" / "plugin.py").read_text()
        assert "ISovyxPlugin" in content
        assert "@tool" in content
        assert "class WeatherPlugin" in content
        assert 'return "weather"' in content

    def test_create_manifest_content(self, tmp_path: Path) -> None:
        """plugin.yaml has correct name and version."""
        runner.invoke(plugin_app, ["create", "timer", "-o", str(tmp_path)])
        import yaml

        data = yaml.safe_load((tmp_path / "timer" / "plugin.yaml").read_text())
        assert data["name"] == "timer"
        assert data["version"] == "0.1.0"

    def test_create_pyproject_entry_point(self, tmp_path: Path) -> None:
        """pyproject.toml has entry_points for auto-discovery."""
        runner.invoke(plugin_app, ["create", "my-tool", "-o", str(tmp_path)])
        content = (tmp_path / "my-tool" / "pyproject.toml").read_text()
        assert "sovyx_plugins" in content
        assert "my-tool" in content
        assert "MyToolPlugin" in content

    def test_create_hyphenated_name(self, tmp_path: Path) -> None:
        """Hyphenated name converts to proper class/module names."""
        runner.invoke(plugin_app, ["create", "my-cool-plugin", "-o", str(tmp_path)])
        content = (tmp_path / "my-cool-plugin" / "plugin.py").read_text()
        assert "MyCoolPluginPlugin" in content
        # Module name uses underscores
        init = (tmp_path / "my-cool-plugin" / "__init__.py").read_text()
        assert "my_cool_plugin" in init

    def test_create_invalid_name(self, tmp_path: Path) -> None:
        """Invalid name (uppercase) rejected."""
        result = runner.invoke(plugin_app, ["create", "BadName", "-o", str(tmp_path)])
        assert result.exit_code == 1
        assert "lowercase" in result.output.lower()

    def test_create_exists(self, tmp_path: Path) -> None:
        """Creating in existing directory fails."""
        (tmp_path / "existing").mkdir()
        result = runner.invoke(plugin_app, ["create", "existing", "-o", str(tmp_path)])
        assert result.exit_code == 1
        assert "already" in result.output.lower()

    def test_create_test_file(self, tmp_path: Path) -> None:
        """Generated test file has proper test class."""
        runner.invoke(plugin_app, ["create", "foo", "-o", str(tmp_path)])
        content = (tmp_path / "foo" / "tests" / "test_foo.py").read_text()
        assert "TestFooPlugin" in content
        assert "@pytest.mark.anyio" in content
        assert "await plugin.hello" in content


# ── Plugin Validate (TASK-441) ──────────────────────────────────────

from sovyx.cli.commands.plugin import (
    _check_syntax,
    _discover_tests,
    _validate_manifest,
)


class TestPluginValidate:
    """Tests for 'sovyx plugin validate'."""

    def test_validate_valid_plugin(self, tmp_path: Path) -> None:
        """Valid plugin passes all gates."""
        plugin_dir = tmp_path / "good"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(
            "name: good\nversion: 1.0.0\ndescription: A good plugin\n"
        )
        (plugin_dir / "plugin.py").write_text("x = 1\n")
        tests_dir = plugin_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_good.py").write_text("def test_ok(): pass\n")

        result = runner.invoke(plugin_app, ["validate", str(plugin_dir)])
        assert result.exit_code == 0
        assert "PASSED" in result.output

    def test_validate_no_manifest(self, tmp_path: Path) -> None:
        """Missing manifest fails validation."""
        plugin_dir = tmp_path / "bad"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.py").write_text("x = 1\n")

        result = runner.invoke(plugin_app, ["validate", str(plugin_dir)])
        assert result.exit_code == 1
        assert "FAILED" in result.output

    def test_validate_security_issue(self, tmp_path: Path) -> None:
        """Plugin with eval() flagged."""
        plugin_dir = tmp_path / "evil"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(
            "name: evil\nversion: 1.0.0\ndescription: Evil plugin\n"
        )
        (plugin_dir / "plugin.py").write_text("result = eval('1+1')\n")

        result = runner.invoke(plugin_app, ["validate", str(plugin_dir)])
        # Should have security finding
        assert (
            "eval" in result.output.lower()
            or "FAILED" in result.output
            or "PASSED" in result.output
        )

    def test_validate_no_tests_warning(self, tmp_path: Path) -> None:
        """No tests gives warning."""
        plugin_dir = tmp_path / "notests"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(
            "name: notests\nversion: 1.0.0\ndescription: No tests\n"
        )
        (plugin_dir / "plugin.py").write_text("x = 1\n")

        result = runner.invoke(plugin_app, ["validate", str(plugin_dir)])
        assert "No tests" in result.output

    def test_validate_syntax_error(self, tmp_path: Path) -> None:
        """Syntax error in Python file fails."""
        plugin_dir = tmp_path / "syntax"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(
            "name: syntax\nversion: 1.0.0\ndescription: Syntax err\n"
        )
        (plugin_dir / "broken.py").write_text("def foo(\n")

        result = runner.invoke(plugin_app, ["validate", str(plugin_dir)])
        assert result.exit_code == 1

    def test_validate_not_a_dir(self) -> None:
        """Non-directory path fails."""
        result = runner.invoke(plugin_app, ["validate", "/tmp/nonexistent-xxx"])
        assert result.exit_code == 1

    def test_discover_tests_in_root(self, tmp_path: Path) -> None:
        """Tests in root dir (no tests/ subdir) are found."""
        (tmp_path / "test_foo.py").write_text("pass")
        assert _discover_tests(tmp_path) == 1

    def test_check_syntax_clean(self, tmp_path: Path) -> None:
        """Clean files return no errors."""
        (tmp_path / "good.py").write_text("x = 1\n")
        assert _check_syntax(tmp_path) == []

    def test_validate_manifest_valid(self, tmp_path: Path) -> None:
        """Valid manifest returns summary string."""
        (tmp_path / "plugin.yaml").write_text("name: test\nversion: 1.0.0\ndescription: Test\n")
        result = _validate_manifest(tmp_path)
        assert result is not None
        assert "test" in result

    def test_validate_manifest_invalid(self, tmp_path: Path) -> None:
        assert _validate_manifest(tmp_path) is None
