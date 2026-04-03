"""Sovyx shared types.

Strongly-typed IDs, enums, and utility functions used across all modules.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import NewType
from uuid import uuid4

# ── Strongly-typed IDs ──────────────────────────────────────────────────────

MindId = NewType("MindId", str)
ConceptId = NewType("ConceptId", str)
EpisodeId = NewType("EpisodeId", str)
RelationId = NewType("RelationId", str)
ConversationId = NewType("ConversationId", str)
PersonId = NewType("PersonId", str)
ChannelId = NewType("ChannelId", str)


def generate_id() -> str:
    """Generate a time-sortable unique ID.

    Format: "{timestamp_ms_hex}_{uuid4}"
    Example: "018e3a1b_550e8400-e29b-41d4-a716-446655440000"

    Sortable by creation time while remaining globally unique.
    """
    timestamp_hex = f"{int(time.time() * 1000):012x}"
    return f"{timestamp_hex}_{uuid4()}"


# ── Enums ───────────────────────────────────────────────────────────────────


class ConceptCategory(Enum):
    """Categories for brain concepts."""

    FACT = "fact"
    PREFERENCE = "preference"
    ENTITY = "entity"
    SKILL = "skill"
    BELIEF = "belief"
    EVENT = "event"
    RELATIONSHIP = "relationship"


class RelationType(Enum):
    """Types of relations between concepts."""

    RELATED_TO = "related_to"
    PART_OF = "part_of"
    CAUSES = "causes"
    CONTRADICTS = "contradicts"
    EXAMPLE_OF = "example_of"
    TEMPORAL = "temporal"
    EMOTIONAL = "emotional"


class ChannelType(Enum):
    """Communication channel types."""

    TELEGRAM = "telegram"
    DISCORD = "discord"
    SIGNAL = "signal"
    CLI = "cli"
    API = "api"


class CognitivePhase(Enum):
    """Cognitive loop phases.

    The canonical enum for all cognitive states. TASK-029's
    CognitiveStateMachine imports this — no separate CognitiveState enum.

    v0.1: IDLE through REFLECTING are transitional.
    v0.5+: CONSOLIDATING and DREAMING become transitional.
    """

    IDLE = "idle"
    PERCEIVING = "perceiving"
    ATTENDING = "attending"
    THINKING = "thinking"
    ACTING = "acting"
    REFLECTING = "reflecting"
    CONSOLIDATING = "consolidating"
    DREAMING = "dreaming"


class PerceptionType(Enum):
    """Types of perceptions entering the cognitive loop."""

    USER_MESSAGE = "user_message"
    TIMER_FIRED = "timer_fired"
    SYSTEM_EVENT = "system_event"
    PROACTIVE = "proactive"
