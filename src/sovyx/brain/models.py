"""Sovyx brain domain models.

Pydantic models for Concept, Episode, and Relation — the three pillars
of the brain's memory system.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator

from sovyx.engine.types import (
    ConceptCategory,
    ConceptId,
    ConversationId,
    EpisodeId,
    MindId,
    RelationId,
    RelationType,
    generate_id,
)


def _coerce_enum(enum_cls: type, value: object) -> object:
    """Coerce an enum value, handling cross-namespace identity mismatches.

    In CI with pytest-xdist, forked workers may re-import modules
    creating enum classes with different identity.  Pydantic's strict
    enum validator rejects ``isinstance`` mismatches.  This helper
    falls back to constructing the enum from its ``.value`` attribute.
    """
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        return enum_cls(value)
    # Cross-namespace enum: same value, different class
    if hasattr(value, "value"):
        return enum_cls(value.value)
    return value


class Concept(BaseModel):
    """A semantic concept in the brain's neocortex.

    Represents a piece of knowledge: facts, preferences, entities,
    skills, beliefs, events, or relationships.
    """

    id: ConceptId = Field(default_factory=lambda: ConceptId(generate_id()))
    mind_id: MindId
    name: str
    content: str = ""
    category: ConceptCategory = ConceptCategory.FACT

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_category(cls, v: object) -> object:
        return _coerce_enum(ConceptCategory, v)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    access_count: int = 0
    last_accessed: datetime | None = None
    emotional_valence: float = Field(default=0.0, ge=-1.0, le=1.0)
    source: str = "conversation"
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    embedding: list[float] | None = None


class Episode(BaseModel):
    """An episodic memory in the brain's hippocampus.

    Represents a conversation exchange with emotional context.
    """

    id: EpisodeId = Field(default_factory=lambda: EpisodeId(generate_id()))
    mind_id: MindId
    conversation_id: ConversationId
    user_input: str
    assistant_response: str
    summary: str | None = None
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    emotional_valence: float = Field(default=0.0, ge=-1.0, le=1.0)
    emotional_arousal: float = Field(default=0.0, ge=-1.0, le=1.0)
    concepts_mentioned: list[ConceptId] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    embedding: list[float] | None = None


class Relation(BaseModel):
    """A synapse between two concepts in the brain's graph.

    Weight represents connection strength (Hebbian learning).
    Co-occurrence tracking enables automatic strengthening.
    """

    id: RelationId = Field(default_factory=lambda: RelationId(generate_id()))
    source_id: ConceptId
    target_id: ConceptId
    relation_type: RelationType = RelationType.RELATED_TO

    @field_validator("relation_type", mode="before")
    @classmethod
    def _coerce_relation_type(cls, v: object) -> object:
        return _coerce_enum(RelationType, v)
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    co_occurrence_count: int = 1
    last_activated: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
