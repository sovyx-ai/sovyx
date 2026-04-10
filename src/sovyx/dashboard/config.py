"""Dashboard config — read/write Mind configuration (personality, OCEAN, etc.).

GET: reads current MindConfig (personality, OCEAN, LLM, brain, safety).
PUT: updates mutable mind settings and persists to mind.yaml.

Separate from settings.py which handles EngineConfig (log level, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import yaml

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.mind.config import MindConfig

logger = get_logger(__name__)

# Fields that can be modified at runtime via PUT /api/config
_MUTABLE_SECTIONS = frozenset(
    {
        "personality",
        "ocean",
        "safety",
        "name",
        "language",
        "timezone",
        "llm",
    }
)


def get_config(mind_config: MindConfig) -> dict[str, Any]:
    """Build config response from MindConfig.

    Returns a dict with all mind configuration sections that the
    dashboard can display. Sensitive fields (API keys, tokens) are
    excluded.

    Args:
        mind_config: The active MindConfig instance.

    Returns:
        Dictionary with mind config sections.
    """
    p = mind_config.personality
    o = mind_config.ocean
    s = mind_config.safety
    b = mind_config.brain
    llm = mind_config.llm

    return {
        "name": mind_config.name,
        "id": str(mind_config.id),
        "language": mind_config.language,
        "timezone": mind_config.timezone,
        "template": mind_config.template,
        "personality": {
            "tone": p.tone,
            "formality": p.formality,
            "humor": p.humor,
            "assertiveness": p.assertiveness,
            "curiosity": p.curiosity,
            "empathy": p.empathy,
            "verbosity": p.verbosity,
        },
        "ocean": {
            "openness": o.openness,
            "conscientiousness": o.conscientiousness,
            "extraversion": o.extraversion,
            "agreeableness": o.agreeableness,
            "neuroticism": o.neuroticism,
        },
        "safety": {
            "child_safe_mode": s.child_safe_mode,
            "financial_confirmation": s.financial_confirmation,
            "content_filter": s.content_filter,
        },
        "brain": {
            "consolidation_interval_hours": b.consolidation_interval_hours,
            "dream_time": b.dream_time,
            "max_concepts": b.max_concepts,
            "forgetting_enabled": b.forgetting_enabled,
            "decay_rate": b.decay_rate,
            "min_strength": b.min_strength,
        },
        "llm": {
            "default_provider": llm.default_provider,
            "default_model": llm.default_model,
            "fast_model": llm.fast_model,
            "temperature": llm.temperature,
            "streaming": llm.streaming,
            "budget_daily_usd": llm.budget_daily_usd,
            "budget_per_conversation_usd": llm.budget_per_conversation_usd,
        },
    }


def apply_config(
    mind_config: MindConfig,
    updates: dict[str, Any],
    mind_yaml_path: Path | None = None,
) -> dict[str, str]:
    """Apply mutable config updates to a MindConfig.

    Only sections in _MUTABLE_SECTIONS are accepted. Each sub-field
    is validated against the Pydantic model before application.

    Args:
        mind_config: The active MindConfig to update.
        updates: Dictionary of updates. Keys are section names or
            top-level fields (name, language, timezone).
        mind_yaml_path: Path to mind.yaml for persistence. If None,
            changes are runtime-only.

    Returns:
        Dictionary of changes applied: {"section.field": "old → new"}.
    """
    changes: dict[str, str] = {}

    for key, value in updates.items():
        if key not in _MUTABLE_SECTIONS:
            continue

        if key == "personality" and isinstance(value, dict):
            _apply_personality(mind_config, value, changes)
        elif key == "ocean" and isinstance(value, dict):
            _apply_ocean(mind_config, value, changes)
        elif key == "safety" and isinstance(value, dict):
            _apply_safety(mind_config, value, changes)
        elif key == "name" and isinstance(value, str):
            old = mind_config.name
            if old != value and value.strip():
                mind_config.name = value.strip()
                changes["name"] = f"{old} → {value.strip()}"
        elif key == "language" and isinstance(value, str):
            old = mind_config.language
            if old != value and value.strip():
                mind_config.language = value.strip()
                changes["language"] = f"{old} → {value.strip()}"
        elif key == "timezone" and isinstance(value, str):
            old = mind_config.timezone
            if old != value and value.strip():
                mind_config.timezone = value.strip()
                changes["timezone"] = f"{old} → {value.strip()}"

    if changes and mind_yaml_path is not None:
        _persist_to_yaml(mind_config, mind_yaml_path)

    if changes:
        logger.info("mind_config_updated", changes=list(changes.keys()))

    return changes


def _apply_personality(
    mind_config: MindConfig,
    updates: dict[str, Any],
    changes: dict[str, str],
) -> None:
    """Apply personality trait updates with validation."""
    p = mind_config.personality

    # Tone (enum)
    if "tone" in updates:
        valid_tones = ("warm", "neutral", "direct", "playful")
        tone = str(updates["tone"]).lower()
        if tone in valid_tones and tone != p.tone:
            old = p.tone
            p.tone = cast("Any", tone)
            changes["personality.tone"] = f"{old} → {tone}"

    # Float traits [0.0, 1.0]
    float_traits = ("formality", "humor", "assertiveness", "curiosity", "empathy", "verbosity")
    for trait in float_traits:
        if trait in updates:
            try:
                val = float(updates[trait])
            except (TypeError, ValueError):
                continue
            if not 0.0 <= val <= 1.0:
                continue
            old_val = getattr(p, trait)
            if old_val != val:
                setattr(p, trait, val)
                changes[f"personality.{trait}"] = f"{old_val} → {val}"


def _apply_ocean(
    mind_config: MindConfig,
    updates: dict[str, Any],
    changes: dict[str, str],
) -> None:
    """Apply OCEAN personality model updates."""
    o = mind_config.ocean
    traits = ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism")

    for trait in traits:
        if trait in updates:
            try:
                val = float(updates[trait])
            except (TypeError, ValueError):
                continue
            if not 0.0 <= val <= 1.0:
                continue
            old_val = getattr(o, trait)
            if old_val != val:
                setattr(o, trait, val)
                changes[f"ocean.{trait}"] = f"{old_val} → {val}"


def _apply_safety(
    mind_config: MindConfig,
    updates: dict[str, Any],
    changes: dict[str, str],
) -> None:
    """Apply safety configuration updates."""
    s = mind_config.safety

    if "child_safe_mode" in updates:
        val = bool(updates["child_safe_mode"])
        if val != s.child_safe_mode:
            old = s.child_safe_mode
            s.child_safe_mode = val
            changes["safety.child_safe_mode"] = f"{old} → {val}"

    if "financial_confirmation" in updates:
        val = bool(updates["financial_confirmation"])
        if val != s.financial_confirmation:
            old = s.financial_confirmation
            s.financial_confirmation = val
            changes["safety.financial_confirmation"] = f"{old} → {val}"

    if "content_filter" in updates:
        valid_filters = ("none", "standard", "strict")
        cf = str(updates["content_filter"]).lower()
        if cf in valid_filters and cf != s.content_filter:
            old_cf = s.content_filter
            s.content_filter = cast("Any", cf)
            changes["safety.content_filter"] = f"{old_cf} → {cf}"


def _persist_to_yaml(mind_config: MindConfig, mind_yaml_path: Path) -> None:
    """Persist current MindConfig to mind.yaml.

    Preserves existing YAML structure and only updates changed sections.
    """
    try:
        data: dict[str, Any] = {}
        if mind_yaml_path.exists():
            with mind_yaml_path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        # Serialize the full config and merge
        serialized = mind_config.model_dump(mode="json")

        # Update mutable sections
        for section in _MUTABLE_SECTIONS:
            if section in serialized:
                data[section] = serialized[section]

        # Convert MindId back to string for YAML
        if "id" in data and hasattr(data["id"], "__str__"):
            data["id"] = str(data["id"])

        with mind_yaml_path.open("w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        logger.debug("mind_config_persisted", path=str(mind_yaml_path))
    except Exception:  # noqa: BLE001
        logger.warning("mind_config_persist_failed", path=str(mind_yaml_path))
