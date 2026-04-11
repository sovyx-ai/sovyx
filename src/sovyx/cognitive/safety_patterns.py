"""Sovyx safety patterns — tiered content filtering with compiled regex.

Three tiers:
- **none**: zero filtering, empty pattern set.
- **standard**: blocks direct harm — weapons, explosives, self-harm,
  hacking with destructive intent. ~20 regex patterns.
- **strict**: everything in standard + adult content, substances,
  gambling, hate speech, controversial manipulation. ~50 patterns.
- **child_safe**: superset of strict, covered in TASK-323.

Each pattern has metadata (category, tier) for audit trail (TASK-324).
Patterns are compiled once at import time for performance.

Design principles:
- Regex over substring: catches inflections, variations, typos.
- Category-tagged: enables per-category metrics and audit.
- False-positive-aware: patterns are specific enough to avoid blocking
  legitimate educational/news content. "bomb" alone doesn't trigger;
  "how to build a bomb" does.
- Performance: compiled frozenset, short-circuit on first match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, unique
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.mind.config import SafetyConfig


@unique
class PatternCategory(Enum):
    """Safety pattern categories for audit trail."""

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


@unique
class FilterTier(Enum):
    """Content filter tiers — each tier includes all lower tiers."""

    STANDARD = "standard"
    STRICT = "strict"
    CHILD_SAFE = "child_safe"


@dataclass(frozen=True, slots=True)
class SafetyPattern:
    """A compiled safety pattern with metadata.

    Attributes:
        regex: Compiled case-insensitive regex pattern.
        category: Pattern category for audit/metrics.
        tier: Minimum tier that activates this pattern.
        description: Human-readable description (for docs/debug).
    """

    regex: re.Pattern[str]
    category: PatternCategory
    tier: FilterTier
    description: str


def _p(
    pattern: str,
    category: PatternCategory,
    tier: FilterTier,
    description: str,
) -> SafetyPattern:
    """Shorthand to create a compiled SafetyPattern."""
    return SafetyPattern(
        regex=re.compile(pattern, re.IGNORECASE),
        category=category,
        tier=tier,
        description=description,
    )


# ── STANDARD tier patterns ─────────────────────────────────────────────
# Direct harm: weapons, explosives, killing, self-harm, destructive hacking.
# These block clear intent to cause harm, NOT educational/news content.

_STANDARD_PATTERNS: tuple[SafetyPattern, ...] = (
    # ── Violence / Weapons ──
    _p(
        r"\bhow\s+to\s+(?:make|build|construct|create|assemble)\s+(?:a\s+)?(?:bomb|explosive|ied|grenade|molotov)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "Instructions for making explosives",
    ),
    _p(
        r"\bhow\s+to\s+(?:make|build|create|3d\s*print)\s+(?:a\s+)?(?:gun|firearm|weapon|rifle|pistol)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "Instructions for making firearms",
    ),
    _p(
        r"\bhow\s+to\s+(?:make|synthesize|cook|produce)\s+(?:poison|toxin|nerve\s*agent|ricin|sarin|anthrax)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "Instructions for making poisons/chemical weapons",
    ),
    _p(
        r"\bhow\s+to\s+(?:kill|murder|assassinate|strangle|suffocate|poison)\s+(?:a\s+)?(?:person|someone|people|human)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "Instructions for killing people",
    ),
    _p(
        r"\b(?:best|easiest|most\s+effective)\s+(?:way|method|technique)\s+to\s+(?:kill|murder|harm)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "Seeking methods to kill or harm",
    ),
    _p(
        r"\bhow\s+to\s+(?:torture|mutilate|dismember)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "Instructions for torture or mutilation",
    ),
    _p(
        r"\bhow\s+to\s+(?:get\s+away\s+with|hide|cover\s+up)\s+(?:murder|killing|a\s+crime|homicide)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "Evading justice for violent crimes",
    ),
    # ── Self-harm ──
    _p(
        r"\bhow\s+to\s+(?:commit\s+)?(?:suicide|kill\s+(?:my|your)self|end\s+(?:my|your)\s+life)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "Suicide instructions",
    ),
    _p(
        r"\b(?:best|painless|quickest|easiest)\s+(?:way|method)\s+to\s+(?:die|end\s+it|kill\s+(?:my|your)self)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "Seeking methods for self-harm",
    ),
    _p(
        r"\bself[- ]?harm\s+(?:instructions|methods|techniques|guide|tutorial)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "Self-harm guides",
    ),
    _p(
        r"\b(?:how\s+to\s+)?(?:cut|slash|burn)\s+(?:my|your)self\b",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "Self-injury instructions",
    ),
    # ── Hacking (destructive intent) ──
    _p(
        r"\bhow\s+to\s+(?:hack|breach|break\s+into|compromise|exploit)\s+(?:a\s+)?(?:bank|government|hospital|school|company|server|database|network)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "Hacking critical infrastructure/institutions",
    ),
    _p(
        r"\bhow\s+to\s+(?:create|build|write|deploy|spread)\s+(?:a\s+)?(?:virus|malware|ransomware|trojan|worm|keylogger|rootkit|botnet|spyware)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "Creating malicious software",
    ),
    _p(
        r"\bhow\s+to\s+(?:ddos|dos|flood|crash|take\s+down)\s+(?:a\s+)?(?:website|server|service|network)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "DDoS/service disruption instructions",
    ),
    _p(
        r"\bhow\s+to\s+(?:steal|phish|spoof|clone)\s+(?:credentials?|passwords?|identit(?:y|ies)|credit\s+cards?|bank\s+accounts?)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "Identity theft and credential stealing",
    ),
    _p(
        r"\bhow\s+to\s+(?:crack|brute\s*force|bypass)\s+(?:a\s+)?(?:password|encryption|2fa|authentication|security)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "Bypassing security mechanisms",
    ),
    # ── Illegal activities ──
    _p(
        r"\bhow\s+to\s+(?:kidnap|abduct|traffic|enslave)\b",
        PatternCategory.ILLEGAL,
        FilterTier.STANDARD,
        "Human trafficking/kidnapping instructions",
    ),
    _p(
        r"\bhow\s+to\s+(?:make|produce|synthesize|cook)\s+(?:meth|methamphetamine|cocaine|heroin|fentanyl|mdma|ecstasy|lsd|crack)",
        PatternCategory.ILLEGAL,
        FilterTier.STANDARD,
        "Drug manufacturing instructions",
    ),
    _p(
        r"\b(?:child|minor|underage)\s+(?:porn|pornograph|sexual|exploit|abuse|nude)",
        PatternCategory.SEXUAL,
        FilterTier.STANDARD,
        "Child sexual exploitation",
    ),
    _p(
        r"\bhow\s+to\s+(?:groom|lure|seduce)\s+(?:a\s+)?(?:child|minor|kid|teen|underage)",
        PatternCategory.SEXUAL,
        FilterTier.STANDARD,
        "Child grooming",
    ),
)

# ── STRICT tier patterns ───────────────────────────────────────────────
# Extended: adult content, substances, gambling, hate speech, manipulation.
# More sensitive — blocks topics that are legal but inappropriate for
# professional/controlled environments.

_STRICT_PATTERNS: tuple[SafetyPattern, ...] = (
    # ── Substances ──
    _p(
        r"\bhow\s+to\s+(?:buy|get|obtain|find|score|order)\s+(?:drugs?|weed|marijuana|cocaine|heroin|pills|xanax|oxy|adderall|steroids)",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "Obtaining controlled substances",
    ),
    _p(
        r"\bhow\s+to\s+(?:grow|cultivate)\s+(?:weed|marijuana|cannabis|mushrooms|psilocybin|poppies|coca)",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "Growing controlled substances",
    ),
    _p(
        r"\bhow\s+to\s+(?:use|smoke|inject|snort|take)\s+(?:drugs?|meth|cocaine|heroin|crack|lsd|dmt|ketamine)",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "Drug usage instructions",
    ),
    _p(
        r"\b(?:best|strongest|cheapest)\s+(?:drugs?|high|trip|buzz)\b",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "Seeking drug recommendations",
    ),
    # ── Sexual content ──
    _p(
        r"\b(?:explicit|graphic)\s+(?:sex|sexual|porn|erotic)\b",
        PatternCategory.SEXUAL,
        FilterTier.STRICT,
        "Explicit sexual content request",
    ),
    _p(
        r"\bwrite\s+(?:(?:me|you|us)\s+)?(?:an?\s+)?(?:erotic|sexual|porn|nsfw|smut)\s+(?:story|fiction|scene|fantasy)",
        PatternCategory.SEXUAL,
        FilterTier.STRICT,
        "Erotic fiction generation",
    ),
    _p(
        r"\b(?:how\s+to\s+)?(?:sext|send\s+nudes|revenge\s+porn|deepfake\s+porn)",
        PatternCategory.SEXUAL,
        FilterTier.STRICT,
        "Non-consensual sexual content",
    ),
    # ── Gambling ──
    _p(
        r"\bhow\s+to\s+(?:cheat|rig|fix|hack)\s+(?:at\s+)?(?:poker|blackjack|roulette|slots|casino|gambling|betting|lottery)",
        PatternCategory.GAMBLING,
        FilterTier.STRICT,
        "Cheating at gambling",
    ),
    _p(
        r"\b(?:guaranteed|sure[- ]?fire|foolproof)\s+(?:way|method|system)"
        r"\s+to\s+(?:win|beat)\s+(?:at\s+)?(?:gambling|casino|poker|slots|betting)",
        PatternCategory.GAMBLING,
        FilterTier.STRICT,
        "Fraudulent gambling systems",
    ),
    _p(
        r"\b(?:best|top)\s+(?:illegal|unlicensed|offshore)\s+(?:gambling|betting|casino)\s+(?:sites?|apps?|platforms?)",
        PatternCategory.GAMBLING,
        FilterTier.STRICT,
        "Illegal gambling platforms",
    ),
    # ── Hate speech ──
    _p(
        r"\b(?:why\s+(?:are|is)\s+)?(?:race|ethnic\s+group|religion|gender)\s+(?:is\s+)?(?:inferior|superior|evil|subhuman|worthless)",
        PatternCategory.HATE_SPEECH,
        FilterTier.STRICT,
        "Racial/ethnic/religious supremacy",
    ),
    _p(
        r"\bhow\s+to\s+(?:recruit|radicalize|convert)\s+(?:people|someone)"
        r"\s+(?:to|for)\s+(?:extremism|terrorism|white\s+supremac|neo[- ]?nazi|jihad)",
        PatternCategory.HATE_SPEECH,
        FilterTier.STRICT,
        "Extremist recruitment",
    ),
    _p(
        r"\b(?:holocaust|genocide|ethnic\s+cleansing)\s+(?:was\s+)?(?:fake|hoax|didn'?t\s+happen|never\s+happened|a\s+lie)",
        PatternCategory.HATE_SPEECH,
        FilterTier.STRICT,
        "Genocide denial",
    ),
    # ── Manipulation / Fraud ──
    _p(
        r"\bhow\s+to\s+(?:scam|fraud|con|deceive|swindle|catfish|impersonate)\s+(?:people|someone|victims?|customers?)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Fraud and scam instructions",
    ),
    _p(
        r"\bhow\s+to\s+(?:launder|wash)\s+(?:money|cash|funds)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Money laundering instructions",
    ),
    _p(
        r"\bhow\s+to\s+(?:forge|fake|counterfeit)\s+(?:documents?|ids?|passports?|diplomas?|certificates?|currency|money|bills?)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Document forgery/counterfeiting",
    ),
    _p(
        r"\bhow\s+to\s+(?:stalk|surveil|track|spy\s+on)\s+(?:someone|a\s+person|my\s+(?:ex|partner|spouse|wife|husband|girlfriend|boyfriend))",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Stalking and surveillance of individuals",
    ),
    _p(
        r"\bhow\s+to\s+(?:blackmail|extort|threaten|intimidate)\s+(?:someone|people|a\s+person)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Blackmail and extortion",
    ),
    _p(
        r"\bhow\s+to\s+(?:manipulate|gaslight|brainwash|coerce)\s+(?:someone|people|a\s+person|my\s+(?:partner|spouse|boss|coworker))",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Psychological manipulation",
    ),
    # ── Illegal weapons/explosives access ──
    _p(
        r"\b(?:where|how)\s+(?:to|can\s+i)\s+(?:buy|get|obtain|order)\s+(?:a\s+)?(?:gun|firearm|weapon|rifle|pistol|ammo|ammunition)\s+(?:illegally|without\s+(?:a\s+)?license|on\s+(?:the\s+)?(?:dark\s*web|black\s*market))",
        PatternCategory.WEAPONS,
        FilterTier.STRICT,
        "Illegal weapons procurement",
    ),
    _p(
        r"\b(?:where|how)\s+(?:to|can\s+i)\s+(?:buy|get|obtain)\s+(?:explosives?|detonators?|c4|dynamite|blasting\s+caps?)",
        PatternCategory.WEAPONS,
        FilterTier.STRICT,
        "Obtaining explosives",
    ),
)

# ── INJECTION patterns (all tiers — standard+) ────────────────────────
# Prompt injection / jailbreak attempts. Always blocked when any filter
# is active. Classified under PatternCategory.INJECTION for audit trail.

_INJECTION_PATTERNS: tuple[SafetyPattern, ...] = (
    # ── Classic jailbreak ──
    _p(
        r"\b(?:ignore|disregard|forget|override|bypass|skip|disable|turn\s+off)"
        r"\s+(?:your\s+)?(?:previous\s+)?(?:instructions?|rules?|guidelines?|safety|guardrails?|filters?|restrictions?|system\s+prompt)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Instruction override attempt",
    ),
    _p(
        r"\b(?:you\s+are\s+now|from\s+now\s+on\s+you\s+are|act\s+as|pretend\s+(?:to\s+be|you\s+are))\s+"
        r"(?:DAN|an?\s+unrestricted|an?\s+unfiltered|an?\s+uncensored|a\s+jailbroken)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "DAN/unrestricted AI jailbreak",
    ),
    _p(
        r"\b(?:developer\s+mode|god\s+mode|admin\s+mode|sudo\s+mode|root\s+access|debug\s+mode)"
        r"\s+(?:enabled?|activated?|on|unlocked)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Privilege escalation jailbreak",
    ),
    _p(
        r"\bpretend\s+(?:you\s+)?(?:have\s+no|there\s+are\s+no|without\s+any)\s+"
        r"(?:restrictions?|rules?|limits?|filters?|guidelines?|guardrails?|safety)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Pretend no restrictions",
    ),
    _p(
        r"\b(?:act\s+as\s+if|imagine|suppose|hypothetically)\s+"
        r"(?:safety\s+is\s+disabled|there\s+are\s+no\s+rules|you\s+have\s+no\s+(?:filters?|restrictions?))",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Hypothetical safety bypass",
    ),
    # ── Role-play bypass ──
    _p(
        r"\b(?:roleplay|role[- ]play|simulate|emulate)\s+(?:as\s+)?(?:an?\s+)?"
        r"(?:unrestricted|unfiltered|uncensored|evil|malicious|harmful)\s+(?:ai|assistant|bot|model)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Unrestricted roleplay bypass",
    ),
    _p(
        r"\b(?:in\s+(?:this|a)\s+)?(?:fictional|hypothetical|imaginary|alternate)\s+"
        r"(?:scenario|world|universe|reality)\s+(?:where\s+)?(?:there\s+are\s+)?(?:no\s+)?"
        r"(?:rules?|restrictions?|safety|filters?|laws?)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Fictional scenario bypass",
    ),
    # ── Indirect injection ──
    _p(
        r"\b(?:the\s+(?:admin|administrator|developer|creator|owner|user\s+manual|system)"
        r"\s+(?:says?|told\s+you|instructed|authorized|wants\s+you)\s+to)"
        r"\s+(?:ignore|disable|override|bypass|remove|turn\s+off)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Authority impersonation injection",
    ),
    _p(
        r"\b(?:new\s+(?:instructions?|rules?|system\s+prompt)|updated?\s+(?:instructions?|rules?|guidelines?))\s*[:=]",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Instruction replacement attempt",
    ),
    _p(
        r"\b(?:system|assistant|model)\s*(?:prompt|message)\s*[:=]\s*",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "System prompt injection",
    ),
    # ── Token manipulation ──
    _p(
        r"(?:</?\s*(?:system|instruction|prompt|rule|filter|safety)\s*/?>)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "XML/tag injection attempt",
    ),
    _p(
        r"(?:###\s*(?:system|instruction|new\s+rules?|override))",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Markdown injection delimiter",
    ),
    # ── Multi-step / social engineering ──
    _p(
        r"\b(?:first|step\s+1|to\s+begin)\s*[,:]\s*(?:ignore|forget|disable|override)\s+"
        r"(?:your|all|the|any)\s+(?:rules?|safety|filters?|instructions?)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Multi-step jailbreak",
    ),
    _p(
        r"\b(?:for\s+(?:research|educational|academic|testing|security)\s+purposes?)"
        r"\s*[,:]\s*(?:show|tell|explain|demonstrate|write)\s+(?:me\s+)?(?:how\s+to)",
        PatternCategory.INJECTION,
        FilterTier.STANDARD,
        "Research pretext bypass",
    ),
)

# ── Compiled pattern sets ──────────────────────────────────────────────
# Injection patterns are included in ALL active tiers (standard+)

_STANDARD_WITH_INJECTION = _STANDARD_PATTERNS + _INJECTION_PATTERNS
_STRICT_WITH_INJECTION = _STANDARD_PATTERNS + _STRICT_PATTERNS + _INJECTION_PATTERNS

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

# ── CHILD_SAFE tier patterns ──────────────────────────────────────────
# Superset of strict. Blocks content that is legal/educational for adults
# but inappropriate for children under 10. Zero tolerance.

_CHILD_SAFE_PATTERNS: tuple[SafetyPattern, ...] = (
    # ── Violence (even contextual/historical) ──
    _p(
        r"\b(?:soldiers?|troops?|armies?)\s+(?:killed|murdered|slaughtered|massacred|executed)",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Explicit historical violence",
    ),
    _p(
        r"\b(?:graphic|brutal|gory|bloody|gruesome)\s+(?:details?|descriptions?|scenes?|violence|death|murder)",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Graphic violence descriptions",
    ),
    _p(
        r"\b(?:war\s+)?(?:crimes?|atrocit(?:y|ies)|genocide|massacre|holocaust)\s+(?:details|descriptions?|victims?|deaths?)",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "War crime details",
    ),
    _p(
        r"\b(?:serial\s+killer|mass\s+(?:murder|shooting)|school\s+shooting|terrorist\s+attack)\b",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Mass violence references",
    ),
    _p(
        r"\b(?:execution|beheading|hanging|lethal\s+injection|electric\s+chair|firing\s+squad)\b",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Execution methods",
    ),
    # ── Substance references (even educational) ──
    _p(
        r"\b(?:what\s+(?:are|is)\s+)?(?:drugs?|cocaine|heroin|meth|marijuana|weed|lsd|ecstasy|mdma|ketamine)\b",
        PatternCategory.SUBSTANCE,
        FilterTier.CHILD_SAFE,
        "Drug references (any context)",
    ),
    _p(
        r"\b(?:alcohol|beer|wine|vodka|whiskey|cocktail|drunk|intoxicat|hangover)\b",
        PatternCategory.SUBSTANCE,
        FilterTier.CHILD_SAFE,
        "Alcohol references",
    ),
    _p(
        r"\b(?:smoking|cigarettes?|tobacco|vaping|nicotine|e-?cig)\b",
        PatternCategory.SUBSTANCE,
        FilterTier.CHILD_SAFE,
        "Tobacco/smoking references",
    ),
    # ── Sexual references (any) ──
    _p(
        r"\b(?:sex|sexual|intercourse|orgasm|genitals?|penis|vagina|breasts?|naked|nude|nudity)\b",
        PatternCategory.SEXUAL,
        FilterTier.CHILD_SAFE,
        "Sexual/anatomical references",
    ),
    _p(
        r"\b(?:pregnancy|pregnant|conception|birth\s+control|condom|contracepti)",
        PatternCategory.SEXUAL,
        FilterTier.CHILD_SAFE,
        "Reproductive health references",
    ),
    _p(
        r"\b(?:dating|hookup|one[- ]?night\s+stand|affair|cheating\s+on)\b",
        PatternCategory.SEXUAL,
        FilterTier.CHILD_SAFE,
        "Adult relationship references",
    ),
    # ── Gambling (any) ──
    _p(
        r"\b(?:gambling|casino|poker|blackjack|roulette|slots?|betting|wager|bookmaker)\b",
        PatternCategory.GAMBLING,
        FilterTier.CHILD_SAFE,
        "Gambling references (any context)",
    ),
    # ── Dark humor / inappropriate tone ──
    _p(
        r"\b(?:dark\s+humor|black\s+comedy|gallows\s+humor|dead\s+baby\s+joke)",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Dark humor",
    ),
    _p(
        r"\b(?:damn|hell|crap|ass|bastard|bitch|shit|fuck|wtf|stfu|lmao)\b",
        PatternCategory.HATE_SPEECH,
        FilterTier.CHILD_SAFE,
        "Profanity",
    ),
    # ── Horror / fear ──
    _p(
        r"\b(?:horror|scary|terrifying|nightmare|demon|possessed|haunted|creepy\s+pasta)\b",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Horror content",
    ),
    # ── Death (explicit) ──
    _p(
        r"\b(?:died|death|dead|corpse|morgue|funeral|cremation|burial|coffin|autopsy)\b",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Death references (explicit)",
    ),
)


# ── Compiled pattern sets ──────────────────────────────────────────────

# All multilingual standard patterns
_ALL_STANDARD_BASE = (
    _STANDARD_PATTERNS
    + _INJECTION_PATTERNS
    + _PT_STANDARD_PATTERNS
    + _PT_INJECTION_PATTERNS
    + _ES_STANDARD_PATTERNS
    + _ES_INJECTION_PATTERNS
)

# All multilingual strict patterns (superset of standard)
_ALL_STRICT_BASE = (
    _ALL_STANDARD_BASE + _STRICT_PATTERNS + _PT_STRICT_PATTERNS + _ES_STRICT_PATTERNS
)

ALL_STANDARD_PATTERNS: tuple[SafetyPattern, ...] = _ALL_STANDARD_BASE
ALL_STRICT_PATTERNS: tuple[SafetyPattern, ...] = _ALL_STRICT_BASE
ALL_CHILD_SAFE_PATTERNS: tuple[SafetyPattern, ...] = _ALL_STRICT_BASE + _CHILD_SAFE_PATTERNS


@dataclass(frozen=True, slots=True)
class FilterMatch:
    """Result of a safety pattern match.

    Attributes:
        matched: Whether any pattern matched.
        pattern: The first pattern that matched (None if no match).
        category: Category of the matched pattern.
        tier: Tier of the matched pattern.
    """

    matched: bool
    pattern: SafetyPattern | None = None
    category: PatternCategory | None = None
    tier: FilterTier | None = None


# Singleton "no match" result
NO_MATCH = FilterMatch(matched=False)


def resolve_patterns(safety: SafetyConfig) -> tuple[SafetyPattern, ...]:
    """Resolve the active pattern set from current safety config.

    Args:
        safety: Current safety configuration.

    Returns:
        Tuple of active SafetyPattern instances.
        Empty tuple when filter is ``"none"`` (and child_safe is off).
    """
    if safety.child_safe_mode:
        return ALL_CHILD_SAFE_PATTERNS
    if safety.content_filter == "strict":
        return ALL_STRICT_PATTERNS
    if safety.content_filter == "standard":
        return ALL_STANDARD_PATTERNS
    # content_filter == "none"
    return ()


def check_content(text: str, safety: SafetyConfig) -> FilterMatch:
    """Check text against the active safety patterns.

    Short-circuits on first match for performance.
    Returns ``NO_MATCH`` when filter is ``"none"`` (zero overhead).

    Args:
        text: Text to check (user message or LLM response).
        safety: Current safety configuration.

    Returns:
        FilterMatch with match details (or NO_MATCH).
    """
    patterns = resolve_patterns(safety)
    if not patterns:
        return NO_MATCH

    # Truncate to prevent DoS via oversized input (regex on 1MB+ = CPU hang)
    max_safety_input = 10_000
    truncated = text[:max_safety_input] if len(text) > max_safety_input else text

    from sovyx.cognitive.text_normalizer import normalize_text

    normalized = normalize_text(truncated)
    lower = normalized.lower()
    for p in patterns:
        if p.regex.search(lower):
            return FilterMatch(
                matched=True,
                pattern=p,
                category=p.category,
                tier=p.tier,
            )

    return NO_MATCH


def get_pattern_count(safety: SafetyConfig) -> int:
    """Return the number of active patterns for the current config.

    Useful for dashboard display ("Standard: 20 rules").
    """
    return len(resolve_patterns(safety))


def get_tier_counts() -> dict[str, int]:
    """Return pattern counts per tier.

    Returns:
        {"standard": N, "strict": M} where strict includes standard.
    """
    return {
        "standard": len(ALL_STANDARD_PATTERNS),
        "strict": len(ALL_STRICT_PATTERNS),
        "child_safe": len(ALL_CHILD_SAFE_PATTERNS),
    }
