import { useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { SendIcon, LoaderIcon, SparklesIcon } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Message {
  role: "assistant" | "user";
  content: string;
}

interface FirstChatStepProps {
  mindName: string;
  language: string;
  provider: string;
  model: string;
  onComplete: () => void;
}

const WELCOME: Record<string, (name: string) => string> = {
  en: (n) =>
    `Hi! I'm ${n}. I run entirely on your hardware \u2014 everything we discuss stays between us. Over time, I'll learn your preferences and get better at helping you.\n\nWhat's on your mind?`,
  pt: (n) =>
    `Oi! Eu sou ${n}. Rodo inteiramente no seu hardware \u2014 tudo o que conversamos fica entre n\u00f3s. Com o tempo, vou aprender suas prefer\u00eancias e melhorar cada vez mais.\n\nNo que posso ajudar?`,
  es: (n) =>
    `\u00a1Hola! Soy ${n}. Funciono completamente en tu hardware \u2014 todo lo que hablemos se queda entre nosotros. Con el tiempo, aprender\u00e9 tus preferencias.\n\n\u00bfEn qu\u00e9 puedo ayudarte?`,
  fr: (n) =>
    `Bonjour ! Je suis ${n}. Je fonctionne enti\u00e8rement sur votre mat\u00e9riel \u2014 tout ce dont nous discutons reste entre nous.\n\nComment puis-je vous aider ?`,
  de: (n) =>
    `Hallo! Ich bin ${n}. Ich laufe vollst\u00e4ndig auf deiner Hardware \u2014 alles, wor\u00fcber wir sprechen, bleibt unter uns.\n\nWie kann ich dir helfen?`,
};

export function FirstChatStep({ mindName, language, provider, model, onComplete }: FirstChatStepProps) {
  const { t } = useTranslation("onboarding");
  const welcomeFn = WELCOME[language] ?? WELCOME.en ?? ((n: string) => `Hi! I'm ${n}.`);
  const [messages, setMessages] = useState<Message[]>([
    { role: "assistant", content: welcomeFn(mindName) },
  ]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [hasReplied, setHasReplied] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;

    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInput("");
    setSending(true);

    try {
      const resp = await api.post<{ response: string }>("/api/chat", {
        message: text,
        user_name: "User",
      });
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: resp.response || "..." },
      ]);
      setHasReplied(true);
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: t("firstChat.errorReply"),
        },
      ]);
      setHasReplied(true);
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }, [input, sending, t]);

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold text-[var(--svx-color-text-primary)]">
          {t("firstChat.title")}
        </h2>
        <p className="mt-1 text-sm text-[var(--svx-color-text-secondary)]">
          {t("firstChat.subtitle", { provider, model })}
        </p>
      </div>

      <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)]">
        <div className="max-h-[320px] min-h-[200px] overflow-y-auto p-4 space-y-3">
          {messages.map((msg, i) => (
            <div
              key={i}
              className={cn(
                "max-w-[85%] rounded-[var(--svx-radius-lg)] px-3.5 py-2.5 text-sm leading-relaxed",
                msg.role === "assistant"
                  ? "bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-primary)]"
                  : "ml-auto bg-[var(--svx-color-brand-primary)] text-white",
              )}
            >
              {msg.role === "assistant" && (
                <div className="mb-1 flex items-center gap-1 text-[10px] font-medium text-[var(--svx-color-text-tertiary)]">
                  <SparklesIcon className="size-3" />
                  {mindName}
                </div>
              )}
              <p className="whitespace-pre-wrap">{msg.content}</p>
            </div>
          ))}
          {sending && (
            <div className="flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]">
              <LoaderIcon className="size-3.5 animate-spin" />
              {t("firstChat.thinking", { name: mindName })}
            </div>
          )}
        </div>

        <div className="border-t border-[var(--svx-color-border-default)] p-3">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void handleSend();
            }}
            className="flex items-center gap-2"
          >
            <input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={t("firstChat.inputPlaceholder")}
              disabled={sending}
              className="flex-1 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-elevated)] px-3 py-2 text-sm text-[var(--svx-color-text-primary)] placeholder:text-[var(--svx-color-text-disabled)] disabled:opacity-50"
              autoFocus
            />
            <Button
              type="submit"
              size="icon"
              disabled={!input.trim() || sending}
            >
              <SendIcon className="size-4" />
            </Button>
          </form>
        </div>
      </div>

      <div className="flex items-center justify-end">
        <Button onClick={onComplete} variant={hasReplied ? "default" : "outline"}>
          {hasReplied ? t("firstChat.exploreButton") : t("firstChat.skipButton")}
        </Button>
      </div>
    </div>
  );
}
