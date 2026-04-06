/**
 * NeuralMesh — Atmospheric animated background.
 *
 * ⚠️ INTENTIONAL RGBA VALUES: CSS gradients in inline styles cannot
 * reference CSS custom properties. All colors are rgba equivalents of:
 * - rgba(139,92,246,...) → --svx-color-brand-primary (#8B5CF6)
 * - rgba(167,139,250,...) → --svx-color-brand-muted (#A78BFA)
 * - rgba(34,211,238,...) → --svx-color-accent-cyan (#22D3EE)
 *
 * Multi-layer CSS-only effect:
 * 1. Dot grid with gentle pulse (neural network nodes)
 * 2. Floating gradient orbs (brand colors: Synapse, Pulse, Awaken)
 * 3. Subtle connection lines between dots
 *
 * Performance: CSS transforms + opacity only (GPU-composited).
 * No JS animation loops. Respects prefers-reduced-motion.
 * Target: <1% CPU on Pi 5.
 *
 * Ref: META-01 §15 (Neural Mesh spec), REFINE-04
 */

export function NeuralMesh() {
  return (
    <div
      className="pointer-events-none fixed inset-0 overflow-hidden"
      aria-hidden="true"
    >
      {/* Layer 1: Dot grid — neural network nodes */}
      <div
        className="absolute inset-0 animate-[mesh-pulse_6s_ease-in-out_infinite]"
        style={{
          backgroundImage:
            "radial-gradient(circle, var(--svx-color-brand-primary) 1px, transparent 1px)",
          backgroundSize: "40px 40px",
          opacity: 0.07,
        }}
      />

      {/* Layer 2: Subtle cross-hatch lines — neural connections */}
      <div
        className="absolute inset-0"
        style={{
          backgroundImage: [
            "linear-gradient(90deg, rgba(139,92,246,0.03) 1px, transparent 1px)",
            "linear-gradient(0deg, rgba(139,92,246,0.03) 1px, transparent 1px)",
          ].join(", "),
          backgroundSize: "40px 40px",
        }}
      />

      {/* Layer 3: Floating orbs — brand atmosphere */}
      {/* Synapse violet — top right, slow drift */}
      <div
        className="absolute -right-24 -top-24 size-[28rem] animate-[orb-drift-1_25s_ease-in-out_infinite] rounded-full"
        style={{
          background:
            "radial-gradient(circle, rgba(139,92,246,0.08) 0%, rgba(139,92,246,0.02) 40%, transparent 70%)",
        }}
      />

      {/* Pulse violet — bottom left, counter-drift */}
      <div
        className="absolute -bottom-32 -left-20 size-[24rem] animate-[orb-drift-2_30s_ease-in-out_infinite] rounded-full"
        style={{
          background:
            "radial-gradient(circle, rgba(167,139,250,0.06) 0%, rgba(167,139,250,0.01) 40%, transparent 70%)",
        }}
      />

      {/* Awaken cyan — center right, subtle float */}
      <div
        className="absolute right-1/4 top-1/3 size-[20rem] animate-[orb-drift-3_35s_ease-in-out_infinite] rounded-full"
        style={{
          background:
            "radial-gradient(circle, rgba(34,211,238,0.04) 0%, rgba(34,211,238,0.01) 40%, transparent 70%)",
        }}
      />

      {/* Layer 4: Top edge glow — horizon line */}
      <div
        className="absolute left-0 right-0 top-0 h-px"
        style={{
          background:
            "linear-gradient(90deg, transparent 0%, rgba(139,92,246,0.15) 30%, rgba(34,211,238,0.1) 70%, transparent 100%)",
        }}
      />
    </div>
  );
}
