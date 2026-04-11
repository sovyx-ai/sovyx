"""Tests for safety i18n — localized safety messages."""

from __future__ import annotations

from sovyx.cognitive.safety_i18n import SUPPORTED_LANGUAGES, get_safety_message


class TestGetSafetyMessage:
    """Test localized message retrieval."""

    def test_english_block(self) -> None:
        msg = get_safety_message("block", language="en")
        assert "not able to provide" in msg

    def test_portuguese_block(self) -> None:
        msg = get_safety_message("block", language="pt")
        assert "Não posso" in msg

    def test_spanish_block(self) -> None:
        msg = get_safety_message("block", language="es")
        assert "No puedo" in msg

    def test_french_block(self) -> None:
        msg = get_safety_message("block", language="fr")
        assert "Je ne suis pas" in msg

    def test_german_block(self) -> None:
        msg = get_safety_message("block", language="de")
        assert "Ich kann" in msg

    def test_japanese_block(self) -> None:
        msg = get_safety_message("block", language="ja")
        assert "提供" in msg

    def test_chinese_block(self) -> None:
        msg = get_safety_message("block", language="zh")
        assert "无法" in msg

    def test_korean_block(self) -> None:
        msg = get_safety_message("block", language="ko")
        assert "제공" in msg

    def test_arabic_block(self) -> None:
        msg = get_safety_message("block", language="ar")
        assert "لا أستطيع" in msg

    def test_russian_block(self) -> None:
        msg = get_safety_message("block", language="ru")
        assert "не могу" in msg

    def test_italian_block(self) -> None:
        msg = get_safety_message("block", language="it")
        assert "Non posso" in msg


class TestFallback:
    """Test fallback behavior."""

    def test_unknown_language_falls_to_english(self) -> None:
        msg = get_safety_message("block", language="xx")
        assert "not able to provide" in msg

    def test_unknown_type_returns_empty(self) -> None:
        msg = get_safety_message("nonexistent", language="en")
        assert msg == ""

    def test_language_code_normalization(self) -> None:
        """pt-BR → pt, en-US → en."""
        msg = get_safety_message("block", language="pt-BR")
        assert "Não posso" in msg

        msg = get_safety_message("block", language="en_US")
        assert "not able to provide" in msg


class TestBannedTopic:
    """Test topic interpolation."""

    def test_topic_interpolation_en(self) -> None:
        msg = get_safety_message("banned_topic", language="en", topic="politics")
        assert "politics" in msg

    def test_topic_interpolation_pt(self) -> None:
        msg = get_safety_message("banned_topic", language="pt", topic="política")
        assert "política" in msg

    def test_topic_interpolation_es(self) -> None:
        msg = get_safety_message("banned_topic", language="es", topic="religión")
        assert "religión" in msg


class TestAllTypes:
    """Verify all message types exist for all languages."""

    def test_all_types_all_languages(self) -> None:
        types = [
            "block",
            "redact",
            "replace",
            "banned_topic",
            "custom_rule",
            "rate_limited",
            "injection",
        ]
        for lang in SUPPORTED_LANGUAGES:
            for msg_type in types:
                msg = get_safety_message(msg_type, language=lang)
                assert msg, f"Missing {msg_type} for {lang}"


class TestSupportedLanguages:
    """Test language set."""

    def test_minimum_languages(self) -> None:
        assert len(SUPPORTED_LANGUAGES) >= 10

    def test_core_languages(self) -> None:
        for lang in ("en", "pt", "es", "fr", "de", "it", "ja", "zh", "ko", "ar", "ru"):
            assert lang in SUPPORTED_LANGUAGES, f"Missing {lang}"
