/**
 * Live audio-level WebSocket hook for the voice setup wizard.
 *
 * Connects to `/api/voice/test/input?token=...&device_id=...` and exposes
 * the decoded meter frames + connection state to React components. The
 * hook is strictly client-driven:
 *
 * * Caller decides when to connect (by providing a non-null `deviceId`
 *   + truthy `enabled`). Flipping either to null / false tears the
 *   stream down.
 * * No auto-reconnect on a 4xxx close — those are terminal (unauth,
 *   disabled, rate-limited, pipeline-active). We surface the close
 *   reason so the UI can render an actionable error.
 * * 1006 / network drop → single-pass exponential backoff up to 5 s,
 *   then give up and hand control back to the caller. The setup
 *   wizard does not need aggressive reconnects the way `useWebSocket`
 *   does.
 *
 * Every frame is runtime-validated against the protocol schema so a
 * drifted backend surfaces as a visible error rather than silent mis-render.
 *
 * Ref: docs/modules/voice-device-test.md
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type {
  VoiceTestCloseReason,
  VoiceTestErrorCode,
  VoiceTestFrame,
  VoiceTestLevelFrame,
  VoiceTestReadyFrame,
} from "@/types/api";
import { VoiceTestFrameSchema } from "@/types/schemas";

const DEFAULT_BASE_URL =
  import.meta.env.VITE_WS_URL ??
  `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;

const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 5_000;
const MAX_RECONNECT_ATTEMPTS = 3;

/** Connection state surfaced to the UI. */
export type AudioLevelStreamState =
  | "idle"
  | "connecting"
  | "ready"
  | "streaming"
  | "error"
  | "closed";

/** One snapshot of the stream exposed to the UI. */
export interface AudioLevelStream {
  /** The current state machine position. */
  state: AudioLevelStreamState;
  /** Most recent `LevelFrame` received — null until the first frame. */
  level: VoiceTestLevelFrame | null;
  /** Server-emitted `ReadyFrame` carrying device info. */
  ready: VoiceTestReadyFrame | null;
  /** Last error code (machine-readable) — set when `state === 'error'`. */
  errorCode: VoiceTestErrorCode | null;
  /** Last error detail (best-effort English). */
  errorDetail: string | null;
  /** Close reason when the server closed the stream cleanly. */
  closeReason: VoiceTestCloseReason | null;
  /** Force a reconnect — only useful after a terminal close. */
  reconnect: () => void;
}

/** Options accepted by :func:`useAudioLevelStream`. */
export interface UseAudioLevelStreamOptions {
  /** PortAudio input index. `null` → system default. */
  deviceId: number | null;
  /** Gate to pause / resume without unmounting. */
  enabled?: boolean;
  /** Target sample rate. Backend clamps to [8_000, 48_000]. */
  sampleRate?: number;
  /** Override the WS base URL (for tests). Defaults to page origin. */
  baseUrl?: string;
  /** Override the auth-token source (for tests). */
  getToken?: () => string;
}

function readToken(): string {
  try {
    return window.sessionStorage?.getItem("sovyx_token") ?? "";
  } catch {
    return "";
  }
}

function codeFromCloseReason(reason: string): VoiceTestErrorCode | null {
  // Best-effort map from close.reason to our error taxonomy. The
  // server sets these explicitly on 4xxx close codes.
  if (
    reason === "disabled" ||
    reason === "rate_limited" ||
    reason === "unauthorized" ||
    reason === "pipeline_active"
  ) {
    // Reuse the enum where values coincide.
    return reason as VoiceTestErrorCode;
  }
  return null;
}

function isTerminalCloseCode(code: number): boolean {
  // 4xxx app codes are terminal — no reconnect.
  return code >= 4000 && code < 5000;
}

/**
 * Live RMS / peak / hold meter stream for the setup wizard.
 *
 * Usage::
 *
 *     const stream = useAudioLevelStream({
 *       deviceId: 2,
 *       enabled: isModalOpen,
 *     });
 *     if (stream.state === "streaming") {
 *       drawMeter(stream.level);
 *     }
 */
export function useAudioLevelStream(
  opts: UseAudioLevelStreamOptions,
): AudioLevelStream {
  const {
    deviceId,
    enabled = true,
    sampleRate = 16_000,
    baseUrl = DEFAULT_BASE_URL,
    getToken = readToken,
  } = opts;

  const [state, setState] = useState<AudioLevelStreamState>("idle");
  const [level, setLevel] = useState<VoiceTestLevelFrame | null>(null);
  const [ready, setReady] = useState<VoiceTestReadyFrame | null>(null);
  const [errorCode, setErrorCode] = useState<VoiceTestErrorCode | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [closeReason, setCloseReason] = useState<VoiceTestCloseReason | null>(
    null,
  );

  const wsRef = useRef<WebSocket | null>(null);
  const attemptsRef = useRef(0);
  const backoffRef = useRef(INITIAL_BACKOFF_MS);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const manualReconnectRef = useRef(0);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const teardown = useCallback(() => {
    clearReconnectTimer();
    const ws = wsRef.current;
    wsRef.current = null;
    if (ws && ws.readyState !== WebSocket.CLOSED) {
      try {
        ws.close();
      } catch {
        // Ignore — already closing.
      }
    }
  }, [clearReconnectTimer]);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    if (!enabled) {
      setState("idle");
      return;
    }

    setState("connecting");
    setErrorCode(null);
    setErrorDetail(null);
    setCloseReason(null);

    const token = getToken();
    const params = new URLSearchParams();
    params.set("token", token);
    if (deviceId !== null) params.set("device_id", String(deviceId));
    params.set("sample_rate", String(sampleRate));

    let ws: WebSocket;
    try {
      ws = new WebSocket(`${baseUrl}/api/voice/test/input?${params.toString()}`);
    } catch (err) {
      setState("error");
      setErrorCode("internal_error");
      setErrorDetail(err instanceof Error ? err.message : String(err));
      return;
    }
    wsRef.current = ws;

    ws.onmessage = (raw) => {
      let payload: unknown;
      try {
        payload = JSON.parse(raw.data as string);
      } catch {
        return;
      }
      const parsed = VoiceTestFrameSchema.safeParse(payload);
      if (!parsed.success) {
        // Drifted payload — log once, swallow.
        console.warn("voice-test frame failed schema", parsed.error);
        return;
      }
      const frame = parsed.data as VoiceTestFrame;
      switch (frame.t) {
        case "ready":
          setReady(frame);
          setState("ready");
          break;
        case "level":
          setLevel(frame);
          setState("streaming");
          break;
        case "error":
          setErrorCode(frame.code);
          setErrorDetail(frame.detail);
          setState("error");
          break;
        case "closed":
          setCloseReason(frame.reason);
          setState("closed");
          break;
      }
    };

    ws.onerror = () => {
      // Transport error — onclose will fire next.
    };

    ws.onclose = (ev) => {
      if (!mountedRef.current) return;
      const terminal = isTerminalCloseCode(ev.code);
      if (terminal) {
        const mapped = codeFromCloseReason(ev.reason);
        if (mapped) setErrorCode(mapped);
        // Always surface the server's close.reason when we have one —
        // the MicTestPanel prefers ``errorDetail`` and the default
        // "Mic test failed" copy hides anything the backend actually
        // said (e.g. "device 7 not found", "output_device busy").
        if (ev.reason) setErrorDetail(ev.reason);
        setState("error");
        return;
      }
      // Transient — retry with backoff, if we have budget left.
      if (attemptsRef.current >= MAX_RECONNECT_ATTEMPTS) {
        setState("closed");
        return;
      }
      attemptsRef.current += 1;
      const delay = backoffRef.current;
      backoffRef.current = Math.min(delay * 2, MAX_BACKOFF_MS);
      reconnectTimerRef.current = setTimeout(() => {
        connect();
      }, delay);
    };
  }, [baseUrl, deviceId, enabled, getToken, sampleRate]);

  const reconnect = useCallback(() => {
    attemptsRef.current = 0;
    backoffRef.current = INITIAL_BACKOFF_MS;
    teardown();
    // Trigger a re-run of the effect by flipping the manual counter.
    manualReconnectRef.current += 1;
    connect();
  }, [connect, teardown]);

  useEffect(() => {
    mountedRef.current = true;
    attemptsRef.current = 0;
    backoffRef.current = INITIAL_BACKOFF_MS;
    if (enabled && deviceId !== undefined) {
      connect();
    } else {
      teardown();
      setState("idle");
    }
    return () => {
      mountedRef.current = false;
      teardown();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, deviceId, sampleRate]);

  return {
    state,
    level,
    ready,
    errorCode,
    errorDetail,
    closeReason,
    reconnect,
  };
}
