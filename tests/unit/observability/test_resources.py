"""Regression tests for ``sovyx.observability.resources``.

Focus on the Windows shutdown-hang fix: ``_capture_psutil_metrics``
must skip ``proc.open_files()`` and ``proc.net_connections()`` when
``skip_expensive=True`` (the path taken on the ``final=True`` snapshot
during shutdown). Skipping is the only safe option because the inner
``os.stat()`` call on Windows blocks indefinitely on handles that are
in the closing state — try/except cannot catch a blocked syscall.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sovyx.observability.resources import _capture_psutil_metrics


class TestSkipExpensive:
    """``skip_expensive=True`` MUST NOT call ``open_files``/``net_connections``."""

    def test_skip_expensive_does_not_call_open_files(self) -> None:
        mock_proc = MagicMock()
        mock_proc.cpu_percent.return_value = 0.0
        mock_proc.memory_info.return_value = MagicMock(rss=100, vms=200)
        mock_proc.num_threads.return_value = 4
        mock_proc.num_handles.return_value = 50
        mock_proc.num_fds.return_value = 50

        with patch("psutil.Process", return_value=mock_proc):
            result = _capture_psutil_metrics(skip_expensive=True)

        mock_proc.open_files.assert_not_called()
        mock_proc.net_connections.assert_not_called()
        assert result["process.open_files_count"] is None
        assert result["process.connections_count"] is None

    def test_skip_expensive_still_returns_cheap_metrics(self) -> None:
        mock_proc = MagicMock()
        mock_proc.cpu_percent.return_value = 12.5
        mock_proc.memory_info.return_value = MagicMock(rss=1000, vms=2000)
        mock_proc.num_threads.return_value = 8
        mock_proc.num_handles.return_value = 99
        mock_proc.num_fds.return_value = 99

        with patch("psutil.Process", return_value=mock_proc):
            result = _capture_psutil_metrics(skip_expensive=True)

        assert result["process.cpu_percent"] == 12.5
        assert result["process.rss_bytes"] == 1000
        assert result["process.vms_bytes"] == 2000
        assert result["process.num_threads"] == 8

    def test_default_does_call_open_files(self) -> None:
        mock_proc = MagicMock()
        mock_proc.cpu_percent.return_value = 0.0
        mock_proc.memory_info.return_value = MagicMock(rss=100, vms=200)
        mock_proc.num_threads.return_value = 4
        mock_proc.num_handles.return_value = 50
        mock_proc.num_fds.return_value = 50
        mock_proc.open_files.return_value = [MagicMock(), MagicMock()]
        mock_proc.net_connections.return_value = [MagicMock()]

        with patch("psutil.Process", return_value=mock_proc):
            result = _capture_psutil_metrics()

        mock_proc.open_files.assert_called_once()
        mock_proc.net_connections.assert_called_once_with(kind="inet")
        assert result["process.open_files_count"] == 2
        assert result["process.connections_count"] == 1

    def test_skip_expensive_keyword_only(self) -> None:
        with patch("psutil.Process") as mock_proc_cls:
            mock_proc = MagicMock()
            mock_proc.cpu_percent.return_value = 0.0
            mock_proc.memory_info.return_value = MagicMock(rss=0, vms=0)
            mock_proc.num_threads.return_value = 1
            mock_proc.num_handles.return_value = 1
            mock_proc.num_fds.return_value = 1
            mock_proc_cls.return_value = mock_proc

            try:
                _capture_psutil_metrics(True)  # type: ignore[misc]
            except TypeError:
                pass
            else:
                msg = "skip_expensive must be keyword-only to prevent positional misuse"
                raise AssertionError(msg)
