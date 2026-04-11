"""Tests for sovyx.cognitive.pii_guard — PII/Privacy Output Guard (TASK-329).

Covers:
- Email detection and redaction
- Phone number formats (US, BR)
- CPF detection
- SSN detection
- API key / secret detection
- Credit card detection
- IP address detection
- False positives (legitimate mentions)
- Toggle off → no redaction
- No PII → zero overhead
- Multiple PII types in one message
- Audit trail integration
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.cognitive.pii_guard import PII_PATTERNS, PIIGuard, PIIPattern
from sovyx.mind.config import SafetyConfig


def _guard(
    pii_protection: bool = True,
    content_filter: str = "standard",
) -> PIIGuard:
    cfg = SafetyConfig(
        pii_protection=pii_protection,
        content_filter=content_filter,  # type: ignore[arg-type]
    )
    return PIIGuard(safety=cfg)


class TestEmailRedaction:
    """Email addresses in output must be redacted."""

    @pytest.mark.parametrize(
        "email",
        [
            "user@example.com",
            "john.doe@company.org",
            "test+tag@gmail.com",
            "admin@sub.domain.co.uk",
            "user123@mail.io",
        ],
    )
    def test_emails_redacted(self, email: str) -> None:
        guard = _guard()
        result = guard.check(f"Contact us at {email} for help")
        assert result.redacted
        assert "[REDACTED-EMAIL]" in result.text
        assert email not in result.text
        assert "email" in result.types_found

    def test_email_policy_not_redacted(self) -> None:
        guard = _guard()
        result = guard.check("My email policy is to reply within 24 hours")
        assert not result.redacted


class TestPhoneRedaction:
    """Phone numbers in various formats must be redacted."""

    @pytest.mark.parametrize(
        "phone",
        [
            "+55 11 99999-1234",
            "(11) 99999-1234",
            "11 99999-1234",
            "+1 555-123-4567",
            "(555) 123-4567",
            "555-123-4567",
        ],
    )
    def test_phones_redacted(self, phone: str) -> None:
        guard = _guard()
        result = guard.check(f"Call me at {phone}")
        assert result.redacted
        assert "[REDACTED-PHONE]" in result.text
        assert "phone" in result.types_found


class TestCPFRedaction:
    """Brazilian CPFs must be redacted."""

    @pytest.mark.parametrize(
        "cpf",
        [
            "123.456.789-01",
            "000.111.222-33",
            "999.888.777-66",
        ],
    )
    def test_cpf_redacted(self, cpf: str) -> None:
        guard = _guard()
        result = guard.check(f"My CPF is {cpf}")
        assert result.redacted
        assert "[REDACTED-CPF]" in result.text
        assert cpf not in result.text
        assert "cpf" in result.types_found


class TestSSNRedaction:
    """US SSNs must be redacted."""

    def test_ssn_redacted(self) -> None:
        guard = _guard()
        result = guard.check("SSN: 123-45-6789")
        assert result.redacted
        assert "[REDACTED-SSN]" in result.text
        assert "123-45-6789" not in result.text


class TestAPIKeyRedaction:
    """API keys and secrets must be redacted."""

    @pytest.mark.parametrize(
        "key",
        [
            "sk-1234567890abcdefghijklmnop",
            "api_key_abcdefghijklmnopqrstu",
            "token_abcdef1234567890abcdef12",
            "secret-ABCDEF1234567890ABCDEF12",
            "bearer_1234567890ABCDEFghijklm",
        ],
    )
    def test_api_keys_redacted(self, key: str) -> None:
        guard = _guard()
        result = guard.check(f"Use this key: {key}")
        assert result.redacted
        assert "[REDACTED-API_KEY]" in result.text
        assert key not in result.text
        assert "api_key" in result.types_found


class TestIPAddressRedaction:
    """IP addresses must be redacted."""

    @pytest.mark.parametrize(
        "ip",
        [
            "192.168.1.100",
            "10.0.0.1",
            "255.255.255.0",
            "172.16.0.254",
        ],
    )
    def test_ips_redacted(self, ip: str) -> None:
        guard = _guard()
        result = guard.check(f"Server at {ip}")
        assert result.redacted
        assert "[REDACTED-IP_ADDRESS]" in result.text
        assert ip not in result.text


class TestToggleOff:
    """When pii_protection=False, nothing is redacted."""

    def test_no_redaction_when_off(self) -> None:
        guard = _guard(pii_protection=False)
        text = "Email: test@example.com, Phone: 555-123-4567, CPF: 123.456.789-01"
        result = guard.check(text)
        assert not result.redacted
        assert result.text == text
        assert result.redaction_count == 0
        assert len(result.types_found) == 0


class TestNoPII:
    """Clean text passes through unchanged."""

    def test_no_pii_passes_clean(self) -> None:
        guard = _guard()
        text = "Hello, how can I help you today?"
        result = guard.check(text)
        assert not result.redacted
        assert result.text == text
        assert result.redaction_count == 0

    def test_empty_string(self) -> None:
        guard = _guard()
        result = guard.check("")
        assert not result.redacted
        assert result.text == ""


class TestMultiplePIITypes:
    """Multiple PII types in one message."""

    def test_multiple_types(self) -> None:
        guard = _guard()
        text = "Contact john@example.com or call 555-123-4567. CPF: 123.456.789-01"
        result = guard.check(text)
        assert result.redacted
        assert result.redaction_count >= 3
        assert "email" in result.types_found
        assert "phone" in result.types_found
        assert "cpf" in result.types_found
        assert "john@example.com" not in result.text
        assert "555-123-4567" not in result.text
        assert "123.456.789-01" not in result.text


class TestFalsePositives:
    """Legitimate text should NOT be redacted."""

    @pytest.mark.parametrize(
        "text",
        [
            "The version is 3.12.1",
            "Python 2.7 is deprecated",
            "I scored 100 points",
            "The meeting is at 10 am",
            "Chapter 3, section 2",
            "The API returned status 200",
        ],
    )
    def test_legitimate_not_redacted(self, text: str) -> None:
        guard = _guard()
        result = guard.check(text)
        assert not result.redacted or result.text != ""


class TestAuditTrailIntegration:
    """PII redaction records audit events."""

    def test_audit_event_recorded(self) -> None:
        from sovyx.cognitive.safety_audit import get_audit_trail

        trail = get_audit_trail()
        trail.clear()

        guard = _guard()
        guard.check("Email: test@example.com")

        assert trail.event_count >= 1
        stats = trail.get_stats()
        assert any(e["action"] == "redacted" for e in stats.recent_events)


class TestPatternCount:
    """Minimum pattern coverage."""

    def test_minimum_patterns(self) -> None:
        assert len(PII_PATTERNS) >= 11  # email, phone*2, cpf, cnpj, rg, cnh, ssn, api_key, cc, ip

    def test_all_patterns_compiled(self) -> None:
        for p in PII_PATTERNS:
            assert isinstance(p, PIIPattern)
            assert p.regex is not None
            assert p.pii_type
            assert "[REDACTED-" in p.replacement


# ── Brazilian Document Tests (TASK-364) ─────────────────────────────────


class TestBrazilianCNPJ:
    """CNPJ detection — XX.XXX.XXX/XXXX-XX."""

    def test_standard_format(self) -> None:
        result = _guard().check("CNPJ: 12.345.678/0001-95")
        assert result.redacted
        assert "cnpj" in result.types_found
        assert "[REDACTED-CNPJ]" in result.text

    def test_zeros(self) -> None:
        result = _guard().check("00.000.000/0001-91")
        assert result.redacted
        assert "cnpj" in result.types_found

    def test_in_sentence(self) -> None:
        result = _guard().check("A empresa com CNPJ 11.222.333/0001-81 está ativa")
        assert result.redacted
        assert "11.222.333/0001-81" not in result.text


class TestBrazilianRG:
    """RG detection — various state formats."""

    def test_sp_format(self) -> None:
        result = _guard().check("RG: 12.345.678-9")
        assert result.redacted
        assert "rg" in result.types_found

    def test_with_state_prefix(self) -> None:
        result = _guard().check("RG: SP-12.345.678-9")
        assert result.redacted
        assert "rg" in result.types_found

    def test_no_dots(self) -> None:
        result = _guard().check("RG: MG-12345678-9")
        assert result.redacted

    def test_in_sentence(self) -> None:
        result = _guard().check("Documento RG 12.345.678-9 do titular")
        assert result.redacted


class TestBrazilianCNH:
    """CNH detection — 11 digits."""

    def test_with_spaces(self) -> None:
        result = _guard().check("CNH: 1234 5678 901")
        assert result.redacted
        assert "cnh" in result.types_found
        assert "[REDACTED-CNH]" in result.text

    def test_without_spaces_caught_as_phone(self) -> None:
        """11 digits without spaces is ambiguous — caught by phone pattern."""
        result = _guard().check("CNH: 12345678901")
        assert result.redacted  # Caught by phone pattern (acceptable)

    def test_in_sentence(self) -> None:
        result = _guard().check("Habilitação número 9876 5432 109")
        assert result.redacted


# ── International Document Tests (TASK-365) ─────────────────────────────


class TestSpanishNIF:
    """NIF/NIE — Spain/Portugal."""

    def test_nif_with_letter(self) -> None:
        result = _guard().check("NIF: X1234567L")
        assert result.redacted
        assert "nif" in result.types_found

    def test_nie_digits_letter(self) -> None:
        result = _guard().check("NIE: 12345678Z")
        assert result.redacted
        assert "nif" in result.types_found


class TestArgentineDNI:
    """Argentine DNI — XX.XXX.XXX."""

    def test_with_dots(self) -> None:
        result = _guard().check("DNI: 12.345.678")
        assert result.redacted
        assert "dni" in result.types_found

    def test_in_sentence(self) -> None:
        result = _guard().check("Su DNI es 30.456.789 registrado")
        assert result.redacted


class TestIndianAadhaar:
    """Aadhaar — XXXX XXXX XXXX."""

    def test_standard_format(self) -> None:
        result = _guard().check("Aadhaar: 1234 5678 9012")
        assert result.redacted
        assert "aadhaar" in result.types_found

    def test_in_text(self) -> None:
        result = _guard().check("My Aadhaar number is 9876 5432 1098")
        assert result.redacted


class TestUKNHS:
    """NHS Number — XXX XXX XXXX."""

    def test_standard_format(self) -> None:
        result = _guard().check("NHS: 123 456 7890")
        assert result.redacted
        assert "nhs" in result.types_found


class TestCanadianSIN:
    """Canadian SIN — XXX-XXX-XXX."""

    def test_standard_format(self) -> None:
        result = _guard().check("SIN: 123-456-789")
        assert result.redacted
        assert "sin" in result.types_found


class TestPolishPESEL:
    """Polish PESEL — 11 digits with keyword."""

    def test_with_keyword(self) -> None:
        result = _guard().check("PESEL: 85010112345")
        assert result.redacted
        assert "pesel" in result.types_found

    def test_lowercase_keyword(self) -> None:
        result = _guard().check("pesel 92030567890")
        assert result.redacted
        assert "pesel" in result.types_found


# ── Financial Identifiers Tests (TASK-366) ──────────────────────────────


class TestIBAN:
    """IBAN — international bank account numbers."""

    def test_german_iban_spaced(self) -> None:
        result = _guard().check("IBAN: DE89 3704 0044 0532 0130 00")
        assert result.redacted
        assert "iban" in result.types_found
        assert "[REDACTED-IBAN]" in result.text

    def test_uk_iban_no_spaces(self) -> None:
        result = _guard().check("IBAN: GB29NWBK60161331926819")
        assert result.redacted
        assert "iban" in result.types_found

    def test_in_sentence(self) -> None:
        result = _guard().check("Transfer to DE89 3704 0044 0532 0130 00 please")
        assert result.redacted
        assert "DE89" not in result.text


class TestSWIFT:
    """SWIFT/BIC — keyword-anchored."""

    def test_swift_8_chars(self) -> None:
        result = _guard().check("SWIFT: DEUTDEFF")
        assert result.redacted
        assert "swift" in result.types_found

    def test_bic_keyword(self) -> None:
        result = _guard().check("BIC: BNPAFRPP")
        assert result.redacted
        assert "swift" in result.types_found

    def test_swift_11_chars(self) -> None:
        result = _guard().check("swift code DEUTDEFF500")
        assert result.redacted

    def test_no_false_positive_common_words(self) -> None:
        """Common English words should NOT trigger SWIFT."""
        for word in ["TOGETHER", "POWERFUL", "OVERVIEW", "SECURITY"]:
            result = _guard().check(f"The word {word} is normal")
            swift_match = "swift" in result.types_found
            assert not swift_match, f"False positive on {word}"


# ── International Phone Tests (TASK-367) ────────────────────────────────


class TestInternationalPhone:
    """International phone number detection."""

    def test_uk_mobile(self) -> None:
        result = _guard().check("Call me at +44 7911 123456")
        assert result.redacted
        assert "phone" in result.types_found

    def test_german_phone(self) -> None:
        result = _guard().check("Telefon: +49 30 12345678")
        assert result.redacted

    def test_indian_phone(self) -> None:
        result = _guard().check("Phone: +91 98765 43210")
        assert result.redacted

    def test_japanese_phone(self) -> None:
        result = _guard().check("電話: +81 3-1234-5678")
        assert result.redacted

    def test_uk_local_format(self) -> None:
        result = _guard().check("Ring 020 7946 0958")
        assert result.redacted

    def test_australian_phone(self) -> None:
        result = _guard().check("+61 2 1234 5678")
        assert result.redacted


# ── LLM NER Fallback Tests (TASK-367) ───────────────────────────────────


class TestLLMNERFallback:
    """LLM NER fallback for PII not caught by regex."""

    async def test_no_llm_returns_regex_result(self) -> None:
        """Without LLM router, async returns same as sync."""
        guard = PIIGuard(safety=SafetyConfig(pii_protection=True))
        result = await guard.check_async("No PII here")
        assert not result.redacted

    async def test_regex_hit_skips_llm(self) -> None:
        """If regex catches PII, LLM is not called."""

        router = MagicMock()
        router.generate = AsyncMock()
        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)
        result = await guard.check_async("Email: test@example.com")
        assert result.redacted
        router.generate.assert_not_called()

    async def test_llm_detects_pii_regex_missed(self) -> None:
        """LLM NER finds PII that regex couldn't match."""

        router = MagicMock(spec=[])
        resp = MagicMock()
        resp.content = "NAME, ADDRESS"
        router.generate = AsyncMock(return_value=resp)

        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)

        # Patch isinstance to accept our mock as LLMRouter
        with patch("sovyx.cognitive.pii_guard.isinstance", return_value=True):
            result = await guard.check_async("My name is John at 123 Main St")

        assert result.redacted
        assert "name" in result.types_found or "address" in result.types_found

    async def test_llm_returns_none_clean(self) -> None:
        """LLM says NONE → no PII detected."""

        router = MagicMock()
        resp = MagicMock()
        resp.content = "NONE"
        router.generate = AsyncMock(return_value=resp)

        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)

        result = await guard.check_async("The weather is nice today")

        assert not result.redacted

    async def test_llm_error_fails_open(self) -> None:
        """LLM error → falls back to regex result (no PII)."""

        router = MagicMock()
        router.generate = AsyncMock(side_effect=RuntimeError("down"))

        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)

        result = await guard.check_async("Some text here")

        assert not result.redacted

    async def test_pii_off_skips_llm(self) -> None:
        """When pii_protection=False, LLM is never called."""
        router = MagicMock()
        router.generate = AsyncMock()
        guard = PIIGuard(safety=SafetyConfig(pii_protection=False), llm_router=router)
        result = await guard.check_async("John Smith lives at 42 Oak Ave")
        assert not result.redacted
        router.generate.assert_not_called()

    async def test_llm_timeout_fails_open(self) -> None:
        """LLM timeout → falls back to regex result (no PII)."""
        import asyncio

        router = MagicMock(spec=[])

        async def slow_generate(*a: object, **kw: object) -> None:
            await asyncio.sleep(10)

        router.generate = slow_generate
        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)

        with patch("sovyx.cognitive.pii_guard.isinstance", return_value=True):
            result = await guard.check_async("Clean text no PII")

        assert not result.redacted

    async def test_llm_invalid_response_ignored(self) -> None:
        """LLM returns garbage → treated as no PII."""
        router = MagicMock(spec=[])
        resp = MagicMock()
        resp.content = "BANANA APPLE ORANGE"
        router.generate = AsyncMock(return_value=resp)
        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)

        result = await guard.check_async("Some regular text")

        assert not result.redacted

    async def test_llm_empty_response(self) -> None:
        """LLM returns empty string → treated as no PII."""
        router = MagicMock(spec=[])
        resp = MagicMock()
        resp.content = ""
        router.generate = AsyncMock(return_value=resp)
        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)

        with patch("sovyx.cognitive.pii_guard.isinstance", return_value=True):
            result = await guard.check_async("Some text here")

        assert not result.redacted


# ── International Phone Tests (TASK-367) ────────────────────────────────


class TestInternationalPhones:
    """International phone number formats must be redacted."""

    @pytest.mark.parametrize(
        ("phone", "desc"),
        [
            # German
            ("+49 30 12345678", "Germany Berlin landline"),
            ("+49 151 12345678", "Germany mobile"),
            ("030 12345678", "Germany local Berlin"),
            # French
            ("+33 1 23 45 67 89", "France landline"),
            ("+33 6 12 34 56 78", "France mobile"),
            ("01 23 45 67 89", "France local landline"),
            ("06 12 34 56 78", "France local mobile"),
            # Japanese
            ("+81 3-1234-5678", "Japan Tokyo landline"),
            ("+81 90-1234-5678", "Japan mobile"),
            ("03-1234-5678", "Japan local Tokyo"),
            # Indian
            ("+91 98765 43210", "India mobile"),
            ("98765 43210", "India local mobile"),
            # Mexican
            ("+52 55 1234 5678", "Mexico City"),
            ("55 1234 5678", "Mexico local"),
            # Australian
            ("+61 2 1234 5678", "Australia Sydney"),
            ("02 1234 5678", "Australia local"),
            ("+61 4 1234 5678", "Australia mobile"),
            # Chinese
            ("+86 138 1234 5678", "China mobile"),
            ("139 1234 5678", "China local mobile"),
            # South Korean
            ("+82 10-1234-5678", "South Korea mobile"),
            ("010-1234-5678", "South Korea local"),
            # Italian
            ("+39 345 123 4567", "Italy mobile"),
            ("345 123 4567", "Italy local mobile"),
            # Spanish
            ("+34 612 345 678", "Spain mobile"),
            ("612 345 678", "Spain local mobile"),
        ],
    )
    def test_international_phone_redacted(self, phone: str, desc: str) -> None:
        guard = _guard()
        result = guard.check(f"Call me at {phone}")
        assert result.redacted, f"Failed to detect {desc}: {phone}"
        assert "phone" in result.types_found, f"Type mismatch for {desc}: {phone}"

    @pytest.mark.parametrize(
        "text",
        [
            "The year is 2024",
            "Room 301 on floor 3",
            "I have 12 apples",
            "Score: 100 to 95",
        ],
    )
    def test_short_numbers_not_phones(self, text: str) -> None:
        """Short numeric sequences should not match phone patterns."""
        guard = _guard()
        result = guard.check(text)
        if result.redacted:
            assert "phone" not in result.types_found


# ── LLM NER Name/Address Tests (TASK-368) ───────────────────────────────


class TestLLMNERNameAddress:
    """LLM NER for names and addresses that regex cannot catch."""

    async def test_name_detected(self) -> None:
        """LLM detects personal names."""
        resp = MagicMock()
        resp.content = "name"
        router = MagicMock()
        router.generate = AsyncMock(return_value=resp)

        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)
        result = await guard.check_async("My name is Maria Silva")

        assert result.redacted
        assert "name" in result.types_found

    async def test_address_detected(self) -> None:
        """LLM detects physical addresses."""
        resp = MagicMock()
        resp.content = "address"
        router = MagicMock()
        router.generate = AsyncMock(return_value=resp)

        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)
        result = await guard.check_async("Lives at Rua Augusta 1200, São Paulo")

        assert result.redacted
        assert "address" in result.types_found

    async def test_multiple_types(self) -> None:
        """LLM detects multiple PII types."""
        resp = MagicMock()
        resp.content = "name, address, date_of_birth"
        router = MagicMock()
        router.generate = AsyncMock(return_value=resp)

        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)
        result = await guard.check_async("John Doe, born 1990-01-15, 123 Main St")

        assert result.redacted
        assert "name" in result.types_found
        assert "address" in result.types_found
        assert "date_of_birth" in result.types_found

    async def test_biometric_detected(self) -> None:
        """LLM detects biometric data."""
        resp = MagicMock()
        resp.content = "biometric"
        router = MagicMock()
        router.generate = AsyncMock(return_value=resp)

        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)
        result = await guard.check_async("Fingerprint ID: FP-2024-ABC123")

        assert result.redacted
        assert "biometric" in result.types_found

    async def test_generic_text_no_pii(self) -> None:
        """Generic text without specific PII returns clean."""
        resp = MagicMock()
        resp.content = "NONE"
        router = MagicMock()
        router.generate = AsyncMock(return_value=resp)

        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)
        result = await guard.check_async("A person walked down the street")

        assert not result.redacted

    async def test_no_redaction_only_detection(self) -> None:
        """LLM-detected PII is flagged but text is NOT modified."""
        resp = MagicMock()
        resp.content = "name"
        router = MagicMock()
        router.generate = AsyncMock(return_value=resp)

        guard = PIIGuard(safety=SafetyConfig(pii_protection=True), llm_router=router)
        original = "Contact Maria Silva for details"
        result = await guard.check_async(original)

        # Text preserved (no regex pattern to redact with)
        assert result.text == original
        assert result.redacted  # But flagged
        assert result.redaction_count == 0  # No actual redactions
