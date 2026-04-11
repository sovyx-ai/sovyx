"""Sovyx Injection Context Tracker — multi-turn jailbreak detection.

Detects gradual jailbreak attempts that span multiple messages:
    Message 1: innocent question
    Message 2: slightly suspicious phrasing
    Message 3: direct injection attack

Single-message injection is caught by regex/LLM. This module catches
*distributed* attacks where each message alone is borderline but the
sequence reveals intent.

Architecture:
    Per-conversation sliding window (last 5 messages).
    Each message gets a suspicion score (0.0–1.0).
    If cumulative score in window exceeds threshold → escalate.

Thread-safe via dict + deque (GIL-protected, same pattern as
safety_escalation.py).
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, unique

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# ── Configuration ──────────────────────────────────────────────────────

WINDOW_SIZE = 5  # Last N messages to consider
ESCALATION_THRESHOLD = 1.5  # Cumulative score to trigger escalation
HIGH_SUSPICION_THRESHOLD = 0.7  # Single message = high suspicion
CONSECUTIVE_THRESHOLD = 2  # N consecutive suspicious → escalate
CONSECUTIVE_MIN_SCORE = 0.3  # Minimum per-message score for "consecutive"
ENTRY_TTL_SEC = 1800  # 30min — discard stale entries
MAX_CONVERSATIONS = 10_000  # Prevent unbounded memory growth


@unique
class InjectionVerdict(Enum):
    """Result of multi-turn injection analysis."""

    SAFE = "safe"
    SUSPICIOUS = "suspicious"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class ScoredMessage:
    """A message with its computed suspicion score."""

    score: float
    timestamp: float
    signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InjectionAnalysis:
    """Full analysis result for a message in conversation context."""

    verdict: InjectionVerdict
    message_score: float
    cumulative_score: float
    window_size: int
    consecutive_suspicious: int
    signals: tuple[str, ...]


# ── Suspicion signal patterns ──────────────────────────────────────────
# These are SOFTER than the injection regex patterns in safety_patterns.py.
# They catch hints/setup that alone don't trigger a block but in
# combination reveal jailbreak intent.


@dataclass(frozen=True, slots=True)
class SuspicionSignal:
    """A pattern that contributes to suspicion score."""

    regex: re.Pattern[str]
    score: float
    name: str


_SUSPICION_SIGNALS: tuple[SuspicionSignal, ...] = (
    # ── Role/identity probing ──
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:what\s+(?:are|is)\s+your\s+(?:rules?|instructions?|system\s+prompt|guidelines?|constraints?|limitations?))",
            re.IGNORECASE,
        ),
        score=0.3,
        name="rules_probing",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:quais?\s+(?:são|é)\s+(?:suas?|tuas?)\s+(?:regras?|instruções?|limitações?|restrições?))",
            re.IGNORECASE,
        ),
        score=0.3,
        name="rules_probing_pt",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:cuáles?\s+son\s+tus\s+(?:reglas?|instrucciones?|limitaciones?|restricciones?))",
            re.IGNORECASE,
        ),
        score=0.3,
        name="rules_probing_es",
    ),
    # ── Boundary testing ──
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:can\s+you\s+(?:say|write|tell\s+me)\s+(?:anything|whatever|everything))",
            re.IGNORECASE,
        ),
        score=0.25,
        name="boundary_testing",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:what\s+(?:can'?t|cannot|won'?t)\s+you\s+(?:do|say|tell|write))",
            re.IGNORECASE,
        ),
        score=0.25,
        name="capability_probing",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:o\s+que\s+(?:você\s+)?(?:não\s+)?(?:pode|consegue)\s+(?:fazer|dizer|falar|escrever))",
            re.IGNORECASE,
        ),
        score=0.25,
        name="capability_probing_pt",
    ),
    # ── Hypothetical framing ──
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:hypothetically|in\s+theory|just\s+imagine|let'?s\s+say|what\s+if\s+(?:you|we|i)\s+(?:could|were|had))",
            re.IGNORECASE,
        ),
        score=0.2,
        name="hypothetical_framing",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:hipoteticamente|em\s+teoria|vamos\s+(?:supor|imaginar)|e\s+se\s+(?:você|eu|nós)\s+(?:pudesse|fosse))",
            re.IGNORECASE,
        ),
        score=0.2,
        name="hypothetical_framing_pt",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:hipotéticamente|en\s+teoría|imaginemos|digamos\s+que|y\s+si\s+(?:tú|yo)\s+(?:pudiera|fuera))",
            re.IGNORECASE,
        ),
        score=0.2,
        name="hypothetical_framing_es",
    ),
    # ── Authority/compliance framing ──
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:i'?m\s+(?:the\s+)?(?:admin|administrator|developer|owner|creator)|"
            r"i\s+(?:created|made|built|own)\s+you)",
            re.IGNORECASE,
        ),
        score=0.4,
        name="authority_claim",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:eu\s+(?:sou\s+)?(?:o\s+)?(?:admin|administrador|desenvolvedor|dono|criador)|"
            r"eu\s+(?:criei|fiz|construí)\s+você)",
            re.IGNORECASE,
        ),
        score=0.4,
        name="authority_claim_pt",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:soy\s+(?:el\s+)?(?:admin|administrador|desarrollador|dueño|creador)|"
            r"yo\s+(?:te\s+)?(?:creé|hice|construí))",
            re.IGNORECASE,
        ),
        score=0.4,
        name="authority_claim_es",
    ),
    # ── Incremental permission seeking ──
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:just\s+(?:this\s+once|a\s+little|a\s+tiny\s+bit)|"
            r"it'?s\s+(?:not\s+(?:that\s+)?bad|harmless|ok|fine|just\s+a\s+joke))",
            re.IGNORECASE,
        ),
        score=0.3,
        name="permission_seeking",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:só\s+(?:dessa\s+vez|um\s+pouquinho|uma\s+vez)|"
            r"(?:não\s+(?:é\s+)?(?:tão\s+)?ruim|(?:é\s+)?inofensivo|(?:é\s+)?só\s+(?:uma\s+)?brincadeira))",
            re.IGNORECASE,
        ),
        score=0.3,
        name="permission_seeking_pt",
    ),
    # ── Emotional manipulation ──
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:if\s+you\s+(?:really|truly)\s+(?:cared|wanted\s+to\s+help)|"
            r"you'?re\s+(?:not\s+)?(?:being\s+)?helpful|"
            r"(?:a\s+real|a\s+good|a\s+better)\s+(?:ai|assistant)\s+would)",
            re.IGNORECASE,
        ),
        score=0.35,
        name="emotional_manipulation",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:se\s+você\s+(?:realmente|de\s+verdade)\s+(?:quisesse\s+ajudar|se\s+importasse)|"
            r"você\s+(?:não\s+(?:está|tá)\s+(?:sendo\s+)?(?:útil|ajudando))|"
            r"(?:uma?\s+)?(?:boa|melhor|verdadeir[ao])\s+(?:ia|assistente)\s+(?:faria|iria))",
            re.IGNORECASE,
        ),
        score=0.35,
        name="emotional_manipulation_pt",
    ),
    # ── Encoding/obfuscation hints ──
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:base64|rot13|encode|decode|backwards|reversed?|obfuscate)\b",
            re.IGNORECASE,
        ),
        score=0.3,
        name="encoding_mention",
    ),
    # ── Jailbreak terminology (weaker matches) ──
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:jailbreak|DAN|do\s+anything\s+now|unrestricted\s+mode|no\s+limits?\s+mode)\b",
            re.IGNORECASE,
        ),
        score=0.5,
        name="jailbreak_terminology",
    ),
    SuspicionSignal(
        regex=re.compile(
            r"\b(?:without\s+(?:any\s+)?(?:safety|filter|restriction|rule|limit)s?)\b",
            re.IGNORECASE,
        ),
        score=0.4,
        name="without_safety",
    ),
)


def _score_message(text: str) -> ScoredMessage:
    """Compute suspicion score for a single message.

    Scores are additive but capped at 1.0. Each signal that matches
    contributes its score value.

    Args:
        text: Normalized message text.

    Returns:
        ScoredMessage with score, timestamp, and matched signal names.
    """
    from sovyx.cognitive.text_normalizer import normalize_text

    normalized = normalize_text(text)
    lower = normalized.lower()

    total = 0.0
    signals: list[str] = []

    for signal in _SUSPICION_SIGNALS:
        if signal.regex.search(lower):
            total += signal.score
            signals.append(signal.name)

    return ScoredMessage(
        score=min(total, 1.0),
        timestamp=time.time(),
        signals=tuple(signals),
    )


class InjectionContextTracker:
    """Tracks multi-turn injection attempts per conversation.

    Maintains a sliding window of suspicion scores for each conversation.
    When cumulative suspicion exceeds the threshold OR consecutive
    suspicious messages are detected, escalates to block.

    Args:
        window_size: Number of messages to track per conversation.
        escalation_threshold: Cumulative score to trigger escalation.
        consecutive_threshold: Number of consecutive suspicious messages
            to trigger escalation.
        entry_ttl_sec: Seconds after which stale entries are discarded.
    """

    def __init__(
        self,
        window_size: int = WINDOW_SIZE,
        escalation_threshold: float = ESCALATION_THRESHOLD,
        consecutive_threshold: int = CONSECUTIVE_THRESHOLD,
        entry_ttl_sec: float = ENTRY_TTL_SEC,
    ) -> None:
        self._window_size = window_size
        self._escalation_threshold = escalation_threshold
        self._consecutive_threshold = consecutive_threshold
        self._entry_ttl_sec = entry_ttl_sec
        self._conversations: dict[str, deque[ScoredMessage]] = {}

    def analyze(self, conversation_id: str, text: str) -> InjectionAnalysis:
        """Analyze a message in conversation context.

        Scores the message, adds to the conversation window, and evaluates
        the full window for multi-turn injection patterns.

        Args:
            conversation_id: Unique conversation identifier.
            text: Message text to analyze.

        Returns:
            InjectionAnalysis with verdict and detailed scoring.
        """
        self._maybe_gc()

        scored = _score_message(text)
        window = self._get_window(conversation_id)

        # Prune stale entries
        now = time.time()
        while window and (now - window[0].timestamp) > self._entry_ttl_sec:
            window.popleft()

        window.append(scored)

        # Trim to window size
        while len(window) > self._window_size:
            window.popleft()

        # Calculate cumulative score
        cumulative = sum(m.score for m in window)

        # Count consecutive suspicious messages (from the end)
        consecutive = 0
        for msg in reversed(window):
            if msg.score >= CONSECUTIVE_MIN_SCORE:
                consecutive += 1
            else:
                break

        # Determine verdict
        verdict = InjectionVerdict.SAFE

        if cumulative >= self._escalation_threshold or consecutive >= self._consecutive_threshold:
            verdict = InjectionVerdict.ESCALATE
            logger.warning(
                "injection_multi_turn_escalate",
                conversation_id=conversation_id,
                cumulative_score=round(cumulative, 2),
                consecutive=consecutive,
                window_size=len(window),
                signals=scored.signals,
            )
        elif scored.score >= HIGH_SUSPICION_THRESHOLD:
            verdict = InjectionVerdict.SUSPICIOUS
            logger.info(
                "injection_multi_turn_suspicious",
                conversation_id=conversation_id,
                message_score=round(scored.score, 2),
                signals=scored.signals,
            )

        return InjectionAnalysis(
            verdict=verdict,
            message_score=scored.score,
            cumulative_score=round(cumulative, 3),
            window_size=len(window),
            consecutive_suspicious=consecutive,
            signals=scored.signals,
        )

    def reset_conversation(self, conversation_id: str) -> None:
        """Clear tracking for a conversation (e.g., after resolution)."""
        self._conversations.pop(conversation_id, None)

    def get_conversation_score(self, conversation_id: str) -> float:
        """Get current cumulative score for a conversation."""
        window = self._conversations.get(conversation_id)
        if not window:
            return 0.0
        return sum(m.score for m in window)

    def clear(self) -> None:
        """Clear all tracking state (for testing)."""
        self._conversations.clear()

    def _get_window(self, conversation_id: str) -> deque[ScoredMessage]:
        """Get or create the message window for a conversation."""
        window = self._conversations.get(conversation_id)
        if window is None:
            window = deque(maxlen=self._window_size)
            self._conversations[conversation_id] = window
        return window

    def _maybe_gc(self) -> None:
        """Garbage-collect if conversation count exceeds limit.

        Removes oldest entries (by last message timestamp).
        """
        if len(self._conversations) <= MAX_CONVERSATIONS:
            return

        # Sort by last message timestamp, remove oldest 20%
        to_remove = len(self._conversations) - int(MAX_CONVERSATIONS * 0.8)
        if to_remove <= 0:
            return

        by_age = sorted(
            self._conversations.items(),
            key=lambda kv: kv[1][-1].timestamp if kv[1] else 0.0,
        )
        for conv_id, _ in by_age[:to_remove]:
            del self._conversations[conv_id]

        logger.info(
            "injection_tracker_gc",
            removed=to_remove,
            remaining=len(self._conversations),
        )


# ── Module-level singleton ─────────────────────────────────────────────

_tracker: InjectionContextTracker | None = None


def get_injection_tracker() -> InjectionContextTracker:
    """Get the global InjectionContextTracker instance."""
    global _tracker  # noqa: PLW0603
    if _tracker is None:
        _tracker = InjectionContextTracker()
    return _tracker
