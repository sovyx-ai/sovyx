"""VAL-13: Coverage gaps for mind/personality.py and mind/config.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


class TestPersonalityDescriptors:
    """Cover all branches of descriptor functions."""

    def test_assertiveness_low(self) -> None:
        from sovyx.mind.personality import _assertiveness_desc

        msg = _assertiveness_desc(0.1)
        assert "defer" in msg

    def test_assertiveness_mid(self) -> None:
        from sovyx.mind.personality import _assertiveness_desc

        msg = _assertiveness_desc(0.5)
        assert "don't push" in msg

    def test_assertiveness_high(self) -> None:
        from sovyx.mind.personality import _assertiveness_desc

        msg = _assertiveness_desc(0.9)
        assert "confidently" in msg

    def test_curiosity_low(self) -> None:
        from sovyx.mind.personality import _curiosity_desc

        msg = _curiosity_desc(0.1)
        assert "tangent" in msg

    def test_curiosity_mid(self) -> None:
        from sovyx.mind.personality import _curiosity_desc

        msg = _curiosity_desc(0.5)
        assert "occasionally" in msg

    def test_curiosity_high(self) -> None:
        from sovyx.mind.personality import _curiosity_desc

        msg = _curiosity_desc(0.9)
        assert "follow-up" in msg

    def test_empathy_low(self) -> None:
        from sovyx.mind.personality import _empathy_desc

        msg = _empathy_desc(0.1)
        assert "solutions" in msg

    def test_empathy_mid(self) -> None:
        from sovyx.mind.personality import _empathy_desc

        msg = _empathy_desc(0.5)
        assert "balance" in msg

    def test_empathy_high(self) -> None:
        from sovyx.mind.personality import _empathy_desc

        msg = _empathy_desc(0.9)
        assert "acknowledge" in msg


class TestMindConfigReadError:
    def test_unreadable_file_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OSError during read raises MindConfigError."""
        from sovyx.mind.config import MindConfigError, load_mind_config

        bad_path = tmp_path / "mind.yaml"
        bad_path.write_text("name: test")

        # Simulate unreadable file (root ignores chmod 0o000)
        def _raise_oserror(*_args: object, **_kwargs: object) -> None:  # noqa: ANN401
            raise OSError("Permission denied")

        from pathlib import Path as _Path

        monkeypatch.setattr(_Path, "read_text", _raise_oserror)
        with pytest.raises(MindConfigError, match="Failed to read"):
            load_mind_config(bad_path)
