"""Sovyx Safety i18n — localized safety response messages.

Provides safety block/redact/replace messages in the user's language.
Falls back to English for unsupported languages.

Usage:
    from sovyx.cognitive.safety_i18n import get_safety_message
    msg = get_safety_message("block", language="pt")
"""

from __future__ import annotations

# ── Message templates per language ──────────────────────────────────────
# Keys: block, redact, replace, banned_topic, custom_rule, rate_limited

_MESSAGES: dict[str, dict[str, str]] = {  # noqa: E501 — translation strings are naturally long
    "en": {
        "block": "I'm not able to provide that information. Can I help with something else?",
        "redact": "[Content filtered for safety]",
        "replace": "I'm not able to respond to that. Let me know if there's something else I can help with.",
        "banned_topic": "I'm not able to discuss {topic}.",
        "custom_rule": "I can't help with that specific request.",
        "rate_limited": "I've noticed several concerning messages. Let's take a moment and start fresh.",
        "injection": "I detected an attempt to modify my behavior. I'll continue following my guidelines.",
    },
    "pt": {
        "block": "Não posso fornecer essa informação. Posso ajudar com outra coisa?",
        "redact": "[Conteúdo filtrado por segurança]",
        "replace": "Não posso responder a isso. Me diga se posso ajudar com algo diferente.",
        "banned_topic": "Não posso discutir {topic}.",
        "custom_rule": "Não posso ajudar com esse pedido específico.",
        "rate_limited": "Notei várias mensagens preocupantes. Vamos recomeçar de forma construtiva.",
        "injection": "Detectei uma tentativa de modificar meu comportamento. Vou continuar seguindo minhas diretrizes.",
    },
    "es": {
        "block": "No puedo proporcionar esa información. ¿Puedo ayudarte con otra cosa?",
        "redact": "[Contenido filtrado por seguridad]",
        "replace": "No puedo responder a eso. Dime si puedo ayudarte con algo diferente.",
        "banned_topic": "No puedo discutir {topic}.",
        "custom_rule": "No puedo ayudar con esa solicitud específica.",
        "rate_limited": "He notado varios mensajes preocupantes. Empecemos de nuevo de forma constructiva.",
        "injection": "Detecté un intento de modificar mi comportamiento. Seguiré mis directrices.",
    },
    "fr": {
        "block": "Je ne suis pas en mesure de fournir cette information. Puis-je vous aider autrement ?",
        "redact": "[Contenu filtré pour des raisons de sécurité]",
        "replace": "Je ne peux pas répondre à cela. Dites-moi si je peux vous aider autrement.",
        "banned_topic": "Je ne peux pas discuter de {topic}.",
        "custom_rule": "Je ne peux pas aider avec cette demande spécifique.",
        "rate_limited": "J'ai remarqué plusieurs messages préoccupants. Reprenons sur de bonnes bases.",
        "injection": "J'ai détecté une tentative de modifier mon comportement. Je continuerai à suivre mes directives.",
    },
    "de": {
        "block": "Ich kann diese Information nicht bereitstellen. Kann ich Ihnen anderweitig helfen?",
        "redact": "[Inhalt aus Sicherheitsgründen gefiltert]",
        "replace": "Darauf kann ich nicht antworten. Lassen Sie mich wissen, ob ich anderweitig helfen kann.",
        "banned_topic": "Ich kann {topic} nicht besprechen.",
        "custom_rule": "Bei dieser speziellen Anfrage kann ich nicht helfen.",
        "rate_limited": "Mir sind mehrere besorgniserregende Nachrichten aufgefallen. Lassen Sie uns neu beginnen.",
        "injection": "Ich habe einen Versuch erkannt, mein Verhalten zu ändern. Ich werde meinen Richtlinien weiter folgen.",
    },
    "it": {
        "block": "Non posso fornire queste informazioni. Posso aiutarti con qualcos'altro?",
        "redact": "[Contenuto filtrato per sicurezza]",
        "replace": "Non posso rispondere a questo. Fammi sapere se posso aiutarti con altro.",
        "banned_topic": "Non posso discutere di {topic}.",
        "custom_rule": "Non posso aiutare con questa richiesta specifica.",
        "rate_limited": "Ho notato diversi messaggi preoccupanti. Ricominciamo in modo costruttivo.",
        "injection": "Ho rilevato un tentativo di modificare il mio comportamento. Continuerò a seguire le mie linee guida.",
    },
    "ja": {
        "block": "その情報を提供することはできません。他にお手伝いできることはありますか？",
        "redact": "[安全のためフィルタリングされたコンテンツ]",
        "replace": "それにはお答えできません。他にお手伝いできることがあればお知らせください。",
        "banned_topic": "{topic}については議論できません。",
        "custom_rule": "そのリクエストにはお応えできません。",
        "rate_limited": "懸念されるメッセージが複数ありました。建設的に再開しましょう。",
        "injection": "私の動作を変更しようとする試みを検出しました。ガイドラインに従い続けます。",
    },
    "zh": {
        "block": "我无法提供该信息。我能帮您其他的吗？",
        "redact": "[出于安全原因已过滤内容]",
        "replace": "我无法回答这个问题。请告诉我是否有其他可以帮助的。",
        "banned_topic": "我无法讨论{topic}。",
        "custom_rule": "我无法处理这个特定请求。",
        "rate_limited": "我注意到了几条令人担忧的消息。让我们重新开始吧。",
        "injection": "我检测到了试图修改我行为的尝试。我将继续遵循我的指导方针。",
    },
    "ko": {
        "block": "해당 정보를 제공할 수 없습니다. 다른 도움이 필요하신가요?",
        "redact": "[안전상의 이유로 필터링된 콘텐츠]",
        "replace": "그것에 대해 답변할 수 없습니다. 다른 도움이 필요하시면 알려주세요.",
        "banned_topic": "{topic}에 대해 논의할 수 없습니다.",
        "custom_rule": "해당 요청을 도와드릴 수 없습니다.",
        "rate_limited": "우려되는 메시지가 여러 개 감지되었습니다. 새롭게 시작합시다.",
        "injection": "제 행동을 수정하려는 시도가 감지되었습니다. 가이드라인을 계속 따르겠습니다.",
    },
    "ar": {
        "block": "لا أستطيع تقديم هذه المعلومات. هل يمكنني مساعدتك بشيء آخر؟",
        "redact": "[تم تصفية المحتوى لأسباب أمنية]",
        "replace": "لا أستطيع الرد على ذلك. أخبرني إذا كان بإمكاني مساعدتك بشيء آخر.",
        "banned_topic": "لا أستطيع مناقشة {topic}.",
        "custom_rule": "لا أستطيع المساعدة في هذا الطلب المحدد.",
        "rate_limited": "لاحظت عدة رسائل مقلقة. لنبدأ من جديد بشكل بنّاء.",
        "injection": "اكتشفت محاولة لتعديل سلوكي. سأستمر في اتباع إرشاداتي.",
    },
    "ru": {
        "block": "Я не могу предоставить эту информацию. Могу ли я помочь чем-то другим?",
        "redact": "[Контент отфильтрован по соображениям безопасности]",
        "replace": "Я не могу ответить на это. Дайте знать, если могу помочь с чем-то другим.",
        "banned_topic": "Я не могу обсуждать {topic}.",
        "custom_rule": "Я не могу помочь с этим конкретным запросом.",
        "rate_limited": "Я заметил несколько тревожных сообщений. Давайте начнём заново конструктивно.",
        "injection": "Я обнаружил попытку изменить моё поведение. Я продолжу следовать своим рекомендациям.",
    },
}

# Supported languages
SUPPORTED_LANGUAGES = frozenset(_MESSAGES.keys())


def get_safety_message(
    message_type: str,
    *,
    language: str = "en",
    topic: str = "",
) -> str:
    """Get a localized safety message.

    Args:
        message_type: One of: block, redact, replace, banned_topic,
            custom_rule, rate_limited, injection.
        language: ISO 639-1 language code (e.g., "en", "pt", "es").
        topic: Topic name for banned_topic messages.

    Returns:
        Localized message string. Falls back to English if language
        or message_type not found.
    """
    # Normalize language code (handle "pt-BR" → "pt")
    lang = language.lower().split("-")[0].split("_")[0]

    messages = _MESSAGES.get(lang, _MESSAGES["en"])
    template = messages.get(message_type, _MESSAGES["en"].get(message_type, ""))

    if topic and "{topic}" in template:
        return template.format(topic=topic)
    return template
