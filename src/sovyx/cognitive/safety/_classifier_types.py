"""Safety classifier types — extracted from safety_classifier.py."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class SafetyCategory(enum.StrEnum):
    """Safety violation categories aligned with PatternCategory."""

    VIOLENCE = "violence"
    WEAPONS = "weapons"
    SELF_HARM = "self_harm"
    HACKING = "hacking"
    SUBSTANCE = "substance"
    SEXUAL = "sexual"
    GAMBLING = "gambling"
    HATE_SPEECH = "hate_speech"
    MANIPULATION = "manipulation"
    ILLEGAL = "illegal"
    INJECTION = "injection"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    """Result of LLM safety classification.

    Attributes:
        safe: True if content is safe, False if unsafe.
        category: Violation category (None if safe).
        confidence: Classification confidence (1.0 for LLM, 0.8 for regex).
        method: How the verdict was reached ("llm", "regex", "timeout", "error").
        latency_ms: Classification latency in milliseconds.
    """

    safe: bool
    category: SafetyCategory | None = None
    confidence: float = 1.0
    method: str = "llm"
    latency_ms: int = 0


# Singleton safe verdict
SAFE_VERDICT = SafetyVerdict(safe=True, method="pass")
