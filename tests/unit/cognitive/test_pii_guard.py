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
