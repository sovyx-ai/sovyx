/**
 * EmptyStateAnimations — Branded CSS-only animations for empty states.
 *
 * Each variant tells a visual story matching its page context.
 * Pure CSS animations — zero JS overhead, <1% CPU.
 *
 * Ref: REFINE-06
 */

import { cn } from "@/lib/utils";

/** Brain empty: Mini neural mesh with pulsing nodes and connections */
export function BrainEmptyAnimation({ className }: { className?: string }) {
  return (
    <div
      className={cn("empty-anim-brain", className)}
      aria-hidden="true"
    >
      <svg viewBox="0 0 120 80" className="empty-anim-brain__svg">
        {/* Connection lines */}
        <line x1="30" y1="25" x2="70" y2="20" className="empty-anim-brain__line empty-anim-brain__line--1" />
        <line x1="70" y1="20" x2="90" y2="50" className="empty-anim-brain__line empty-anim-brain__line--2" />
        <line x1="30" y1="25" x2="50" y2="55" className="empty-anim-brain__line empty-anim-brain__line--3" />
        <line x1="50" y1="55" x2="90" y2="50" className="empty-anim-brain__line empty-anim-brain__line--4" />
        <line x1="70" y1="20" x2="50" y2="55" className="empty-anim-brain__line empty-anim-brain__line--5" />
        {/* Nodes */}
        <circle cx="30" cy="25" r="5" className="empty-anim-brain__node empty-anim-brain__node--1" />
        <circle cx="70" cy="20" r="6" className="empty-anim-brain__node empty-anim-brain__node--2" />
        <circle cx="90" cy="50" r="4.5" className="empty-anim-brain__node empty-anim-brain__node--3" />
        <circle cx="50" cy="55" r="5.5" className="empty-anim-brain__node empty-anim-brain__node--4" />
      </svg>
    </div>
  );
}

/** Conversations empty: Chat bubble animation */
export function ConversationsEmptyAnimation({ className }: { className?: string }) {
  return (
    <div
      className={cn("empty-anim-chat", className)}
      aria-hidden="true"
    >
      <div className="empty-anim-chat__bubble empty-anim-chat__bubble--1">
        <span className="empty-anim-chat__dot" />
        <span className="empty-anim-chat__dot" />
        <span className="empty-anim-chat__dot" />
      </div>
      <div className="empty-anim-chat__bubble empty-anim-chat__bubble--2">
        <span className="empty-anim-chat__dot" />
        <span className="empty-anim-chat__dot" />
        <span className="empty-anim-chat__dot" />
      </div>
    </div>
  );
}

/** Logs empty: Terminal cursor blinking */
export function LogsEmptyAnimation({ className }: { className?: string }) {
  return (
    <div
      className={cn("empty-anim-terminal", className)}
      aria-hidden="true"
    >
      <div className="empty-anim-terminal__line">
        <span className="empty-anim-terminal__prompt">$</span>
        <span className="empty-anim-terminal__cursor" />
      </div>
    </div>
  );
}

/** Chart empty: Flat-line pulse that "wakes up" */
export function ChartEmptyAnimation({ className }: { className?: string }) {
  return (
    <div
      className={cn("empty-anim-pulse", className)}
      aria-hidden="true"
    >
      <svg viewBox="0 0 200 60" className="empty-anim-pulse__svg" preserveAspectRatio="none">
        <polyline
          className="empty-anim-pulse__line"
          points="0,30 40,30 55,30 65,10 75,50 85,25 95,35 105,30 200,30"
          fill="none"
        />
        <line x1="0" y1="30" x2="200" y2="30" className="empty-anim-pulse__baseline" />
      </svg>
    </div>
  );
}
