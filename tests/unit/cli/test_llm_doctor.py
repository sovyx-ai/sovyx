"""Unit tests — `sovyx.cli.commands.llm.doctor` + `setup` (Mission C6 §T3.1).

Uses :class:`CliRunner` for fast in-process testing. A separate
true-subprocess integration suite at
``tests/integration/cli/test_llm_doctor_subprocess.py`` covers exit-code
+ argv parsing parity.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sovyx.cli.commands import llm as llm_cmd
from sovyx.llm._provider_health import (
    DiscoveryVerdict,
    LLMRouterDiscoveryReport,
    scan_llm_provider_health,
)

runner = CliRunner()


def _make_report(
    *,
    env: dict[str, str] | None = None,
    ollama_ping: bool = False,
    ollama_models: tuple[str, ...] | None = None,
    default_provider: str = "",
    default_model: str = "",
) -> LLMRouterDiscoveryReport:
    return scan_llm_provider_health(
        env or {},
        ollama_ping_result=ollama_ping,
        ollama_models=ollama_models,
        default_provider=default_provider,
        default_model=default_model,
    )


class TestDoctorCommand:
    def test_fully_available_exits_zero(self) -> None:
        healthy = _make_report(
            ollama_ping=True,
            ollama_models=("llama3.1:latest",),
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        with patch.object(
            llm_cmd,
            "_gather_live_report",
            new=AsyncMock(return_value=healthy),
        ):
            result = runner.invoke(llm_cmd.llm_app, ["doctor"])
        assert result.exit_code == 0
        assert "FULLY_AVAILABLE" in result.output

    def test_no_provider_configured_exits_one(self) -> None:
        degraded = _make_report()
        assert degraded.verdict is DiscoveryVerdict.NO_PROVIDER_CONFIGURED
        with patch.object(
            llm_cmd,
            "_gather_live_report",
            new=AsyncMock(return_value=degraded),
        ):
            result = runner.invoke(llm_cmd.llm_app, ["doctor"])
        assert result.exit_code == 1
        assert "NO_PROVIDER_CONFIGURED" in result.output

    def test_partial_health_exits_zero(self) -> None:
        report = _make_report(
            env={"ANTHROPIC_API_KEY": "ok", "OPENAI_API_KEY": "bad"},
            ollama_ping=True,
            ollama_models=("a:b",),
        )
        with patch.object(
            llm_cmd,
            "_gather_live_report",
            new=AsyncMock(return_value=report),
        ):
            result = runner.invoke(llm_cmd.llm_app, ["doctor"])
        # PARTIAL_HEALTH is informational — exit 0 per the doctor contract.
        assert result.exit_code == 0

    def test_json_mode_emits_valid_json(self) -> None:
        report = _make_report()
        with patch.object(
            llm_cmd,
            "_gather_live_report",
            new=AsyncMock(return_value=report),
        ):
            result = runner.invoke(llm_cmd.llm_app, ["doctor", "--json"])
        # Strip Rich formatting — print_json emits to stdout
        # Find the JSON object in output
        start = result.output.find("{")
        assert start >= 0
        payload = json.loads(result.output[start:])
        assert payload["verdict"] == "no_provider_configured"
        assert payload["configured_count"] == 0
        assert len(payload["per_provider"]) == 10

    def test_ollama_unreachable_exits_one(self) -> None:
        report = _make_report(
            default_provider="ollama",
            default_model="llama3.1:latest",
        )
        with patch.object(
            llm_cmd,
            "_gather_live_report",
            new=AsyncMock(return_value=report),
        ):
            result = runner.invoke(llm_cmd.llm_app, ["doctor"])
        assert result.exit_code == 1
        assert "OLLAMA_UNREACHABLE" in result.output

    def test_health_alias_invokes_doctor(self) -> None:
        report = _make_report()
        with patch.object(
            llm_cmd,
            "_gather_live_report",
            new=AsyncMock(return_value=report),
        ):
            result = runner.invoke(llm_cmd.llm_app, ["health"])
        assert result.exit_code == 1
        assert "NO_PROVIDER_CONFIGURED" in result.output


def _write_mind_yaml(
    data_dir: Path,
    mind_id: str,
    *,
    default_provider: str,
    default_model: str,
) -> None:
    mind_dir = data_dir / mind_id
    mind_dir.mkdir(parents=True, exist_ok=True)
    (mind_dir / "mind.yaml").write_text(
        "name: TestMind\n"
        "language: en\n"
        "llm:\n"
        f'  default_provider: "{default_provider}"\n'
        f'  default_model: "{default_model}"\n',
        encoding="utf-8",
    )


def _mock_ollama(models: list[str]) -> MagicMock:
    instance = MagicMock()
    instance.is_available = True
    instance.ping = AsyncMock(return_value=True)
    instance.list_models = AsyncMock(return_value=models)
    return instance


class TestResolveMindLlmDefaults:
    """DOCTOR-4 — the resolver that ends the hardcoded ""/"" starvation."""

    def test_single_mind_resolves_its_llm_defaults(self, tmp_path: Path) -> None:
        _write_mind_yaml(
            tmp_path,
            "sovyx",
            default_provider="ollama",
            default_model="qwen2.5:3b",
        )
        provider, model = llm_cmd.resolve_mind_llm_defaults(None, data_dir=tmp_path)
        assert provider == "ollama"
        assert model == "qwen2.5:3b"

    def test_zero_minds_degrades_to_env_only(self, tmp_path: Path) -> None:
        assert llm_cmd.resolve_mind_llm_defaults(None, data_dir=tmp_path) == ("", "")

    def test_multiple_minds_without_flag_degrades_to_env_only(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "alpha", default_provider="ollama", default_model="a")
        _write_mind_yaml(tmp_path, "beta", default_provider="ollama", default_model="b")
        assert llm_cmd.resolve_mind_llm_defaults(None, data_dir=tmp_path) == ("", "")

    def test_explicit_mind_selects_it_among_many(self, tmp_path: Path) -> None:
        _write_mind_yaml(tmp_path, "alpha", default_provider="ollama", default_model="a")
        _write_mind_yaml(tmp_path, "beta", default_provider="ollama", default_model="b")
        assert llm_cmd.resolve_mind_llm_defaults("beta", data_dir=tmp_path) == ("ollama", "b")

    def test_explicit_unknown_mind_fails_loudly(self, tmp_path: Path) -> None:
        """AP #48 — an explicit typo must not silently scan env-only."""
        import typer

        with pytest.raises(Exception) as exc_info:
            llm_cmd.resolve_mind_llm_defaults("ghost", data_dir=tmp_path)
        assert isinstance(exc_info.value, typer.BadParameter)

    def test_malformed_yaml_degrades_to_env_only(self, tmp_path: Path) -> None:
        mind_dir = tmp_path / "sovyx"
        mind_dir.mkdir(parents=True)
        (mind_dir / "mind.yaml").write_text("{not: [valid", encoding="utf-8")
        assert llm_cmd.resolve_mind_llm_defaults(None, data_dir=tmp_path) == ("", "")


class TestDoctorMindConfigWiring:
    """DOCTOR-4 (AP #71 class) — CLI path exercises the REAL verdict machinery.

    Pre-fix ``sovyx llm doctor`` hardcoded ``default_provider=""`` /
    ``default_model=""``, so a mind pinned to a nonexistent Ollama
    model still showed FULLY_AVAILABLE — the DEFAULT_MODEL_UNAVAILABLE
    branch was structurally unreachable from the CLI.
    """

    def test_pinned_nonexistent_ollama_model_yields_default_model_unavailable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.llm.providers import ollama

        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        _write_mind_yaml(
            tmp_path,
            "sovyx",
            default_provider="ollama",
            default_model="ghost:model",
        )
        with patch.object(
            ollama,
            "OllamaProvider",
            return_value=_mock_ollama(["qwen2.5:3b"]),
        ):
            result = runner.invoke(llm_cmd.llm_app, ["doctor"])
        assert result.exit_code == 1
        assert "DEFAULT_MODEL_UNAVAILABLE" in result.output

    def test_unpinned_mind_keeps_env_only_behaviour(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty defaults in mind.yaml → pre-fix behaviour unchanged."""
        from sovyx.llm.providers import ollama

        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        _write_mind_yaml(tmp_path, "sovyx", default_provider="", default_model="")
        with patch.object(
            ollama,
            "OllamaProvider",
            return_value=_mock_ollama(["qwen2.5:3b"]),
        ):
            result = runner.invoke(llm_cmd.llm_app, ["doctor"])
        assert result.exit_code == 0
        assert "FULLY_AVAILABLE" in result.output

    def test_explicit_unknown_mind_id_exits_with_usage_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SOVYX_DATA_DIR", str(tmp_path))
        result = runner.invoke(llm_cmd.llm_app, ["doctor", "--mind-id", "ghost"])
        assert result.exit_code == 2
        assert "not found" in result.output


class TestSetupCommandValidation:
    def test_unknown_provider_exits_two(self) -> None:
        result = runner.invoke(
            llm_cmd.llm_app,
            ["setup", "--non-interactive", "--provider", "nonexistent"],
        )
        assert result.exit_code == 2
        assert "Unknown provider" in result.output

    def test_non_interactive_missing_provider_exits_two(self) -> None:
        result = runner.invoke(
            llm_cmd.llm_app,
            ["setup", "--non-interactive"],
        )
        assert result.exit_code == 2
        assert "--provider is required" in result.output

    def test_non_interactive_cloud_missing_key_exits_two(self) -> None:
        result = runner.invoke(
            llm_cmd.llm_app,
            ["setup", "--non-interactive", "--provider", "anthropic"],
        )
        assert result.exit_code == 2
        assert "requires an API key" in result.output


class TestSetupCommandCloudHappyPath:
    def test_valid_key_persists_and_exits_zero(self, tmp_path: Path) -> None:
        mock_provider = MagicMock()
        with (
            patch.object(llm_cmd, "create_provider", return_value=mock_provider),
            patch.object(
                llm_cmd,
                "test_provider",
                new=AsyncMock(return_value=(True, "OK")),
            ),
        ):
            result = runner.invoke(
                llm_cmd.llm_app,
                [
                    "setup",
                    "--non-interactive",
                    "--provider",
                    "anthropic",
                    "--api-key",
                    "sk-test-12345",
                    "--data-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0
        assert "configured" in result.output.lower()
        secrets_file = tmp_path / "secrets.env"
        assert secrets_file.exists()
        content = secrets_file.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-test-12345" in content

    def test_invalid_key_exits_one_does_not_persist(self, tmp_path: Path) -> None:
        mock_provider = MagicMock()
        with (
            patch.object(llm_cmd, "create_provider", return_value=mock_provider),
            patch.object(
                llm_cmd,
                "test_provider",
                new=AsyncMock(return_value=(False, "Auth failed: 401")),
            ),
        ):
            result = runner.invoke(
                llm_cmd.llm_app,
                [
                    "setup",
                    "--non-interactive",
                    "--provider",
                    "anthropic",
                    "--api-key",
                    "sk-invalid",
                    "--data-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 1
        assert (tmp_path / "secrets.env").exists() is False

    def test_provider_creation_failure_exits_one(self, tmp_path: Path) -> None:
        with patch.object(llm_cmd, "create_provider", return_value=None):
            result = runner.invoke(
                llm_cmd.llm_app,
                [
                    "setup",
                    "--non-interactive",
                    "--provider",
                    "anthropic",
                    "--api-key",
                    "sk-test",
                    "--data-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 1
        assert "Failed to instantiate" in result.output


class TestSetupCommandOllama:
    def test_ollama_unreachable_exits_one(self) -> None:
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.is_available = False
        mock_instance.ping = AsyncMock(return_value=False)
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            result = runner.invoke(
                llm_cmd.llm_app,
                ["setup", "--non-interactive", "--provider", "ollama"],
            )
        assert result.exit_code == 1
        assert "not reachable" in result.output

    def test_ollama_no_models_exits_one(self) -> None:
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.is_available = True
        mock_instance.ping = AsyncMock(return_value=True)
        mock_instance.list_models = AsyncMock(return_value=[])
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            result = runner.invoke(
                llm_cmd.llm_app,
                ["setup", "--non-interactive", "--provider", "ollama"],
            )
        assert result.exit_code == 1
        assert "no models" in result.output

    def test_ollama_reachable_with_models_exits_zero(self) -> None:
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.is_available = True
        mock_instance.ping = AsyncMock(return_value=True)
        mock_instance.list_models = AsyncMock(return_value=["llama3.1:latest"])
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            result = runner.invoke(
                llm_cmd.llm_app,
                ["setup", "--non-interactive", "--provider", "ollama"],
            )
        assert result.exit_code == 0
        assert "reachable" in result.output
