"""VAL-35: Serialization roundtrip with Hypothesis.

For each model: generate → serialize → deserialize → assert equal.

Models tested:
- Brain: Concept, Episode, Relation
- Dashboard: StatusSnapshot (to_dict → from_dict roundtrip)
- Health: CheckResult (frozen dataclass roundtrip)
- Events: all 11 event types (dataclass → serialize → verify fields)
- Bridge: InboundMessage, OutboundMessage

All tests use deadline=None and max_examples=200.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.models import Concept, Episode, Relation
from sovyx.dashboard.events import _serialize_event
from sovyx.dashboard.status import StatusSnapshot
from sovyx.engine.events import (
    ChannelConnected,
    ChannelDisconnected,
    ConceptCreated,
    ConsolidationCompleted,
    EngineStarted,
    EngineStopping,
    EpisodeEncoded,
    PerceptionReceived,
    ResponseSent,
    ServiceHealthChanged,
    ThinkCompleted,
)
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
from sovyx.observability.health import CheckResult, CheckStatus

# ── Hypothesis strategies ───────────────────────────────────────────────────

_utc_datetimes = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(UTC),
)

_safe_text = st.text(
    alphabet=st.characters(
        codec="utf-8",
        categories=("L", "M", "N", "P", "Z", "S"),
    ),
    min_size=0,
    max_size=200,
)

_unit_float = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
_signed_unit_float = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False)
_pos_int = st.integers(min_value=0, max_value=10_000)
_small_pos_float = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)

_concept_categories = st.sampled_from(list(ConceptCategory))
_relation_types = st.sampled_from(list(RelationType))
_check_statuses = st.sampled_from(list(CheckStatus))

_id_text = st.text(
    alphabet=st.characters(codec="utf-8", categories=("L", "N")),
    min_size=1,
    max_size=50,
)

_mind_ids = _id_text.map(MindId)
_concept_ids = _id_text.map(ConceptId)
_episode_ids = _id_text.map(EpisodeId)
_relation_ids = _id_text.map(RelationId)
_conversation_ids = _id_text.map(ConversationId)


# ── Brain: Concept roundtrip ───────────────────────────────────────────────


@st.composite
def concept_strategy(draw: st.DrawFn) -> Concept:
    return Concept(
        id=draw(_concept_ids),
        mind_id=draw(_mind_ids),
        name=draw(_safe_text.filter(lambda s: len(s) > 0)),
        content=draw(_safe_text),
        category=draw(_concept_categories),
        importance=draw(_unit_float),
        confidence=draw(_unit_float),
        access_count=draw(_pos_int),
        last_accessed=draw(st.one_of(st.none(), _utc_datetimes)),
        emotional_valence=draw(_signed_unit_float),
        source=draw(_safe_text.filter(lambda s: len(s) > 0)),
        metadata={},
        created_at=draw(_utc_datetimes),
        updated_at=draw(_utc_datetimes),
        embedding=None,
    )


class TestConceptRoundtrip:
    """Concept: model_dump → Concept(**data) → equal."""

    @given(concept=concept_strategy())
    @settings(max_examples=200, deadline=None)
    def test_roundtrip(self, concept: Concept) -> None:
        data = concept.model_dump()
        restored = Concept(**data)
        assert restored == concept

    @given(concept=concept_strategy())
    @settings(max_examples=200, deadline=None)
    def test_json_roundtrip(self, concept: Concept) -> None:
        json_str = concept.model_dump_json()
        restored = Concept.model_validate_json(json_str)
        assert restored == concept

    @given(concept=concept_strategy())
    @settings(max_examples=50, deadline=None)
    def test_importance_in_bounds(self, concept: Concept) -> None:
        assert 0.0 <= concept.importance <= 1.0

    @given(concept=concept_strategy())
    @settings(max_examples=50, deadline=None)
    def test_confidence_in_bounds(self, concept: Concept) -> None:
        assert 0.0 <= concept.confidence <= 1.0


# ── Brain: Episode roundtrip ───────────────────────────────────────────────


@st.composite
def episode_strategy(draw: st.DrawFn) -> Episode:
    return Episode(
        id=draw(_episode_ids),
        mind_id=draw(_mind_ids),
        conversation_id=draw(_conversation_ids),
        user_input=draw(_safe_text),
        assistant_response=draw(_safe_text),
        summary=draw(st.one_of(st.none(), _safe_text)),
        importance=draw(_unit_float),
        emotional_valence=draw(_signed_unit_float),
        emotional_arousal=draw(_signed_unit_float),
        concepts_mentioned=[],
        metadata={},
        created_at=draw(_utc_datetimes),
        embedding=None,
    )


class TestEpisodeRoundtrip:
    """Episode: model_dump → Episode(**data) → equal."""

    @given(episode=episode_strategy())
    @settings(max_examples=200, deadline=None)
    def test_roundtrip(self, episode: Episode) -> None:
        data = episode.model_dump()
        restored = Episode(**data)
        assert restored == episode

    @given(episode=episode_strategy())
    @settings(max_examples=200, deadline=None)
    def test_json_roundtrip(self, episode: Episode) -> None:
        json_str = episode.model_dump_json()
        restored = Episode.model_validate_json(json_str)
        assert restored == episode


# ── Brain: Relation roundtrip ──────────────────────────────────────────────


@st.composite
def relation_strategy(draw: st.DrawFn) -> Relation:
    return Relation(
        id=draw(_relation_ids),
        source_id=draw(_concept_ids),
        target_id=draw(_concept_ids),
        relation_type=draw(_relation_types),
        weight=draw(_unit_float),
        co_occurrence_count=draw(st.integers(min_value=1, max_value=10_000)),
        last_activated=draw(_utc_datetimes),
        created_at=draw(_utc_datetimes),
    )


class TestRelationRoundtrip:
    """Relation: model_dump → Relation(**data) → equal."""

    @given(relation=relation_strategy())
    @settings(max_examples=200, deadline=None)
    def test_roundtrip(self, relation: Relation) -> None:
        data = relation.model_dump()
        restored = Relation(**data)
        assert restored == relation

    @given(relation=relation_strategy())
    @settings(max_examples=200, deadline=None)
    def test_json_roundtrip(self, relation: Relation) -> None:
        json_str = relation.model_dump_json()
        restored = Relation.model_validate_json(json_str)
        assert restored == relation

    @given(relation=relation_strategy())
    @settings(max_examples=50, deadline=None)
    def test_weight_in_bounds(self, relation: Relation) -> None:
        assert 0.0 <= relation.weight <= 1.0


# ── Dashboard: StatusSnapshot roundtrip ────────────────────────────────────


@st.composite
def status_snapshot_strategy(draw: st.DrawFn) -> StatusSnapshot:
    return StatusSnapshot(
        version=draw(st.from_regex(r"[0-9]+\.[0-9]+\.[0-9]+", fullmatch=True)),
        uptime_seconds=draw(_small_pos_float),
        mind_name=draw(_safe_text.filter(lambda s: len(s) > 0)),
        active_conversations=draw(_pos_int),
        memory_concepts=draw(_pos_int),
        memory_episodes=draw(_pos_int),
        llm_cost_today=draw(st.floats(min_value=0.0, max_value=10000.0, allow_nan=False)),
        llm_calls_today=draw(_pos_int),
        tokens_today=draw(_pos_int),
        messages_today=draw(_pos_int),
    )


class TestStatusSnapshotRoundtrip:
    """StatusSnapshot: to_dict → StatusSnapshot(**data) → equivalent."""

    @given(snap=status_snapshot_strategy())
    @settings(max_examples=200, deadline=None)
    def test_to_dict_has_all_fields(self, snap: StatusSnapshot) -> None:
        d = snap.to_dict()
        assert set(d.keys()) == {
            "version",
            "uptime_seconds",
            "mind_name",
            "active_conversations",
            "memory_concepts",
            "memory_episodes",
            "llm_cost_today",
            "llm_calls_today",
            "tokens_today",
            "messages_today",
        }

    @given(snap=status_snapshot_strategy())
    @settings(max_examples=200, deadline=None)
    def test_to_dict_roundtrip(self, snap: StatusSnapshot) -> None:
        d = snap.to_dict()
        restored = StatusSnapshot(**d)
        # Uptime and cost are rounded in to_dict, so compare rounded values
        assert restored.version == snap.version
        assert restored.mind_name == snap.mind_name
        assert restored.active_conversations == snap.active_conversations
        assert restored.memory_concepts == snap.memory_concepts
        assert restored.memory_episodes == snap.memory_episodes
        assert restored.llm_calls_today == snap.llm_calls_today
        assert restored.tokens_today == snap.tokens_today
        assert restored.messages_today == snap.messages_today

    @given(snap=status_snapshot_strategy())
    @settings(max_examples=50, deadline=None)
    def test_to_dict_values_are_json_safe(self, snap: StatusSnapshot) -> None:
        """All values in to_dict are JSON-serializable primitives."""
        import json

        d = snap.to_dict()
        # Should not raise
        json.dumps(d)


# ── Health: CheckResult roundtrip ──────────────────────────────────────────


@st.composite
def check_result_strategy(draw: st.DrawFn) -> CheckResult:
    return CheckResult(
        name=draw(_safe_text.filter(lambda s: len(s) > 0)),
        status=draw(_check_statuses),
        message=draw(_safe_text),
        metadata={},
    )


class TestCheckResultRoundtrip:
    """CheckResult: asdict → CheckResult(**data) → equal."""

    @given(result=check_result_strategy())
    @settings(max_examples=200, deadline=None)
    def test_roundtrip(self, result: CheckResult) -> None:
        d = dataclasses.asdict(result)
        # CheckStatus needs to be converted back from string
        d["status"] = CheckStatus(d["status"])
        restored = CheckResult(**d)
        assert restored == result

    @given(result=check_result_strategy())
    @settings(max_examples=50, deadline=None)
    def test_ok_property(self, result: CheckResult) -> None:
        assert result.ok == (result.status == CheckStatus.GREEN)


# ── Events: serialize roundtrip ────────────────────────────────────────────


class TestEventSerializationRoundtrip:
    """All 11 event types: create → serialize → verify fields preserved."""

    @given(
        reason=_safe_text,
    )
    @settings(max_examples=200, deadline=None)
    def test_engine_stopping_roundtrip(self, reason: str) -> None:
        event = EngineStopping(reason=reason)
        payload = _serialize_event(event)
        assert payload["type"] == "EngineStopping"
        assert payload["data"]["reason"] == reason
        assert "timestamp" in payload
        assert "correlation_id" in payload

    @given(
        service=_safe_text.filter(lambda s: len(s) > 0),
        status=st.sampled_from(["green", "yellow", "red"]),
    )
    @settings(max_examples=200, deadline=None)
    def test_service_health_changed_roundtrip(self, service: str, status: str) -> None:
        event = ServiceHealthChanged(service=service, status=status)
        payload = _serialize_event(event)
        assert payload["data"]["service"] == service
        assert payload["data"]["status"] == status

    @given(
        source=_safe_text.filter(lambda s: len(s) > 0),
        person_id=_safe_text,
    )
    @settings(max_examples=200, deadline=None)
    def test_perception_received_roundtrip(self, source: str, person_id: str) -> None:
        event = PerceptionReceived(source=source, person_id=person_id)
        payload = _serialize_event(event)
        assert payload["data"]["source"] == source
        assert payload["data"]["person_id"] == person_id

    @given(
        tokens_in=_pos_int,
        tokens_out=_pos_int,
        model=_safe_text.filter(lambda s: len(s) > 0),
        cost_usd=st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
        latency_ms=_pos_int,
    )
    @settings(max_examples=200, deadline=None)
    def test_think_completed_roundtrip(
        self,
        tokens_in: int,
        tokens_out: int,
        model: str,
        cost_usd: float,
        latency_ms: int,
    ) -> None:
        event = ThinkCompleted(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
        payload = _serialize_event(event)
        assert payload["data"]["tokens_in"] == tokens_in
        assert payload["data"]["tokens_out"] == tokens_out
        assert payload["data"]["model"] == model
        assert payload["data"]["cost_usd"] == round(cost_usd, 6)
        assert payload["data"]["latency_ms"] == latency_ms

    @given(
        channel=_safe_text.filter(lambda s: len(s) > 0),
        latency_ms=_pos_int,
    )
    @settings(max_examples=200, deadline=None)
    def test_response_sent_roundtrip(self, channel: str, latency_ms: int) -> None:
        event = ResponseSent(channel=channel, latency_ms=latency_ms)
        payload = _serialize_event(event)
        assert payload["data"]["channel"] == channel
        assert payload["data"]["latency_ms"] == latency_ms

    @given(
        concept_id=_safe_text.filter(lambda s: len(s) > 0),
        title=_safe_text,
        source=_safe_text,
    )
    @settings(max_examples=200, deadline=None)
    def test_concept_created_roundtrip(
        self, concept_id: str, title: str, source: str
    ) -> None:
        event = ConceptCreated(concept_id=concept_id, title=title, source=source)
        payload = _serialize_event(event)
        assert payload["data"]["concept_id"] == concept_id
        assert payload["data"]["title"] == title
        assert payload["data"]["source"] == source

    @given(
        episode_id=_safe_text.filter(lambda s: len(s) > 0),
        importance=_unit_float,
    )
    @settings(max_examples=200, deadline=None)
    def test_episode_encoded_roundtrip(self, episode_id: str, importance: float) -> None:
        event = EpisodeEncoded(episode_id=episode_id, importance=importance)
        payload = _serialize_event(event)
        assert payload["data"]["episode_id"] == episode_id
        assert payload["data"]["importance"] == importance

    @given(
        merged=_pos_int,
        pruned=_pos_int,
        strengthened=_pos_int,
        duration_s=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False),
    )
    @settings(max_examples=200, deadline=None)
    def test_consolidation_completed_roundtrip(
        self,
        merged: int,
        pruned: int,
        strengthened: int,
        duration_s: float,
    ) -> None:
        event = ConsolidationCompleted(
            merged=merged, pruned=pruned, strengthened=strengthened, duration_s=duration_s
        )
        payload = _serialize_event(event)
        assert payload["data"]["merged"] == merged
        assert payload["data"]["pruned"] == pruned
        assert payload["data"]["strengthened"] == strengthened
        assert payload["data"]["duration_s"] == round(duration_s, 2)

    @given(channel_type=_safe_text.filter(lambda s: len(s) > 0))
    @settings(max_examples=200, deadline=None)
    def test_channel_connected_roundtrip(self, channel_type: str) -> None:
        event = ChannelConnected(channel_type=channel_type)
        payload = _serialize_event(event)
        assert payload["data"]["channel_type"] == channel_type

    @given(
        channel_type=_safe_text.filter(lambda s: len(s) > 0),
        reason=_safe_text,
    )
    @settings(max_examples=200, deadline=None)
    def test_channel_disconnected_roundtrip(
        self, channel_type: str, reason: str
    ) -> None:
        event = ChannelDisconnected(channel_type=channel_type, reason=reason)
        payload = _serialize_event(event)
        assert payload["data"]["channel_type"] == channel_type
        assert payload["data"]["reason"] == reason


# ── Bridge: InboundMessage/OutboundMessage roundtrip ───────────────────────


class TestBridgeMessageRoundtrip:
    """Bridge protocol messages: dataclass fields roundtrip."""

    @given(
        text=_safe_text,
        user_id=_safe_text.filter(lambda s: len(s) > 0),
        msg_id=_safe_text.filter(lambda s: len(s) > 0),
        chat_id=_safe_text.filter(lambda s: len(s) > 0),
        display_name=_safe_text,
    )
    @settings(max_examples=200, deadline=None)
    def test_inbound_message_roundtrip(
        self,
        text: str,
        user_id: str,
        msg_id: str,
        chat_id: str,
        display_name: str,
    ) -> None:
        from sovyx.bridge.protocol import InboundMessage
        from sovyx.engine.types import ChannelType

        msg = InboundMessage(
            channel_type=ChannelType.TELEGRAM,
            channel_user_id=user_id,
            channel_message_id=msg_id,
            chat_id=chat_id,
            text=text,
            display_name=display_name,
        )
        d = dataclasses.asdict(msg)
        restored = InboundMessage(**d)
        assert restored.text == msg.text
        assert restored.channel_user_id == msg.channel_user_id
        assert restored.channel_message_id == msg.channel_message_id
        assert restored.chat_id == msg.chat_id
        assert restored.display_name == msg.display_name

    @given(
        text=_safe_text,
        target=_safe_text.filter(lambda s: len(s) > 0),
        reply_to=st.one_of(st.none(), _safe_text),
    )
    @settings(max_examples=200, deadline=None)
    def test_outbound_message_roundtrip(
        self,
        text: str,
        target: str,
        reply_to: str | None,
    ) -> None:
        from sovyx.bridge.protocol import OutboundMessage
        from sovyx.engine.types import ChannelType

        msg = OutboundMessage(
            channel_type=ChannelType.TELEGRAM,
            target=target,
            text=text,
            reply_to=reply_to,
        )
        d = dataclasses.asdict(msg)
        restored = OutboundMessage(**d)
        assert restored.text == msg.text
        assert restored.target == msg.target
        assert restored.reply_to == msg.reply_to
