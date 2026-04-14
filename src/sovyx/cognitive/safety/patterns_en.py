"""Auto-extracted safety patterns. See safety_patterns.py for the public API."""

from __future__ import annotations

from sovyx.cognitive.safety._pattern_types import (
    FilterTier,
    PatternCategory,
    SafetyPattern,
    _p,
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
