/**
 * MessageTags — inline row of module/plugin tags shown above every
 * assistant message in the chat thread.
 *
 * The backend guarantees at least one tag (`"brain"`) on every response
 * and prepends plugin names when tools participated in the ReAct loop.
 * This component surfaces those tags so the user can always trace which
 * modules produced the reply.
 *
 * Label + colour are looked up in a central map; unknown tag names
 * fall through to a neutral pill with the raw name so a newly-installed
 * plugin never breaks the UI. i18n labels live under the `chat.tags.*`
 * namespace with `defaultValue` pointing at the raw tag name.
 *
 * Tailwind JIT note: the class strings for each tag colour are listed
 * as literal entries below so the compiler can statically detect them.
 * Interpolating a token name into a class (``text-[var(--svx-color-${x})]``)
 * would silently produce unstyled pills because Tailwind can't see
 * those class names at build time.
 */

import { memo } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

/**
 * Static mapping from tag name to its pill class string. Each entry
 * colours both the text and a 10 %-alpha background using the same
 * ``--svx-color-*`` token, matching the pill style from plugin-card's
 * footer badges.
 */
const TAG_CLASSES: Record<string, string> = {
  brain:
    "text-[var(--svx-color-brand-primary)] bg-[var(--svx-color-brand-primary)]/10",
  financial_math:
    "text-[var(--svx-color-success)] bg-[var(--svx-color-success)]/10",
  weather: "text-[var(--svx-color-info)] bg-[var(--svx-color-info)]/10",
  knowledge:
    "text-[var(--svx-color-warning)] bg-[var(--svx-color-warning)]/10",
  web_intelligence:
    "text-[var(--svx-color-accent-cyan)] bg-[var(--svx-color-accent-cyan)]/10",
};

const TAG_FALLBACK_CLASS =
  "bg-[var(--svx-color-bg-elevated)] text-[var(--svx-color-text-secondary)]";

interface MessageTagsProps {
  tags: string[];
}

function MessageTagsImpl({ tags }: MessageTagsProps) {
  const { t } = useTranslation("chat");
  if (tags.length === 0) return null;

  return (
    <div
      className="flex flex-wrap items-center gap-1 pb-1"
      aria-label={t("tags.aria")}
    >
      {tags.map((tag) => {
        const label = t(`tags.${tag}`, { defaultValue: tag });
        const tagClass = TAG_CLASSES[tag] ?? TAG_FALLBACK_CLASS;
        return (
          <span
            key={tag}
            data-tag={tag}
            className={cn(
              "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium",
              tagClass,
            )}
          >
            {label}
          </span>
        );
      })}
    </div>
  );
}

export const MessageTags = memo(MessageTagsImpl);
