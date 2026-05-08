/**
 * use-resolved-mind-id — single source of truth for the operator's
 * active mind id on the dashboard.
 *
 * Resolves the active mind id by fetching ``/api/onboarding/state``
 * once per page lifetime and caching the result module-level so
 * every consumer shares the same fetch + warning ledger.
 *
 * Closes the structural side of CLAUDE.md anti-pattern #35
 * (the "mindId='default'" sentinel reincurrence) — every component
 * that needs the resolved mind id MUST use this hook instead of
 * duplicating the fetch+state+warn logic. The bug class had 5
 * occurrences across v0.31.0..v0.31.7 because each new component
 * with a ``mindId`` prop was at risk of hardcoding the literal
 * sentinel; funneling all consumers through one resolver removes
 * the surface area entirely.
 *
 * Companion mitigation: ``dashboard/eslint.config.js`` ships an
 * ESLint rule that blocks JSX literal ``mindId="default"`` and
 * default-param ``mindId = "default"`` outside of ``tests/``.
 *
 * Why ``useSyncExternalStore`` + module-level singleton:
 *   The mind id is a property of the active daemon, not of a
 *   component instance. Re-fetching it from every consumer's
 *   ``useEffect`` would (a) flood the backend with redundant
 *   requests on a settings/onboarding page that mounts multiple
 *   "mind-aware" components, and (b) risk inconsistency if two
 *   sibling components observed different points in time. The
 *   module-level singleton resolves once + replays the cached
 *   value to every subsequent subscriber via the standard
 *   ``useSyncExternalStore`` API (React 18+) — which avoids the
 *   ``setState-in-effect`` anti-pattern flagged by
 *   ``react-hooks/set-state-in-effect``. Test environments can
 *   reset via ``__resetResolvedMindIdCacheForTests``.
 *
 * Sentinel + warn semantics:
 *   The hook NEVER returns the literal ``"default"`` from a real
 *   resolution path — only as a final fallback when the fetch
 *   fails or the daemon yields a null mind_id. The single
 *   ``console.warn`` breadcrumb fires exactly once per page
 *   lifetime (gated by the singleton), so operators triaging a
 *   misrouted calibration profile see a single line of
 *   provenance instead of a fan-out across every consuming
 *   component.
 *
 * Sister hook:
 *   ``useOnboardingState`` (this same module) exposes the full
 *   ``OnboardingState`` payload to callers that need the OTHER
 *   fields (mind_name, provider_configured, ollama_*) and shares
 *   the same module-level fetch — only one network call total per
 *   page lifetime regardless of how many consumers mount.
 */

import { useSyncExternalStore } from "react";
import { api, isAbortError } from "@/lib/api";
import { OnboardingStateSchema } from "@/types/schemas";
import type { OnboardingState } from "@/types/api";

/** The sentinel value used as a final fallback ONLY. */
const DEFAULT_SENTINEL = "default";

/** Resolved value plus diagnostic flags shared by every subscriber. */
interface ResolvedMindIdSnapshot {
  /** Resolved mind id; ``"default"`` only as the final fallback. */
  mindId: string;
  /** ``true`` if the resolved value is the ``"default"`` fallback. */
  isFallback: boolean;
  /** ``true`` while the initial fetch is in flight. */
  isLoading: boolean;
}

/** Snapshot for the broader onboarding-state hook subscribers. */
interface OnboardingStateSnapshot {
  /** Full payload, ``null`` while loading or after a fetch failure. */
  state: OnboardingState | null;
  /** ``true`` while the initial fetch is in flight. */
  isLoading: boolean;
  /** ``true`` if the fetch failed (``state`` will be ``null``). */
  isError: boolean;
}

/** Module-level singleton state shared across every subscriber. */
interface SingletonState {
  /** Cached full onboarding payload once resolved (``null`` on failure). */
  state: OnboardingState | null;
  /** ``true`` once a fetch attempt has completed (success OR failure). */
  resolved: boolean;
  /** ``true`` if the last fetch failed. */
  errored: boolean;
  /** Pending fetch promise, dedupes concurrent first-mount fetches. */
  inFlight: Promise<void> | null;
  /** Whether the single-fire warn has already fired for this lifetime. */
  warned: boolean;
  /** Subscribers notified on state transitions. */
  subscribers: Set<() => void>;
  /** Memoised mind-id snapshot — referenced by ``useSyncExternalStore``. */
  cachedMindIdSnapshot: ResolvedMindIdSnapshot;
  /** Memoised onboarding snapshot — referenced by ``useSyncExternalStore``. */
  cachedOnboardingSnapshot: OnboardingStateSnapshot;
}

/** Loading-state snapshots served while the singleton is unresolved. */
const LOADING_MIND_ID_SNAPSHOT: ResolvedMindIdSnapshot = {
  mindId: DEFAULT_SENTINEL,
  isFallback: true,
  isLoading: true,
};

const LOADING_ONBOARDING_SNAPSHOT: OnboardingStateSnapshot = {
  state: null,
  isLoading: true,
  isError: false,
};

const state: SingletonState = {
  state: null,
  resolved: false,
  errored: false,
  inFlight: null,
  warned: false,
  subscribers: new Set(),
  cachedMindIdSnapshot: LOADING_MIND_ID_SNAPSHOT,
  cachedOnboardingSnapshot: LOADING_ONBOARDING_SNAPSHOT,
};

function computeMindIdSnapshot(): ResolvedMindIdSnapshot {
  if (!state.resolved) {
    return LOADING_MIND_ID_SNAPSHOT;
  }
  if (state.errored || state.state === null) {
    return {
      mindId: DEFAULT_SENTINEL,
      isFallback: true,
      isLoading: false,
    };
  }
  const resolved = state.state.mind_id ?? null;
  if (resolved === null || resolved === "") {
    return {
      mindId: DEFAULT_SENTINEL,
      isFallback: true,
      isLoading: false,
    };
  }
  return {
    mindId: resolved,
    isFallback: resolved === DEFAULT_SENTINEL,
    isLoading: false,
  };
}

function computeOnboardingSnapshot(): OnboardingStateSnapshot {
  if (!state.resolved) {
    return LOADING_ONBOARDING_SNAPSHOT;
  }
  return {
    state: state.state,
    isLoading: false,
    isError: state.errored,
  };
}

function refreshSnapshots(): void {
  state.cachedMindIdSnapshot = computeMindIdSnapshot();
  state.cachedOnboardingSnapshot = computeOnboardingSnapshot();
}

function notifyAll(): void {
  refreshSnapshots();
  for (const sub of state.subscribers) {
    sub();
  }
}

function warnFallbackOnce(reason: string): void {
  if (state.warned) return;
  state.warned = true;
  console.warn(
    `[useResolvedMindId] resolved mind_id is unavailable (${reason}); ` +
      `falling back to "${DEFAULT_SENTINEL}" sentinel. Components that ` +
      "thread mindId into per-mind APIs may end up writing to " +
      "<data_dir>/default/ instead of the active mind's directory. " +
      "The backend resolver is the safety net; verify " +
      "/api/onboarding/state response.",
  );
}

async function fetchOnce(signal?: AbortSignal): Promise<void> {
  // Dedupe: if a fetch is already in flight, await it instead of
  // firing a second request from the next-mounted consumer.
  if (state.inFlight) {
    await state.inFlight;
    return;
  }
  if (state.resolved) {
    // Already resolved (success or failure) — nothing to do.
    return;
  }

  const work = (async (): Promise<void> => {
    try {
      const data = await api.get<OnboardingState>("/api/onboarding/state", {
        schema: OnboardingStateSchema,
        signal,
      });
      state.state = data;
      state.errored = false;
      state.resolved = true;
      const resolved = data.mind_id ?? null;
      if (resolved === null || resolved === "") {
        warnFallbackOnce("/api/onboarding/state returned null mind_id");
      } else if (resolved === DEFAULT_SENTINEL) {
        warnFallbackOnce(
          'daemon yielded literal "default" — likely no mind loaded',
        );
      }
    } catch (err) {
      if (isAbortError(err)) {
        // Caller unmounted before the fetch resolved — leave the
        // singleton untouched so the next consumer can retry. Do
        // NOT warn (no failure observed yet).
        return;
      }
      state.state = null;
      state.errored = true;
      state.resolved = true;
      warnFallbackOnce(
        err instanceof Error
          ? `fetch failed: ${err.message}`
          : "fetch failed",
      );
    } finally {
      notifyAll();
    }
  })();
  state.inFlight = work;
  try {
    await work;
  } finally {
    state.inFlight = null;
  }
}

/**
 * Subscribe a listener to singleton state transitions. Used by the
 * ``useSyncExternalStore`` plumbing — also kicks off the dedup'd
 * fetch on first subscription if the singleton hasn't resolved yet.
 * Returns the unsubscribe handle.
 */
function subscribe(listener: () => void): () => void {
  state.subscribers.add(listener);
  if (!state.resolved && state.inFlight === null) {
    void fetchOnce();
  }
  return () => {
    state.subscribers.delete(listener);
  };
}

function getMindIdSnapshot(): ResolvedMindIdSnapshot {
  return state.cachedMindIdSnapshot;
}

function getOnboardingSnapshot(): OnboardingStateSnapshot {
  return state.cachedOnboardingSnapshot;
}

/**
 * Reset the module-level singleton. ONLY for tests — production
 * code MUST never call this. Exported with a verbose name so the
 * intent is unmistakable in code review.
 */
export function __resetResolvedMindIdCacheForTests(): void {
  state.state = null;
  state.resolved = false;
  state.errored = false;
  state.inFlight = null;
  state.warned = false;
  state.subscribers.clear();
  state.cachedMindIdSnapshot = LOADING_MIND_ID_SNAPSHOT;
  state.cachedOnboardingSnapshot = LOADING_ONBOARDING_SNAPSHOT;
}

/**
 * Pre-seed the singleton with a resolved ``OnboardingState`` so tests
 * never fire the actual ``api.get`` from the hook. ONLY for tests —
 * production code MUST never call this. Use to remove the timing
 * dependency between the page's ``mockResolvedValueOnce`` queue and
 * the hook's own fetch.
 */
export function __seedResolvedMindIdForTests(
  onboardingState: OnboardingState,
): void {
  state.state = onboardingState;
  state.resolved = true;
  state.errored = false;
  state.inFlight = null;
  refreshSnapshots();
}

/**
 * Resolve the operator's active mind id.
 *
 * Returns:
 *   * ``mindId``     — resolved id, or ``"default"`` only as final fallback.
 *   * ``isFallback`` — ``true`` when the value is the sentinel.
 *   * ``isLoading``  — ``true`` while the initial fetch is in flight.
 *
 * Multiple consumers on the same page share one fetch — the
 * module-level singleton dedupes concurrent first-mount calls and
 * replays the cached value to every subsequent subscriber.
 */
export function useResolvedMindId(): ResolvedMindIdSnapshot {
  return useSyncExternalStore(
    subscribe,
    getMindIdSnapshot,
    getMindIdSnapshot,
  );
}

/**
 * Read the full ``OnboardingState`` payload from the same singleton
 * that backs ``useResolvedMindId``. Use when a page legitimately
 * needs ``mind_name`` / ``provider_configured`` / ``ollama_*`` —
 * everything else MUST go through ``useResolvedMindId``.
 *
 * Sharing the singleton means every dashboard page (onboarding,
 * settings, …) makes at most ONE ``/api/onboarding/state`` request
 * per lifetime regardless of how many consumers mount.
 */
export function useOnboardingState(): OnboardingStateSnapshot {
  return useSyncExternalStore(
    subscribe,
    getOnboardingSnapshot,
    getOnboardingSnapshot,
  );
}
