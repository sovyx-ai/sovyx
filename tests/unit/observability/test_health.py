"""Tests for sovyx.observability.health — 10 health checks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.observability.health import (
    BrainIndexedCheck,
    ChannelConnectedCheck,
    CheckResult,
    CheckStatus,
    ConsolidationCheck,
    CostBudgetCheck,
    CPUCheck,
    DatabaseCheck,
    DiskSpaceCheck,
    HealthCheck,
    HealthRegistry,
    LLMReachableCheck,
    ModelLoadedCheck,
    RAMCheck,
    create_default_registry,
)

# ── CheckResult ─────────────────────────────────────────────────────────────


class TestCheckResult:
    """CheckResult dataclass."""

    def test_ok_when_green(self) -> None:
        r = CheckResult(name="test", status=CheckStatus.GREEN, message="ok")
        assert r.ok is True

    def test_not_ok_when_yellow(self) -> None:
        r = CheckResult(name="test", status=CheckStatus.YELLOW, message="warn")
        assert r.ok is False

    def test_not_ok_when_red(self) -> None:
        r = CheckResult(name="test", status=CheckStatus.RED, message="bad")
        assert r.ok is False

    def test_metadata_default_empty(self) -> None:
        r = CheckResult(name="t", status=CheckStatus.GREEN, message="ok")
        assert r.metadata == {}

    def test_frozen(self) -> None:
        r = CheckResult(name="t", status=CheckStatus.GREEN, message="ok")
        with pytest.raises(AttributeError):
            r.name = "other"  # type: ignore[misc]


# ── HealthRegistry ──────────────────────────────────────────────────────────


class _DummyCheck(HealthCheck):
    def __init__(self, result: CheckResult) -> None:
        self._result = result

    @property
    def name(self) -> str:
        return self._result.name

    async def check(self) -> CheckResult:
        return self._result


class _SlowCheck(HealthCheck):
    @property
    def name(self) -> str:
        return "Slow"

    async def check(self) -> CheckResult:
        await asyncio.sleep(1.0)  # cancelled by 0.1s test timeout
        return CheckResult(name="Slow", status=CheckStatus.GREEN, message="done")


class _CrashCheck(HealthCheck):
    @property
    def name(self) -> str:
        return "Crash"

    async def check(self) -> CheckResult:
        msg = "boom"
        raise RuntimeError(msg)


class TestHealthRegistry:
    """HealthRegistry run_all and summary."""

    @pytest.mark.asyncio()
    async def test_run_all_empty(self) -> None:
        reg = HealthRegistry()
        results = await reg.run_all()
        assert results == []

    @pytest.mark.asyncio()
    async def test_run_all_collects_results(self) -> None:
        reg = HealthRegistry()
        reg.register(_DummyCheck(CheckResult("A", CheckStatus.GREEN, "ok")))
        reg.register(_DummyCheck(CheckResult("B", CheckStatus.YELLOW, "warn")))

        results = await reg.run_all()
        assert len(results) == 2
        assert results[0].name == "A"
        assert results[1].name == "B"

    @pytest.mark.asyncio()
    async def test_timeout_returns_red(self) -> None:
        reg = HealthRegistry()
        reg.register(_SlowCheck())

        results = await reg.run_all(timeout=0.1)
        assert len(results) == 1
        assert results[0].status == CheckStatus.RED
        assert "timed out" in results[0].message

    @pytest.mark.asyncio()
    async def test_crash_returns_red(self) -> None:
        reg = HealthRegistry()
        reg.register(_CrashCheck())

        results = await reg.run_all()
        assert len(results) == 1
        assert results[0].status == CheckStatus.RED
        assert "boom" in results[0].message

    def test_summary_all_green(self) -> None:
        reg = HealthRegistry()
        results = [
            CheckResult("A", CheckStatus.GREEN, "ok"),
            CheckResult("B", CheckStatus.GREEN, "ok"),
        ]
        assert reg.summary(results) == CheckStatus.GREEN

    def test_summary_yellow_wins(self) -> None:
        reg = HealthRegistry()
        results = [
            CheckResult("A", CheckStatus.GREEN, "ok"),
            CheckResult("B", CheckStatus.YELLOW, "warn"),
        ]
        assert reg.summary(results) == CheckStatus.YELLOW

    def test_summary_red_wins(self) -> None:
        reg = HealthRegistry()
        results = [
            CheckResult("A", CheckStatus.GREEN, "ok"),
            CheckResult("B", CheckStatus.YELLOW, "warn"),
            CheckResult("C", CheckStatus.RED, "bad"),
        ]
        assert reg.summary(results) == CheckStatus.RED

    def test_check_count(self) -> None:
        reg = HealthRegistry()
        assert reg.check_count == 0
        reg.register(_DummyCheck(CheckResult("A", CheckStatus.GREEN, "ok")))
        assert reg.check_count == 1


# ── DiskSpaceCheck ──────────────────────────────────────────────────────────


class TestDiskSpaceCheck:
    """DiskSpaceCheck thresholds."""

    @pytest.mark.asyncio()
    async def test_green_when_plenty(self) -> None:
        check = DiskSpaceCheck()
        result = await check.check()
        # Assuming test machine has > 1GB free
        assert result.status in {CheckStatus.GREEN, CheckStatus.YELLOW}
        assert "free" in result.message
        assert "free_gb" in result.metadata

    @pytest.mark.asyncio()
    async def test_red_when_low(self) -> None:
        mock_usage = MagicMock()
        mock_usage.free = 100 * 1024 * 1024  # 100 MB
        mock_usage.total = 100 * 1024**3
        mock_usage.used = mock_usage.total - mock_usage.free

        with patch("sovyx.observability.health.shutil.disk_usage", return_value=mock_usage):
            check = DiskSpaceCheck()
            result = await check.check()
            assert result.status == CheckStatus.RED
            assert "Critical" in result.message

    @pytest.mark.asyncio()
    async def test_yellow_when_medium(self) -> None:
        mock_usage = MagicMock()
        mock_usage.free = 700 * 1024 * 1024  # 700 MB
        mock_usage.total = 100 * 1024**3
        mock_usage.used = mock_usage.total - mock_usage.free

        with patch("sovyx.observability.health.shutil.disk_usage", return_value=mock_usage):
            check = DiskSpaceCheck()
            result = await check.check()
            assert result.status == CheckStatus.YELLOW

    def test_name(self) -> None:
        assert DiskSpaceCheck().name == "Disk Space"

    @pytest.mark.asyncio()
    async def test_nonexistent_path_returns_red(self) -> None:
        """DiskSpaceCheck with nonexistent path returns RED, not crash."""
        check = DiskSpaceCheck(path=Path("/nonexistent/path/xyz"))
        result = await check.check()
        assert result.status == CheckStatus.RED
        assert "Cannot check disk" in result.message


# ── RAMCheck ────────────────────────────────────────────────────────────────


class TestRAMCheck:
    """RAMCheck thresholds."""

    @pytest.mark.asyncio()
    async def test_returns_result(self) -> None:
        check = RAMCheck()
        result = await check.check()
        assert result.status in {CheckStatus.GREEN, CheckStatus.YELLOW, CheckStatus.RED}
        assert "available_mb" in result.metadata

    @pytest.mark.asyncio()
    async def test_red_when_low(self) -> None:
        mock_mem = MagicMock()
        mock_mem.available = 100 * 1024 * 1024  # 100 MB
        mock_mem.total = 4 * 1024**3
        mock_mem.percent = 97.5

        with patch("psutil.virtual_memory", return_value=mock_mem):
            result = await RAMCheck().check()
            assert result.status == CheckStatus.RED

    @pytest.mark.asyncio()
    async def test_yellow_when_medium(self) -> None:
        mock_mem = MagicMock()
        mock_mem.available = 300 * 1024 * 1024  # 300 MB
        mock_mem.total = 4 * 1024**3
        mock_mem.percent = 92.5

        with patch("psutil.virtual_memory", return_value=mock_mem):
            result = await RAMCheck().check()
            assert result.status == CheckStatus.YELLOW

    def test_name(self) -> None:
        assert RAMCheck().name == "RAM"


# ── CPUCheck ────────────────────────────────────────────────────────────────


class TestCPUCheck:
    """CPUCheck thresholds."""

    @pytest.mark.asyncio()
    async def test_green_when_low(self) -> None:
        with patch("psutil.cpu_percent", return_value=25.0):
            result = await CPUCheck().check()
            assert result.status == CheckStatus.GREEN

    @pytest.mark.asyncio()
    async def test_yellow_when_high(self) -> None:
        with patch("psutil.cpu_percent", return_value=90.0):
            result = await CPUCheck().check()
            assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_red_when_critical(self) -> None:
        with patch("psutil.cpu_percent", return_value=98.0):
            result = await CPUCheck().check()
            assert result.status == CheckStatus.RED

    def test_name(self) -> None:
        assert CPUCheck().name == "CPU"


# ── DatabaseCheck ───────────────────────────────────────────────────────────


class TestDatabaseCheck:
    """DatabaseCheck with callable."""

    @pytest.mark.asyncio()
    async def test_green_when_writable(self) -> None:
        write_fn = AsyncMock()
        result = await DatabaseCheck(write_fn=write_fn).check()
        assert result.status == CheckStatus.GREEN
        write_fn.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_red_when_write_fails(self) -> None:
        write_fn = AsyncMock(side_effect=RuntimeError("DB locked"))
        result = await DatabaseCheck(write_fn=write_fn).check()
        assert result.status == CheckStatus.RED
        assert "DB locked" in result.message

    @pytest.mark.asyncio()
    async def test_yellow_when_not_configured(self) -> None:
        result = await DatabaseCheck().check()
        assert result.status == CheckStatus.YELLOW


# ── BrainIndexedCheck ───────────────────────────────────────────────────────


class TestBrainIndexedCheck:
    """BrainIndexedCheck with callable."""

    @pytest.mark.asyncio()
    async def test_green_when_loaded(self) -> None:
        result = await BrainIndexedCheck(is_loaded_fn=lambda: True).check()
        assert result.status == CheckStatus.GREEN

    @pytest.mark.asyncio()
    async def test_yellow_when_not_loaded(self) -> None:
        result = await BrainIndexedCheck(is_loaded_fn=lambda: False).check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_yellow_when_not_configured(self) -> None:
        result = await BrainIndexedCheck().check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_red_on_exception(self) -> None:
        def boom() -> bool:
            msg = "fail"
            raise RuntimeError(msg)

        result = await BrainIndexedCheck(is_loaded_fn=boom).check()
        assert result.status == CheckStatus.RED


# ── LLMReachableCheck ───────────────────────────────────────────────────────


class TestLLMReachableCheck:
    """LLMReachableCheck with async callable."""

    @pytest.mark.asyncio()
    async def test_green_when_available(self) -> None:
        fn = AsyncMock(return_value=[("anthropic", True), ("openai", True)])
        result = await LLMReachableCheck(provider_status_fn=fn).check()
        assert result.status == CheckStatus.GREEN
        assert "2 provider" in result.message

    @pytest.mark.asyncio()
    async def test_red_when_none_available(self) -> None:
        fn = AsyncMock(return_value=[("anthropic", False)])
        result = await LLMReachableCheck(provider_status_fn=fn).check()
        assert result.status == CheckStatus.RED

    @pytest.mark.asyncio()
    async def test_yellow_when_not_configured(self) -> None:
        result = await LLMReachableCheck().check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_red_on_exception(self) -> None:
        fn = AsyncMock(side_effect=RuntimeError("network"))
        result = await LLMReachableCheck(provider_status_fn=fn).check()
        assert result.status == CheckStatus.RED


# ── ModelLoadedCheck ────────────────────────────────────────────────────────


class TestModelLoadedCheck:
    """ModelLoadedCheck file existence."""

    @pytest.mark.asyncio()
    async def test_green_when_model_exists(self, tmp_path: Path) -> None:
        (tmp_path / "model.onnx").write_text("fake")
        result = await ModelLoadedCheck(model_dir=tmp_path).check()
        assert result.status == CheckStatus.GREEN
        assert "model.onnx" in result.message

    @pytest.mark.asyncio()
    async def test_yellow_when_no_model(self, tmp_path: Path) -> None:
        result = await ModelLoadedCheck(model_dir=tmp_path).check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_yellow_when_dir_missing(self) -> None:
        result = await ModelLoadedCheck(model_dir=Path("/nonexistent")).check()
        assert result.status == CheckStatus.YELLOW


# ── ChannelConnectedCheck ───────────────────────────────────────────────────


class TestChannelConnectedCheck:
    """ChannelConnectedCheck with callable."""

    @pytest.mark.asyncio()
    async def test_green_when_connected(self) -> None:
        fn = lambda: [("telegram", True)]  # noqa: E731
        result = await ChannelConnectedCheck(channel_status_fn=fn).check()
        assert result.status == CheckStatus.GREEN

    @pytest.mark.asyncio()
    async def test_red_when_disconnected(self) -> None:
        fn = lambda: [("telegram", False)]  # noqa: E731
        result = await ChannelConnectedCheck(channel_status_fn=fn).check()
        assert result.status == CheckStatus.RED

    @pytest.mark.asyncio()
    async def test_yellow_when_not_configured(self) -> None:
        result = await ChannelConnectedCheck().check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_red_on_exception(self) -> None:
        def boom() -> list[tuple[str, bool]]:
            msg = "fail"
            raise RuntimeError(msg)

        result = await ChannelConnectedCheck(channel_status_fn=boom).check()
        assert result.status == CheckStatus.RED


# ── ConsolidationCheck ──────────────────────────────────────────────────────


class TestConsolidationCheck:
    """ConsolidationCheck with callable."""

    @pytest.mark.asyncio()
    async def test_green_when_running(self) -> None:
        result = await ConsolidationCheck(is_running_fn=lambda: True).check()
        assert result.status == CheckStatus.GREEN

    @pytest.mark.asyncio()
    async def test_yellow_when_not_running(self) -> None:
        result = await ConsolidationCheck(is_running_fn=lambda: False).check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_yellow_when_not_configured(self) -> None:
        result = await ConsolidationCheck().check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_red_on_exception(self) -> None:
        def boom() -> bool:
            msg = "fail"
            raise RuntimeError(msg)

        result = await ConsolidationCheck(is_running_fn=boom).check()
        assert result.status == CheckStatus.RED


# ── CostBudgetCheck ─────────────────────────────────────────────────────────


class TestCostBudgetCheck:
    """CostBudgetCheck thresholds."""

    @pytest.mark.asyncio()
    async def test_green_under_80pct(self) -> None:
        result = await CostBudgetCheck(get_spend_fn=lambda: 0.5, daily_budget=1.0).check()
        assert result.status == CheckStatus.GREEN
        assert "$0.5000" in result.message

    @pytest.mark.asyncio()
    async def test_yellow_between_80_100pct(self) -> None:
        result = await CostBudgetCheck(get_spend_fn=lambda: 0.9, daily_budget=1.0).check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_red_over_100pct(self) -> None:
        result = await CostBudgetCheck(get_spend_fn=lambda: 1.5, daily_budget=1.0).check()
        assert result.status == CheckStatus.RED
        assert "exceeded" in result.message

    @pytest.mark.asyncio()
    async def test_yellow_when_not_configured(self) -> None:
        result = await CostBudgetCheck().check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_zero_budget_zero_spend(self) -> None:
        result = await CostBudgetCheck(get_spend_fn=lambda: 0.0, daily_budget=0.0).check()
        assert result.status == CheckStatus.GREEN

    @pytest.mark.asyncio()
    async def test_zero_budget_with_spend_is_red(self) -> None:
        """Any spend with zero budget should be RED (over budget)."""
        result = await CostBudgetCheck(get_spend_fn=lambda: 0.5, daily_budget=0.0).check()
        assert result.status == CheckStatus.RED

    @pytest.mark.asyncio()
    async def test_red_on_exception(self) -> None:
        def boom() -> float:
            msg = "fail"
            raise RuntimeError(msg)

        result = await CostBudgetCheck(get_spend_fn=boom).check()
        assert result.status == CheckStatus.RED


# ── create_default_registry ─────────────────────────────────────────────────


class TestCreateDefaultRegistry:
    """Factory creates registry with 10 checks."""

    def test_creates_10_checks(self) -> None:
        reg = create_default_registry()
        assert reg.check_count == 10

    @pytest.mark.asyncio()
    async def test_runs_all_without_crash(self) -> None:
        reg = create_default_registry()
        results = await reg.run_all(timeout=5.0)
        assert len(results) == 10
        # Unconfigured checks should be YELLOW, not RED
        for r in results:
            if "not configured" in r.message:
                assert r.status == CheckStatus.YELLOW

    @pytest.mark.asyncio()
    async def test_with_all_callbacks(self) -> None:
        reg = create_default_registry(
            db_write_fn=AsyncMock(),
            brain_loaded_fn=lambda: True,
            llm_status_fn=AsyncMock(return_value=[("test", True)]),
            channel_status_fn=lambda: [("telegram", True)],
            consolidation_fn=lambda: True,
            cost_spend_fn=lambda: 0.1,
            cost_budget=1.0,
        )
        results = await reg.run_all(timeout=5.0)
        assert len(results) == 10


# ── Offline Registry ────────────────────────────────────────────────────────


class TestCreateOfflineRegistry:
    """create_offline_registry returns a registry with only offline checks."""

    def test_has_four_checks(self) -> None:
        from sovyx.observability.health import create_offline_registry

        registry = create_offline_registry()
        assert len(registry._checks) == 4

    def test_check_names(self) -> None:
        from sovyx.observability.health import create_offline_registry

        registry = create_offline_registry()
        names = {c.name for c in registry._checks}
        assert names == {"Disk Space", "RAM", "CPU", "Embedding Model"}

    @pytest.mark.asyncio
    async def test_all_run_successfully(self) -> None:
        from sovyx.observability.health import create_offline_registry

        registry = create_offline_registry()
        results = await registry.run_all(timeout=10.0)
        assert len(results) == 4
        # All should return some result (not crash)
        for r in results:
            assert r.name
            assert r.status


# ── Package Exports ─────────────────────────────────────────────────────────


class TestObservabilityExports:
    """Verify health types are accessible from observability package."""

    def test_check_result_importable(self) -> None:
        from sovyx.observability import CheckResult

        assert CheckResult is not None

    def test_check_status_importable(self) -> None:
        from sovyx.observability import CheckStatus

        assert CheckStatus.GREEN.value == "green"

    def test_health_registry_importable(self) -> None:
        from sovyx.observability import HealthRegistry

        assert HealthRegistry is not None

    def test_create_default_registry_importable(self) -> None:
        from sovyx.observability import create_default_registry

        assert callable(create_default_registry)

    def test_create_offline_registry_importable(self) -> None:
        from sovyx.observability import create_offline_registry

        assert callable(create_offline_registry)
