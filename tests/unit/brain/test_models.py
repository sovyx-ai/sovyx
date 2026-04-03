"""Tests for sovyx.brain.models — domain models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sovyx.brain.models import Concept, Episode, Relation
from sovyx.engine.types import (
    ConceptCategory,
    ConceptId,
    ConversationId,
    MindId,
    RelationType,
)


class TestConcept:
    """Concept model validation."""

    def test_defaults(self) -> None:
        c = Concept(mind_id=MindId("aria"), name="test")
        assert c.importance == 0.5
        assert c.category == ConceptCategory.FACT
        assert c.id != ""
        assert c.embedding is None

    def test_importance_range(self) -> None:
        with pytest.raises(ValidationError):
            Concept(mind_id=MindId("aria"), name="test", importance=1.5)
        with pytest.raises(ValidationError):
            Concept(mind_id=MindId("aria"), name="test", importance=-0.1)

    def test_confidence_range(self) -> None:
        with pytest.raises(ValidationError):
            Concept(mind_id=MindId("aria"), name="test", confidence=2.0)

    def test_emotional_valence_range(self) -> None:
        with pytest.raises(ValidationError):
            Concept(mind_id=MindId("aria"), name="test", emotional_valence=1.5)

    def test_all_categories(self) -> None:
        for cat in ConceptCategory:
            c = Concept(mind_id=MindId("aria"), name="test", category=cat)
            assert c.category == cat


class TestEpisode:
    """Episode model validation."""

    def test_defaults(self) -> None:
        e = Episode(
            mind_id=MindId("aria"),
            conversation_id=ConversationId("conv1"),
            user_input="hello",
            assistant_response="hi",
        )
        assert e.importance == 0.5
        assert e.concepts_mentioned == []

    def test_importance_range(self) -> None:
        with pytest.raises(ValidationError):
            Episode(
                mind_id=MindId("aria"),
                conversation_id=ConversationId("c"),
                user_input="hi",
                assistant_response="hey",
                importance=2.0,
            )


class TestRelation:
    """Relation model validation."""

    def test_defaults(self) -> None:
        r = Relation(
            source_id=ConceptId("a"),
            target_id=ConceptId("b"),
        )
        assert r.weight == 0.5
        assert r.relation_type == RelationType.RELATED_TO
        assert r.co_occurrence_count == 1

    def test_weight_range(self) -> None:
        with pytest.raises(ValidationError):
            Relation(
                source_id=ConceptId("a"),
                target_id=ConceptId("b"),
                weight=1.5,
            )
