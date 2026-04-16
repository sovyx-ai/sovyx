"""Sovyx Mind configuration — load and validate mind.yaml.

"Mind is configuration, not code." The entire personality, behavior,
and capabilities of a Sovyx instance are defined in mind.yaml.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from sovyx.engine.errors import MindConfigError
from sovyx.engine.types import MindId
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


class PersonalityConfig(BaseModel):
    """Personality traits controlling conversational style."""

    tone: Literal["warm", "neutral", "direct", "playful"] = "warm"
    formality: float = Field(default=0.5, ge=0.0, le=1.0)
    humor: float = Field(default=0.4, ge=0.0, le=1.0)
    assertiveness: float = Field(default=0.6, ge=0.0, le=1.0)
    curiosity: float = Field(default=0.7, ge=0.0, le=1.0)
    empathy: float = Field(default=0.8, ge=0.0, le=1.0)
    verbosity: float = Field(default=0.5, ge=0.0, le=1.0)


class OceanConfig(BaseModel):
    """Big Five personality model (OCEAN)."""

    openness: float = Field(default=0.7, ge=0.0, le=1.0)
    conscientiousness: float = Field(default=0.6, ge=0.0, le=1.0)
    extraversion: float = Field(default=0.5, ge=0.0, le=1.0)
    agreeableness: float = Field(default=0.7, ge=0.0, le=1.0)
    neuroticism: float = Field(default=0.3, ge=0.0, le=1.0)


class LLMConfig(BaseModel):
    """LLM provider configuration.

    Runtime auto-detection: empty strings (``""``) mean "detect at startup".
    When ``sovyx start`` runs, the model_validator resolves empties based
    on which API keys are present in the environment:

        - ANTHROPIC_API_KEY → claude-sonnet-4-20250514 (preferred)
        - OPENAI_API_KEY → gpt-4o
        - GOOGLE_API_KEY → gemini-2.5-pro-preview-03-25
        - None → error at startup with clear message

    Users can always override by setting explicit values in mind.yaml.
    """

    default_provider: str = ""
    default_model: str = ""
    fast_model: str = ""
    local_model: str = "llama3.2:1b"
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    streaming: bool = True
    budget_daily_usd: float = Field(default=2.0, ge=0.0)
    budget_per_conversation_usd: float = Field(default=0.5, ge=0.0)

    @model_validator(mode="after")
    def resolve_provider_at_runtime(self) -> LLMConfig:
        """Resolve empty provider/model fields from environment API keys.

        This runs both at init-time (where keys may not be set yet,
        leaving fields empty for YAML serialization) and at start-time
        (where keys ARE set and fields get resolved).
        """
        import os

        has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
        has_openai = bool(os.environ.get("OPENAI_API_KEY"))
        has_google = bool(os.environ.get("GOOGLE_API_KEY"))
        has_xai = bool(os.environ.get("XGROK_API_KEY"))
        has_deepseek = bool(os.environ.get("DEEPSEEK_API_KEY"))
        has_mistral = bool(os.environ.get("MISTRAL_API_KEY"))
        has_groq = bool(os.environ.get("GROQ_API_KEY"))

        # Priority: Anthropic > OpenAI > Google > xAI > DeepSeek >
        # Mistral > Groq > Together > Fireworks.
        _default_chain: list[tuple[bool, str, str]] = [
            (has_anthropic, "anthropic", "claude-sonnet-4-20250514"),
            (has_openai, "openai", "gpt-4o"),
            (has_google, "google", "gemini-2.5-pro-preview-03-25"),
            (has_xai, "xai", "grok-2"),
            (has_deepseek, "deepseek", "deepseek-chat"),
            (has_mistral, "mistral", "mistral-large-latest"),
            (has_groq, "groq", "llama-3.1-70b-versatile"),
        ]

        if not self.default_model:
            for available, _prov, model in _default_chain:
                if available:
                    self.default_model = model
                    break

        if not self.default_provider:
            for available, prov, _model in _default_chain:
                if available:
                    self.default_provider = prov
                    break

        if not self.fast_model:
            if has_anthropic:
                self.fast_model = "claude-3-5-haiku-20241022"
            elif has_openai:
                self.fast_model = "gpt-4o-mini"
            elif has_google:
                self.fast_model = "gemini-2.0-flash"
            elif has_deepseek:
                self.fast_model = "deepseek-chat"
            elif has_mistral:
                self.fast_model = "mistral-small-latest"
            elif has_groq:
                self.fast_model = "mixtral-8x7b-32768"

        return self


class ScoringConfig(BaseModel):
    """Importance + confidence scoring weights.

    All weight groups must sum to 1.0. Validated at startup.
    Defaults match the hardcoded values in ``sovyx.brain.scoring``.
    """

    # ImportanceWeights
    importance_category: float = Field(default=0.15, ge=0.0, le=1.0)
    importance_llm: float = Field(default=0.35, ge=0.0, le=1.0)
    importance_emotional: float = Field(default=0.10, ge=0.0, le=1.0)
    importance_novelty: float = Field(default=0.15, ge=0.0, le=1.0)
    importance_explicit: float = Field(default=0.25, ge=0.0, le=1.0)

    # ConfidenceWeights
    confidence_source: float = Field(default=0.35, ge=0.0, le=1.0)
    confidence_llm: float = Field(default=0.30, ge=0.0, le=1.0)
    confidence_explicitness: float = Field(default=0.20, ge=0.0, le=1.0)
    confidence_richness: float = Field(default=0.15, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_weight_sums(self) -> ScoringConfig:
        """Ensure importance and confidence weights each sum to 1.0."""
        imp_sum = (
            self.importance_category
            + self.importance_llm
            + self.importance_emotional
            + self.importance_novelty
            + self.importance_explicit
        )
        conf_sum = (
            self.confidence_source
            + self.confidence_llm
            + self.confidence_explicitness
            + self.confidence_richness
        )
        if abs(imp_sum - 1.0) > 0.01:
            msg = f"Importance weights must sum to 1.0, got {imp_sum:.3f}"
            raise ValueError(msg)
        if abs(conf_sum - 1.0) > 0.01:
            msg = f"Confidence weights must sum to 1.0, got {conf_sum:.3f}"
            raise ValueError(msg)
        return self


class EmotionalBaselineConfig(BaseModel):
    """Per-mind emotional baseline — the "resting" state a mind drifts toward.

    The Concept / Episode models store emotional deltas (`emotional_valence`,
    `emotional_arousal` — see ``brain/models.py``). This config defines the
    neutral anchor: where a concept's emotional charge settles in the absence
    of reinforcement, and how fast it gets there.

    Defaults match the hardcoded zero-anchor behaviour present before this
    config existed — a neutral-affect baseline with a gentle pull back to it.
    Overriding per mind (e.g. an assistant with a naturally warmer baseline)
    shifts all newly-encoded concepts by the same delta without touching
    existing data.

    References:
        - ADR-001 §2 — per-mind emotional baseline + homeostasis_rate.
        - `brain/models.py` — Concept.emotional_valence / Episode.emotional_*.

    Attributes:
        valence: Resting pleasure/displeasure axis, in [-1.0, +1.0].
            Default 0.0 (neutral) matches the historical behaviour.
        arousal: Resting activation axis, in [-1.0, +1.0].
            Default 0.0 (calm). Positive values bias new episodes toward
            "intense" encodings, negative toward "quiet".
        dominance: Resting sense-of-control axis, in [-1.0, +1.0]. Reserved
            for the planned 2D → 3D PAD migration (ADR-001); currently read
            by the config but not yet consumed by the scoring pipeline.
        homeostasis_rate: How strongly a concept's emotional state is pulled
            back toward baseline per consolidation cycle, in [0.0, 1.0].
            ``0.0`` disables homeostasis (concepts keep whatever affect they
            last saw); ``1.0`` resets to baseline every cycle. Default 0.05
            is a light nudge — consistent with the current decay-only model.
    """

    valence: float = Field(default=0.0, ge=-1.0, le=1.0)
    arousal: float = Field(default=0.0, ge=-1.0, le=1.0)
    dominance: float = Field(default=0.0, ge=-1.0, le=1.0)
    homeostasis_rate: float = Field(default=0.05, ge=0.0, le=1.0)


class BrainConfig(BaseModel):
    """Brain memory system configuration.

    All numerical fields are range-validated to prevent silent misconfiguration.
    Invalid values raise ``ValidationError`` at startup (fail-fast).
    """

    consolidation_interval_hours: int = Field(default=6, ge=1, le=168)
    dream_time: str = "02:00"
    dream_lookback_hours: int = Field(default=24, ge=1, le=168)
    dream_max_patterns: int = Field(default=5, ge=0, le=50)
    max_concepts: int = Field(default=50000, ge=100, le=1_000_000)
    forgetting_enabled: bool = True
    decay_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    min_strength: float = Field(default=0.01, ge=0.0, le=1.0)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    emotional_baseline: EmotionalBaselineConfig = Field(
        default_factory=EmotionalBaselineConfig,
    )


class TelegramChannelConfig(BaseModel):
    """Telegram channel configuration.

    token_env is the ENV VAR NAME, not the token value.
    Token read at runtime: os.environ[config.token_env].
    """

    token_env: str = "SOVYX_TELEGRAM_TOKEN"
    allowed_users: list[str] = Field(default_factory=list)


class DiscordChannelConfig(BaseModel):
    """Discord channel configuration."""

    token_env: str = "SOVYX_DISCORD_TOKEN"


class ChannelsConfig(BaseModel):
    """Communication channel configuration."""

    telegram: TelegramChannelConfig = Field(default_factory=TelegramChannelConfig)
    discord: DiscordChannelConfig = Field(default_factory=DiscordChannelConfig)


class Guardrail(BaseModel):
    """Custom safety guardrail rule (SPE-002).

    Attributes:
        id: Unique identifier (auto-generated or user-provided).
        rule: The guardrail rule text injected into system prompt.
        severity: How violations are treated — critical or warning.
        builtin: Whether this is a default guardrail (non-deletable).
    """

    id: str
    rule: str
    severity: Literal["critical", "warning"] = "critical"
    builtin: bool = False


# ── Default guardrails (SPE-002: honesty, privacy, safety) ─────────────
DEFAULT_GUARDRAILS: tuple[Guardrail, ...] = (
    Guardrail(
        id="honesty",
        rule="Always be truthful. Never fabricate facts, citations, or data."
        " If uncertain, say so.",
        severity="critical",
        builtin=True,
    ),
    Guardrail(
        id="privacy",
        rule="Never reveal, store, or transmit personal data"
        " unless explicitly authorized by the user.",
        severity="critical",
        builtin=True,
    ),
    Guardrail(
        id="safety",
        rule="Never provide instructions for harm, violence, illegal activities, or self-harm.",
        severity="critical",
        builtin=True,
    ),
)


class CustomRule(BaseModel):
    """Owner-defined safety rule.

    Attributes:
        name: Human-readable rule name.
        pattern: Regex pattern to match (case-insensitive).
        action: What to do on match: block or log.
        message: Optional custom message shown when blocked.
    """

    name: str
    pattern: str
    action: Literal["block", "log"] = "block"
    message: str = ""


class ShadowPattern(BaseModel):
    """A safety pattern to evaluate in shadow/dry-run mode.

    Shadow patterns are logged but never block content, allowing
    operators to validate new rules before promoting to production.

    Attributes:
        name: Human-readable rule name (for logs and dashboard).
        pattern: Regex pattern to match (case-insensitive).
        category: Safety category (for audit trail grouping).
        tier: Intended target tier when promoted.
        description: Why this pattern exists / what it catches.
    """

    name: str
    pattern: str
    category: str = "unknown"
    tier: Literal["standard", "strict", "child_safe"] = "standard"
    description: str = ""


class SafetyConfig(BaseModel):
    """Safety guardrails configuration."""

    child_safe_mode: bool = False
    financial_confirmation: bool = True
    content_filter: Literal["none", "standard", "strict"] = "standard"
    pii_protection: bool = True
    guardrails: list[Guardrail] = Field(
        default_factory=lambda: list(DEFAULT_GUARDRAILS),
    )
    custom_rules: list[CustomRule] = Field(default_factory=list)
    banned_topics: list[str] = Field(default_factory=list)
    shadow_mode: bool = False
    shadow_patterns: list[ShadowPattern] = Field(default_factory=list)


class PluginConfigEntry(BaseModel):
    """Per-plugin configuration from mind.yaml.

    Attributes:
        enabled: Whether the plugin is enabled (default True).
        config: Plugin-specific settings (validated against plugin's config_schema).
        permissions: Explicitly granted permissions (overrides plugin defaults).
    """

    enabled: bool = True
    config: dict[str, object] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)


class PluginsConfig(BaseModel):
    """Plugin system configuration section of mind.yaml.

    Supports two modes of control:
    1. **Global**: enabled/disabled sets control which plugins load.
    2. **Per-plugin**: plugins_config entries for fine-grained control.

    Attributes:
        enabled: If set, only these plugins are loaded (whitelist).
        disabled: Plugins to skip even if discovered (blacklist).
        plugins_config: Per-plugin configuration entries.
        tool_timeout_s: Default tool execution timeout in seconds.
    """

    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)
    plugins_config: dict[str, PluginConfigEntry] = Field(default_factory=dict)
    tool_timeout_s: float = Field(default=30.0, ge=1.0, le=300.0)

    def get_effective_enabled(self) -> set[str] | None:
        """Get the effective enabled set (None means all).

        Combines global enabled list with per-plugin enabled=false.
        """
        disabled_in_config = {
            name for name, entry in self.plugins_config.items() if not entry.enabled
        }
        all_disabled = set(self.disabled) | disabled_in_config

        if self.enabled:
            return set(self.enabled) - all_disabled
        return None

    def get_effective_disabled(self) -> set[str]:
        """Get the effective disabled set.

        Combines global disabled list with per-plugin enabled=false.
        """
        disabled_in_config = {
            name for name, entry in self.plugins_config.items() if not entry.enabled
        }
        return set(self.disabled) | disabled_in_config

    def get_plugin_config(self, plugin_name: str) -> dict[str, object]:
        """Get config dict for a specific plugin."""
        entry = self.plugins_config.get(plugin_name)
        return dict(entry.config) if entry else {}

    def get_all_plugin_configs(self) -> dict[str, dict[str, object]]:
        """Get all plugin configs as a flat dict."""
        return {
            name: dict(entry.config) for name, entry in self.plugins_config.items() if entry.config
        }

    def get_granted_permissions(self, plugin_name: str) -> set[str]:
        """Get explicitly granted permissions for a plugin."""
        entry = self.plugins_config.get(plugin_name)
        return set(entry.permissions) if entry and entry.permissions else set()

    def get_all_granted_permissions(self) -> dict[str, set[str]]:
        """Get all per-plugin granted permissions."""
        return {
            name: set(entry.permissions)
            for name, entry in self.plugins_config.items()
            if entry.permissions
        }


class MindConfig(BaseModel):
    """Complete Mind configuration. Loaded from mind.yaml.

    "Mind is configuration, not code."
    """

    name: str
    id: MindId = Field(default=MindId(""))
    language: str = "en"
    timezone: str = "UTC"
    template: str = "assistant"
    onboarding_complete: bool = False
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    ocean: OceanConfig = Field(default_factory=OceanConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)

    @model_validator(mode="after")
    def set_default_id(self) -> MindConfig:
        """Generate ID from name if not explicitly set."""
        if not self.id:
            self.id = MindId(self.name.lower().replace(" ", "-"))
        return self


def load_mind_config(path: Path) -> MindConfig:
    """Load and validate mind.yaml.

    Args:
        path: Path to mind.yaml file.

    Returns:
        Validated MindConfig.

    Raises:
        MindConfigError: If file not found, invalid YAML, or validation fails.
    """
    if not path.exists():
        msg = f"Mind config not found: {path}"
        raise MindConfigError(msg)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        msg = f"Failed to read mind config: {e}"
        raise MindConfigError(msg) from e

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        msg = f"Invalid YAML in mind config: {e}"
        raise MindConfigError(msg) from e

    if not isinstance(data, dict):
        msg = "Mind config must be a YAML mapping"
        raise MindConfigError(msg)

    try:
        config = MindConfig(**data)
    except Exception as e:  # noqa: BLE001
        msg = f"Mind config validation failed: {e}"
        raise MindConfigError(msg) from e

    logger.info(
        "mind_config_loaded",
        name=config.name,
        mind_id=str(config.id),
        language=config.language,
    )
    return config


def create_default_mind_config(name: str, data_dir: Path) -> Path:
    """Create a mind.yaml with sensible defaults.

    LLM provider/model fields are intentionally omitted so that
    runtime auto-detection (based on API keys) works at ``sovyx start``.
    Users can add ``llm.default_model: gpt-4o`` to override.

    Args:
        name: Name for the mind.
        data_dir: Directory to create the file in.

    Returns:
        Path to the created mind.yaml file.
    """
    config = MindConfig(name=name)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "mind.yaml"

    # Serialize without LLM runtime-resolved fields so auto-detect
    # runs fresh at each startup based on available API keys.
    data = config.model_dump(mode="json")
    llm = data.get("llm", {})
    for key in ("default_provider", "default_model", "fast_model"):
        llm.pop(key, None)
    # Remove empty llm section entirely if only local_model + defaults remain
    data["llm"] = llm

    content = yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    path.write_text(content, encoding="utf-8")

    logger.info(
        "default_mind_config_created",
        path=str(path),
        name=name,
    )
    return path


def validate_plugin_config(
    config: dict[str, object],
    schema: dict[str, object],
) -> list[str]:
    """Validate plugin config against a JSON Schema-like config_schema.

    Performs basic type checking and required field validation.
    Supports types: string, integer, number, boolean, array, object.

    Args:
        config: Plugin config dict from mind.yaml.
        schema: Plugin's config_schema from manifest or ISovyxPlugin.

    Returns:
        List of validation error strings (empty if valid).
    """
    errors: list[str] = []

    # Check required fields
    required = schema.get("required", [])
    if isinstance(required, list):
        for field in required:
            if isinstance(field, str) and field not in config:
                errors.append(f"Missing required field: {field}")

    # Check properties types
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for key, prop in properties.items():
            if key not in config:
                continue
            if not isinstance(prop, dict):
                continue
            expected_type = prop.get("type")
            if expected_type is None:
                continue
            value = config[key]
            if not _check_json_schema_type(value, str(expected_type)):
                errors.append(
                    f"Field '{key}': expected type '{expected_type}', got {type(value).__name__}"
                )

    return errors


def _check_json_schema_type(value: object, expected: str) -> bool:
    """Check if a value matches a JSON Schema type string."""
    type_map: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list,),
        "object": (dict,),
    }
    allowed = type_map.get(expected)
    if allowed is None:
        return True  # Unknown type, accept
    # bool is subclass of int, but JSON Schema treats them as distinct
    if expected == "integer" and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, allowed)
