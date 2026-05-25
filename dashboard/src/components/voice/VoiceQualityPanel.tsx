/**
 * VoiceQualityPanel — Phase 4 / T4.26 + T4.37 dashboard panels.
 *
 * Two complementary widgets in one component (cheaper to share the
 * single ``/api/voice/quality-snapshot`` poll between them):
 *
 * 1. **Capture SNR distribution** (T4.37) — current SNR p50 +
 *    verdict bucket (Excellent / Good / Degraded / Poor) per the
 *    bands published in ``docs/audio-quality.md``. Surfaces the
 *    sample count so operators see the reading is real (not
 *    silence-only "no signal" empty buffer).
 *
 * 2. **Voice quality MOS p50/p95 trending** (T4.26) — when
 *    ``dnsmos_extras_installed`` is False (Sovyx default), the
 *    panel labels the displayed MOS as a SNR-PROXY estimate
 *    derived from the SNR p50 via the standard mapping
 *    ``MOS ≈ 1 + (SNR / 7)`` (rough Skype/Zoom calibration over
 *    0-21 dB SNR range). The panel makes the proxy nature
 *    explicit + links the operator to the install path. When the
 *    extras land, the panel switches to the true DNSMOS reading
 *    (no UI change required — the disclaimer just disappears).
 *
 * The panel polls every 5 s. The single-source-of-truth
 * aggregator backing the endpoint is the same one the orchestrator
 * heartbeat consumes, so the dashboard reads exactly what the
 * heartbeat logs report.
 */

import { useCallback, useEffect, useState, type ReactElement } from "react";
import { Trans, useTranslation } from "react-i18next";
import {
  AlertTriangleIcon,
  ActivityIcon,
  AudioWaveformIcon,
  CheckCircle2Icon,
  ClockIcon,
  InfoIcon,
  Loader2Icon,
  PackageIcon,
  TrendingUpIcon,
} from "lucide-react";
import { api, isAbortError } from "@/lib/api";
import { VoiceQualitySnapshotResponseSchema } from "@/types/schemas";
import type {
  VoiceQualitySnapshotResponse,
  VoiceQualityVerdict,
} from "@/types/api";

const POLL_INTERVAL_MS = 5_000;

/** Map SNR p50 → MOS proxy. Used only when DNSMOS extras absent. */
function snrToMosProxy(snrDb: number): number {
  // Linear ramp 0 dB → 1.0 (poor), 21 dB → 4.0 (excellent).
  const raw = 1.0 + snrDb / 7.0;
  return Math.max(1.0, Math.min(4.5, raw));
}

// v0.32.5 Phase 4.B.2 — verdict-keyed maps now resolve i18n keys
// (translated at render time via ``t()``) instead of literal English
// strings. Keys mirror the backend's ``VoiceQualityVerdict`` enum so a
// new verdict added server-side surfaces here as a missing-key warning
// in i18n debug mode rather than silently rendering nothing.
const VERDICT_LABEL_KEY: Record<VoiceQualityVerdict, string> = {
  excellent: "qualityPanel.snr.verdict.excellent",
  good: "qualityPanel.snr.verdict.good",
  degraded: "qualityPanel.snr.verdict.degraded",
  poor: "qualityPanel.snr.verdict.poor",
  no_signal: "qualityPanel.snr.verdict.noSignal",
};

const VERDICT_COLOR: Record<VoiceQualityVerdict, string> = {
  excellent: "text-emerald-500",
  good: "text-sky-500",
  degraded: "text-amber-500",
  poor: "text-rose-500",
  no_signal: "text-muted-foreground",
};

const VERDICT_ICON: Record<VoiceQualityVerdict, ReactElement> = {
  excellent: <CheckCircle2Icon className="h-5 w-5 text-emerald-500" />,
  good: <CheckCircle2Icon className="h-5 w-5 text-sky-500" />,
  degraded: <AlertTriangleIcon className="h-5 w-5 text-amber-500" />,
  poor: <AlertTriangleIcon className="h-5 w-5 text-rose-500" />,
  no_signal: <ClockIcon className="h-5 w-5 text-muted-foreground" />,
};

const VERDICT_DESCRIPTION_KEY: Record<VoiceQualityVerdict, string> = {
  excellent: "qualityPanel.snr.verdictDescription.excellent",
  good: "qualityPanel.snr.verdictDescription.good",
  degraded: "qualityPanel.snr.verdictDescription.degraded",
  poor: "qualityPanel.snr.verdictDescription.poor",
  no_signal: "qualityPanel.snr.verdictDescription.noSignal",
};

export function VoiceQualityPanel() {
  const { t } = useTranslation("voice");
  const [snapshot, setSnapshot] = useState<VoiceQualitySnapshotResponse | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await api.get<VoiceQualitySnapshotResponse>(
        "/api/voice/quality-snapshot",
        { signal, schema: VoiceQualitySnapshotResponseSchema },
      );
      setSnapshot(data);
      setError(null);
    } catch (err) {
      if (isAbortError(err)) return;
      // ``Snapshot fetch failed`` was the legacy fallback string —
      // post-v0.32.5 the wrapping ``errorPrefix`` template carries the
      // localised "Voice quality snapshot unavailable: {error}" prose
      // and ``error`` here remains the raw underlying message (often a
      // status-code string from ``api.get``) so operators see the
      // technical signal even if i18n misses the wrapper.
      setError(err instanceof Error ? err.message : "Snapshot fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const handle = window.setInterval(() => {
      void load();
    }, POLL_INTERVAL_MS);
    return () => {
      ctrl.abort();
      window.clearInterval(handle);
    };
  }, [load]);

  if (loading && !snapshot) {
    return (
      <div className="rounded-lg border bg-card p-6">
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2Icon className="h-4 w-4 animate-spin" />
          <span>{t("qualityPanel.loading")}</span>
        </div>
      </div>
    );
  }

  if (error && !snapshot) {
    return (
      <div className="rounded-lg border border-rose-500/30 bg-card p-6">
        <div className="flex items-center gap-2 text-rose-500">
          <AlertTriangleIcon className="h-4 w-4" />
          <span>{t("qualityPanel.errorPrefix", { error })}</span>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          {t("qualityPanel.errorBody")}
        </p>
      </div>
    );
  }

  if (!snapshot) {
    return null;
  }

  const verdict = snapshot.snr_verdict;
  const snrPanel = (
    <div className="rounded-lg border bg-card p-6">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <AudioWaveformIcon className="h-4 w-4" />
          {t("qualityPanel.snr.title")}
        </h3>
        <span className="text-xs text-muted-foreground">T4.37</span>
      </div>
      <div className="flex items-baseline gap-3">
        {VERDICT_ICON[verdict]}
        <div>
          <div className={`text-2xl font-semibold ${VERDICT_COLOR[verdict]}`}>
            {t(VERDICT_LABEL_KEY[verdict])}
          </div>
          <div className="text-xs text-muted-foreground">
            {snapshot.snr_p50_db !== null ? (
              <Trans
                i18nKey="qualityPanel.snr.p50Label"
                ns="voice"
                values={{
                  value: snapshot.snr_p50_db.toFixed(1),
                  count: `${snapshot.snr_sample_count} ${
                    snapshot.snr_sample_count === 1
                      ? t("qualityPanel.snr.sampleSingular")
                      : t("qualityPanel.snr.samplePlural")
                  }`,
                }}
                components={[<span className="font-mono" key="p50" />]}
              />
            ) : (
              <>{t("qualityPanel.snr.noSamples")}</>
            )}
          </div>
        </div>
      </div>
      <p className="mt-3 text-sm text-muted-foreground">
        {t(VERDICT_DESCRIPTION_KEY[verdict])}
      </p>
      <div className="mt-3 flex flex-wrap gap-3 text-xs text-muted-foreground">
        <span>
          <Trans
            i18nKey="qualityPanel.snr.thresholds.excellent"
            ns="voice"
            components={[<strong key="t" />]}
          />
        </span>
        <span>
          <Trans
            i18nKey="qualityPanel.snr.thresholds.good"
            ns="voice"
            components={[<strong key="t" />]}
          />
        </span>
        <span>
          <Trans
            i18nKey="qualityPanel.snr.thresholds.degraded"
            ns="voice"
            components={[<strong key="t" />]}
          />
        </span>
        <span>
          <Trans
            i18nKey="qualityPanel.snr.thresholds.poor"
            ns="voice"
            components={[<strong key="t" />]}
          />
        </span>
      </div>
    </div>
  );

  // LIVE-2 DNSMOS wire-up — the panel may only claim a live DNSMOS reading
  // when the backend actually produced one (quality_mode === "dnsmos_live"
  // with a real overall MOS). Otherwise the MOS shown is the SNR-derived
  // proxy, regardless of whether the extras are merely installed — this is
  // what stops the "live DNN inference" badge from lying over a proxy.
  const isLive =
    snapshot.quality_mode === "dnsmos_live" && snapshot.dnsmos_ovrl_mos != null;
  const showProxy = !isLive;
  let mosLabel = "—";
  let mosDetail = t("qualityPanel.mos.awaitingSamples");
  if (isLive) {
    mosLabel = (snapshot.dnsmos_ovrl_mos as number).toFixed(2);
    mosDetail = t("qualityPanel.mos.dnsmosDetail", { mos: mosLabel });
  } else if (snapshot.snr_p50_db !== null) {
    mosLabel = snrToMosProxy(snapshot.snr_p50_db).toFixed(2);
    mosDetail = t("qualityPanel.mos.snrProxyDetail", {
      snr: snapshot.snr_p50_db.toFixed(1),
    });
  }

  const mosPanel = (
    <div className="rounded-lg border bg-card p-6">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <TrendingUpIcon className="h-4 w-4" />
          {t("qualityPanel.mos.title")}
        </h3>
        <span className="text-xs text-muted-foreground">T4.26</span>
      </div>
      <div className="flex items-baseline gap-3">
        <ActivityIcon className="h-5 w-5 text-sky-500" />
        <div>
          <div className="text-2xl font-semibold">{mosLabel}</div>
          <div className="text-xs text-muted-foreground">{mosDetail}</div>
        </div>
      </div>
      <p className="mt-3 text-sm text-muted-foreground">
        {t("qualityPanel.mos.scaleHint")}
      </p>
      {/* DNSMOS extras not installed → SNR-proxy + install instructions. */}
      {showProxy && snapshot.quality_mode === "dnsmos_unavailable" && (
        <div
          className="mt-3 rounded-md border border-amber-500/30 bg-amber-50/50 p-3 text-xs dark:bg-amber-500/10"
          data-testid="dnsmos-proxy-disclaimer"
        >
          <div className="mb-1 flex items-center gap-2 font-semibold text-amber-700 dark:text-amber-400">
            <InfoIcon className="h-3.5 w-3.5" />
            {t("qualityPanel.mos.proxyDisclaimerTitle")}
          </div>
          <p className="text-muted-foreground">
            {t("qualityPanel.mos.proxyDisclaimerBody")}
          </p>
          <ol className="mt-2 list-decimal pl-5 text-muted-foreground">
            <li>
              <Trans
                i18nKey="qualityPanel.mos.proxyDisclaimerStep1"
                ns="voice"
                components={[
                  <code className="rounded bg-muted px-1" key="cmd" />,
                ]}
              />
            </li>
            <li>{t("qualityPanel.mos.proxyDisclaimerStep2")}</li>
            <li>{t("qualityPanel.mos.proxyDisclaimerStep3")}</li>
          </ol>
        </div>
      )}
      {/* LIVE-2 DNSMOS wire-up — installed but NOT producing live scores.
          Truthfully says the reading is still the SNR proxy; never claims
          live inference. */}
      {showProxy && snapshot.quality_mode === "dnsmos_inactive" && (
        <div
          className="mt-3 rounded-md border border-amber-500/30 bg-amber-50/50 p-3 text-xs dark:bg-amber-500/10"
          data-testid="dnsmos-inactive-note"
        >
          <div className="mb-1 flex items-center gap-2 font-semibold text-amber-700 dark:text-amber-400">
            <InfoIcon className="h-3.5 w-3.5" />
            {t("qualityPanel.mos.dnsmosInactiveTitle")}
          </div>
          <p className="text-muted-foreground">
            {t("qualityPanel.mos.dnsmosInactiveBody")}
          </p>
        </div>
      )}
      {/* Live DNSMOS DNN inference — rendered ONLY when the backend
          actually produced a DNSMOS overall MOS (never over an SNR proxy). */}
      {isLive && (
        <div
          className="mt-3 flex items-center gap-2 text-xs text-emerald-700 dark:text-emerald-400"
          data-testid="dnsmos-live-badge"
        >
          <PackageIcon className="h-3.5 w-3.5" />
          {t("qualityPanel.mos.dnsmosLiveBadge")}
        </div>
      )}
    </div>
  );

  // Optional companion: noise-floor drift readiness + AGC2 stats.
  const driftPanel = (
    <div className="rounded-lg border bg-card p-6">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <ActivityIcon className="h-4 w-4" />
          {t("qualityPanel.drift.title")}
        </h3>
        <span className="text-xs text-muted-foreground">T4.38</span>
      </div>
      {snapshot.noise_floor.ready ? (
        <div className="text-sm">
          <div>
            <Trans
              i18nKey="qualityPanel.drift.driftLine"
              ns="voice"
              values={{
                value:
                  snapshot.noise_floor.drift_db !== null
                    ? `${snapshot.noise_floor.drift_db.toFixed(2)} dB`
                    : t("qualityPanel.drift.driftMissing"),
                shortDb: snapshot.noise_floor.short_avg_db?.toFixed(1) ?? "—",
                longDb: snapshot.noise_floor.long_avg_db?.toFixed(1) ?? "—",
              }}
              components={[
                <span className="font-mono" key="drift" />,
                <span
                  className="ml-2 text-xs text-muted-foreground"
                  key="windows"
                />,
              ]}
            />
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            {t("qualityPanel.drift.driftFormula")}
          </p>
        </div>
      ) : (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2Icon className="h-4 w-4 animate-spin" />
          <span>
            {t("qualityPanel.drift.warmingUp", {
              count: snapshot.noise_floor.long_sample_count,
            })}
          </span>
        </div>
      )}
    </div>
  );

  const agc2Panel = snapshot.agc2 ? (
    <div className="rounded-lg border bg-card p-6">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <AudioWaveformIcon className="h-4 w-4" />
          {t("qualityPanel.agc2.title")}
        </h3>
        <span className="text-xs text-muted-foreground">T4.51-T4.52</span>
      </div>
      <div className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <div className="text-xs text-muted-foreground">
            {t("qualityPanel.agc2.currentGain")}
          </div>
          <div className="font-mono text-base">
            {snapshot.agc2.current_gain_db.toFixed(2)} dB
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">
            {t("qualityPanel.agc2.speechLevelEst")}
          </div>
          <div className="font-mono text-base">
            {snapshot.agc2.speech_level_dbfs.toFixed(2)} dBFS
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">
            {t("qualityPanel.agc2.framesProcessed")}
          </div>
          <div className="font-mono text-base">
            {snapshot.agc2.frames_processed.toLocaleString()}
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">
            {t("qualityPanel.agc2.vadSilenced")}
          </div>
          <div className="font-mono text-base">
            {snapshot.agc2.frames_vad_silenced.toLocaleString()}
            {snapshot.agc2.frames_processed > 0 && (
              <span className="ml-2 text-xs text-muted-foreground">
                (
                {(
                  (snapshot.agc2.frames_vad_silenced * 100) /
                  snapshot.agc2.frames_processed
                ).toFixed(1)}
                %)
              </span>
            )}
          </div>
        </div>
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        {t("qualityPanel.agc2.vadSilencedNote")}
      </p>
    </div>
  ) : (
    <div className="rounded-lg border bg-card p-6">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <AudioWaveformIcon className="h-4 w-4" />
          {t("qualityPanel.agc2.title")}
        </h3>
        <span className="text-xs text-muted-foreground">T4.51-T4.52</span>
      </div>
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <InfoIcon className="h-4 w-4" />
        <span>{t("qualityPanel.agc2.notActive")}</span>
      </div>
    </div>
  );

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      {snrPanel}
      {mosPanel}
      {driftPanel}
      {agc2Panel}
    </div>
  );
}
