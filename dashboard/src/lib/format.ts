/**
 * Formatting utilities for the dashboard.
 *
 * ZERO-01: Single source of truth for all formatters.
 * All time/number formatting lives here — no local copies in components.
 */

/** Format seconds into human-readable duration (e.g. "2d 14h", "3h 25m", "45s") */
export function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;

  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const mins = Math.floor((seconds % 3600) / 60);

  if (days > 0) return `${days}d ${hours}h`;
  return `${hours}h ${mins}m`;
}

/** Format cost with appropriate precision ($0.0042, $1.23, $12.50) */
export function formatCost(amount: number): string {
  if (amount === 0) return "$0.00";
  if (amount < 0.01) return `$${amount.toFixed(4)}`;
  if (amount < 1) return `$${amount.toFixed(3)}`;
  return `$${amount.toFixed(2)}`;
}

/** Format large numbers with locale separators (1,234,567) */
export function formatNumber(n: number): string {
  return n.toLocaleString("en-US");
}

/** Format relative time from ISO string */
export function formatTimeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const secs = Math.floor(diff / 1000);

  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

/**
 * Format ISO timestamp to HH:mm:ss (24h).
 * Used by: activity feed, log rows — anywhere precise seconds matter.
 */
export function formatTimePrecise(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return "—";
  }
}

/**
 * Format ISO timestamp to HH:mm (24h).
 * Used by: chat bubbles — compact time without seconds.
 */
export function formatTimeShort(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return "—";
  }
}

/**
 * Format unix millisecond timestamp to HH:mm (24h).
 * Used by: recharts axis/tooltip — receives numeric timestamps.
 */
export function formatChartTime(ts: number): string {
  return new Date(ts).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
