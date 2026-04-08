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

    Auto-detection: if no explicit model is set in mind.yaml, the default
    model is chosen based on available API keys:
        - ANTHROPIC_API_KEY set → claude-sonnet-4-20250514
        - OPENAI_API_KEY set (no Anthropic) → gpt-4o
        - Neither → claude-sonnet-4-20250514 (will fail with clear error)
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
    def auto_detect_provider(self) -> LLMConfig:
        """Auto-detect LLM provider from available API keys."""
        import os

        has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
        has_openai = bool(os.environ.get("OPENAI_API_KEY"))

        if not self.default_model:
            if has_anthropic:
                self.default_model = "claude-sonnet-4-20250514"
            elif has_openai:
                self.default_model = "gpt-4o"
            else:
                self.default_model = "claude-sonnet-4-20250514"

        if not self.default_provider:
            if has_anthropic:
                self.default_provider = "anthropic"
            elif has_openai:
                self.default_provider = "openai"
            else:
                self.default_provider = "anthropic"

        if not self.fast_model:
            if has_openai and not has_anthropic:
                self.fast_model = "gpt-4o-mini"
            else:
                self.fast_model = "claude-3-5-haiku-20241022"

        return self


class BrainConfig(BaseModel):
    """Brain memory system configuration.

    All numerical fields are range-validated to prevent silent misconfiguration.
    Invalid values raise ``ValidationError`` at startup (fail-fast).
    """

    consolidation_interval_hours: int = Field(default=6, ge=1, le=168)
    dream_time: str = "02:00"
    max_concepts: int = Field(default=50000, ge=100, le=1_000_000)
    forgetting_enabled: bool = True
    decay_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    min_strength: float = Field(default=0.01, ge=0.0, le=1.0)


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


class SafetyConfig(BaseModel):
    """Safety guardrails configuration."""

    child_safe_mode: bool = False
    financial_confirmation: bool = True
    content_filter: Literal["none", "standard", "strict"] = "standard"


class MindConfig(BaseModel):
    """Complete Mind configuration. Loaded from mind.yaml.

    "Mind is configuration, not code."
    """

    name: str
    id: MindId = Field(default=MindId(""))
    language: str = "en"
    timezone: str = "UTC"
    template: str = "assistant"
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    ocean: OceanConfig = Field(default_factory=OceanConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

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
    except Exception as e:
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

    Args:
        name: Name for the mind.
        data_dir: Directory to create the file in.

    Returns:
        Path to the created mind.yaml file.
    """
    config = MindConfig(name=name)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "mind.yaml"

    content = yaml.dump(
        config.model_dump(mode="json"),
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
