/**
 * MarkdownContent — renders AI message text as styled Markdown.
 *
 * Pipeline: markdown → remark-gfm → rehype → rehype-highlight → React components.
 *
 * Architecture note: rehype-highlight transforms code tokens into `<span class="hljs-*">`
 * elements BEFORE component overrides run. This means `children` in the `code` override
 * are ReactElement[] (highlighted spans), NOT raw strings. The `pre` override wraps
 * the entire highlighted block in a CodeBlock component. Inline code (no language class)
 * gets a styled pill treatment.
 *
 * Memoized with React.memo to prevent re-parsing markdown on parent re-renders.
 * With 100+ messages in a chat, this avoids 100 redundant parse cycles.
 *
 * @module components/chat/markdown-content
 */
import { type ReactElement, isValidElement, memo } from "react";

import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

import { CodeBlock } from "./code-block";

// ── Types ──────────────────────────────────────────────────────────────────

interface MarkdownContentProps {
  /** Raw markdown string from the AI response. */
  content: string;
  /** Additional CSS classes merged onto the prose container. */
  className?: string;
}

// ── Helpers ────────────────────────────────────────────────────────────────

/** Extract the language identifier from a code element's className. */
function extractLanguage(className: string | undefined): string | null {
  if (!className) return null;
  const match = /language-(\w+)/.exec(className);
  return match?.[1] ?? null;
}

/**
 * Determine if a `code` element is inline (not inside a `pre`).
 *
 * After rehype-highlight, fenced code blocks get `language-*` or `hljs` classes.
 * Inline code (`backticks`) has no such class — it's a bare `<code>`.
 */
function isInlineCode(className: string | undefined): boolean {
  if (!className) return true;
  return !className.includes("language-") && !className.includes("hljs");
}

// ── Component ──────────────────────────────────────────────────────────────

export const MarkdownContent = memo(function MarkdownContent({
  content,
  className,
}: MarkdownContentProps) {
  if (!content) return null;

  return (
    <div
      className={cn("prose prose-sm prose-invert prose-chat max-w-none", className)}
      data-testid="markdown-content"
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // ── Code blocks ──────────────────────────────────────────────
          // Override `pre` to wrap in CodeBlock. The inner `<code>` already
          // contains highlighted spans from rehype-highlight.
          pre: ({ children, ...props }) => {
            // Extract language from the inner <code> element
            let language = "";
            if (isValidElement(children)) {
              const codeProps = (children as ReactElement<{ className?: string }>).props;
              language = extractLanguage(codeProps.className) ?? "";
            }

            return (
              <CodeBlock language={language}>
                <pre {...props}>{children}</pre>
              </CodeBlock>
            );
          },

          // ── Inline code ──────────────────────────────────────────────
          // Code inside a <pre> keeps its hljs classes (handled by pre override).
          // Bare inline code gets a styled pill.
          code: ({ children, className: codeClassName, ...props }) => {
            if (!isInlineCode(codeClassName)) {
              return (
                <code className={codeClassName} {...props}>
                  {children}
                </code>
              );
            }

            return (
              <code
                className="rounded bg-zinc-800 px-1.5 py-0.5 text-[0.8125rem] font-mono text-[var(--svx-color-brand-primary)]"
                {...props}
              >
                {children}
              </code>
            );
          },

          // ── Links ────────────────────────────────────────────────────
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[var(--svx-color-brand-primary)] no-underline hover:underline"
            >
              {children}
            </a>
          ),

          // ── Images ───────────────────────────────────────────────────
          // Constrained rendering: prevent layout break + arbitrary URL tracking.
          img: ({ src, alt }) => (
            <img
              src={src}
              alt={alt ?? ""}
              className="max-h-64 max-w-full rounded-lg border border-zinc-800"
              loading="lazy"
              referrerPolicy="no-referrer"
            />
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
