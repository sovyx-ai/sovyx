"""Sovyx brain scoring — multi-signal importance and confidence computation.

Two orthogonal axes for concept quality assessment:

- **Importance**: "How much does this MATTER?" (relevance, memorability)
- **Confidence**: "How much can we TRUST this?" (certainty, reliability)

Both use weighted multi-signal formulas with bounded [0.05, 1.0] output.

Designed for three stages:
1. **Initial scoring** — at concept creation time
2. **Evolution scoring** — during access/interaction
3. **Batch recalculation** — during consolidation cycles

The floor of 0.05 ensures no concept reaches absolute zero
(the pruning threshold in consolidation is 0.01).

Architecture reference: SOVYX-DYNAMIC-IMPORTANCE-MISSION.md §Architecture
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import ClassVar

# ── Weight configurations ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ImportanceWeights:
    """Weights for initial importance scoring.

    Each weight represents the contribution of one signal to the final
    importance score. Must sum to 1.0 (validated in __post_init__).

    Signals:
        category_base: Per-category baseline (entity=0.80, fact=0.60, etc.)
        llm_assessment: LLM-assessed importance from extraction prompt
        emotional: Emotional memorability (|valence| → more memorable)
        novelty: Semantic novelty (distance from existing knowledge)
        explicit_signal: User explicitly asked to remember (0 or 1)
    """

    category_base: float = 0.15
    llm_assessment: float = 0.35
    emotional: float = 0.10
    novelty: float = 0.15
    explicit_signal: float = 0.25

    def __post_init__(self) -> None:
        total = (
            self.category_base
            + self.llm_assessment
            + self.emotional
            + self.novelty
            + self.explicit_signal
        )
        if abs(total - 1.0) > 0.001:
            msg = f"ImportanceWeights must sum to 1.0, got {total:.4f}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ConfidenceWeights:
    """Weights for initial confidence scoring.

    Signals:
        source_quality: Extraction method quality (LLM > regex)
        llm_assessment: LLM self-assessed certainty
        explicitness: Was the info directly stated vs. inferred?
        content_richness: Content length/detail as quality proxy
    """

    source_quality: float = 0.35
    llm_assessment: float = 0.30
    explicitness: float = 0.20
    content_richness: float = 0.15

    def __post_init__(self) -> None:
        total = (
            self.source_quality + self.llm_assessment + self.explicitness + self.content_richness
        )
        if abs(total - 1.0) > 0.001:
            msg = f"ConfidenceWeights must sum to 1.0, got {total:.4f}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class EvolutionWeights:
    """Weights for importance recalculation during consolidation.

    Signals:
        momentum: Current importance (history matters, prevents jarring changes)
        access: Normalized access frequency (used = important)
        connectivity: Graph degree centrality (connected = important)
        recency: Exponential decay based on last access
        emotional: Emotional weight (|valence| sustains importance)
    """

    momentum: float = 0.50
    access: float = 0.15
    connectivity: float = 0.15
    recency: float = 0.10
    emotional: float = 0.10

    def __post_init__(self) -> None:
        total = self.momentum + self.access + self.connectivity + self.recency + self.emotional
        if abs(total - 1.0) > 0.001:
            msg = f"EvolutionWeights must sum to 1.0, got {total:.4f}"
            raise ValueError(msg)


# ── Floor constant ─────────────────────────────────────────────────────

_SCORE_FLOOR = 0.05  # Absolute minimum for any score


# ── PAD 3D emotional intensity (ADR-001) ───────────────────────────────
#
# The ``emotional`` axis of ImportanceWeights takes a single scalar in
# [0, 1]. With PAD 3D we combine the three axes' magnitudes into that
# scalar via fixed sub-weights that sum to 1.0 — keeps the overall
# ImportanceWeights validation intact (no 3-axis-specific sub-fields
# to break external callers).
#
# Sub-weight rationale:
#   * Valence 0.45 — pleasure/displeasure dominates memorability (classic
#     von Restorff effect).
#   * Arousal 0.30 — activation is the second-strongest predictor of
#     recall (LaBar & Cabeza 2006).
#   * Dominance 0.25 — agency/control differentiates same-valence-and-
#     arousal emotion pairs (fear vs anger) but the absolute effect on
#     memorability is smaller than valence.
#
# abs() on every axis: both extremes are memorable. A "fear" concept
# (low dominance, high arousal, negative valence) is just as
# retention-boosted as "triumph" (high dominance, high arousal,
# positive valence).

_VALENCE_WEIGHT = 0.45
_AROUSAL_WEIGHT = 0.30
_DOMINANCE_WEIGHT = 0.25


def _emotional_intensity(
    valence: float,
    arousal: float = 0.0,
    dominance: float = 0.0,
) -> float:
    """Combine the three PAD axes into a single [0, 1] intensity scalar.

    Defaults of 0.0 for arousal + dominance keep the function
    backward-compatible: callers that only pass ``valence`` get the
    pre-ADR-001 behaviour of ``abs(valence) * 0.45`` — slightly
    smaller than the legacy ``abs(valence) * 1.0`` but scaled by the
    same ``ImportanceWeights.emotional`` outside this helper, so the
    net effect on un-migrated rows is proportional, not a regression.
    """
    return _clamp01(
        _VALENCE_WEIGHT * _clamp01(abs(valence))
        + _AROUSAL_WEIGHT * _clamp01(abs(arousal))
        + _DOMINANCE_WEIGHT * _clamp01(abs(dominance)),
    )


# ── Importance Scorer ──────────────────────────────────────────────────


class ImportanceScorer:
    """Multi-signal importance scoring for brain concepts.

    Importance represents how much a concept MATTERS to the user.
    Higher importance = survives decay longer, ranks higher in search,
    gets larger node in graph visualization.

    Usage::

        scorer = ImportanceScorer()

        # At creation:
        initial = scorer.score_initial(0.80, llm_importance=0.7, novelty=0.9)

        # On access:
        boosted = scorer.score_access_boost(current=0.6, access_count=5)

        # During consolidation:
        evolved = scorer.recalculate(
            current_importance=0.6, access_count=10,
            degree=5, avg_weight=0.7, max_degree=20,
            emotional_valence=0.3, days_since_access=7.0,
            max_access=50,
        )
    """

    def __init__(
        self,
        weights: ImportanceWeights | None = None,
        evolution: EvolutionWeights | None = None,
    ) -> None:
        self._w = weights or ImportanceWeights()
        self._ew = evolution or EvolutionWeights()

    def score_initial(
        self,
        category_base: float,
        llm_importance: float = 0.5,
        emotional_valence: float = 0.0,
        novelty: float = 0.5,
        explicit_signal: bool = False,
        *,
        emotional_arousal: float = 0.0,
        emotional_dominance: float = 0.0,
    ) -> float:
        """Score at concept creation time.

        Combines multiple signals into a single importance value.
        When ``explicit_signal`` is True (user said "remember this"),
        the floor is raised to 0.85.

        Args:
            category_base: Per-category importance (0.0-1.0).
            llm_importance: LLM-assessed importance (0.0-1.0).
            emotional_valence: Pleasure axis (-1.0 to 1.0).
                Absolute value contributes to the emotional signal.
            novelty: Semantic novelty score (0.0-1.0).
                1.0 = completely new topic, 0.0 = exact duplicate.
            explicit_signal: True if user explicitly asked to remember.
            emotional_arousal: Activation axis (-1.0 to 1.0) per
                ADR-001 (PAD 3D). Absolute value contributes.
            emotional_dominance: Agency axis (-1.0 to 1.0) per
                ADR-001 (PAD 3D). Absolute value contributes — fear
                (low dominance) and anger (high dominance) are both
                memorable, so we treat either extreme as a retention
                booster.

        Returns:
            Importance value in [0.05, 1.0].
        """
        raw = (
            self._w.category_base * _clamp01(category_base)
            + self._w.llm_assessment * _clamp01(llm_importance)
            + self._w.emotional
            * _emotional_intensity(
                emotional_valence,
                emotional_arousal,
                emotional_dominance,
            )
            + self._w.novelty * _clamp01(novelty)
            + self._w.explicit_signal * (1.0 if explicit_signal else 0.0)
        )
        if explicit_signal:
            raw = max(raw, 0.85)
        return max(_SCORE_FLOOR, min(1.0, raw))

    def score_access_boost(self, current: float, access_count: int) -> float:
        """Diminishing returns boost on concept access.

        Each access gives a smaller boost. The curve flattens at high
        access counts to prevent access-only inflation.

        Args:
            current: Current importance value.
            access_count: Total access count after this access.

        Returns:
            New importance value in [0.05, 1.0].
        """
        # Hyperbolic decay: boost shrinks as access_count grows
        boost = 0.03 / (1.0 + access_count * 0.1)
        return min(1.0, current + boost)

    def score_connectivity(
        self,
        degree: int,
        avg_weight: float,
        max_degree: int,
    ) -> float:
        """Connectivity component for importance recalculation.

        Combines normalized degree centrality (how connected) with
        average edge weight (how strong the connections are).

        Args:
            degree: Number of relations this concept has.
            avg_weight: Average weight of relations (0.0-1.0).
            max_degree: Maximum degree in the graph (for normalization).

        Returns:
            Connectivity score in [0.0, 1.0].
        """
        if max_degree <= 0:
            return 0.0
        degree_norm = min(1.0, degree / max(1, max_degree))
        return degree_norm * 0.7 + _clamp01(avg_weight) * 0.3

    def score_recency(self, days_since_access: float) -> float:
        """Recency score with 30-day half-life exponential decay.

        Recently accessed concepts score near 1.0.
        After 30 days without access: ~0.37.
        After 90 days: ~0.05.

        Args:
            days_since_access: Days since last access (≥0).

        Returns:
            Recency score in [0.0, 1.0].
        """
        return math.exp(-max(0.0, days_since_access) / 30.0)

    def recalculate(
        self,
        current_importance: float,
        access_count: int,
        degree: int,
        avg_weight: float,
        max_degree: int,
        emotional_valence: float,
        days_since_access: float,
        max_access: int = 1,
        *,
        emotional_arousal: float = 0.0,
        emotional_dominance: float = 0.0,
    ) -> float:
        """Full importance recalculation during consolidation.

        Uses evolution weights to combine momentum (current value),
        access patterns, connectivity, recency, and emotional weight.

        Includes velocity damping: importance can change at most 0.10
        per consolidation cycle to prevent runaway inflation/deflation.

        Args:
            current_importance: Current importance before recalculation.
            access_count: Total access count for this concept.
            degree: Number of relations (edges) in the graph.
            avg_weight: Average weight of relations.
            max_degree: Maximum degree across all concepts.
            emotional_valence: Current pleasure axis (-1.0 to 1.0).
            days_since_access: Days since last access.
            max_access: Maximum access_count across all concepts.
            emotional_arousal: Current activation axis (-1.0 to 1.0)
                per ADR-001 (PAD 3D).
            emotional_dominance: Current agency axis (-1.0 to 1.0)
                per ADR-001 (PAD 3D).

        Returns:
            New importance value in [0.05, 1.0].
        """
        # Compute signal components
        access_score = math.log1p(access_count) / math.log1p(max(1, max_access))
        connectivity = self.score_connectivity(degree, avg_weight, max_degree)
        recency = self.score_recency(days_since_access)
        emotional = _emotional_intensity(
            emotional_valence,
            emotional_arousal,
            emotional_dominance,
        )

        # Weighted combination
        evolved = (
            self._ew.momentum * _clamp01(current_importance)
            + self._ew.access * access_score
            + self._ew.connectivity * connectivity
            + self._ew.recency * recency
            + self._ew.emotional * emotional
        )

        # Velocity damping: limit change per cycle to ±0.10
        max_delta = 0.10
        delta = evolved - current_importance
        if abs(delta) > max_delta:
            evolved = current_importance + max_delta * (1.0 if delta > 0 else -1.0)

        # Soft ceiling: resistance above 0.90 prevents permanent max-out
        if evolved > 0.90:
            excess = evolved - 0.90
            evolved = 0.90 + excess * 0.3  # 70% damping above 0.90

        # Floor recovery: recently accessed concepts shouldn't be stuck at floor
        if current_importance < 0.10 and days_since_access < 7.0:
            evolved = max(evolved, 0.15)

        return max(_SCORE_FLOOR, min(1.0, evolved))


# ── Confidence Scorer ──────────────────────────────────────────────────


class ConfidenceScorer:
    """Multi-signal confidence scoring for brain concepts.

    Confidence represents epistemic certainty: how much can we TRUST
    this information? Higher confidence = stated more clearly,
    higher opacity in graph visualization, fewer uncertainty markers
    in LLM context.

    Usage::

        scorer = ConfidenceScorer()

        # At creation:
        initial = scorer.score_initial(
            source_quality="llm_explicit",
            llm_confidence=0.85,
            is_explicit=True,
            content_length=50,
        )

        # On corroboration:
        boosted = scorer.score_corroboration(current=0.7, corroboration_count=3)

        # On staleness:
        decayed = scorer.score_staleness_decay(current=0.8, days_since_access=60.0)
    """

    _SOURCE_CONFIDENCE: ClassVar[dict[str, tuple[float, float]]] = {
        "llm_explicit": (0.75, 0.95),
        "llm_inferred": (0.45, 0.70),
        "regex_fallback": (0.30, 0.55),
        "system": (0.90, 1.00),
        "corroboration": (0.80, 1.00),
    }

    def __init__(self, weights: ConfidenceWeights | None = None) -> None:
        self._w = weights or ConfidenceWeights()

    def get_source_confidence(self, source: str) -> float:
        """Return midpoint confidence for extraction source quality.

        Args:
            source: Source type key (e.g. ``"llm_explicit"``).

        Returns:
            Confidence midpoint in [0.0, 1.0].
        """
        low, high = self._SOURCE_CONFIDENCE.get(source, (0.40, 0.60))
        return (low + high) / 2

    def score_initial(
        self,
        source_quality: str = "llm_explicit",
        llm_confidence: float = 0.7,
        is_explicit: bool = True,
        content_length: int = 0,
    ) -> float:
        """Score at concept creation time.

        Combines source quality, LLM self-assessment, directness of
        statement, and content richness.

        Args:
            source_quality: Extraction source key.
            llm_confidence: LLM-assessed certainty (0.0-1.0).
            is_explicit: Whether info was directly stated (vs. inferred).
            content_length: Character length of content (richness proxy).

        Returns:
            Confidence value in [0.05, 1.0].
        """
        source_score = self.get_source_confidence(source_quality)
        explicitness = 1.0 if is_explicit else 0.3
        richness = min(1.0, content_length / 100)

        raw = (
            self._w.source_quality * source_score
            + self._w.llm_assessment * _clamp01(llm_confidence)
            + self._w.explicitness * explicitness
            + self._w.content_richness * richness
        )
        return max(_SCORE_FLOOR, min(1.0, raw))

    def score_corroboration(
        self,
        current: float,
        corroboration_count: int,  # noqa: ARG002 — reserved for future decay curves
    ) -> float:
        """Asymptotic confidence boost from repeated corroboration.

        Each mention increases confidence, but with diminishing returns.
        This prevents all repeated concepts from reaching 1.0 equally.

        Args:
            current: Current confidence value.
            corroboration_count: Number of times corroborated (for future use).

        Returns:
            New confidence value in [0.05, 1.0].
        """
        boost = 0.08 * (1.0 - current)
        return min(1.0, current + boost)

    def score_staleness_decay(
        self,
        current: float,
        days_since_access: float,
    ) -> float:
        """Gentle confidence decay for unaccessed concepts.

        Confidence decays very slowly — ~2% per 90 days of non-access.
        Capped at 15% maximum decay to prevent collapse.

        Args:
            current: Current confidence value.
            days_since_access: Days since last access (≥0).

        Returns:
            New confidence value in [0.05, 1.0].
        """
        decay_factor = 0.02 * (max(0.0, days_since_access) / 90.0)
        decayed = current * (1.0 - min(decay_factor, 0.15))
        return max(_SCORE_FLOOR, decayed)

    def score_content_update(self, current: float, content_grew: bool) -> float:
        """Small confidence bump when concept content gets enriched.

        Richer content = better evidence = slightly more confident.

        Args:
            current: Current confidence value.
            content_grew: Whether the content was updated with more text.

        Returns:
            New confidence value in [0.05, 1.0].
        """
        if content_grew:
            return min(1.0, current + 0.03)
        return current

    def score_contradiction(self, current: float) -> float:
        """Reduce confidence when contradicted by new information.

        A contradiction drops confidence by 40%, with a floor to
        prevent complete erasure of potentially valid info.

        Args:
            current: Current confidence value.

        Returns:
            New confidence value in [0.10, 1.0].
        """
        return max(0.10, current * 0.60)


# ── Score Normalizer ───────────────────────────────────────────────────


class ScoreNormalizer:
    """Prevent importance/confidence from converging to uniform values.

    Applied after recalculation during consolidation. Ensures meaningful
    spread across the concept population.

    Principles:
    - Relative ordering is ALWAYS preserved
    - Never pushes below floor (0.05)
    - Only activates when spread is dangerously narrow
    - Min-max scaling to target range

    Usage::

        normalizer = ScoreNormalizer()
        scores = [(id1, 0.52), (id2, 0.53), (id3, 0.51)]
        normalized = normalizer.normalize(scores)
        # Now spread across [0.10, 0.95] range
    """

    def __init__(
        self,
        min_spread: float = 0.20,
        target_min: float = 0.10,
        target_max: float = 0.95,
        floor: float = _SCORE_FLOOR,
    ) -> None:
        self._min_spread = min_spread
        self._target_min = target_min
        self._target_max = target_max
        self._floor = floor

    def normalize(
        self,
        scores: list[tuple[str, float]],
    ) -> list[tuple[str, float]]:
        """Normalize scores if spread is too narrow.

        Returns new scores. No-op if spread is already healthy.
        Uses rank-preserving min-max scaling.

        Args:
            scores: List of (concept_id, score) tuples.

        Returns:
            List of (concept_id, new_score) tuples with same ordering.
        """
        if len(scores) < 3:  # noqa: PLR2004
            return scores

        values = [s for _, s in scores]
        current_min = min(values)
        current_max = max(values)
        current_spread = current_max - current_min

        if current_spread >= self._min_spread:
            return scores  # Healthy spread — no action needed

        # All identical — distribute evenly while preserving IDs
        if current_min == current_max:
            n = len(scores)
            step = (self._target_max - self._target_min) / max(1, n - 1)
            # Sort by ID for deterministic ordering
            sorted_scores = sorted(scores, key=lambda x: x[0])
            return [
                (cid, max(self._floor, self._target_min + i * step))
                for i, (cid, _) in enumerate(sorted_scores)
            ]

        # Min-max stretch to target range (preserves relative ordering)
        result: list[tuple[str, float]] = []
        for cid, val in scores:
            normalized = (val - current_min) / (current_max - current_min)
            stretched = self._target_min + normalized * (self._target_max - self._target_min)
            result.append((cid, max(self._floor, stretched)))

        return result

    def normalize_by_category(
        self,
        concepts: list[tuple[str, str, float]],
    ) -> list[tuple[str, float]]:
        """Per-category normalization to preserve inter-category variance.

        Normalizes within each category independently, ensuring no single
        category's concepts collapse to the same value.

        Args:
            concepts: List of (concept_id, category, score) tuples.

        Returns:
            List of (concept_id, new_score) tuples.
        """
        by_cat: dict[str, list[tuple[str, float]]] = {}
        for cid, cat, score in concepts:
            by_cat.setdefault(cat, []).append((cid, score))

        result: list[tuple[str, float]] = []
        for cat_scores in by_cat.values():
            result.extend(self.normalize(cat_scores))
        return result


# ── Helpers ────────────────────────────────────────────────────────────


def _clamp01(value: float) -> float:
    """Clamp a value to [0.0, 1.0]."""
    return max(0.0, min(1.0, value))
