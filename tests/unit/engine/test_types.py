"""Tests for sovyx.engine.types — shared types and enums."""

from __future__ import annotations

from sovyx.engine.types import (
    ChannelId,
    ChannelType,
    CognitivePhase,
    ConceptCategory,
    ConceptId,
    ConversationId,
    EpisodeId,
    MindId,
    PerceptionType,
    PersonId,
    RelationId,
    RelationType,
    generate_id,
)


class TestStronglyTypedIds:
    """NewType IDs are just str wrappers."""

    def test_mind_id(self) -> None:
        mid = MindId("aria")
        assert mid == "aria"
        assert isinstance(mid, str)

    def test_concept_id(self) -> None:
        cid = ConceptId("concept-123")
        assert isinstance(cid, str)

    def test_episode_id(self) -> None:
        eid = EpisodeId("episode-456")
        assert isinstance(eid, str)

    def test_relation_id(self) -> None:
        rid = RelationId("rel-789")
        assert isinstance(rid, str)

    def test_conversation_id(self) -> None:
        cid = ConversationId("conv-abc")
        assert isinstance(cid, str)

    def test_person_id(self) -> None:
        pid = PersonId("person-def")
        assert isinstance(pid, str)

    def test_channel_id(self) -> None:
        chid = ChannelId("chan-ghi")
        assert isinstance(chid, str)


class TestGenerateId:
    """ID generation."""

    def test_returns_string(self) -> None:
        id_ = generate_id()
        assert isinstance(id_, str)

    def test_unique(self) -> None:
        ids = {generate_id() for _ in range(100)}
        assert len(ids) == 100

    def test_contains_underscore_separator(self) -> None:
        id_ = generate_id()
        assert "_" in id_

    def test_sortable_by_time(self) -> None:
        import time

        id1 = generate_id()
        time.sleep(0.01)
        id2 = generate_id()
        # Hex timestamp prefix ensures lexicographic sorting = time sorting
        assert id1 < id2

    def test_format_has_hex_prefix(self) -> None:
        id_ = generate_id()
        prefix = id_.split("_")[0]
        # Should be 12-char hex
        assert len(prefix) == 12
        int(prefix, 16)  # Should not raise


class TestConceptCategory:
    """Concept category enum."""

    def test_all_values(self) -> None:
        expected = {"fact", "preference", "entity", "skill", "belief", "event", "relationship"}
        actual = {c.value for c in ConceptCategory}
        assert actual == expected

    def test_access_by_name(self) -> None:
        assert ConceptCategory.FACT.value == "fact"
        assert ConceptCategory.PREFERENCE.value == "preference"


class TestRelationType:
    """Relation type enum."""

    def test_all_values(self) -> None:
        expected = {
            "related_to",
            "part_of",
            "causes",
            "contradicts",
            "example_of",
            "temporal",
            "emotional",
        }
        actual = {r.value for r in RelationType}
        assert actual == expected


class TestChannelType:
    """Channel type enum."""

    def test_all_values(self) -> None:
        expected = {"telegram", "discord", "signal", "cli", "api"}
        actual = {c.value for c in ChannelType}
        assert actual == expected


class TestCognitivePhase:
    """Cognitive phase enum."""

    def test_all_v01_phases(self) -> None:
        # v0.1 transitional phases
        v01 = {
            CognitivePhase.IDLE,
            CognitivePhase.PERCEIVING,
            CognitivePhase.ATTENDING,
            CognitivePhase.THINKING,
            CognitivePhase.ACTING,
            CognitivePhase.REFLECTING,
        }
        assert len(v01) == 6

    def test_future_phases_exist(self) -> None:
        # v0.5+ phases exist but not yet transitional
        assert CognitivePhase.CONSOLIDATING.value == "consolidating"
        assert CognitivePhase.DREAMING.value == "dreaming"

    def test_total_phases(self) -> None:
        assert len(CognitivePhase) == 8


class TestPerceptionType:
    """Perception type enum."""

    def test_all_values(self) -> None:
        expected = {"user_message", "timer_fired", "system_event", "proactive"}
        actual = {p.value for p in PerceptionType}
        assert actual == expected
