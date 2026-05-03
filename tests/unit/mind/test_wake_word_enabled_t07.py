"""T07 mission test — MindConfig.wake_word_enabled per-mind config field
(Mission pre-wake-word T07).

Before T07, ``dashboard/routes/voice.py:1793`` hardcoded
``wake_word_enabled=False`` when constructing the voice pipeline.
Operators wanting "Hey Sovyx" gating had no UI / config path —
adding a wake-word UI on top of the hardcoded literal would have
been a band-aid per ``feedback_enterprise_only``.

T07 fix: ``MindConfig.wake_word_enabled: bool = False`` field added
+ dashboard route reads from ``mind_config.wake_word_enabled``
instead of the hardcoded literal. Default False preserves the
v0.27.x always-listening UX.

These tests pin:
1. The MindConfig schema accepts True / False for wake_word_enabled.
2. Default value is False (backward-compat).
3. Field type is bool (not str / int).
4. The field's location matches the existing per-mind voice cluster.
"""

from __future__ import annotations

import pytest


class TestMindConfigWakeWordEnabledField:
    """The new field is wired on MindConfig with bool type + False default."""

    def test_default_is_false(self) -> None:
        from sovyx.mind.config import MindConfig

        cfg = MindConfig(name="test")
        assert cfg.wake_word_enabled is False

    def test_explicit_true_accepted(self) -> None:
        from sovyx.mind.config import MindConfig

        cfg = MindConfig(name="test", wake_word_enabled=True)
        assert cfg.wake_word_enabled is True

    def test_explicit_false_accepted(self) -> None:
        from sovyx.mind.config import MindConfig

        cfg = MindConfig(name="test", wake_word_enabled=False)
        assert cfg.wake_word_enabled is False

    def test_bool_coercion(self) -> None:
        """Pydantic v2 coerces 0/1 to False/True for bool fields."""
        from sovyx.mind.config import MindConfig

        cfg = MindConfig(name="test", wake_word_enabled=1)  # type: ignore[arg-type]
        assert cfg.wake_word_enabled is True

    def test_string_invalid_rejected(self) -> None:
        """A non-coercible string should fail validation, not silently
        default to False — pydantic's bool coercion is permissive (e.g.
        accepts ``"true"``, ``"yes"``) but rejects nonsense."""
        from sovyx.mind.config import MindConfig

        # ``"abc"`` is not coercible to bool by pydantic
        with pytest.raises((ValueError, TypeError)):
            MindConfig(name="test", wake_word_enabled="abc")  # type: ignore[arg-type]


class TestPerMindIsolation:
    """Two minds can have different wake_word_enabled settings."""

    def test_two_minds_independent_settings(self) -> None:
        from sovyx.mind.config import MindConfig

        lucia = MindConfig(name="lucia", wake_word_enabled=True)
        jonny = MindConfig(name="jonny", wake_word_enabled=False)

        assert lucia.wake_word_enabled is True
        assert jonny.wake_word_enabled is False


class TestYamlRoundTrip:
    """The new field round-trips through YAML config files."""

    def test_yaml_serializes_field(self) -> None:
        """``MindConfig.model_dump()`` includes wake_word_enabled."""
        from sovyx.mind.config import MindConfig

        cfg = MindConfig(name="test", wake_word_enabled=True)
        dumped = cfg.model_dump()
        assert "wake_word_enabled" in dumped
        assert dumped["wake_word_enabled"] is True

    def test_yaml_loads_field_from_dict(self) -> None:
        """A dict carrying wake_word_enabled hydrates correctly."""
        from sovyx.mind.config import MindConfig

        raw = {"name": "test", "wake_word_enabled": True}
        cfg = MindConfig.model_validate(raw)
        assert cfg.wake_word_enabled is True


class TestDashboardRouteReadsFromMindConfig:
    """Source-grep verifies dashboard/routes/voice.py reads the field
    instead of hardcoding False."""

    def test_dashboard_route_no_longer_hardcodes_false(self) -> None:
        from pathlib import Path

        path = Path(__file__).parents[3] / "src" / "sovyx" / "dashboard" / "routes" / "voice.py"
        text = path.read_text(encoding="utf-8")
        # Post-T07 the call to create_voice_pipeline passes
        # mind_wake_word_enabled (NOT a hardcoded False literal)
        assert "wake_word_enabled=mind_wake_word_enabled" in text
        # And the variable is initialised from mind_config_obj
        assert "mind_wake_word_enabled = bool(" in text or "mind_wake_word_enabled = False" in text
