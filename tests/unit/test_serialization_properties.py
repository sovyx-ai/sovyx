"""VAL-35: Serialization roundtrip — property-based tests."""

from __future__ import annotations

import dataclasses

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.models import Concept, Episode, Relation
from sovyx.context.budget import TokenBudget
from sovyx.engine.types import (
    ConceptCategory,
    ConceptId,
    ConversationId,
    EpisodeId,
    MindId,
    RelationType,
)
from sovyx.llm.models import LLMResponse

_alnum = st.characters(whitelist_categories=("L", "N"))
_ids = st.text(min_size=1, max_size=20, alphabet=_alnum)

concepts = st.builds(
    Concept,
    id=_ids.map(ConceptId),
    mind_id=_ids.map(MindId),
    name=st.text(min_size=1, max_size=100),
    content=st.text(min_size=1, max_size=500),
    category=st.sampled_from(list(ConceptCategory)),
    importance=st.floats(0, 1),
    confidence=st.floats(0, 1),
    access_count=st.integers(0, 1000),
)

episodes = st.builds(
    Episode,
    id=_ids.map(EpisodeId),
    mind_id=_ids.map(MindId),
    conversation_id=_ids.map(ConversationId),
    summary=st.text(min_size=1, max_size=200),
    importance=st.floats(0, 1),
)

relations = st.builds(
    Relation,
    source_id=_ids.map(ConceptId),
    target_id=_ids.map(ConceptId),
    relation_type=st.sampled_from(list(RelationType)),
    weight=st.floats(0, 1),
)


class TestConceptRoundtrip:
    @settings(deadline=None)
    @given(c=concepts)
    def test_json(self, c: Concept) -> None:
        assert Concept.model_validate_json(c.model_dump_json()).id == c.id

    @settings(deadline=None)
    @given(c=concepts)
    def test_dict(self, c: Concept) -> None:
        assert Concept.model_validate(c.model_dump()) == c


class TestEpisodeRoundtrip:
    @settings(deadline=None)
    @given(e=episodes)
    def test_json(self, e: Episode) -> None:
        assert Episode.model_validate_json(e.model_dump_json()).id == e.id


class TestRelationRoundtrip:
    @settings(deadline=None)
    @given(r=relations)
    def test_json(self, r: Relation) -> None:
        restored = Relation.model_validate_json(r.model_dump_json())
        assert restored.source_id == r.source_id


class TestLLMResponseRoundtrip:
    @settings(deadline=None)
    @given(
        content=st.text(max_size=200),
        model=st.text(min_size=1, max_size=50),
        tokens_in=st.integers(0, 100000),
        tokens_out=st.integers(0, 100000),
        latency=st.integers(0, 60000),
        cost=st.floats(0, 100),
    )
    def test_dataclass(
        self,
        content: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        latency: int,
        cost: float,
    ) -> None:
        resp = LLMResponse(
            content=content,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency,
            cost_usd=cost,
            finish_reason="stop",
            provider="test",
        )
        d = dataclasses.asdict(resp)
        restored = LLMResponse(**d)
        assert restored.content == resp.content
        assert restored.tokens_in == resp.tokens_in


class TestTokenBudgetRoundtrip:
    @settings(deadline=None)
    @given(st.data())
    def test_frozen(self, data: st.DataObject) -> None:
        vals = [data.draw(st.integers(0, 10000)) for _ in range(6)]
        budget = TokenBudget(
            system_prompt=vals[0],
            memory_concepts=vals[1],
            memory_episodes=vals[2],
            temporal=vals[3],
            conversation=vals[4],
            response_reserve=vals[5],
            total=sum(vals),
        )
        assert TokenBudget(**dataclasses.asdict(budget)) == budget
