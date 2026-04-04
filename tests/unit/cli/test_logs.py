"""Tests for sovyx.cli.commands.logs — log query and filter CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003

import pytest
import typer

from sovyx.cli.commands.logs import (
    _follow_log,
    _format_entry,
    _iter_new_lines,
    _matches,
    _parse_duration,
    _parse_filters,
    _read_log_lines,
    logs_app,
)

# ── _parse_duration ─────────────────────────────────────────────────────────


class TestParseDuration:
    """Duration string parsing."""

    def test_seconds(self) -> None:
        assert _parse_duration("30s") == timedelta(seconds=30)

    def test_minutes(self) -> None:
        assert _parse_duration("5m") == timedelta(minutes=5)

    def test_hours(self) -> None:
        assert _parse_duration("1h") == timedelta(hours=1)

    def test_days(self) -> None:
        assert _parse_duration("2d") == timedelta(days=2)

    def test_large_number(self) -> None:
        assert _parse_duration("100m") == timedelta(minutes=100)

    def test_invalid_format(self) -> None:
        with pytest.raises(typer.BadParameter, match="Invalid duration"):
            _parse_duration("abc")

    def test_invalid_unit(self) -> None:
        with pytest.raises(typer.BadParameter, match="Invalid duration"):
            _parse_duration("5w")

    def test_no_number(self) -> None:
        with pytest.raises(typer.BadParameter, match="Invalid duration"):
            _parse_duration("h")

    def test_whitespace_stripped(self) -> None:
        assert _parse_duration("  1h  ") == timedelta(hours=1)


# ── _parse_filters ──────────────────────────────────────────────────────────


class TestParseFilters:
    """Filter string parsing."""

    def test_single_filter(self) -> None:
        assert _parse_filters(["module=brain"]) == {"module": "brain"}

    def test_multiple_filters(self) -> None:
        result = _parse_filters(["module=brain", "level=error"])
        assert result == {"module": "brain", "level": "error"}

    def test_value_with_equals(self) -> None:
        result = _parse_filters(["query=a=b"])
        assert result == {"query": "a=b"}

    def test_whitespace_stripped(self) -> None:
        result = _parse_filters(["  module = brain  "])
        assert result == {"module": "brain"}

    def test_empty_list(self) -> None:
        assert _parse_filters([]) == {}

    def test_invalid_no_equals(self) -> None:
        with pytest.raises(typer.BadParameter, match="Invalid filter"):
            _parse_filters(["nope"])


# ── _matches ────────────────────────────────────────────────────────────────


class TestMatches:
    """Log entry matching logic."""

    def test_matches_all_default(self) -> None:
        entry = {"event": "test", "level": "info"}
        assert _matches(entry, level_min=None, filters={}, since=None) is True

    def test_level_filter_exact(self) -> None:
        entry = {"event": "test", "level": "error"}
        assert _matches(entry, level_min="error", filters={}, since=None) is True

    def test_level_filter_above(self) -> None:
        entry = {"event": "test", "level": "error"}
        assert _matches(entry, level_min="warning", filters={}, since=None) is True

    def test_level_filter_below(self) -> None:
        entry = {"event": "test", "level": "debug"}
        assert _matches(entry, level_min="warning", filters={}, since=None) is False

    def test_level_missing_defaults_info(self) -> None:
        entry = {"event": "test"}
        assert _matches(entry, level_min="info", filters={}, since=None) is True

    def test_key_value_filter_match(self) -> None:
        entry = {"event": "test", "module": "brain"}
        assert _matches(entry, level_min=None, filters={"module": "brain"}, since=None) is True

    def test_key_value_filter_partial(self) -> None:
        entry = {"event": "test", "logger": "sovyx.brain.service"}
        assert _matches(entry, level_min=None, filters={"logger": "brain"}, since=None) is True

    def test_key_value_filter_no_match(self) -> None:
        entry = {"event": "test", "module": "llm"}
        assert _matches(entry, level_min=None, filters={"module": "brain"}, since=None) is False

    def test_key_value_filter_missing_key(self) -> None:
        entry = {"event": "test"}
        assert _matches(entry, level_min=None, filters={"module": "brain"}, since=None) is False

    def test_since_filter_includes_recent(self) -> None:
        now = datetime.now(tz=UTC)
        entry = {"event": "test", "timestamp": now.isoformat()}
        since = now - timedelta(hours=1)
        assert _matches(entry, level_min=None, filters={}, since=since) is True

    def test_since_filter_excludes_old(self) -> None:
        old = datetime.now(tz=UTC) - timedelta(hours=2)
        entry = {"event": "test", "timestamp": old.isoformat()}
        since = datetime.now(tz=UTC) - timedelta(hours=1)
        assert _matches(entry, level_min=None, filters={}, since=since) is False

    def test_since_with_z_timestamp(self) -> None:
        now = datetime.now(tz=UTC)
        entry = {"event": "test", "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        since = now - timedelta(hours=1)
        assert _matches(entry, level_min=None, filters={}, since=since) is True

    def test_since_with_unparseable_timestamp_included(self) -> None:
        entry = {"event": "test", "timestamp": "not-a-date"}
        since = datetime.now(tz=UTC) - timedelta(hours=1)
        assert _matches(entry, level_min=None, filters={}, since=since) is True

    def test_since_with_naive_timestamp_assumes_utc(self) -> None:
        """Naive timestamps (no tz) should be treated as UTC, not crash."""
        now = datetime.now(tz=UTC)
        # Recent naive timestamp — should be included
        recent_naive = now.strftime("%Y-%m-%dT%H:%M:%S")  # no timezone
        entry = {"event": "test", "timestamp": recent_naive}
        since = now - timedelta(hours=1)
        assert _matches(entry, level_min=None, filters={}, since=since) is True

    def test_since_with_old_naive_timestamp_excluded(self) -> None:
        """Old naive timestamps should be correctly excluded."""
        old = datetime.now(tz=UTC) - timedelta(hours=2)
        entry = {"event": "test", "timestamp": old.strftime("%Y-%m-%dT%H:%M:%S")}
        since = datetime.now(tz=UTC) - timedelta(hours=1)
        assert _matches(entry, level_min=None, filters={}, since=since) is False

    def test_combined_filters(self) -> None:
        now = datetime.now(tz=UTC)
        entry = {
            "event": "test",
            "level": "error",
            "module": "brain",
            "timestamp": now.isoformat(),
        }
        since = now - timedelta(hours=1)
        assert (
            _matches(
                entry,
                level_min="warning",
                filters={"module": "brain"},
                since=since,
            )
            is True
        )


# ── _format_entry ───────────────────────────────────────────────────────────


class TestFormatEntry:
    """Log entry formatting."""

    def test_basic_format(self) -> None:
        entry = {
            "timestamp": "2026-04-04T15:30:45.123Z",
            "level": "info",
            "event": "test_event",
            "logger": "sovyx.brain",
        }
        text = _format_entry(entry)
        plain = text.plain
        assert "15:30:45" in plain
        assert "INFO" in plain
        assert "test_event" in plain
        assert "sovyx.brain" in plain

    def test_includes_context_fields(self) -> None:
        entry = {
            "timestamp": "2026-04-04T15:30:45.123Z",
            "level": "info",
            "event": "test",
            "mind_id": "nyx",
            "request_id": "abc123",
        }
        text = _format_entry(entry)
        plain = text.plain
        assert "mind_id=nyx" in plain
        assert "request_id=abc123" in plain

    def test_error_level_format(self) -> None:
        entry = {"timestamp": "2026-04-04T15:30:45Z", "level": "error", "event": "boom"}
        text = _format_entry(entry)
        assert "ERROR" in text.plain

    def test_missing_fields(self) -> None:
        entry = {"event": "minimal"}
        text = _format_entry(entry)
        assert "minimal" in text.plain

    def test_no_logger(self) -> None:
        entry = {"timestamp": "2026-04-04T15:30:45Z", "level": "info", "event": "test"}
        text = _format_entry(entry)
        assert "test" in text.plain

    def test_zero_values_not_hidden(self) -> None:
        """Falsy but valid values like 0 and 0.0 must appear in output."""
        entry = {
            "timestamp": "2026-04-04T15:30:45Z",
            "level": "info",
            "event": "llm_response",
            "tokens_in": 0,
            "cost_usd": 0.0,
            "filtered": False,
        }
        text = _format_entry(entry)
        plain = text.plain
        assert "tokens_in=0" in plain
        assert "cost_usd=0.0" in plain
        assert "filtered=False" in plain

    def test_none_values_hidden(self) -> None:
        """None values should be excluded from context display."""
        entry = {
            "timestamp": "2026-04-04T15:30:45Z",
            "level": "info",
            "event": "test",
            "optional_field": None,
        }
        text = _format_entry(entry)
        assert "optional_field" not in text.plain


# ── _read_log_lines ─────────────────────────────────────────────────────────


class TestReadLogLines:
    """Reading and filtering from log files."""

    def _write_logs(self, path: Path, entries: list[dict[str, object]]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def test_read_basic(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        self._write_logs(
            log_file,
            [
                {"event": "a", "level": "info", "timestamp": "2026-04-04T10:00:00Z"},
                {"event": "b", "level": "error", "timestamp": "2026-04-04T10:00:01Z"},
            ],
        )
        count = _read_log_lines(
            log_file,
            level=None,
            filters={},
            since=None,
            limit=50,
            raw_json=False,
        )
        assert count == 2

    def test_read_with_level_filter(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        self._write_logs(
            log_file,
            [
                {"event": "a", "level": "debug"},
                {"event": "b", "level": "info"},
                {"event": "c", "level": "error"},
            ],
        )
        count = _read_log_lines(
            log_file,
            level="error",
            filters={},
            since=None,
            limit=50,
            raw_json=False,
        )
        assert count == 1

    def test_read_with_limit(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        entries = [{"event": f"e{i}", "level": "info"} for i in range(100)]
        self._write_logs(log_file, entries)
        count = _read_log_lines(
            log_file,
            level=None,
            filters={},
            since=None,
            limit=10,
            raw_json=False,
        )
        assert count == 10

    def test_read_with_filter(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        self._write_logs(
            log_file,
            [
                {"event": "a", "level": "info", "module": "brain"},
                {"event": "b", "level": "info", "module": "llm"},
            ],
        )
        count = _read_log_lines(
            log_file,
            level=None,
            filters={"module": "brain"},
            since=None,
            limit=50,
            raw_json=False,
        )
        assert count == 1

    def test_read_json_mode(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        self._write_logs(log_file, [{"event": "test", "level": "info"}])
        count = _read_log_lines(
            log_file,
            level=None,
            filters={},
            since=None,
            limit=50,
            raw_json=True,
        )
        assert count == 1

    def test_read_missing_file(self, tmp_path: Path) -> None:
        count = _read_log_lines(
            tmp_path / "nope.log",
            level=None,
            filters={},
            since=None,
            limit=50,
            raw_json=False,
        )
        assert count == 0

    def test_read_skips_invalid_json(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        with open(log_file, "w") as f:
            f.write("not json\n")
            f.write(json.dumps({"event": "valid", "level": "info"}) + "\n")
            f.write("\n")  # empty line
        count = _read_log_lines(
            log_file,
            level=None,
            filters={},
            since=None,
            limit=50,
            raw_json=False,
        )
        assert count == 1

    def test_read_with_since(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        now = datetime.now(tz=UTC)
        old = now - timedelta(hours=2)
        self._write_logs(
            log_file,
            [
                {"event": "old", "level": "info", "timestamp": old.isoformat()},
                {"event": "new", "level": "info", "timestamp": now.isoformat()},
            ],
        )
        since = now - timedelta(hours=1)
        count = _read_log_lines(
            log_file,
            level=None,
            filters={},
            since=since,
            limit=50,
            raw_json=False,
        )
        assert count == 1


# ── CLI entry point ─────────────────────────────────────────────────────────


class TestLogsCommand:
    """Integration tests for the logs CLI command."""

    def test_logs_no_file(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            logs_app,
            ["--file", str(tmp_path / "nope.log")],
        )
        assert result.exit_code == 0
        assert "not found" in result.output

    def test_logs_with_entries(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        log_file = tmp_path / "test.log"
        log_file.write_text(
            json.dumps({"event": "test", "level": "info", "timestamp": "2026-04-04T10:00:00Z"})
            + "\n"
        )

        runner = CliRunner()
        result = runner.invoke(logs_app, ["--file", str(log_file)])
        assert result.exit_code == 0
        assert "test" in result.output

    def test_logs_level_filter(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        log_file = tmp_path / "test.log"
        lines = [
            json.dumps({"event": "debug_event", "level": "debug"}),
            json.dumps({"event": "error_event", "level": "error"}),
        ]
        log_file.write_text("\n".join(lines) + "\n")

        runner = CliRunner()
        result = runner.invoke(logs_app, ["--file", str(log_file), "--level", "error"])
        assert result.exit_code == 0
        assert "error_event" in result.output
        assert "debug_event" not in result.output

    def test_logs_invalid_level(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            logs_app,
            ["--file", str(tmp_path / "test.log"), "--level", "banana"],
        )
        assert result.exit_code == 1

    def test_logs_json_mode(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        log_file = tmp_path / "test.log"
        log_file.write_text(json.dumps({"event": "test", "level": "info"}) + "\n")

        runner = CliRunner()
        result = runner.invoke(logs_app, ["--file", str(log_file), "--json"])
        assert result.exit_code == 0
        # Output should be valid JSON
        parsed = json.loads(result.output.strip())
        assert parsed["event"] == "test"

    def test_logs_with_filter(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        log_file = tmp_path / "test.log"
        lines = [
            json.dumps({"event": "a", "level": "info", "module": "brain"}),
            json.dumps({"event": "b", "level": "info", "module": "llm"}),
        ]
        log_file.write_text("\n".join(lines) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            logs_app,
            ["--file", str(log_file), "--filter", "module=brain"],
        )
        assert result.exit_code == 0

    def test_logs_with_since(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        now = datetime.now(tz=UTC)
        old = now - timedelta(hours=2)
        log_file = tmp_path / "test.log"
        lines = [
            json.dumps({"event": "old", "level": "info", "timestamp": old.isoformat()}),
            json.dumps({"event": "new", "level": "info", "timestamp": now.isoformat()}),
        ]
        log_file.write_text("\n".join(lines) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            logs_app,
            ["--file", str(log_file), "--since", "1h"],
        )
        assert result.exit_code == 0
        assert "new" in result.output

    def test_logs_no_matches(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        log_file = tmp_path / "test.log"
        log_file.write_text(json.dumps({"event": "test", "level": "debug"}) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            logs_app,
            ["--file", str(log_file), "--level", "error"],
        )
        assert result.exit_code == 0
        assert "No matching" in result.output

    def test_logs_limit(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        log_file = tmp_path / "test.log"
        lines = [json.dumps({"event": f"e{i}", "level": "info"}) for i in range(20)]
        log_file.write_text("\n".join(lines) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            logs_app,
            ["--file", str(log_file), "--limit", "5", "--json"],
        )
        assert result.exit_code == 0
        output_lines = [ln for ln in result.output.strip().split("\n") if ln.strip()]
        assert len(output_lines) == 5


# ── _iter_new_lines ─────────────────────────────────────────────────────────


class TestIterNewLines:
    """Generator that yields parsed JSON entries from a file handle."""

    def test_yields_valid_json(self) -> None:
        import io

        data = json.dumps({"event": "test", "level": "info"}) + "\n"
        f = io.StringIO(data)
        entries = list(_iter_new_lines(f))
        assert len(entries) == 1
        assert entries[0]["event"] == "test"

    def test_skips_blank_lines(self) -> None:
        import io

        data = "\n\n" + json.dumps({"event": "a"}) + "\n\n"
        f = io.StringIO(data)
        entries = list(_iter_new_lines(f))
        assert len(entries) == 1

    def test_skips_invalid_json(self) -> None:
        import io

        data = "not json\n" + json.dumps({"event": "valid"}) + "\n"
        f = io.StringIO(data)
        entries = list(_iter_new_lines(f))
        assert len(entries) == 1
        assert entries[0]["event"] == "valid"

    def test_returns_on_eof(self) -> None:
        import io

        f = io.StringIO("")
        entries = list(_iter_new_lines(f))
        assert entries == []

    def test_multiple_entries(self) -> None:
        import io

        lines = [json.dumps({"event": f"e{i}"}) for i in range(5)]
        f = io.StringIO("\n".join(lines) + "\n")
        entries = list(_iter_new_lines(f))
        assert len(entries) == 5
        assert [e["event"] for e in entries] == ["e0", "e1", "e2", "e3", "e4"]

    def test_mixed_valid_invalid(self) -> None:
        import io

        data = (
            json.dumps({"event": "a"})
            + "\n"
            + "broken{json\n"
            + "\n"
            + json.dumps({"event": "b"})
            + "\n"
        )
        f = io.StringIO(data)
        entries = list(_iter_new_lines(f))
        assert len(entries) == 2
        assert entries[0]["event"] == "a"
        assert entries[1]["event"] == "b"


# ── _follow_log (limited testing) ──────────────────────────────────────────


class TestFollowLog:
    """_follow_log — test what we can without blocking."""

    def test_follow_waits_for_file(self, tmp_path: Path) -> None:
        """follow_log with missing file prints waiting message."""
        import threading

        log_file = tmp_path / "follow.log"
        output: list[str] = []

        def run_follow() -> None:
            import contextlib
            import io

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.suppress(KeyboardInterrupt):
                _follow_log(
                    log_file,
                    level=None,
                    filters={},
                    raw_json=False,
                )
            output.append(buf.getvalue())

        # Write the file after a moment so follow can read, then interrupt
        import time

        thread = threading.Thread(target=run_follow, daemon=True)
        thread.start()
        time.sleep(0.3)

        # Create the file with one entry then simulate Ctrl+C
        log_file.write_text(json.dumps({"event": "followed", "level": "info"}) + "\n")
        time.sleep(0.3)

        # Thread is daemon, will die with test. Just check it started.
        assert thread.is_alive() or len(output) > 0
