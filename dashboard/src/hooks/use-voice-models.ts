/**
 * use-voice-models — disk-truth state for the setup wizard's model list.
 *
 * Responsibilities:
 * 1. Fetch ``GET /api/voice/models/status`` on mount and expose the
 *    per-model ``installed`` flag + aggregate missing-size.
 * 2. Expose ``startDownload()`` which POSTs ``/api/voice/models/download``
 *    and then polls ``GET /api/voice/models/download/{task_id}`` every
 *    750 ms until terminal.
 * 3. Re-fetch disk status on terminal, so the UI flips the checkmarks
 *    from cloud → green without a page reload.
 *
 * Concurrency: if the backend already has an in-flight task it returns
 * the existing task_id — we just adopt it.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type {
  VoiceModelDownloadProgress,
  VoiceModelsStatusResponse,
} from "@/types/api";
import {
  VoiceModelDownloadProgressSchema,
  VoiceModelsStatusResponseSchema,
} from "@/types/schemas";

const POLL_INTERVAL_MS = 750;

export interface UseVoiceModelsResult {
  status: VoiceModelsStatusResponse | null;
  statusLoading: boolean;
  statusError: string | null;
  download: VoiceModelDownloadProgress | null;
  downloading: boolean;
  startDownload: () => Promise<void>;
  refresh: () => Promise<void>;
}

export function useVoiceModels(): UseVoiceModelsResult {
  const [status, setStatus] = useState<VoiceModelsStatusResponse | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [download, setDownload] = useState<VoiceModelDownloadProgress | null>(
    null,
  );
  const pollingRef = useRef(false);
  const cancelRef = useRef(false);

  const refresh = useCallback(async () => {
    setStatusLoading(true);
    setStatusError(null);
    try {
      const data = await api.get<VoiceModelsStatusResponse>(
        "/api/voice/models/status",
        { schema: VoiceModelsStatusResponseSchema },
      );
      setStatus(data);
    } catch (err) {
      setStatusError(err instanceof Error ? err.message : "Status fetch failed");
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => {
      cancelRef.current = true;
    };
  }, [refresh]);

  const poll = useCallback(
    async (taskId: string): Promise<void> => {
      if (pollingRef.current) return;
      pollingRef.current = true;
      try {
        while (!cancelRef.current) {
          try {
            const p = await api.get<VoiceModelDownloadProgress>(
              `/api/voice/models/download/${taskId}`,
              { schema: VoiceModelDownloadProgressSchema },
            );
            setDownload(p);
            if (p.status !== "running") {
              // Terminal — re-fetch disk truth so green checks flip.
              await refresh();
              return;
            }
          } catch {
            // Transient — let the next tick retry.
          }
          await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        }
      } finally {
        pollingRef.current = false;
      }
    },
    [refresh],
  );

  const startDownload = useCallback(async () => {
    try {
      const started = await api.post<VoiceModelDownloadProgress>(
        "/api/voice/models/download",
        {},
        { schema: VoiceModelDownloadProgressSchema },
      );
      setDownload(started);
      if (started.status === "running") {
        void poll(started.task_id);
      } else {
        // Already done (nothing to fetch) — bounce status so UI syncs.
        await refresh();
      }
    } catch (err) {
      setDownload({
        task_id: "",
        status: "error",
        total_models: 0,
        completed_models: 0,
        current_model: null,
        error: err instanceof Error ? err.message : "Download request failed",
      });
    }
  }, [poll, refresh]);

  const downloading = download?.status === "running";
  return {
    status,
    statusLoading,
    statusError,
    download,
    downloading,
    startDownload,
    refresh,
  };
}
