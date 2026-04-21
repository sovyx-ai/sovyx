"""PIIRedactor — structlog processor that scrubs personally identifiable data.

Two layers of defence run on every record:

1. **Field-class verbosity** — keys that are known to carry PII
   (``user_message``, ``transcript``, ``prompt``, ``response``,
   ``email``, ``phone`` and their plural variants) are processed
   through one of four modes selected per-class in
   :class:`sovyx.engine.config.ObservabilityPIIConfig`:

   - ``minimal`` — drop the value entirely (replace with
     ``"[redacted]"``).
   - ``redacted`` — pattern-mask known PII while preserving the rest
     of the string. Default for free-form text.
   - ``hashed`` — replace with deterministic ``"sha256:<12hex>"`` so
     two log lines mentioning the same email correlate without ever
     exposing the raw value. Default for ``email`` / ``phone``.
   - ``full`` — pass-through (dev-only; production CI gate forbids).

2. **Global regex sweep** — every other string value passes through
   the same regex-based mask. Envelope and protocol fields
   (``timestamp``, ``event``, ``logger``, …) are explicitly excluded
   so the canonical event name is never mutated.

The sweep is idempotent: ``[redacted-email]`` does not match
``EMAIL_RE``, so a record processed twice produces the same output.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §7 Task 1.4
and §22.3 (no real PII in fixtures — enforced by the CI gate added
under P11+.4).
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from sovyx.engine.config import ObservabilityPIIConfig

Mode = Literal["minimal", "redacted", "hashed", "full"]

# ── Regex constants ─────────────────────────────────────────────────────────
# Compiled once at import. Each pattern errs on the side of recall: a
# false-positive redaction is preferable to leaking PII. The Luhn check
# below filters card false-positives because the pattern alone matches
# any 13-19 digit run.

EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE_BR_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?[\s-]?)?(?:9\s?)?\d{4}[\s-]?\d{4}\b"
)
PHONE_E164_RE: Final[re.Pattern[str]] = re.compile(r"\+\d{1,3}[\s-]?\d{4,14}\b")
CPF_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
CNPJ_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
IPV4_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"
)
LUHN_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_valid(digits: str) -> bool:
    """Return True when *digits* (already stripped of separators) passes Luhn."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _mask_credit_cards(text: str) -> str:
    """Replace Luhn-valid card-shaped sequences with ``[redacted-card]``."""

    def _repl(match: re.Match[str]) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        return "[redacted-card]" if _luhn_valid(digits) else match.group(0)

    return LUHN_RE.sub(_repl, text)


_REGEX_REDACTORS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (EMAIL_RE, "[redacted-email]"),
    (CPF_RE, "[redacted-cpf]"),
    (CNPJ_RE, "[redacted-cnpj]"),
    (PHONE_E164_RE, "[redacted-phone]"),
    (PHONE_BR_RE, "[redacted-phone]"),
    (IPV4_RE, "[redacted-ipv4]"),
)


def _apply_regex_sweep(value: str) -> str:
    """Run every PII regex over *value* in priority order.

    Credit-card masking runs first because the Luhn-validated card
    pattern ``\\d{4} \\d{4} \\d{4} \\d{4}`` overlaps the Brazilian phone
    pattern ``\\d{4}[\\s-]?\\d{4}``; without this ordering, a valid card
    would be over-redacted as two phones and the meaningful
    ``[redacted-card]`` token would be lost.
    """
    out = _mask_credit_cards(value)
    for pattern, replacement in _REGEX_REDACTORS:
        out = pattern.sub(replacement, out)
    return out


def _hash_value(value: str) -> str:
    """Deterministic, irreversible 12-hex-char SHA-256 prefix."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _apply_verbosity(value: str, mode: Mode) -> str:
    """Project a single string through the selected verbosity mode."""
    if mode == "minimal":
        return "[redacted]"
    if mode == "hashed":
        return _hash_value(value)
    if mode == "full":
        return value
    return _apply_regex_sweep(value)


# Field name → ObservabilityPIIConfig attribute. The same physical PII
# class can show up under several keys in the wild (singular, plural,
# qualified) so each lexical form maps independently.
_PII_FIELD_CLASSES: Final[dict[str, str]] = {
    "user_message": "user_messages",
    "user_messages": "user_messages",
    "message": "user_messages",
    "transcript": "transcripts",
    "transcripts": "transcripts",
    "stt_text": "transcripts",
    "voice.transcript": "transcripts",
    "voice.text": "transcripts",
    "prompt": "prompts",
    "prompts": "prompts",
    "system_prompt": "prompts",
    "user_prompt": "prompts",
    "plugin.args_preview": "prompts",
    "plugin.result_preview": "responses",
    "response": "responses",
    "responses": "responses",
    "completion": "responses",
    "tts_text": "responses",
    "email": "emails",
    "emails": "emails",
    "from_email": "emails",
    "to_email": "emails",
    "phone": "phones",
    "phones": "phones",
    "phone_number": "phones",
}

# Envelope, schema, and routing fields that the sweep must never touch.
_PROTECTED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "event",
        "schema_version",
        "process_id",
        "host",
        "sovyx_version",
        "sequence_no",
        "saga_id",
        "cause_id",
        "span_id",
        "trace_id",
    }
)


class PIIRedactor:
    """Structlog processor that applies per-field-class + global PII redaction.

    Construct with the daemon's :class:`ObservabilityPIIConfig`; the
    processor snapshots each field-class mode at construction so the
    hot-path emit does not re-read the config.
    """

    __slots__ = ("_field_modes",)

    def __init__(self, config: ObservabilityPIIConfig) -> None:
        self._field_modes: dict[str, Mode] = {
            key: getattr(config, attr) for key, attr in _PII_FIELD_CLASSES.items()
        }

    def __call__(
        self,
        logger: Any,  # noqa: ANN401 — opaque structlog logger reference.
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Redact PII in-place inside *event_dict*."""
        for key in list(event_dict):
            value = event_dict[key]
            if not isinstance(value, str):
                continue
            if key in _PROTECTED_KEYS:
                continue
            mode = self._field_modes.get(key)
            if mode is not None:
                event_dict[key] = _apply_verbosity(value, mode)
            else:
                event_dict[key] = _apply_regex_sweep(value)
        return event_dict


__all__ = [
    "CNPJ_RE",
    "CPF_RE",
    "EMAIL_RE",
    "IPV4_RE",
    "LUHN_RE",
    "PHONE_BR_RE",
    "PHONE_E164_RE",
    "Mode",
    "PIIRedactor",
]
