/**
 * PricingSourceBanner — surfaces a warning when the active model uses
 * fallback pricing (provider default or global default rates instead
 * of the per-model authoritative table).
 *
 * Closes issue #45. Renders nothing for ``source === "exact"`` so the
 * common case is invisible.
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangleIcon } from "lucide-react";

import { api } from "@/lib/api";
import { PricingInfoResponseSchema } from "@/types/schemas";
import type { PricingInfoResponse } from "@/types/api";

interface PricingSourceBannerProps {
  provider: string;
  model: string;
}

export function PricingSourceBanner({
  provider,
  model,
}: PricingSourceBannerProps) {
  const { t } = useTranslation("settings");
  const [info, setInfo] = useState<PricingInfoResponse | null>(null);

  useEffect(() => {
    if (!model) {
      setInfo(null);
      return;
    }
    const controller = new AbortController();
    const params = new URLSearchParams();
    params.set("model", model);
    if (provider) params.set("provider", provider);

    void api
      .get<PricingInfoResponse>(
        `/api/providers/pricing-info?${params.toString()}`,
        { signal: controller.signal, schema: PricingInfoResponseSchema },
      )
      .then((res) => {
        setInfo(res);
      })
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        // Pricing info is best-effort UX; failures don't affect functionality.
        setInfo(null);
      });

    return () => controller.abort();
  }, [provider, model]);

  if (!info) return null;
  if (info.source !== "provider_default" && info.source !== "global_default") {
    // ``exact`` is the common case (no banner). Anything else (including
    // an unknown future enum value or a mock that returns the wrong shape
    // in tests) is also silent — the banner only renders for the two
    // known fallback values.
    return null;
  }
  if (
    typeof info.input_per_1m_usd !== "number" ||
    typeof info.output_per_1m_usd !== "number"
  ) {
    return null;
  }

  const headlineKey =
    info.source === "provider_default"
      ? "pricing.fallbackProviderDefault"
      : "pricing.fallbackGlobalDefault";
  const headlineFallback =
    info.source === "provider_default"
      ? "Cost estimates use provider-default rates for this model."
      : "Cost estimates use a global-default rate for this model.";

  return (
    <div
      className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3"
      data-testid="pricing-source-banner"
      data-source={info.source}
    >
      <div className="flex items-start gap-2">
        <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-amber-500" />
        <div className="space-y-1 text-xs">
          <p className="font-medium text-foreground">
            {t(headlineKey, headlineFallback)}
          </p>
          <p className="text-muted-foreground">
            {t(
              "pricing.fallbackBody",
              "Reports show ${{input}} / ${{output}} per 1M tokens — these are estimates, not authoritative rates from the provider. Add the model to the pricing table for exact tracking.",
              {
                input: info.input_per_1m_usd.toFixed(2),
                output: info.output_per_1m_usd.toFixed(2),
              },
            )}
          </p>
        </div>
      </div>
    </div>
  );
}
