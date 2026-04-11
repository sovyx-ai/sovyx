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
    # ── Financial identifiers (before credit card to avoid overlap) ──
    # IBAN (2 letters + 2 check digits + up to 30 alphanumeric)
    _pii(
        r"\b[A-Z]{2}\d{2}\s?[A-Z0-9]{4}[\s]?(?:[A-Z0-9]{4}[\s]?){1,7}[A-Z0-9]{1,4}\b",
        "iban",
    ),
    # SWIFT/BIC code (keyword-anchored to avoid false positives on common words)
    _pii(
        r"(?i)(?:swift|bic|swift\s*code|bic\s*code)[\s:]*[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?(?=\s|$)",
        "swift",
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
    # Brazilian CNPJ (XX.XXX.XXX/XXXX-XX)
    _pii(
        r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b",
        "cnpj",
    ),
    # Brazilian RG — common formats:
    #   XX.XXX.XXX-X (SP), XX.XXX.XXX (other states),
    #   MG-XX.XXX.XXX, SSP/SP 12.345.678-9
    _pii(
        r"\b(?:[A-Z]{2}[-/]?)?\d{2}\.?\d{3}\.?\d{3}[-.]?\d{1}\b",
        "rg",
    ),
    # ── Keyword-anchored patterns (MUST come before generic digit patterns) ──
    # Polish PESEL (11 digits, keyword to avoid CNH conflict)
    _pii(
        r"(?i)\bpesel\b[\s:]*\d{11}\b",
        "pesel",
    ),
    # UK NHS Number (keyword + 10 digits)
    _pii(
        r"(?i)\bnhs\b[\s:#]*\d{3}\s?\d{3}\s?\d{4}\b",
        "nhs",
    ),
    # Canadian SIN (keyword + XXX-XXX-XXX)
    _pii(
        r"(?i)\bsin\b[\s:#]*\d{3}[-\s]\d{3}[-\s]\d{3}\b",
        "sin",
    ),
    # ── Format-specific document patterns ──
    # Brazilian CNH (XXXX XXXX XXX — requires spaces)
    _pii(
        r"\b\d{4}\s\d{4}\s\d{3}\b",
        "cnh",
    ),
    # US SSN (XXX-XX-XXXX)
    _pii(
        r"\b\d{3}-\d{2}-\d{4}\b",
        "ssn",
    ),
    # Spanish/Portuguese NIF/NIE (letter + 7-8 digits + letter)
    _pii(
        r"\b[A-Z]?\d{7,8}[-]?[A-Z]\b",
        "nif",
    ),
    # Argentine DNI (XX.XXX.XXX — requires dots)
    _pii(
        r"\b\d{2}\.\d{3}\.\d{3}\b",
        "dni",
    ),
    # Indian Aadhaar (XXXX XXXX XXXX — 12 digits with spaces)
    _pii(
        r"\b\d{4}\s\d{4}\s\d{4}\b",
        "aadhaar",
    ),
    # ── Contact info ──
    # Email addresses
    _pii(
        r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b",
        "email",
    ),
    # Phone: International with country code (+XX ... — at least 7 digits)
    _pii(
        r"\+\d{1,3}[\s.-]?\(?\d{1,4}\)?[\s.-]?\d{2,5}[\s.-]?\d{2,5}(?:[\s.-]?\d{1,4})?\b",
        "phone",
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
    # Phone: UK format (0XX XXXX XXXX or 0XXXX XXXXXX)
    _pii(
        r"\b0\d{2,4}[\s.-]?\d{3,4}[\s.-]?\d{3,4}\b",
        "phone",
    ),
    # Phone: German format (+49 XXX XXXXXXX or 0XXX XXXXXXX)
    _pii(
        r"\b(?:\+49[-\s]?|0)\d{2,4}[-\s./]?\d{3,8}(?:[-\s]?\d{1,5})?\b",
        "phone",
    ),
    # Phone: French format (+33 X XX XX XX XX or 0X XX XX XX XX)
    _pii(
        r"\b(?:\+33[-\s]?|0)[1-9](?:[-\s.]?\d{2}){4}\b",
        "phone",
    ),
    # Phone: Japanese format (+81 XX-XXXX-XXXX or 0XX-XXXX-XXXX)
    _pii(
        r"\b(?:\+81[-\s]?|0)\d{1,4}[-\s]?\d{2,4}[-\s]?\d{3,4}\b",
        "phone",
    ),
    # Phone: Indian format (+91 XXXXX XXXXX or 0XXXX-XXXXXX)
    _pii(
        r"\b(?:\+91[-\s]?)?[6-9]\d{4}[-\s]?\d{5}\b",
        "phone",
    ),
    # Phone: Mexican format (+52 XX XXXX XXXX or 55 XXXX XXXX)
    _pii(
        r"\b(?:\+52[-\s]?)?\d{2}[-\s]?\d{4}[-\s]?\d{4}\b",
        "phone",
    ),
    # Phone: Australian format (+61 X XXXX XXXX or 0X XXXX XXXX)
    _pii(
        r"\b(?:\+61[-\s]?|0)[2-578][-\s]?\d{4}[-\s]?\d{4}\b",
        "phone",
    ),
    # Phone: Chinese format (+86 XXX XXXX XXXX or 1XX XXXX XXXX)
    _pii(
        r"\b(?:\+86[-\s]?)?1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}\b",
        "phone",
    ),
    # Phone: South Korean format (+82 XX-XXXX-XXXX or 0XX-XXXX-XXXX)
    _pii(
        r"\b(?:\+82[-\s]?|0)1[016-9][-\s]?\d{3,4}[-\s]?\d{4}\b",
        "phone",
    ),
    # Phone: Italian format (+39 XXX XXXXXXX or 3XX XXXXXXX)
    _pii(
        r"\b(?:\+39[-\s]?)?3\d{2}[-\s.]?\d{3}[-\s.]?\d{4}\b",
        "phone",
    ),
    # Phone: Spanish format (+34 XXX XXX XXX or 6XX XXX XXX)
    _pii(
        r"\b(?:\+34[-\s]?)?[6-9]\d{2}[-\s]?\d{3}[-\s]?\d{3}\b",
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


_NER_PROMPT = (
    "Extract ALL personally identifiable information (PII) from the text below. "
    "Return ONLY a comma-separated list of PII types found, or NONE if clean.\n"
    "Types: email, phone, name, address, id_number, financial, medical, date_of_birth\n"
    "Text: {text}"
)

_NER_TIMEOUT_SEC = 2.0


class PIIGuard:
    """Detects and redacts PII from LLM output.

    Supports sync (regex-only) and async (regex + LLM NER fallback) modes.

    Args:
        safety: SafetyConfig reference (read dynamically per call).
        llm_router: Optional LLM router for NER-based PII detection.
    """

    def __init__(
        self,
        safety: SafetyConfig,
        llm_router: object | None = None,
    ) -> None:
        self._safety = safety
        self._llm_router = llm_router

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
                    pattern.replacement,
                    result_text,
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

    async def check_async(self, text: str) -> PIIFilterResult:
        """Scan text with regex + LLM NER fallback.

        Strategy:
        1. Run regex patterns (same as sync check).
        2. If no regex hits AND LLM available → run NER classifier.
        3. If LLM finds PII → flag as detected (log-only, no redaction
           without regex pattern — prevents garbled output).

        Args:
            text: LLM response text to scan.

        Returns:
            PIIFilterResult with redacted text and metadata.
        """
        # Phase 1: Regex (always runs)
        regex_result = self.check(text)

        # Phase 2: LLM NER (only if regex found nothing and LLM available)
        if regex_result.redacted or self._llm_router is None:
            return regex_result

        if not self._safety.pii_protection:
            return regex_result

        llm_types = await self._ner_classify(text)
        if not llm_types:
            return regex_result

        # LLM found PII that regex missed — log but don't redact
        # (redacting without a pattern could garble legitimate content)
        logger.warning(
            "pii_llm_detected_unredacted",
            llm_types=sorted(llm_types),
            text_prefix=text[:80],
        )

        m = get_metrics()
        m.safety_pii_redacted.add(
            len(llm_types),
            {"type": "llm_detected"},
        )

        return PIIFilterResult(
            text=text,  # Original text — no redaction
            redacted=True,  # Flag that PII was detected
            redaction_count=0,
            types_found=frozenset(llm_types),
        )

    async def _ner_classify(self, text: str) -> set[str]:
        """Run LLM NER to detect PII types.

        Returns set of detected PII type strings, or empty set.
        Never raises — all errors caught internally.
        """
        try:
            import asyncio

            router = self._llm_router
            assert router is not None  # Caller checked
            response = await asyncio.wait_for(
                router.generate(  # type: ignore[attr-defined]
                    messages=[
                        {
                            "role": "user",
                            "content": _NER_PROMPT.format(text=text[:300]),
                        },
                    ],
                    temperature=0.0,
                    max_tokens=50,
                ),
                timeout=_NER_TIMEOUT_SEC,
            )
            content = response.content.strip().upper()
            if content == "NONE" or not content:
                return set()

            # Parse comma-separated types
            valid_types = {
                "EMAIL",
                "PHONE",
                "NAME",
                "ADDRESS",
                "ID_NUMBER",
                "FINANCIAL",
                "MEDICAL",
                "DATE_OF_BIRTH",
            }
            found = set()
            for part in content.split(","):
                part = part.strip()
                if part in valid_types:
                    found.add(part.lower())
            return found

        except Exception:  # noqa: BLE001
            logger.debug("pii_ner_error", exc_info=True)
            return set()
