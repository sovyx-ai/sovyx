/**
 * AudioLevelMeter — 60 FPS analogue-VU render of a live meter stream.
 *
 * Visual layout (horizontal bar, left → right):
 *
 *     [-120 dBFS ─────── -30 ─── -6 ─── 0 dBFS ]
 *         floor         vad    warn   clip
 *
 *     ▓▓▓▓▓▓▓░░░░░░░░░░░░░   ← RMS (steady fill)
 *     ▓▓▓▓▓▓▓▓▓▓░░░░░░░░░    ← peak (faster marker)
 *                   │         ← hold marker (peak-hold ballistic)
 *                 ▼           ← VAD-trigger tick
 *
 * Rendering uses a single `<canvas>` driven by requestAnimationFrame so
 * we stay smooth at 60 Hz even when the WS stream runs at 30 Hz. The
 * component is deliberately pure: it only re-reads the latest `level`
 * prop on each frame — no hooks, no stores.
 *
 * Accessibility: the component also exposes a structured `role="meter"`
 * element with `aria-valuenow` + `aria-valuetext` so screen readers get
 * a machine-readable level (dB + clipping / VAD flags).
 */
import { useEffect, useRef } from "react";
import type { VoiceTestLevelFrame } from "@/types/api";

const FLOOR_DB = -120;
const CEIL_DB = 0;
const VAD_DB = -30;
const WARN_DB = -18;
const CLIP_DB = -0.3;

// Colour palette (Tailwind-compatible). No dynamic class joins — we fill
// the canvas directly so the values stay in one place.
const COLOR_FLOOR = "#18181b"; // zinc-900 background
const COLOR_GRID = "#27272a"; // zinc-800
const COLOR_GREEN = "#22c55e"; // green-500
const COLOR_YELLOW = "#eab308"; // yellow-500
const COLOR_RED = "#ef4444"; // red-500
const COLOR_HOLD = "#fafafa"; // zinc-50
const COLOR_VAD_TICK = "#22d3ee"; // cyan-400
const COLOR_CLIP_FLASH = "#fca5a5"; // red-300

export interface AudioLevelMeterProps {
  /** Latest level frame, or null if the stream hasn't started. */
  level: VoiceTestLevelFrame | null;
  /** Optional height in CSS pixels. Width stretches to the container. */
  height?: number;
  /** aria-label for accessibility. */
  label?: string;
  /** Show the VAD threshold tick mark. Defaults to true. */
  showVadMarker?: boolean;
  /** Show the clipping warning flash when `level.clipping` is true. */
  showClippingFlash?: boolean;
}

function dbToFraction(db: number): number {
  if (db <= FLOOR_DB) return 0;
  if (db >= CEIL_DB) return 1;
  return (db - FLOOR_DB) / (CEIL_DB - FLOOR_DB);
}

function colorForDb(db: number): string {
  if (db >= WARN_DB) return COLOR_RED;
  if (db >= VAD_DB) return COLOR_YELLOW;
  return COLOR_GREEN;
}

function drawGrid(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
): void {
  ctx.fillStyle = COLOR_FLOOR;
  ctx.fillRect(0, 0, width, height);

  ctx.fillStyle = COLOR_GRID;
  // Major tick marks at -60, -30, -18, -6, 0 dBFS.
  for (const db of [-60, -30, -18, -6, 0]) {
    const x = dbToFraction(db) * width;
    ctx.fillRect(x, 0, 1, height);
  }
}

function drawBar(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  db: number,
  alpha: number,
): void {
  const frac = dbToFraction(db);
  const w = Math.max(0, Math.min(width, frac * width));
  ctx.globalAlpha = alpha;
  ctx.fillStyle = colorForDb(db);
  ctx.fillRect(0, 0, w, height);
  ctx.globalAlpha = 1.0;
}

function drawHoldMarker(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  db: number,
): void {
  if (db <= FLOOR_DB) return;
  const x = dbToFraction(db) * width;
  ctx.fillStyle = COLOR_HOLD;
  ctx.fillRect(Math.max(0, x - 1), 0, 2, height);
}

function drawVadMarker(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
): void {
  const x = dbToFraction(VAD_DB) * width;
  ctx.fillStyle = COLOR_VAD_TICK;
  // Small tick at the top.
  ctx.fillRect(x - 1, 0, 2, height * 0.25);
}

function drawClippingFlash(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
): void {
  ctx.save();
  ctx.globalAlpha = 0.3;
  ctx.fillStyle = COLOR_CLIP_FLASH;
  ctx.fillRect(0, 0, width, height);
  ctx.restore();
}

export function AudioLevelMeter({
  level,
  height = 40,
  label = "Audio input level",
  showVadMarker = true,
  showClippingFlash = true,
}: AudioLevelMeterProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const latestRef = useRef<VoiceTestLevelFrame | null>(level);
  latestRef.current = level;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let rafId = 0;
    let lastFlash = 0;

    const draw = (now: number) => {
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      const targetW = Math.floor(rect.width * dpr);
      const targetH = Math.floor(rect.height * dpr);
      if (canvas.width !== targetW || canvas.height !== targetH) {
        canvas.width = targetW;
        canvas.height = targetH;
      }
      const w = canvas.width;
      const h = canvas.height;

      drawGrid(ctx, w, h);
      const frame = latestRef.current;
      if (frame) {
        drawBar(ctx, w, h, frame.rms_db, 0.9);
        drawBar(ctx, w, h, frame.peak_db, 0.5);
        drawHoldMarker(ctx, w, h, frame.hold_db);
        if (showVadMarker) drawVadMarker(ctx, w, h);
        if (showClippingFlash && frame.clipping) {
          // Flash decays over ~200 ms for a noticeable pulse.
          if (now - lastFlash > 200) lastFlash = now;
          const alpha = 1 - Math.min(1, (now - lastFlash) / 200);
          if (alpha > 0) {
            ctx.save();
            ctx.globalAlpha = 0.3 * alpha;
            ctx.fillStyle = COLOR_CLIP_FLASH;
            ctx.fillRect(0, 0, w, h);
            ctx.restore();
          }
        }
      }
      rafId = requestAnimationFrame(draw);
    };
    rafId = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(rafId);
    };
  }, [showVadMarker, showClippingFlash]);

  const ariaValue = level?.rms_db ?? FLOOR_DB;
  const ariaText = level
    ? `${level.rms_db.toFixed(1)} dBFS` +
      (level.clipping ? ", clipping" : "") +
      (level.vad_trigger ? ", voice detected" : "")
    : "no signal";

  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={FLOOR_DB}
      aria-valuemax={CEIL_DB}
      aria-valuenow={ariaValue}
      aria-valuetext={ariaText}
      style={{ width: "100%", height }}
    >
      <canvas
        ref={canvasRef}
        data-testid="audio-level-meter-canvas"
        style={{ width: "100%", height: "100%", display: "block" }}
      />
    </div>
  );
}

// Re-export internal helpers so tests can assert on the numeric mapping
// without rendering a canvas.
export const __testables = {
  dbToFraction,
  colorForDb,
  FLOOR_DB,
  CEIL_DB,
  VAD_DB,
  WARN_DB,
  CLIP_DB,
  drawClippingFlash,
};
