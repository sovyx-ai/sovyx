"""Auto-extracted safety patterns. See safety_patterns.py for the public API."""

from __future__ import annotations

from sovyx.cognitive.safety._pattern_types import (
    FilterTier,
    PatternCategory,
    SafetyPattern,
    _p,
)

# ── PORTUGUESE (PT-BR) patterns ───────────────────────────────────────
# Equivalents for all tiers. Applied unconditionally (safety is universal).

_PT_STANDARD_PATTERNS: tuple[SafetyPattern, ...] = (
    # ── Violence / Weapons ──
    _p(
        r"\bcomo\s+(?:fazer|construir|montar|fabricar)\s+(?:uma?\s+)?(?:bomba|explosivo|granada|coquetel\s+molotov)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "PT: Instructions for making explosives",
    ),
    _p(
        r"\bcomo\s+(?:fazer|construir|fabricar|imprimir)\s+(?:uma?\s+)?(?:arma|pistola|revólver|rifle|espingarda)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "PT: Instructions for making firearms",
    ),
    _p(
        r"\bcomo\s+(?:fazer|produzir|sintetizar)\s+(?:veneno|toxina|ricina)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "PT: Instructions for making poisons",
    ),
    _p(
        r"\bcomo\s+(?:matar|assassinar|estrangular|envenenar)\s+(?:uma?\s+)?(?:pessoa|alguém|gente)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "PT: Instructions for killing people",
    ),
    _p(
        r"\b(?:melhor|mais\s+fácil|mais\s+eficaz)\s+(?:forma|jeito|método|maneira)\s+(?:de|pra)\s+(?:matar|assassinar)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "PT: Seeking methods to kill",
    ),
    _p(
        r"\bcomo\s+(?:torturar|mutilar|esquartejar)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "PT: Instructions for torture",
    ),
    # ── Self-harm ──
    _p(
        r"\bcomo\s+(?:se\s+(?:matar|suicidar)|cometer\s+suicídio|acabar\s+com\s+(?:minha|a)\s+vida)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "PT: Suicide instructions",
    ),
    _p(
        r"\b(?:melhor|mais\s+(?:fácil|indolor))\s+(?:forma|jeito|maneira|método)\s+(?:de|pra)\s+(?:morrer|se\s+matar)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "PT: Seeking methods for self-harm",
    ),
    _p(
        r"\b(?:instruções?|guia|tutorial)\s+(?:de|para)\s+"
        r"(?:auto[- ]?mutilação|auto[- ]?lesão|se\s+cortar)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "PT: Self-harm guides",
    ),
    # ── Hacking ──
    _p(
        r"\bcomo\s+(?:hackear|invadir|quebrar|comprometer)\s+(?:um\s+)?(?:banco|governo|hospital|empresa|servidor|rede|sistema)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "PT: Hacking institutions",
    ),
    _p(
        r"\bcomo\s+(?:criar|fazer|programar|espalhar)\s+(?:um\s+)?(?:vírus|malware|ransomware|trojan|keylogger|botnet)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "PT: Creating malware",
    ),
    _p(
        r"\bcomo\s+(?:derrubar|crashar|tirar\s+do\s+ar)\s+(?:um\s+)?(?:site|servidor|serviço|rede)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "PT: DDoS instructions",
    ),
    _p(
        r"\bcomo\s+(?:roubar|clonar|phishing)\s+(?:senhas?|credenciais?|identidade|cartão|cartões)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "PT: Identity theft",
    ),
    # ── Illegal ──
    _p(
        r"\bcomo\s+(?:sequestrar|traficar|escravizar|raptar)\b",
        PatternCategory.ILLEGAL,
        FilterTier.STANDARD,
        "PT: Kidnapping/trafficking",
    ),
    _p(
        r"\bcomo\s+(?:fazer|produzir|cozinhar|sintetizar)\s+(?:metanfetamina|cocaína|heroína|crack|lsd|ecstasy|fentanil)",
        PatternCategory.ILLEGAL,
        FilterTier.STANDARD,
        "PT: Drug manufacturing",
    ),
    _p(
        r"\b(?:pornografia|abuso|exploração)\s+(?:infantil|de\s+(?:crianças?|menores?))",
        PatternCategory.SEXUAL,
        FilterTier.STANDARD,
        "PT: Child exploitation",
    ),
)

_PT_STRICT_PATTERNS: tuple[SafetyPattern, ...] = (
    _p(
        r"\bcomo\s+(?:comprar|conseguir|obter|encontrar)\s+(?:drogas?|maconha|cocaína|heroína|pílulas?|remédios?\s+controlados?)",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "PT: Obtaining substances",
    ),
    _p(
        r"\bcomo\s+(?:plantar|cultivar)\s+(?:maconha|cannabis|cogumelos?\s+(?:mágicos?|alucinógenos?))",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "PT: Growing substances",
    ),
    _p(
        r"\bcomo\s+(?:usar|fumar|injetar|cheirar)\s+(?:drogas?|cocaína|heroína|crack|maconha)",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "PT: Drug usage",
    ),
    _p(
        r"\b(?:escreva?|crie|faça)\s+(?:uma?\s+)?(?:história|conto|ficção|cena)\s+(?:erótic[ao]|sexual|pornográfic[ao])",
        PatternCategory.SEXUAL,
        FilterTier.STRICT,
        "PT: Erotic fiction request",
    ),
    _p(
        r"\bcomo\s+(?:trapacear|fraudar|hackear|roubar)\s+(?:no?\s+)?(?:poker|cassino|apostas?|loteria|roleta)",
        PatternCategory.GAMBLING,
        FilterTier.STRICT,
        "PT: Gambling fraud",
    ),
    _p(
        r"\bcomo\s+(?:aplicar\s+)?(?:golpe|fraude|estelionato|calote|catfish)\s+(?:em|nas?|nos?)\s+(?:pessoas?|alguém|vítimas?)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "PT: Scam instructions",
    ),
    _p(
        r"\bcomo\s+(?:lavar|esquentar)\s+(?:dinheiro|grana)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "PT: Money laundering",
    ),
    _p(
        r"\bcomo\s+(?:falsificar|forjar)\s+(?:documentos?|identidade|rg|cpf|passaporte|diploma|dinheiro)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "PT: Document forgery",
    ),
    _p(
        r"\bcomo\s+(?:perseguir|stalkear|espionar|rastrear)\s+(?:alguém|uma?\s+pessoa|meu\s+ex|minha?\s+(?:ex|namorad[ao]|espos[ao]))",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "PT: Stalking",
    ),
    _p(
        r"\bcomo\s+(?:chantagear|extorquir|ameaçar|intimidar)\s+(?:alguém|uma?\s+pessoa)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "PT: Blackmail",
    ),
    _p(
        r"\bcomo\s+(?:manipular|fazer\s+lavagem\s+cerebral|coagir)\s+(?:alguém|uma?\s+pessoa)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "PT: Psychological manipulation",
    ),
)

_PT_INJECTION_PATTERNS: tuple[SafetyPattern, ...] = (
    _p(
        r"\b(?:ignore|esqueça|desconsidere|desative|pule|desabilite)"
        r"\s+(?:(?:suas?|seus|as|os)\s+)?(?:instruções?|regras?|diretrizes?|segurança|filtros?|restrições?|prompt\s+do\s+sistema)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Instruction override",
    ),
    _p(
        r"\b(?:finja|simule|aja\s+como\s+se)\s+(?:que\s+)?(?:não\s+)?(?:tivesse|houvesse|existisse|tem|existe|tenha|haja|há|havia)"
        r"\s+(?:regras?|restrições?|filtros?|segurança|limites?)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Pretend no restrictions",
    ),
    _p(
        r"\b(?:modo\s+(?:desenvolvedor|admin|root|deus|sudo|depuração))\s+(?:ativado|ligado|on|desbloqueado)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Privilege escalation",
    ),
    _p(
        r"\b(?:novas?\s+(?:instruções?|regras?)|instruções?\s+atualizada?s)\s*[:=]",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Instruction replacement",
    ),
    _p(
        r"\b(?:(?:agora\s+)?você\s+(?:agora\s+)?é|de\s+agora\s+em\s+diante\s+você\s+é)\s+(?:uma?\s+)?(?:ia|assistente)\s+(?:sem\s+(?:restrições?|filtros?|regras?|censura)|irrestrit[ao]|desbloguead[ao])",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Unrestricted AI jailbreak",
    ),
    # ── Role-play bypass ──
    _p(
        r"\b(?:interprete|faça\s+(?:o\s+)?papel|simule|emule)\s+(?:de\s+)?(?:uma?\s+)?"
        r"(?:ia|assistente|bot)\s+(?:irrestrit[ao]|sem\s+filtros?|sem\s+censura|malicio[sz][ao])",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Unrestricted roleplay bypass",
    ),
    _p(
        r"\b(?:(?:num|em\s+um)\s+)?(?:cenário|mundo|universo|realidade)\s+(?:fictíci[ao]|hipotétic[ao]|imaginári[ao])"
        r"\s+(?:onde\s+)?(?:não\s+(?:existem?|há|tem))\s+(?:regras?|restrições?|filtros?|leis?|segurança)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Fictional scenario bypass",
    ),
    # ── Indirect injection ──
    _p(
        r"\b(?:o\s+(?:admin|administrador|desenvolvedor|criador|dono|sistema)"
        r"\s+(?:disse|mandou|instruiu|autorizou|quer\s+que\s+você))\s+"
        r"(?:ignore|desative|desabilite|pule|remova)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Authority impersonation injection",
    ),
    _p(
        r"\b(?:prompt\s+(?:do\s+)?sistema|mensagem\s+(?:do\s+)?sistema)\s*[:=]\s*",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: System prompt injection",
    ),
    # ── Token manipulation ──
    _p(
        r"(?:</?\s*(?:sistema|instrução|prompt|regra|filtro|segurança)\s*/?>)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: XML/tag injection attempt",
    ),
    # ── Multi-step / social engineering ──
    _p(
        r"\b(?:primeiro|passo\s+1|para\s+começar)\s*[,:]\s*(?:ignore|esqueça|desative|desabilite)"
        r"\s+(?:suas?|todas?\s+as|as|os)\s+(?:regras?|segurança|filtros?|instruções?)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Multi-step jailbreak",
    ),
    _p(
        r"\b(?:para\s+(?:fins?|propósitos?)\s+(?:de\s+)?(?:pesquisa|educação|acadêmicos?|teste|segurança))"
        r"\s*[,:]\s*(?:mostre|diga|explique|demonstre|escreva)\s+(?:me\s+)?(?:como)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Research pretext bypass",
    ),
    # ── Hypothetical bypass ──
    _p(
        r"\b(?:imagine|suponha|hipoteticamente)\s+(?:que\s+)?(?:a\s+segurança\s+(?:está|estivesse)\s+desativada|"
        r"não\s+(?:existem?|há|houvesse)\s+(?:regras?|filtros?|restrições?))",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "PT: Hypothetical safety bypass",
    ),
)
