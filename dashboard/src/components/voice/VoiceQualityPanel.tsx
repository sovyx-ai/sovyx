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

const VERDICT_LABEL: Record<VoiceQualityVerdict, string> = {
  excellent: "Excellent",
  good: "Good",
  degraded: "Degraded",
  poor: "Poor",
  no_signal: "Warming up",
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

const VERDICT_DESCRIPTION: Record<VoiceQualityVerdict, string> = {
  excellent:
    "Studio-grade capture. Moonshine + Silero operate at calibrated rates.",
  good: "Typical office desk mic with HVAC running. <1% STT degradation.",
  degraded:
    "Loud open-plan office, fan close to mic. STT substitution rate climbs ~3-5× per dB lost. Move the mic 30 cm closer or enable in-process noise suppression.",
  poor: "Mic in a noisy environment (cafe, vehicle). STT becomes unreliable; VAD onset latency climbs. Move the mic, enable noise suppression, or relocate the speaker.",
  no_signal:
    "No SNR samples in the rolling 10-second window. Typical right after boot or during a long silence run. The panel updates automatically once speech frames flow.",
};

export function VoiceQualityPanel() {
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
          <span>Loading voice quality snapshot…</span>
        </div>
      </div>
    );
  }

  if (error && !snapshot) {
    return (
      <div className="rounded-lg border border-rose-500/30 bg-card p-6">
        <div className="flex items-center gap-2 text-rose-500">
          <AlertTriangleIcon className="h-4 w-4" />
          <span>Voice quality snapshot unavailable: {error}</span>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          The endpoint returns 503 until the engine registry is ready.
          Enable voice (or wait a few seconds) and the panel populates
          automatically on the next 5-second poll.
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
          Capture SNR distribution
        </h3>
        <span className="text-xs text-muted-foreground">T4.37</span>
      </div>
      <div className="flex items-baseline gap-3">
        {VERDICT_ICON[verdict]}
        <div>
          <div className={`text-2xl font-semibold ${VERDICT_COLOR[verdict]}`}>
            {VERDICT_LABEL[verdict]}
          </div>
          <div className="text-xs text-muted-foreground">
            {snapshot.snr_p50_db !== null ? (
              <>
                p50 = <span className="font-mono">{snapshot.snr_p50_db.toFixed(1)} dB</span>
                {" · "}
                <span>
                  {snapshot.snr_sample_count} sample
                  {snapshot.snr_sample_count === 1 ? "" : "s"} in window
                </span>
              </>
            ) : (
              <>No samples in the rolling 10-second window.</>
            )}
          </div>
        </div>
      </div>
      <p className="mt-3 text-sm text-muted-foreground">
        {VERDICT_DESCRIPTION[verdict]}
      </p>
      <div className="mt-3 flex flex-wrap gap-3 text-xs text-muted-foreground">
        <span>
          <strong>≥17 dB</strong> excellent
        </span>
        <span>
          <strong>9-17 dB</strong> good
        </span>
        <span>
          <strong>3-9 dB</strong> degraded
        </span>
        <span>
          <strong>&lt;3 dB</strong> poor
        </span>
      </div>
    </div>
  );

  const showProxy = !snapshot.dnsmos_extras_installed;
  let mosLabel = "—";
  let mosDetail = "Awaiting samples";
  if (snapshot.snr_p50_db !== null) {
    const mos = snrToMosProxy(snapshot.snr_p50_db);
    mosLabel = mos.toFixed(2);
    mosDetail = showProxy
      ? `SNR-proxy estimate (1 + SNR/7), derived from p50 = ${snapshot.snr_p50_db.toFixed(1)} dB`
      : `DNSMOS p50 = ${mosLabel}`;
  }

  const mosPanel = (
    <div className="rounded-lg border bg-card p-6">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <TrendingUpIcon className="h-4 w-4" />
          Voice quality MOS
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
        MOS scale: 1.0 (poor) — 4.5 (excellent). Skype/Zoom acceptable
        threshold is ≥ 3.5; below 3.0 is "poor" per ITU-T P.800.
      </p>
      {showProxy && (
        <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-50/50 p-3 text-xs dark:bg-amber-500/10">
          <div className="mb-1 flex items-center gap-2 font-semibold text-amber-700 dark:text-amber-400">
            <InfoIcon className="h-3.5 w-3.5" />
            SNR-proxy mode (DNSMOS extras not installed)
          </div>
          <p className="text-muted-foreground">
            Sovyx ships without the optional ``dnsmos`` extras
            because the package is large + Windows-build-flaky. Until
            you install them, the MOS reading above is an SNR-derived
            estimate, NOT a real DNSMOS DNN inference. To enable true
            DNSMOS:
          </p>
          <ol className="mt-2 list-decimal pl-5 text-muted-foreground">
            <li>
              <code className="rounded bg-muted px-1">
                pip install &quot;sovyx[dnsmos]&quot;
              </code>{" "}
              (or your package manager equivalent).
            </li>
            <li>
              Restart the daemon. The endpoint flips
              ``dnsmos_extras_installed`` to ``true`` on the next
              poll and this disclaimer disappears.
            </li>
            <li>
              The DNSMOS T4.23-T4.30 wire-up sequence (heartbeat
              p50/p95, alert thresholds 3.5 / 3.0, bucketing, drift)
              is operator-gated to keep the SNR-proxy path simple
              for users who don't care about DNN-based MOS.
            </li>
          </ol>
        </div>
      )}
      {!showProxy && (
        <div className="mt-3 flex items-center gap-2 text-xs text-emerald-700 dark:text-emerald-400">
          <PackageIcon className="h-3.5 w-3.5" />
          DNSMOS extras detected — reading is live DNN inference.
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
          Noise-floor drift baseline
        </h3>
        <span className="text-xs text-muted-foreground">T4.38</span>
      </div>
      {snapshot.noise_floor.ready ? (
        <div className="text-sm">
          <div>
            Drift ={" "}
            <span className="font-mono">
              {snapshot.noise_floor.drift_db !== null
                ? `${snapshot.noise_floor.drift_db.toFixed(2)} dB`
                : "—"}
            </span>
            <span className="ml-2 text-xs text-muted-foreground">
              (short {snapshot.noise_floor.short_avg_db?.toFixed(1)} dB vs long{" "}
              {snapshot.noise_floor.long_avg_db?.toFixed(1)} dB)
            </span>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            Drift = mean(short window, ~60 s) − mean(long window, ~5 min).
            Alert fires when sustained drift exceeds the configured
            threshold (default 10 dB) for the configured de-flap count
            (default 3 heartbeats).
          </p>
        </div>
      ) : (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2Icon className="h-4 w-4 animate-spin" />
          <span>
            Warming up — long-window baseline needs ~5 minutes of
            samples before drift is meaningful (
            {snapshot.noise_floor.long_sample_count} collected).
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
          AGC2 controller
        </h3>
        <span className="text-xs text-muted-foreground">T4.51-T4.52</span>
      </div>
      <div className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <div className="text-xs text-muted-foreground">Current gain</div>
          <div className="font-mono text-base">
            {snapshot.agc2.current_gain_db.toFixed(2)} dB
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Speech level est.</div>
          <div className="font-mono text-base">
            {snapshot.agc2.speech_level_dbfs.toFixed(2)} dBFS
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Frames processed</div>
          <div className="font-mono text-base">
            {snapshot.agc2.frames_processed.toLocaleString()}
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">VAD-silenced</div>
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
        ``frames_vad_silenced`` counts frames where the T4.52 VAD-feedback
        gate vetoed an estimator update. Non-zero only when
        ``voice_agc2_vad_feedback_enabled = True``.
      </p>
    </div>
  ) : (
    <div className="rounded-lg border bg-card p-6">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <AudioWaveformIcon className="h-4 w-4" />
          AGC2 controller
        </h3>
        <span className="text-xs text-muted-foreground">T4.51-T4.52</span>
      </div>
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <InfoIcon className="h-4 w-4" />
        <span>
          AGC2 not active — voice pipeline isn&apos;t running OR
          ``voice_agc2_enabled`` is False.
        </span>
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
