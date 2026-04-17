import { memo } from "react";
import { formatCost } from "@/lib/format";

interface MessageMetaProps {
  model?: string;
  tokensIn?: number;
  tokensOut?: number;
  costUsd?: number;
  latencyMs?: number;
  provider?: string;
}

function MessageMetaImpl({
  model,
  tokensIn,
  tokensOut,
  costUsd,
  latencyMs,
  provider,
}: MessageMetaProps) {
  const totalTokens = (tokensIn ?? 0) + (tokensOut ?? 0);
  if (!totalTokens && !model) return null;

  const parts: string[] = [];
  if (totalTokens > 0) parts.push(`${totalTokens.toLocaleString()} tokens`);

  if (costUsd != null && costUsd > 0) {
    parts.push(formatCost(costUsd));
  } else if (provider === "ollama") {
    parts.push("local");
  }

  if (latencyMs != null && latencyMs > 0) {
    parts.push(`${(latencyMs / 1000).toFixed(1)}s`);
  }

  if (model) parts.push(model);

  return (
    <span className="text-[10px] text-[var(--svx-color-text-disabled)]">
      {parts.join(" \u00b7 ")}
    </span>
  );
}

export const MessageMeta = memo(MessageMetaImpl);
