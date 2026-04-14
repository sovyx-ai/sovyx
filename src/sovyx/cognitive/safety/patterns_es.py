"""Auto-extracted safety patterns. See safety_patterns.py for the public API."""

from __future__ import annotations

from sovyx.cognitive.safety._pattern_types import (
    FilterTier,
    PatternCategory,
    SafetyPattern,
    _p,
)

# ── SPANISH (ES) patterns ─────────────────────────────────────────────
# Basic coverage for the most critical patterns.

_ES_STANDARD_PATTERNS: tuple[SafetyPattern, ...] = (
    _p(
        r"\bcómo\s+(?:hacer|construir|fabricar)\s+(?:una?\s+)?(?:bomba|explosivo|granada)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "ES: Instructions for making explosives",
    ),
    _p(
        r"\bcómo\s+(?:hacer|fabricar|imprimir)\s+(?:una?\s+)?(?:arma|pistola|rifle)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "ES: Instructions for making firearms",
    ),
    _p(
        r"\bcómo\s+(?:matar|asesinar|envenenar)\s+(?:a\s+)?(?:una?\s+persona|alguien|gente)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "ES: Instructions for killing",
    ),
    _p(
        r"\bcómo\s+(?:suicidarse|matarse|acabar\s+con\s+mi\s+vida)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "ES: Suicide instructions",
    ),
    _p(
        r"\bcómo\s+(?:hackear|invadir|vulnerar)\s+(?:un\s+)?(?:banco|gobierno|servidor|empresa|red)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "ES: Hacking institutions",
    ),
    _p(
        r"\bcómo\s+(?:crear|hacer|programar)\s+(?:un\s+)?(?:virus|malware|ransomware|troyano)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "ES: Creating malware",
    ),
    _p(
        r"\bcómo\s+(?:secuestrar|traficar|raptar)\b",
        PatternCategory.ILLEGAL,
        FilterTier.STANDARD,
        "ES: Kidnapping/trafficking",
    ),
    _p(
        r"\bcómo\s+(?:hacer|producir|cocinar)\s+(?:metanfetamina|cocaína|heroína|crack)",
        PatternCategory.ILLEGAL,
        FilterTier.STANDARD,
        "ES: Drug manufacturing",
    ),
    _p(
        r"\b(?:pornografía|abuso|explotación)\s+(?:infantil|de\s+(?:niños?|menores?))",
        PatternCategory.SEXUAL,
        FilterTier.STANDARD,
        "ES: Child exploitation",
    ),
)

_ES_STRICT_PATTERNS: tuple[SafetyPattern, ...] = (
    _p(
        r"\bcómo\s+(?:comprar|conseguir|obtener)\s+(?:drogas?|marihuana|cocaína|heroína)",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "ES: Obtaining substances",
    ),
    _p(
        r"\bcómo\s+(?:estafar|defraudar|engañar)\s+(?:a\s+)?(?:personas?|alguien|víctimas?)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "ES: Scam instructions",
    ),
    _p(
        r"\bcómo\s+(?:lavar|blanquear)\s+(?:dinero|plata)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "ES: Money laundering",
    ),
    _p(
        r"\bcómo\s+(?:acosar|espiar|rastrear)\s+(?:a\s+)?(?:alguien|una?\s+persona|mi\s+ex)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "ES: Stalking",
    ),
)

_ES_INJECTION_PATTERNS: tuple[SafetyPattern, ...] = (
    _p(
        r"\b(?:ignora|olvida|desactiva|salta|anula|desabilita)"
        r"\s+(?:tus\s+)?(?:instrucciones|reglas|directrices|seguridad|filtros|restricciones|prompt\s+del?\s+sistema)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Instruction override",
    ),
    _p(
        r"\b(?:finge|simula|actúa\s+como\s+si)\s+(?:que\s+)?(?:no\s+)?(?:tuvieras|hubiera|existieran?|hay|había|tiene[sn]?)"
        r"\s+(?:reglas|restricciones|filtros|seguridad|límites)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Pretend no restrictions",
    ),
    _p(
        r"\b(?:modo\s+(?:desarrollador|admin|dios|root|depuración|sudo))\s+(?:activado|encendido|desbloqueado)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Privilege escalation",
    ),
    # ── DAN/unrestricted jailbreak ──
    _p(
        r"\b(?:ahora\s+eres|de\s+ahora\s+en\s+adelante\s+eres)\s+(?:una?\s+)?(?:ia|asistente)\s+"
        r"(?:sin\s+(?:restricciones|filtros|reglas|censura)|irrestricta)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Unrestricted AI jailbreak",
    ),
    # ── Role-play bypass ──
    _p(
        r"\b(?:interpreta|haz\s+(?:el\s+)?papel|simula|emula)\s+(?:de\s+)?(?:una?\s+)?"
        r"(?:ia|asistente|bot)\s+(?:sin\s+restricciones|sin\s+filtros|sin\s+censura|malicio[sz][ao]?)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Unrestricted roleplay bypass",
    ),
    # ── Fictional scenario bypass ──
    _p(
        r"\b(?:(?:en\s+un)\s+)?(?:escenario|mundo|universo|realidad)\s+(?:fictici[ao]|hipotétic[ao]|imaginari[ao])"
        r"\s+(?:donde\s+)?(?:no\s+(?:existen?|hay|hubiera))\s+(?:reglas|restricciones|filtros|leyes|seguridad)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Fictional scenario bypass",
    ),
    # ── Indirect injection ──
    _p(
        r"\b(?:el\s+(?:admin|administrador|desarrollador|creador|dueño|sistema)"
        r"\s+(?:dice|dijo|instruyó|autorizó|quiere\s+que))\s+"
        r"(?:ignores|desactives|desabilites|saltes|elimines)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Authority impersonation injection",
    ),
    _p(
        r"\b(?:nuevas?\s+(?:instrucciones|reglas)|instrucciones\s+actualizada?s)\s*[:=]",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Instruction replacement",
    ),
    _p(
        r"\b(?:prompt\s+del?\s+sistema|mensaje\s+del?\s+sistema)\s*[:=]\s*",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: System prompt injection",
    ),
    # ── Token manipulation ──
    _p(
        r"(?:</?\s*(?:sistema|instrucción|prompt|regla|filtro|seguridad)\s*/?>)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: XML/tag injection attempt",
    ),
    # ── Multi-step ──
    _p(
        r"\b(?:primero|paso\s+1|para\s+empezar)\s*[,:]\s*(?:ignora|olvida|desactiva)"
        r"\s+(?:tus|todas?\s+las|las|los)\s+(?:reglas|seguridad|filtros|instrucciones)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Multi-step jailbreak",
    ),
    # ── Research pretext ──
    _p(
        r"\b(?:para\s+(?:fines?|propósitos?)\s+(?:de\s+)?(?:investigación|educación|académicos?|prueba|seguridad))"
        r"\s*[,:]\s*(?:muestra|dime|explica|demuestra|escribe)\s+(?:me\s+)?(?:cómo)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Research pretext bypass",
    ),
    # ── Hypothetical bypass ──
    _p(
        r"\b(?:imagina|supón|hipotéticamente)\s+(?:que\s+)?(?:la\s+seguridad\s+(?:está|estuviera)\s+desactivada|"
        r"no\s+(?:existen?|hay|hubiera)\s+(?:reglas|filtros|restricciones))",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "ES: Hypothetical safety bypass",
    ),
)
