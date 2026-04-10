/**
 * CodeBlock — syntax-highlighted code with copy button.
 *
 * Wraps `<pre>` content already highlighted by rehype-highlight.
 * Children are ReactNode (spans with hljs classes), NOT raw strings.
 * Copy extracts plain text via ref.textContent.
 *
 * All user-facing labels use i18n (chat:codeBlock namespace).
 *
 * @module components/chat/code-block
 */
import { type ReactNode, useCallback, useRef, useState } from "react";

import { CheckIcon, ClipboardIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

interface CodeBlockProps {
  /** Programming language identifier (e.g. "typescript", "python") */
  language?: string;
  /** Pre-highlighted ReactNode from rehype-highlight */
  children: ReactNode;
}

/** Milliseconds to show "Copied" feedback before reverting. */
const COPIED_FEEDBACK_MS = 1_500;

export function CodeBlock({ language, children }: CodeBlockProps) {
  const { t } = useTranslation("chat");
  const [copied, setCopied] = useState(false);
  const codeRef = useRef<HTMLDivElement>(null);

  const handleCopy = useCallback(async () => {
    const text = codeRef.current?.textContent ?? "";
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), COPIED_FEEDBACK_MS);
  }, []);

  return (
    <div className="group relative my-3 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900">
      {/* Header: language label + copy button */}
      <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-1.5">
        <span className="text-xs text-zinc-500" data-testid="code-lang">
          {language || t("codeBlock.code", "code")}
        </span>
        <button
          type="button"
          onClick={() => void handleCopy()}
          className="flex items-center gap-1.5 rounded px-2 py-0.5 text-xs text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
          aria-label={t("codeBlock.copyAria", "Copy code")}
          data-testid="copy-code-btn"
        >
          {copied ? (
            <>
              <CheckIcon className="size-3.5 text-emerald-400" />
              <span>{t("codeBlock.copied", "Copied")}</span>
            </>
          ) : (
            <>
              <ClipboardIcon className="size-3.5" />
              <span>{t("codeBlock.copy", "Copy")}</span>
            </>
          )}
        </button>
      </div>

      {/* Code content — ref for textContent extraction */}
      <div
        ref={codeRef}
        className="overflow-x-auto text-sm leading-relaxed [&_pre]:m-0 [&_pre]:p-4"
        data-testid="code-content"
      >
        {children}
      </div>
    </div>
  );
}
