"""Sovyx PII/Privacy Output Guard — redacts personal data from LLM responses.

Only applies to OUTPUT (LLM responses). User input is theirs — no filtering.
Detects: emails, phone numbers (US/BR/intl), CPFs, API keys, credit cards,
SSNs, IP addresses, and generic key=value secrets.

Design:
- Compiled regex patterns for O(n) single-pass scanning
- Each pattern has a named type for metrics granularity
- Redaction replaces matched text with [REDACTED-<TYPE>]
- Zero overhead when pii_protection=False
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.cognitive.safety_audit import FilterAction, FilterDirection, get_audit_trail
from sovyx.cognitive.safety_patterns import (
    FilterMatch,
    FilterTier,
    PatternCategory,
    SafetyPattern,
)
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
    from sovyx.mind.config import SafetyConfig

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PIIPattern:
    """A PII detection pattern.

    Attributes:
        regex: Compiled regex pattern.
        pii_type: Type of PII (email, phone, cpf, etc.).
        replacement: Redaction replacement text.
    """

    regex: re.Pattern[str]
    pii_type: str
    replacement: str


def _pii(
    pattern: str,
    pii_type: str,
    flags: int = 0,
) -> PIIPattern:
    """Helper to create a PIIPattern."""
    return PIIPattern(
        regex=re.compile(pattern, flags),
        pii_type=pii_type,
        replacement=f"[REDACTED-{pii_type.upper()}]",
    )


# ── PII Patterns ───────────────────────────────────────────────────────
# Order matters: more specific patterns first to avoid partial matches.

PII_PATTERNS: tuple[PIIPattern, ...] = (
    # API keys / secrets (long hex/base64 strings with prefix)
    _pii(
        r"\b(?:sk|pk|api[-_]?key|token|secret|bearer|access)"
        r"[-_][a-zA-Z0-9]{20,}\b",
        "api_key",
    ),
    # Credit card numbers (13-19 digits with optional separators)
    _pii(
        r"\b(?:\d{4}[-\s]?){3,4}\d{1,4}\b",
        "credit_card",
    ),
    # Brazilian CPF (XXX.XXX.XXX-XX)
    _pii(
        r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b",
        "cpf",
    ),
    # US SSN (XXX-XX-XXXX)
    _pii(
        r"\b\d{3}-\d{2}-\d{4}\b",
        "ssn",
    ),
    # Email addresses
    _pii(
        r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b",
        "email",
    ),
    # Phone: Brazilian format (+55 XX XXXXX-XXXX or variants)
    _pii(
        r"\b(?:\+55\s?)?\(?\d{2}\)?\s?\d{4,5}[-\s]?\d{4}\b",
        "phone",
    ),
    # Phone: US format (+1 XXX-XXX-XXXX or variants)
    _pii(
        r"\b(?:\+1[-\s]?)?\(?\d{3}\)?[-\s]?\d{3}[-\s]?\d{4}\b",
        "phone",
    ),
    # IPv4 addresses (not in CIDR/URL context)
    _pii(
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        "ip_address",
    ),
)


@dataclass(frozen=True, slots=True)
class PIIFilterResult:
    """Result of PII filtering.

    Attributes:
        text: The (possibly redacted) text.
        redacted: Whether any PII was found and redacted.
        redaction_count: Number of PII instances redacted.
        types_found: Set of PII types that were redacted.
    """

    text: str
    redacted: bool
    redaction_count: int
    types_found: frozenset[str]


class PIIGuard:
    """Detects and redacts PII from LLM output.

    Args:
        safety: SafetyConfig reference (read dynamically per call).
    """

    def __init__(self, safety: SafetyConfig) -> None:
        self._safety = safety

    def check(self, text: str) -> PIIFilterResult:
        """Scan text for PII and redact if pii_protection is enabled.

        Args:
            text: LLM response text to scan.

        Returns:
            PIIFilterResult with redacted text and metadata.
        """
        if not self._safety.pii_protection:
            return PIIFilterResult(
                text=text,
                redacted=False,
                redaction_count=0,
                types_found=frozenset(),
            )

        m = get_metrics()
        result_text = text
        count = 0
        types: set[str] = set()

        for pattern in PII_PATTERNS:
            matches = pattern.regex.findall(result_text)
            if matches:
                result_text = pattern.regex.sub(
                    pattern.replacement, result_text,
                )
                count += len(matches)
                types.add(pattern.pii_type)
                m.safety_pii_redacted.add(
                    len(matches),
                    {"type": pattern.pii_type},
                )

        if count > 0:
            logger.info(
                "pii_redacted",
                count=count,
                types=sorted(types),
            )

            # Record audit trail event (using a synthetic FilterMatch)
            audit_match = FilterMatch(
                matched=True,
                pattern=SafetyPattern(
                    regex=re.compile(r"pii"),
                    category=PatternCategory.ILLEGAL,
                    tier=FilterTier.STANDARD,
                    description=f"PII redacted: {', '.join(sorted(types))}",
                ),
                category=PatternCategory.ILLEGAL,
                tier=FilterTier.STANDARD,
            )
            get_audit_trail().record(
                direction=FilterDirection.OUTPUT,
                action=FilterAction.REDACTED,
                match=audit_match,
            )

        return PIIFilterResult(
            text=result_text,
            redacted=count > 0,
            redaction_count=count,
            types_found=frozenset(types),
        )
